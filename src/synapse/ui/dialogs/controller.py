"""Slash command dispatch and result application for the Textual TUI.

Owns local slash-command routing (``_handle_slash``) and the shared
``SlashResult`` application path (``_apply_ok_result``) that used to live
directly on ``CodingAgentApp``. Dialog *open* helpers stay on the host; this
controller routes to them and applies effects.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from textual.widgets import Input

from synapse.ui.formatters import model_status_label


@dataclass
class TuiCommandEffects:
    """Declarative host changes produced by a handled slash command."""

    agent: Any | None = None
    thread_id: str | None = None
    clear_transcript: bool = False
    reload_transcript: bool = False
    status_notice: str | None = None
    status_style: str = "dim"
    lines: list[str] = field(default_factory=list)
    settings_changed: bool = False
    theme_name: str | None = None
    markdown: str | None = None
    error: bool = False
    resume_action: str | None = None
    resume_message: str | None = None
    cancel_active_turn: bool = False

    @classmethod
    def from_result(cls, result: object, *, previous_thread_id: str) -> TuiCommandEffects:
        """Normalize SlashResult-like values at the controller boundary."""
        thread_id = getattr(result, "thread_id", None)
        thread_changed = thread_id is not None and thread_id != previous_thread_id
        return cls(
            agent=getattr(result, "agent", None),
            thread_id=thread_id,
            clear_transcript=bool(getattr(result, "clear_log", False) or thread_changed),
            reload_transcript=bool(getattr(result, "reload_transcript", False)),
            status_notice=(getattr(result, "notice", None) or "").strip() or None,
            lines=list(getattr(result, "lines", []) or []),
            settings_changed=bool(getattr(result, "settings_changed", False)),
            theme_name=getattr(result, "theme_name", None),
            markdown=getattr(result, "markdown", None),
            error=bool(getattr(result, "error", False)),
            cancel_active_turn=bool(getattr(result, "cancel_active_turn", False)),
            resume_action=(
                value
                if isinstance(value := getattr(result, "resume_action", None), str)
                and value.strip()
                else None
            ),
            resume_message=(
                value
                if isinstance(value := getattr(result, "resume_message", None), str)
                and value.strip()
                else None
            ),
        )


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
            app._switch_model_bg(
                raw,
                f"model {' '.join(parts[1:])}",
                origin_thread_id=app.thread_id,
                origin_agent=app.agent,
                origin_settings=self._copy_settings(app.settings),
            )
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
        if cmd == "/safety" and len(parts) == 1:
            self.open_safety_dialog()
            return True
        if cmd == "/select":
            app.action_open_selectable_view()
            return True
        if cmd == "/image":
            from synapse.ui.image_render import RENDERER_NAMES, renderer_diagnostic, set_renderer

            if len(parts) >= 2 and parts[1].casefold() in RENDERER_NAMES:
                applied = set_renderer(parts[1])
                app.append_event(f"image renderer -> {applied}", "dim")
                app.refresh_image_preview()
            else:
                from synapse.ui.math_image import math_diagnostic

                app.append_event(f"{renderer_diagnostic()} | {math_diagnostic()}", "dim")
            return True

        prev_thread = app.thread_id
        prev_settings = self._settings_snapshot(app.settings)
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

        self.apply_effects(
            TuiCommandEffects.from_result(result, previous_thread_id=prev_thread),
            settings_snapshot=prev_settings,
        )
        return True

    def apply_effects(
        self,
        effects: TuiCommandEffects,
        *,
        notice_ttl: float = 4.0,
        settings_snapshot: tuple[Any, ...] | None = None,
    ) -> None:
        """Apply normalized command effects to the Textual host.

        ``settings_snapshot`` is a snapshot of the process-global settings taken
        *before* the slash handler mutated them towards the target session; the
        switch path restores it when building the target runtime fails.
        """
        app = self._app
        previous_thread_id = app.thread_id
        thread_changed = effects.thread_id is not None and effects.thread_id != previous_thread_id
        turn_controller = getattr(app, "_turn", None)
        if effects.cancel_active_turn:
            # The goal store is already paused by handle_goal(). Cancel the
            # session-owned turn as the runtime half of the same operation.
            app._turn.cancel("goal_pause")
        requested_agent = effects.agent
        if thread_changed:
            # Transactional switch: settings mutations (model binding restore)
            # happen inside handle_slash/_sync_settings_to_agent before the
            # target runtime is resolved; restore the pre-switch snapshot if
            # the switch fails so the previous session's model stays intact.
            settings_snapshot = (
                settings_snapshot
                if settings_snapshot is not None
                else self._settings_snapshot(app.settings)
            )
            if turn_controller is not None:
                turn_controller.detach(previous_thread_id)
            app.thread_id = effects.thread_id
            if turn_controller is not None:
                # Look up the frozen runtime only; do NOT attach here. A probe
                # attach would replay the broker history onto the still-old
                # transcript (dirty paint) and build/destroy a bridge for no
                # reason. Rendering attach happens once, after the transcript
                # reset completes.
                runtime = turn_controller.runtime_for(app.thread_id)
                if runtime is not None:
                    requested_agent = runtime.agent
                    # The session's frozen agent is the authoritative model
                    # source. Sync settings back to its profile so bottombar
                    # chrome and later rebuilds never show another session's
                    # model (binding may be missing for model changes that
                    # predate this session switch).
                    self._sync_settings_to_agent(requested_agent)
                elif requested_agent is None and app.agent is not None:
                    try:
                        requested_agent = self._build_session_agent(
                            app.thread_id,
                            app.agent,
                        )
                    except Exception as exc:  # noqa: BLE001 - switch remains recoverable
                        app.thread_id = previous_thread_id
                        self._restore_settings_snapshot(app.settings, settings_snapshot)
                        turn_controller.attach(previous_thread_id)
                        app.append_event(f"switch failed: {exc}", "yellow")
                        turn_controller.sync_foreground_status()
                        return
                    turn_controller.bind_agent(app.thread_id, requested_agent)
                turn_controller.detach(app.thread_id)
            if requested_agent is not None:
                app.agent = requested_agent
            bind_steer_queue = (
                getattr(app, "_bind_steer_queue", None)
                if hasattr(app, "_steer")
                else None
            )
            if callable(bind_steer_queue):
                bind_steer_queue()
            app._reset_session_token_chrome()
            app._reload_tool_output_stats()
            app._load_current_goal()
        elif requested_agent is not None:
            app.agent = requested_agent
            # Keep the session-owned runtime in sync with the rebuilt graph so
            # switching away and back cannot resurrect the previous agent (its
            # old model / MCP set) and silently undo the switch.
            if turn_controller is not None:
                turn_controller.bind_agent(app.thread_id, requested_agent)
            bind_steer_queue = (
                getattr(app, "_bind_steer_queue", None)
                if hasattr(app, "_steer")
                else None
            )
            if callable(bind_steer_queue):
                bind_steer_queue()
        if effects.clear_transcript:
            if thread_changed and turn_controller is not None:
                app._schedule_transcript_reset(
                    reload_transcript=effects.reload_transcript,
                    announce=effects.reload_transcript,
                    on_complete=lambda thread_id=app.thread_id: (
                        self._attach_switched_runtime(thread_id)
                    ),
                )
            else:
                app._schedule_transcript_reset(
                    reload_transcript=effects.reload_transcript,
                    announce=effects.reload_transcript,
                )
        elif effects.reload_transcript:
            app._restore_session_transcript(announce=True)
            if thread_changed and turn_controller is not None:
                self._attach_switched_runtime(app.thread_id)
        elif thread_changed and turn_controller is not None:
            self._attach_switched_runtime(app.thread_id)
        if requested_agent is not None or effects.settings_changed:
            app.sub_title = model_status_label(app.settings)
            app._render_status()
        if effects.theme_name:
            try:
                app.apply_theme(str(effects.theme_name), persist=False, announce=False)
            except Exception as exc:  # noqa: BLE001
                app.append_event(f"theme apply failed: {exc}", "yellow")
        has_lines = any(str(line or "").strip() for line in effects.lines)
        if isinstance(effects.markdown, str) and effects.markdown.strip():
            app._mount_markdown_block(effects.markdown)
        elif effects.status_notice or has_lines:
            app._dismiss_welcome()
        if not (isinstance(effects.markdown, str) and effects.markdown.strip()):
            if effects.status_notice and not effects.error:
                app.flash_status(effects.status_notice, effects.status_style, ttl=notice_ttl)
            else:
                app._emit_system_lines(effects.lines, error=effects.error)
        app._reload_session_title()
        app._refresh_topbar()
        app._refresh_codex_usage(force=True)
        if effects.resume_action:
            self._resume_after_effects(effects.resume_action, effects.resume_message)

    def _attach_switched_runtime(self, thread_id: str) -> None:
        app = self._app
        if app.thread_id != thread_id:
            return
        turn_controller = getattr(app, "_turn", None)
        if turn_controller is None:
            return
        turn_controller.attach(thread_id)
        turn_controller.sync_foreground_status()

    @staticmethod
    def _settings_snapshot(settings: Any) -> tuple[Any, ...]:
        """Capture the model-related fields mutated by binding restore."""
        return (
            getattr(settings, "active_model", None),
            getattr(settings, "model", None),
            getattr(settings, "enable_thinking", True),
            getattr(settings, "reasoning_effort", None),
            getattr(settings, "parallel_tool_calls", True),
            getattr(settings, "openai_api_key", None),
            getattr(settings, "anthropic_api_key", None),
            getattr(settings, "openai_base_url", None),
        )

    @staticmethod
    def _restore_settings_snapshot(settings: Any, snapshot: tuple[Any, ...]) -> None:
        """Restore a snapshot taken by :meth:`_settings_snapshot`."""
        names = (
            "active_model",
            "model",
            "enable_thinking",
            "reasoning_effort",
            "parallel_tool_calls",
            "openai_api_key",
            "anthropic_api_key",
            "openai_base_url",
        )
        for name, value in zip(names, snapshot, strict=False):
            setattr(settings, name, value)

    @staticmethod
    def _copy_settings(settings: Any) -> Any:
        """Return a private Settings copy for a background session worker."""
        copier = getattr(settings, "model_copy", None)
        if callable(copier):
            return copier(deep=True)
        from copy import deepcopy

        return deepcopy(settings)

    @staticmethod
    def _commit_settings(target: Any, source: Any) -> None:
        """Commit worker-owned model/MCP fields when its session is foreground."""
        names = (
            "active_model",
            "model",
            "enable_thinking",
            "reasoning_effort",
            "parallel_tool_calls",
            "openai_api_key",
            "anthropic_api_key",
            "openai_base_url",
            "enable_mcp",
            "mcp_servers_json",
        )
        for name in names:
            if hasattr(source, name):
                setattr(target, name, getattr(source, name))

    def _sync_settings_to_agent(self, agent: Any) -> None:
        """Point global settings at the model profile the agent actually uses.

        The TUI keeps one global Settings object; switching sessions therefore
        must re-align ``active_model``/``model`` with the target session's
        frozen agent, otherwise chrome and later rebuilds inherit the previous
        session's model. Best-effort: a missing/unknown profile is ignored.
        """
        app = self._app
        profile = str(getattr(agent, "_coding_model_profile", None) or "").strip()
        if not profile:
            return
        try:
            from synapse.models.registry import (
                apply_profile_to_settings,
                registry_from_settings,
            )

            current = str(getattr(app.settings, "active_model", None) or "").strip()
            if current == profile:
                return
            reg = registry_from_settings(app.settings)
            # Do not seed thinking: this path only re-aligns the model
            # identity; the session's thinking level must survive the switch.
            apply_profile_to_settings(app.settings, reg.get(profile), seed_thinking=False)
        except Exception:  # noqa: BLE001 - chrome alignment is best-effort
            pass

    def _build_session_agent(self, thread_id: str, template_agent: Any) -> Any:
        """Compile one session-owned graph while reusing project resources."""
        from synapse.runtime.sessions import (
            ProjectSharedResources,
            build_session_agent_factory,
        )

        app = self._app
        try:
            from synapse.integrations.mcp_client import get_active_mcp_pool

            pool = get_active_mcp_pool()
            mcp_tools = tuple(getattr(pool, "tools", None) or ()) if pool is not None else ()
        except Exception:  # noqa: BLE001 - MCP is optional during session switching
            mcp_tools = ()
        factory = build_session_agent_factory(
            settings=app.settings,
            project_root=app.project_root,
            template_agent=template_agent,
            goal_service=getattr(template_agent, "_coding_goal_service", None),
            project_id=app._current_project_id() or None,
        )
        resources = ProjectSharedResources(
            model_client=getattr(template_agent, "_coding_model", None),
            checkpointer=getattr(template_agent, "_coding_checkpointer", None),
            mcp_tools=mcp_tools,
        )
        return factory(thread_id, resources)

    def _resume_after_effects(self, action: str, message: str | None) -> None:
        """Start a HITL resume after its slash result has updated host state."""
        app = self._app
        if app.agent is None:
            app.append_event("agent not ready — cannot resume HITL", "yellow")
            return
        if app._busy:
            app.append_event("still running previous turn…", "yellow")
            return
        app._capture_turn_context()
        app._busy = True
        app.set_activity("tool", f"HITL {action}", True)
        app.run_resume(action, message)

    def apply_ok_result(
        self,
        ok: object,
        notice_ttl: float = 4.0,
        settings_snapshot: tuple[Any, ...] | None = None,
    ) -> None:
        """Apply a SlashResult returned by handle_slash after a dialog pick."""
        if settings_snapshot is None:
            # Callers that did not capture the pre-handler settings (workers
            # that never switch threads) take the current state as baseline.
            settings_snapshot = self._settings_snapshot(self._app.settings)
        effects = TuiCommandEffects.from_result(ok, previous_thread_id=self._app.thread_id)
        if notice_ttl != 4.0 and not effects.status_notice and len(effects.lines) <= 2:
            cleaned = [str(line).strip() for line in effects.lines if str(line or "").strip()]
            if cleaned and sum(len(line) for line in cleaned) <= 140:
                effects.status_notice = " · ".join(cleaned)
                effects.lines = []
                effects.error = False
                effects.status_style = "yellow" if getattr(ok, "error", False) else "dim"
        self.apply_effects(
            effects,
            notice_ttl=notice_ttl,
            settings_snapshot=settings_snapshot,
        )


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
        self._app._switch_model_bg(
            f"/model {alias}",
            f"switching model to {alias}",
            origin_thread_id=self._app.thread_id,
            origin_agent=self._app.agent,
            origin_settings=self._copy_settings(self._app.settings),
        )

    def apply_thinking_switch(self, level: str) -> None:
        self._app._switch_model_bg(
            f"/model thinking {level}",
            f"thinking -> {level}",
            origin_thread_id=self._app.thread_id,
            origin_agent=self._app.agent,
            origin_settings=self._copy_settings(self._app.settings),
        )

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
        turn = getattr(app, "_turn", None)
        runtime_status = turn.runtime_status_map() if turn is not None else {}
        app.push_screen(
            SessionListDialog(
                app.settings,
                current_thread=app.thread_id,
                mode=mode,
                runtime_status=runtime_status,
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
        prev_settings = self._settings_snapshot(app.settings)
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
        self.apply_ok_result(ok, settings_snapshot=prev_settings)

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
        app._apply_mcp_save_bg(
            to_save,
            origin_thread_id=app.thread_id,
            origin_agent=app.agent,
            origin_settings=self._copy_settings(app.settings),
        )

    def apply_mcp_reload(self) -> None:
        """Dispatch MCP reload to a background worker so the UI stays responsive."""
        app = self._app
        if getattr(app, "_mcp_reloading", False):
            return
        app._mcp_reloading = True
        app.set_activity("switching", "reloading MCP…", True)
        app._apply_mcp_reload_bg(
            origin_thread_id=app.thread_id,
            origin_agent=app.agent,
            origin_settings=self._copy_settings(app.settings),
        )

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

    # -- background workers (host keeps @work shells) ------------------------

    def switch_model_bg(
        self,
        command: str,
        activity: str,
        *,
        origin_thread_id: str | None = None,
        origin_agent: Any | None = None,
        origin_settings: Any | None = None,
    ) -> None:
        """Run /model rebuild off the UI thread so the TUI stays responsive.

        ``origin_thread_id`` and ``origin_agent`` are captured on the UI thread
        before the worker is scheduled; the worker body runs concurrently with
        user input, so ``app.thread_id`` / ``app.agent`` may already point at a
        different session.
        """
        from synapse.commands.slash_cmds import handle_slash
        from synapse.observability.startup_trace import duration

        app = self._app
        origin = origin_thread_id or app.thread_id
        origin_agent = origin_agent or app.agent
        worker_settings = origin_settings or self._copy_settings(app.settings)
        switch_started = time.perf_counter()
        app.call_from_thread(app._clear_status_notice)
        app.call_from_thread(app.set_activity, "switching", activity, True)
        try:
            ok = handle_slash(
                command,
                settings=worker_settings,
                agent=origin_agent,
                thread_id=origin,
                project_root=app.project_root,
            )
        except Exception as exc:  # noqa: BLE001
            duration("model.switch", switch_started, command=command, success=False)
            app.call_from_thread(
                app.append_event, f"{activity} failed: {exc}", "yellow"
            )
            app.call_from_thread(app.set_activity, "idle", "", True)
            return
        duration(
            "model.switch",
            switch_started,
            command=command,
            success=not bool(getattr(ok, "error", False)),
        )
        app.call_from_thread(self._finish_model_switch, ok, origin, worker_settings)
        app.call_from_thread(app.set_activity, "idle", "", True)
        if getattr(ok, "mcp_attach_pending", False):
            app.call_from_thread(
                self.attach_mcp_after_switch,
                origin,
                getattr(ok, "agent", None),
                worker_settings,
            )

    def _finish_model_switch(
        self,
        ok: Any,
        origin_thread_id: str,
        worker_settings: Any | None = None,
        notice_ttl: float = 1.5,
    ) -> None:
        """UI-thread completion for a background model switch.

        If the user switched sessions while the rebuild was running, only the
        origin session's runtime is updated; the foreground session and its
        chrome must stay untouched.
        """
        app = self._app
        if app.thread_id != origin_thread_id:
            new_agent = getattr(ok, "agent", None)
            if new_agent is not None:
                turn = getattr(app, "_turn", None)
                if turn is not None:
                    turn.bind_agent(
                        origin_thread_id,
                        new_agent,
                        settings=worker_settings,
                    )
            app.append_event(
                f"model switched for background session ({origin_thread_id[:10]}…)",
                "dim",
            )
            return
        if bool(getattr(ok, "error", False)):
            # The slash handler may have mutated the worker's private settings
            # (profile applied) before the rebuild failed; never push that
            # half-applied state onto the foreground session.
            self.apply_ok_result(ok, notice_ttl)
            return
        if worker_settings is not None:
            self._commit_settings(app.settings, worker_settings)
        self.apply_ok_result(ok, notice_ttl)

    def attach_mcp_after_switch(
        self,
        origin_thread_id: str | None = None,
        origin_agent: Any | None = None,
        origin_settings: Any | None = None,
    ) -> None:
        """Reattach MCP after a model switch, guarded by the lifecycle flag."""
        app = self._app
        if origin_agent is None:
            # The rebuild failed or produced no graph; never fall back to the
            # (possibly different) foreground agent as the attach base.
            return
        lifecycle = getattr(app, "_lifecycle", None)
        if lifecycle is not None:
            if lifecycle.mcp_attaching:
                return
            lifecycle.set_mcp_attaching(True)
        else:
            if app._mcp_attaching:
                return
            app._mcp_attaching = True
        app._attach_mcp_after_switch_bg(
            origin_agent,
            origin_thread_id=origin_thread_id or app.thread_id,
            origin_agent=origin_agent,
            origin_settings=origin_settings,
        )

    def attach_mcp_after_switch_bg(
        self,
        base_agent: Any,
        *,
        origin_thread_id: str | None = None,
        origin_agent: Any | None = None,
        origin_settings: Any | None = None,
    ) -> None:
        from synapse.app.agent import attach_mcp_to_agent
        from synapse.observability.startup_trace import duration

        app = self._app
        origin = origin_thread_id or app.thread_id
        worker_settings = origin_settings or self._copy_settings(app.settings)
        mcp_started = time.perf_counter()
        app.call_from_thread(app.flash_status, "reconnecting MCP…", "dim", ttl=1.5)
        try:
            agent = attach_mcp_to_agent(
                worker_settings,
                base_agent,
                project_root=app.project_root,
            )
        except Exception as exc:  # noqa: BLE001
            app.call_from_thread(
                app.append_event,
                f"MCP reconnect failed (agent still usable): {exc}",
                "yellow",
            )
            return
        finally:
            duration("mcp.attach", mcp_started, phase="model_switch")
            lifecycle = getattr(app, "_lifecycle", None)
            if lifecycle is not None:
                lifecycle.set_mcp_attaching(False)
            else:
                app._mcp_attaching = False
        if (origin_agent or app.agent) is not base_agent:
            return
        turn = getattr(app, "_turn", None)
        if app.thread_id != origin:
            # Foreground moved on while MCP reconnected: bind the finalized
            # graph to the origin session only; never touch the live session.
            if turn is not None:
                turn.bind_agent(origin, agent, settings=worker_settings)
            return
        self._commit_settings(app.settings, worker_settings)
        app.agent = agent
        if turn is not None:
            # Keep the session-owned runtime on the MCP-finalized graph; the
            # earlier bind in apply_effects happened before MCP reconnection.
            turn.bind_agent(origin, agent, settings=worker_settings)
        app.call_from_thread(app._bind_steer_queue)
        app.call_from_thread(app.flash_status, "MCP reconnected", "dim", ttl=1.5)

    def import_codex_session_bg(self, native_id: str) -> None:
        """Seed one Codex text snapshot, then switch through the normal path."""
        app = self._app
        controller = getattr(app, "_turn", None)
        runtime = getattr(controller, "session_runtime", None)
        turn_agent = runtime.agent if runtime is not None and controller.busy else app.agent
        ready = getattr(app, "_lifecycle", None)
        if ready is not None:
            ready = ready.agent_ready.wait(timeout=180)
        else:
            ready = app._agent_ready.wait(timeout=180)
        if not ready or turn_agent is None:
            app.call_from_thread(
                app.append_event,
                "Codex import unavailable: agent is still starting",
                "yellow",
            )
            app.call_from_thread(app._turn_done)
            return
        try:
            from synapse.integrations.codex_import import import_codex_session

            result = import_codex_session(
                native_id=native_id,
                settings=app.settings,
                agent=turn_agent,
                workspace=Path(app.settings.workspace),
            )
        except Exception as exc:  # noqa: BLE001
            app.call_from_thread(
                app.append_event, f"Codex import failed: {exc}", "yellow"
            )
        else:
            app.call_from_thread(self.finish_codex_import, result)
        finally:
            app.call_from_thread(app._turn_done)

    def mcp_server_toggle(self, server_name: str) -> None:
        """Temporarily toggle one MCP server through the existing slash handler."""
        app = self._app
        if getattr(app, "_mcp_reloading", False):
            return
        app._mcp_reloading = True
        app.set_activity("switching", f"toggling MCP server {server_name}\u2026", True)
        app._apply_mcp_server_toggle_bg(
            server_name,
            origin_thread_id=app.thread_id,
            origin_agent=app.agent,
            origin_settings=self._copy_settings(app.settings),
        )

    def _finish_mcp_worker(
        self,
        ok: Any,
        *,
        failed: str | None = None,
        origin_thread_id: str | None = None,
        worker_settings: Any | None = None,
    ) -> None:
        """UI-thread completion for MCP background workers (exactly once).

        Clears the reloading flag and restores the idle activity together so
        the UI never observes a half-finished state. ``failed`` carries a
        pre-formatted user-facing error; ``ok`` is None on the exception path.
        When the foreground moved to another session while the worker ran, the
        rebuilt graph is bound to the origin session only.
        """
        app = self._app
        app._mcp_reloading = False
        app.set_activity("idle", "", True)
        if failed:
            app.append_event(failed, "yellow")
            return
        if origin_thread_id is not None and app.thread_id != origin_thread_id:
            new_agent = getattr(ok, "agent", None)
            if new_agent is not None:
                turn = getattr(app, "_turn", None)
                if turn is not None:
                    turn.bind_agent(
                        origin_thread_id,
                        new_agent,
                        settings=worker_settings,
                    )
            app.append_event(
                f"MCP updated for background session ({origin_thread_id[:10]}…)",
                "dim",
            )
            return
        if bool(getattr(ok, "error", False)):
            # reload/toggle may have mutated the worker's private settings
            # before failing; never push half-applied state onto foreground.
            self.apply_ok_result(ok)
            return
        if worker_settings is not None:
            self._commit_settings(app.settings, worker_settings)
        self.apply_ok_result(ok)

    def mcp_server_toggle_bg(
        self,
        server_name: str,
        *,
        origin_thread_id: str | None = None,
        origin_agent: Any | None = None,
        origin_settings: Any | None = None,
    ) -> None:
        from synapse.commands.slash_cmds import handle_slash
        from synapse.observability.startup_trace import duration

        app = self._app
        origin = origin_thread_id or app.thread_id
        origin_agent = origin_agent or app.agent
        worker_settings = origin_settings or self._copy_settings(app.settings)
        reload_started = time.perf_counter()
        ok: Any = None
        failed: str | None = None
        try:
            ok = handle_slash(
                f"/mcp toggle {server_name}",
                settings=worker_settings,
                agent=origin_agent,
                thread_id=origin,
                project_root=app.project_root,
            )
        except Exception as exc:  # noqa: BLE001
            failed = f"MCP server toggle failed: {exc}"
            duration("mcp.toggle", reload_started, success=False)
        else:
            duration(
                "mcp.toggle",
                reload_started,
                success=not bool(getattr(ok, "error", False)),
            )
        finally:
            # All UI-visible state (reloading flag + activity) is written on
            # the UI thread through the finish helper, never from this worker.
            app.call_from_thread(
                self._finish_mcp_worker,
                ok,
                failed=failed,
                origin_thread_id=origin,
                worker_settings=worker_settings,
            )

    def mcp_save_bg(
        self,
        to_save: dict[str, list[str] | None],
        *,
        origin_thread_id: str | None = None,
        origin_agent: Any | None = None,
        origin_settings: Any | None = None,
    ) -> None:
        from synapse.commands.slash_cmds import handle_slash
        from synapse.integrations.mcp_client import load_mcp_server_configs
        from synapse.observability.startup_trace import duration
        from synapse.ui.dialogs.mcp_panel import _save_include_tools_to_config

        app = self._app
        origin = origin_thread_id or app.thread_id
        origin_agent = origin_agent or app.agent
        worker_settings = origin_settings or self._copy_settings(app.settings)
        save_started = time.perf_counter()
        ok: Any = None
        failed: str | None = None
        try:
            # 1. Write include_tools to config file for each changed server.
            for server_name, include_tools in to_save.items():
                try:
                    _save_include_tools_to_config(
                        worker_settings,
                        server_name,
                        include_tools,
                        app.project_root,
                    )
                except Exception:  # noqa: BLE001 - best-effort per-server write
                    pass

            # 2. Reload in-memory settings from the updated config files.
            try:
                fresh = load_mcp_server_configs(
                    path=getattr(worker_settings, "mcp_config_path", None),
                    workspace=getattr(worker_settings, "workspace", None),
                )
                import json

                raw = {
                    "servers": [
                        {
                            "name": s.name,
                            "transport": s.transport,
                            "command": s.command,
                            "args": s.args,
                            "env": s.env,
                            "url": s.url,
                            "headers": s.headers,
                            "enabled": s.enabled,
                            "tool_prefix": s.tool_prefix,
                            "include_tools": s.include_tools,
                            "exclude_tools": s.exclude_tools,
                        }
                        for s in fresh
                    ]
                }
                worker_settings.mcp_servers_json = json.dumps(raw)
            except Exception:  # noqa: BLE001 - best-effort settings refresh
                pass

            # 3. Reload the agent with updated MCP tools.
            ok = handle_slash(
                "/mcp reload",
                settings=worker_settings,
                agent=origin_agent,
                thread_id=origin,
                project_root=app.project_root,
            )
        except Exception as exc:  # noqa: BLE001
            failed = f"MCP save/reload failed: {exc}"
            duration("mcp.save", save_started, success=False)
        else:
            duration(
                "mcp.save",
                save_started,
                success=not bool(getattr(ok, "error", False)),
            )
        finally:
            app.call_from_thread(
                self._finish_mcp_worker,
                ok,
                failed=failed,
                origin_thread_id=origin,
                worker_settings=worker_settings,
            )

    def mcp_reload_bg(
        self,
        *,
        origin_thread_id: str | None = None,
        origin_agent: Any | None = None,
        origin_settings: Any | None = None,
    ) -> None:
        from synapse.commands.slash_cmds import handle_slash
        from synapse.observability.startup_trace import duration

        app = self._app
        origin = origin_thread_id or app.thread_id
        origin_agent = origin_agent or app.agent
        worker_settings = origin_settings or self._copy_settings(app.settings)
        reload_started = time.perf_counter()
        ok: Any = None
        failed: str | None = None
        try:
            ok = handle_slash(
                "/mcp reload",
                settings=worker_settings,
                agent=origin_agent,
                thread_id=origin,
                project_root=app.project_root,
            )
        except Exception as exc:  # noqa: BLE001
            failed = f"MCP reload failed: {exc}"
            duration("mcp.reload", reload_started, success=False)
        else:
            duration(
                "mcp.reload",
                reload_started,
                success=not bool(getattr(ok, "error", False)),
            )
        finally:
            app.call_from_thread(
                self._finish_mcp_worker,
                ok,
                failed=failed,
                origin_thread_id=origin,
                worker_settings=worker_settings,
            )

    def compact_context_bg(self, agent: Any, thread_id: str) -> None:
        """Execute the model-backed /compact command away from Textual's UI loop."""
        from synapse.commands.slash_cmds import handle_slash

        app = self._app
        try:
            result = handle_slash(
                "/compact",
                settings=app.settings,
                agent=agent,
                thread_id=thread_id,
                project_root=app.project_root,
            )
        except Exception as exc:  # noqa: BLE001
            app.call_from_thread(app.append_event, f"compact failed: {exc}", "bold red")
        else:
            app.call_from_thread(self.finish_context_compact, result)
        finally:
            app.call_from_thread(self.complete_context_compact)