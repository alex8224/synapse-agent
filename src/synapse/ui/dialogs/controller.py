"""Slash command dispatch and result application for the Textual TUI.

Owns local slash-command routing (``_handle_slash``) and the shared
``SlashResult`` application path (``_apply_ok_result``) that used to live
directly on ``CodingAgentApp``. Dialog *open* helpers stay on the host; this
controller routes to them and applies effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from textual.widgets import Input

from synapse.ui.formatters import model_status_label


@dataclass
class TuiCommandEffects:
    """Declarative effects of a handled slash command (future refactor target).

    Currently informational: the controller still mutates host state directly
    for compatibility; this dataclass documents the intended effect surface.
    """

    agent: Any | None = None
    thread_id: str | None = None
    clear_transcript: bool = False
    reload_transcript: bool = False
    status_notice: str | None = None
    lines: list[str] = field(default_factory=list)


class SlashController:
    """Route local slash commands and apply their results."""

    def __init__(self, app: Any) -> None:
        self._app = app

    def handle_slash(self, text: str) -> bool:
        """Handle local slash commands. Return True if consumed."""
        from synapse.commands.slash_cmds import handle_slash

        app = self._app
        if app.agent is None:
            low = text.strip().split()[0].casefold() if text.strip() else ""
            if low not in {
                "/quit", "/exit", "/help", "/?", "/clear",
                "/theme", "/model", "/switch", "/safety",
            }:
                app.append_event(
                    "agent still starting — try again in a moment",
                    "yellow",
                )
                return True

        # ---- dialog-capable commands (push ModalScreen) ----
        raw = (text or "").strip()
        parts = raw.split()
        cmd = parts[0].casefold() if parts else ""

        if cmd == "/compact":
            app._start_context_compact()
            return True
        if cmd in {"/compression", "/tool-output", "/tool-compress"} and len(parts) == 1:
            self.open_compression_diagnostics()
            return True
        if cmd == "/model" and len(parts) == 1:
            self.open_model_dialog(parts[1:])
            return True
        if cmd == "/model":
            # Args form (/model <alias> [thinking ...]): rebuild in background.
            app._switch_model_bg(raw, f"model {' '.join(parts[1:])}")
            return True
        if cmd == "/switch" and len(parts) == 1:
            self.open_session_dialog(["switch"])
            return True
        if cmd == "/session" and len(parts) >= 2 and parts[1].casefold() in {"delete", "del", "rm"}:
            # /session delete (without thread_id) → pick from list
            if len(parts) == 2:
                self.open_session_dialog(parts)
                return True
        if cmd == "/codex":
            if len(parts) == 1 or (len(parts) == 2 and parts[1].casefold() == "import"):
                self.open_codex_import_dialog()
                return True
            if len(parts) == 2 and parts[1].casefold() in {"reset", "credits", "resets"}:
                app._open_codex_reset_dialog()
                return True
            if len(parts) == 3 and parts[1].casefold() == "import":
                self.start_codex_import(parts[2])
                return True
            app.append_event("usage: /codex import [native_id]  |  /codex reset", "yellow")
            return True
        if cmd == "/theme" and (len(parts) == 1 or parts[1].casefold() in {"list", "ls"}):
            self.open_theme_dialog()
            return True
        if cmd == "/mcp" and len(parts) == 1:
            self.open_mcp_dialog()
            return True
        if cmd in {"/subagents", "/agents"} and len(parts) == 1:
            self.open_subagent_monitor()
            return True
        if cmd == "/safety" and len(parts) == 1:
            self.open_safety_dialog()
            return True
        if cmd == "/select":
            app.action_open_selectable_view()
            return True

        prev_thread = app.thread_id
        result = handle_slash(
            text,
            settings=app.settings,
            agent=app.agent,
            thread_id=app.thread_id,
            project_root=app.project_root,
        )
        if not result.handled:
            return False
        if result.exit_requested:
            app.exit()
            return True

        if result.agent is not None:
            app.agent = result.agent
            app._bind_steer_queue()

        thread_changed = False
        if result.thread_id is not None and result.thread_id != prev_thread:
            app.thread_id = result.thread_id
            thread_changed = True

        clear_log = bool(result.clear_log or thread_changed)
        reload_transcript = bool(getattr(result, "reload_transcript", False))
        if clear_log:
            app._schedule_transcript_reset(
                reload_transcript=reload_transcript,
                announce=reload_transcript,
            )
            if thread_changed:
                app._reset_session_token_chrome()
                app._load_current_goal()

        # Title may change via /rename, /switch, /new, first-message bind, etc.
        app._reload_session_title()
        app._refresh_topbar()
        if result.agent is not None or getattr(result, "settings_changed", False):
            app.sub_title = model_status_label(app.settings)
            app._render_status()

        # Restore visual history after switch/new. LLM context follows thread_id
        # via checkpointer; this only rebuilds the transcript chrome.
        if reload_transcript and not clear_log:
            app._restore_session_transcript(announce=True)
            app._refresh_topbar()

        theme_name = getattr(result, "theme_name", None)
        if theme_name:
            try:
                app.apply_theme(str(theme_name), persist=False, announce=False)
            except Exception as exc:  # noqa: BLE001
                app.append_event(f"theme apply failed: {exc}", "yellow")

        _notice = (getattr(result, "notice", None) or "").strip()
        _has_lines = bool([x for x in (result.lines or []) if str(x or "").strip()])
        _markdown = getattr(result, "markdown", None)
        if isinstance(_markdown, str) and _markdown.strip():
            app._mount_markdown_block(_markdown)
        elif _notice or _has_lines:
            app._dismiss_welcome()
        if isinstance(_markdown, str) and _markdown.strip():
            pass  # already rendered
        elif _notice and not result.error:
            app.flash_status(_notice, "dim")
        else:
            app._emit_system_lines(result.lines, error=bool(result.error))

        # HITL: /approve or /reject resumes the paused graph.
        resume_action = getattr(result, "resume_action", None)
        if resume_action:
            if app.agent is None:
                app.append_event("agent not ready — cannot resume HITL", "yellow")
                return True
            if app._busy:
                app.append_event("still running previous turn…", "yellow")
                return True
            app._capture_turn_context()
            app._busy = True
            app.set_activity("tool", f"HITL {resume_action}", True)
            app.run_resume(
                str(resume_action),
                getattr(result, "resume_message", None),
            )
        return True

    def apply_ok_result(self, ok: object, notice_ttl: float = 4.0) -> None:
        """Apply a SlashResult returned by handle_slash after a dialog pick."""
        app = self._app
        agent = getattr(ok, "agent", None)
        if agent is not None:
            app.agent = agent
            app._bind_steer_queue()
        thread_id = getattr(ok, "thread_id", None)
        thread_changed = thread_id is not None and thread_id != app.thread_id
        if thread_changed:
            app.thread_id = thread_id
            app._reset_session_token_chrome()
            app._reload_tool_output_stats()
            app._load_current_goal()
        clear_log = thread_changed or bool(getattr(ok, "clear_log", False))
        reload_transcript = bool(getattr(ok, "reload_transcript", False))
        if clear_log:
            app._schedule_transcript_reset(
                reload_transcript=reload_transcript,
                announce=reload_transcript,
            )
        if agent is not None or getattr(ok, "settings_changed", False):
            app.sub_title = model_status_label(app.settings)
            app._render_status()
        if reload_transcript and not clear_log:
            app._restore_session_transcript(announce=True)
        theme_name = getattr(ok, "theme_name", None)
        if theme_name:
            try:
                app.apply_theme(str(theme_name), persist=False, announce=False)
            except Exception as exc:  # noqa: BLE001
                app.append_event(f"theme apply failed: {exc}", "yellow")
        _notice = (getattr(ok, "notice", None) or "").strip()
        if _notice and not getattr(ok, "error", False):
            app.flash_status(_notice, "dim", ttl=notice_ttl)
        else:
            lines = getattr(ok, "lines", []) or []
            if notice_ttl != 4.0 and len(lines) <= 2:
                cleaned = [str(x).strip() for x in lines if str(x or "").strip()]
                if cleaned and sum(len(x) for x in cleaned) <= 140:
                    style = "yellow" if getattr(ok, "error", False) else "dim"
                    app.flash_status(" · ".join(cleaned), style, ttl=notice_ttl)
                else:
                    app._emit_system_lines(
                        lines, error=bool(getattr(ok, "error", False))
                    )
            else:
                app._emit_system_lines(
                    lines,
                    error=bool(getattr(ok, "error", False)),
                )
        app._reload_session_title()
        app._refresh_topbar()
        app._refresh_codex_usage(force=True)


    # -- dialogs ------------------------------------------------------------

    def open_compression_diagnostics(self) -> None:
        from synapse.ui.dialogs import CompressionDiagnosticsDialog

        app = self._app
        app._reload_tool_output_stats()
        app.push_screen(
            CompressionDiagnosticsDialog(app._tool_output_repo, app.thread_id),
        )

    def open_model_dialog(self, _args: list[str]) -> None:
        from synapse.ui.dialogs import ModelPickerDialog

        self._app.push_screen(
            ModelPickerDialog(self._app.settings),
            self.on_model_dialog_done,
        )

    def on_model_dialog_done(self, result: object) -> None:
        if result is None:
            return
        action, value = result
        if action == "model":
            self.apply_model_switch(value)
        elif action == "thinking":
            self.apply_thinking_switch(value)

    def apply_model_switch(self, alias: str) -> None:
        self._app._switch_model_bg(f"/model {alias}", f"switching model to {alias}")

    def apply_thinking_switch(self, level: str) -> None:
        self._app._switch_model_bg(f"/model thinking {level}", f"thinking -> {level}")

    def open_theme_dialog(self) -> None:
        from synapse.ui.dialogs import ThemePickerDialog

        self._app.push_screen(
            ThemePickerDialog(self._app.settings, project_root=self._app.project_root),
            self.on_theme_dialog_done,
        )

    def open_theme_designer(self) -> None:
        from synapse.ui.dialogs.theme_designer import ThemeDesignerDialog

        self._app.push_screen(
            ThemeDesignerDialog(self._app.settings, project_root=self._app.project_root),
            self.on_theme_dialog_done,
        )

    def on_theme_dialog_done(self, result: object) -> None:
        if result is None:
            return
        action, name = result
        if action == "theme":
            try:
                self._app.apply_theme(str(name), persist=True, announce=True)
            except Exception as exc:  # noqa: BLE001
                self._app.append_event(f"theme failed: {exc}", "yellow")

    def open_codex_import_dialog(self) -> None:
        app = self._app
        if app._busy:
            app.append_event("still running previous turn…", "yellow")
            return
        from synapse.ui.dialogs import CodexSessionListDialog

        app.push_screen(
            CodexSessionListDialog(app.settings),
            self.on_codex_import_dialog_done,
        )

    def on_codex_import_dialog_done(self, result: object) -> None:
        if result is None:
            return
        action, native_id = result
        if action == "codex-import" and native_id:
            self.start_codex_import(str(native_id))

    def start_codex_import(self, native_id: str) -> None:
        app = self._app
        if app._busy:
            app.append_event("still running previous turn…", "yellow")
            return
        app._capture_turn_context()
        app._busy = True
        app.set_activity("importing", "importing Codex session", True)
        app.flash_status("importing Codex session…", "dim")
        app._sync_prompt_placeholder()
        app._import_codex_session_bg(native_id)

    def finish_codex_import(self, result: Any) -> None:
        app = self._app
        self.apply_session_switch(str(result.thread_id))
        status = "reused" if result.reused else "recovered" if result.recovered else "imported"
        app.flash_status(f"Codex session {status}: {result.thread_id}", "dim")

    def open_session_dialog(self, parts: list[str]) -> None:
        mode = "switch"
        if len(parts) >= 2 and parts[1].casefold() in {"delete", "del", "rm"}:
            mode = "delete"
        elif len(parts) >= 2 and parts[1].casefold() in {"switch", "sel"}:
            mode = "switch"
        elif len(parts) >= 2 and parts[1].casefold() in {"multi_delete", "multi"}:
            mode = "multi_delete"
        from synapse.ui.dialogs import SessionListDialog

        app = self._app
        app.push_screen(
            SessionListDialog(
                app.settings,
                current_thread=app.thread_id,
                mode=mode,
            ),
            self.on_session_dialog_done,
        )

    def on_session_dialog_done(self, result: object) -> None:
        if result is None:
            return
        action, thread_ids = result
        # Always a list now (single-select modes wrap in list too).
        if action == "switch":
            if thread_ids:
                self.apply_session_switch(thread_ids[0])
        elif action == "delete" or action == "multi_delete":
            if thread_ids:
                self.apply_session_multi_delete(thread_ids)

    def apply_session_multi_delete(self, thread_ids: list[str]) -> None:
        """Batch delete sessions, one by one."""
        from synapse.commands.slash_cmds import handle_slash

        app = self._app
        deleted = 0
        failed = 0
        for tid in thread_ids:
            try:
                ok = handle_slash(
                    f"/session delete {tid}",
                    settings=app.settings,
                    agent=app.agent,
                    thread_id=app.thread_id,
                    project_root=app.project_root,
                )
                if ok:
                    deleted += 1
                else:
                    failed += 1
            except Exception:  # noqa: BLE001
                failed += 1
        if deleted:
            app.append_event(
                f"Deleted {deleted} session{'s' if deleted != 1 else ''}",
                "green",
            )
        if failed:
            app.append_event(
                f"Failed to delete {failed} session{'s' if failed != 1 else ''}",
                "yellow",
            )
        self.apply_ok_result(deleted > 0)

    def apply_session_switch(self, thread_id: str) -> None:
        from synapse.commands.slash_cmds import handle_slash

        app = self._app
        try:
            ok = handle_slash(
                f"/switch {thread_id}",
                settings=app.settings,
                agent=app.agent,
                thread_id=app.thread_id,
                project_root=app.project_root,
            )
        except Exception as exc:  # noqa: BLE001
            app.append_event(f"switch failed: {exc}", "yellow")
            return
        self.apply_ok_result(ok)

    def apply_session_delete(self, thread_id: str) -> None:
        from synapse.commands.slash_cmds import handle_slash

        app = self._app
        try:
            ok = handle_slash(
                f"/session delete {thread_id}",
                settings=app.settings,
                agent=app.agent,
                thread_id=app.thread_id,
                project_root=app.project_root,
            )
        except Exception as exc:  # noqa: BLE001
            app.append_event(f"delete failed: {exc}", "yellow")
            return
        self.apply_ok_result(ok)

    def open_mcp_dialog(self) -> None:
        from synapse.ui.dialogs import McpPanelDialog

        app = self._app
        app.push_screen(
            McpPanelDialog(
                app.settings,
                project_root=app.project_root,
            ),
            self.on_mcp_dialog_done,
        )

    def open_subagent_monitor(self) -> None:
        from synapse.ui.dialogs import SubagentMonitorDialog

        self._app.push_screen(SubagentMonitorDialog(self._app._subagent_monitor))

    def on_mcp_dialog_done(self, result: object) -> None:
        if result is None:
            return
        action = result[0] if result else None
        if action == "mcp-reload":
            self.apply_mcp_reload()
        elif action == "mcp-save":
            to_save = result[1] if len(result) > 1 else {}
            self.apply_mcp_save(to_save)
        elif action == "mcp-toggle-server":
            server_name = result[1] if len(result) > 1 else ""
            if server_name:
                self.apply_mcp_server_toggle(server_name)

    def apply_mcp_server_toggle(self, server_name: str) -> None:
        """Toggle one MCP server on/off via the host's background worker."""
        self._app._apply_mcp_server_toggle(server_name)

    def apply_mcp_save(self, to_save: dict[str, list[str] | None]) -> None:
        """Write include_tools to config, then reload — all on a worker thread."""
        app = self._app
        if not to_save:
            self.apply_mcp_reload()
            return
        if getattr(app, "_mcp_reloading", False):
            return
        app._mcp_reloading = True
        app.set_activity("switching", "saving MCP config…", True)
        app._apply_mcp_save_bg(to_save)

    def apply_mcp_reload(self) -> None:
        """Dispatch MCP reload to a background worker so the UI stays responsive."""
        app = self._app
        if getattr(app, "_mcp_reloading", False):
            return
        app._mcp_reloading = True
        app.set_activity("switching", "reloading MCP…", True)
        app._apply_mcp_reload_bg()

    def open_safety_dialog(self) -> None:
        from synapse.ui.dialogs import SafetyPanelDialog

        self._app.push_screen(
            SafetyPanelDialog(self._app.settings),
            self.on_safety_dialog_done,
        )

    def on_safety_dialog_done(self, result: object) -> None:
        if result is None:
            return
        action, profile = result
        if action == "safety":
            from synapse.commands.slash_cmds import handle_slash

            app = self._app
            try:
                ok = handle_slash(
                    f"/safety {profile}",
                    settings=app.settings,
                    agent=app.agent,
                    thread_id=app.thread_id,
                    project_root=app.project_root,
                )
            except Exception as exc:  # noqa: BLE001
                app.append_event(f"safety switch failed: {exc}", "yellow")
                return
            self.apply_ok_result(ok)

    # -- context compact -----------------------------------------------------

    def start_context_compact(self) -> None:
        """Run /compact in a worker so model summarization cannot freeze the TUI."""
        app = self._app
        if app._busy:
            app.append_event("still running previous turn…", "yellow")
            return
        if app.agent is None:
            app.append_event("agent still starting — try again in a moment", "yellow")
            return

        app._busy = True
        app._compacting_context = True
        app.set_activity("compacting", "compacting context", True)
        app.flash_status("compacting context…", "dim")
        app._sync_prompt_placeholder()
        app._compact_context_bg(app.agent, app.thread_id)

    def finish_context_compact(self, result: Any) -> None:
        """Render the completed compact command result on the UI thread."""
        markdown = getattr(result, "markdown", None)
        if isinstance(markdown, str) and markdown.strip():
            self._app._mount_markdown_block(markdown)
            return
        self._app._emit_system_lines(
            getattr(result, "lines", []) or [],
            error=bool(getattr(result, "error", False)),
        )

    def complete_context_compact(self) -> None:
        """Restore interactive state without treating /compact as a user turn."""
        app = self._app
        app._compacting_context = False
        app._busy = False
        app._sync_prompt_placeholder()
        app.set_activity("idle", "ready", True)
        app.query_one("#prompt", Input).focus()
