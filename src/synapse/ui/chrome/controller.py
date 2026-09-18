"""Chrome state and rendering: usage, tool-output stats, git, session title.

Owns the topbar/bottombar rendering helpers that used to live directly on
``CodingAgentApp``. The Textual host keeps the registries and Textual wiring
and forwards here; the controller reads/writes host state through ``_app``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from rich.text import Text

import synapse.ui.tui_styles as _styles
from synapse.integrations.openai_usage import ConsumeResetResult
from synapse.sessions.transcript_projection import TranscriptUsage
from synapse.ui.bottombar import (
    BottomBarContext,
)
from synapse.ui.bottombar import (
    install_default_components as install_default_bottombar_components,
)
from synapse.ui.dialogs.codex_reset import CodexResetDialog
from synapse.ui.formatters import (
    format_byte_count,
    format_context_occupancy_label,
    format_mcp_status_label,
    format_token_count,
    format_token_rate,
    model_status_label,
    short_workspace_label,
)
from synapse.ui.topbar import (
    TopBarContext,
    install_default_components,
    layout_from_registry,
)
from synapse.ui.topbar.git_chrome import probe_git_branch_chrome
from synapse.ui.topbar.widget import TopBar
from synapse.ui.tui_styles import _TOPBAR_BRANCH_MARK


class ChromeController:
    """Rendering helpers for topbar/bottombar chrome state."""

    def __init__(self, app: Any) -> None:
        self._app = app

    # -- MCP status label ----------------------------------------------------

    def _current_agent(self) -> Any | None:
        """Return the active session's frozen agent when one exists."""
        app = self._app
        turn = getattr(app, "_turn", None)
        if turn is not None:
            agent = turn.agent_for_session(app.thread_id)
            if agent is not None:
                return agent
        return getattr(app, "agent", None)

    def mcp_snapshot(self) -> tuple[bool, list[str], list[str], list[str], bool]:
        from synapse.app.agent import build_coding_agent

        app = self._app
        enabled = bool(getattr(app.settings, "enable_mcp", True))
        # Prefer the current session's agent: the module-level last_mcp_* are
        # process-global snapshots of the most recent build, and the live MCP
        # pool may have been replaced by another session's reload. The frozen
        # agent records the server/tool set it actually compiled in.
        agent = self._current_agent()
        if agent is not None:
            attached = bool(getattr(agent, "_coding_mcp_attached", False))
            servers = list(getattr(agent, "_coding_mcp_servers", []) or [])
            tools = list(getattr(agent, "_coding_mcp_tool_names", []) or [])
            if not attached:
                # This session never attached MCP tools (deferred start or
                # disabled); report it as off even if another session's pool
                # is alive.
                return enabled, [], [], [], True
            if tools:
                return enabled, servers, tools, [], False
            # Attached flag set but no tool metadata (older agent build):
            # fall back to the last-build snapshot rather than the live pool.
            fallback_servers = list(getattr(build_coding_agent, "last_mcp_servers", []) or [])
            fallback_tools = list(getattr(build_coding_agent, "last_mcp_tool_names", []) or [])
            return enabled, fallback_servers, fallback_tools, [], False
        servers = list(getattr(build_coding_agent, "last_mcp_servers", []) or [])
        tools = list(getattr(build_coding_agent, "last_mcp_tool_names", []) or [])
        warnings = list(getattr(build_coding_agent, "last_mcp_warnings", []) or [])
        deferred = bool(getattr(build_coding_agent, "last_mcp_deferred", False))
        return enabled, servers, tools, warnings, deferred

    def mcp_label(self) -> str:
        enabled, servers, tools, warnings, deferred = self.mcp_snapshot()
        return format_mcp_status_label(
            enabled=enabled,
            servers=servers,
            tools=tools,
            warnings=warnings,
            deferred=deferred,
        )

    # -- session title --------------------------------------------------------

    def current_session_model_label(self) -> str:
        """Model label for the active session, not the global settings.

        The frozen session agent is the authoritative model source; falling
        back to settings keeps chrome alive during startup and cold sessions.
        """
        app = self._app
        agent = self._current_agent()
        if agent is None:
            return model_status_label(app.settings)
        profile = str(getattr(agent, "_coding_model_profile", None) or "").strip()
        if not profile:
            return model_status_label(app.settings)
        try:
            from types import SimpleNamespace

            from synapse.models.registry import format_model_status

            registry = getattr(agent, "_coding_model_registry", None)
            if registry is None:
                return model_status_label(app.settings)
            prof = registry.get(profile)
            view = SimpleNamespace(
                model=str(getattr(prof, "model", None) or profile),
                enable_thinking=getattr(app.settings, "enable_thinking", True),
                reasoning_effort=getattr(app.settings, "reasoning_effort", None),
            )
            return format_model_status(view)
        except Exception:  # noqa: BLE001 - chrome rendering is best-effort
            return model_status_label(app.settings)

    def reload_session_title(self) -> None:
        """Load human title for the active thread into chrome state."""
        app = self._app
        title = ""
        try:
            from synapse.sessions.store import SessionStore

            store = getattr(app, "_session_store", None)
            if store is None:
                store = SessionStore(app.settings.resolved_sessions_path())
                app._session_store = store
            info = store.get(app.thread_id)
            if info is not None:
                title = (info.title or "").strip()
        except Exception:  # noqa: BLE001
            title = ""
        app._session_title = title

    def session_title_label(self, *, max_len: int = 48) -> str:
        title = (getattr(self._app, "_session_title", "") or "").strip()
        if not title:
            # Compact fallback so middle is never empty.
            tid = str(getattr(self._app, "thread_id", "") or "")
            title = tid if len(tid) <= 12 else f"{tid[:8]}…"
        if len(title) <= max_len:
            return title
        return title[: max(0, max_len - 1)] + "…"

    # -- usage ------------------------------------------------------------------

    def context_window_tokens(self) -> int | None:
        """Model context window (tokens) from chat model profile or models.json."""
        app = self._app
        agent = getattr(app, "agent", None)
        model = getattr(agent, "_coding_model", None) if agent is not None else None
        profile = getattr(model, "profile", None) if model is not None else None
        if isinstance(profile, dict):
            raw = profile.get("max_input_tokens")
            try:
                n = int(raw) if raw is not None else 0
            except (TypeError, ValueError):
                n = 0
            if n > 0:
                return n

        reg = (
            getattr(agent, "_coding_model_registry", None)
            if agent is not None
            else None
        )
        name = None
        if agent is not None:
            name = getattr(agent, "_coding_model_profile", None)
        if not name:
            name = getattr(app.settings, "active_model", None) or getattr(
                app.settings, "model", None
            )
        if reg is not None and name:
            try:
                prof = reg.get(name)
                win = getattr(prof, "context_window", None)
                if win is not None and int(win) > 0:
                    return int(win)
            except Exception:  # noqa: BLE001
                pass

        try:
            from synapse.models.registry import registry_from_settings

            reg2 = registry_from_settings(app.settings)
            if reg2 is not None:
                prof2 = reg2.get(name)
                win2 = getattr(prof2, "context_window", None)
                if win2 is not None and int(win2) > 0:
                    return int(win2)
        except Exception:  # noqa: BLE001
            pass
        return None

    def usage_right_label(self) -> str | Text:
        """Render token totals and occupancy using distinct theme colors.

        Input, cache, output, and the current context occupancy each keep their
        own visual role while separators remain muted.
        """
        app = self._app
        last_in = int(getattr(app, "_context_tokens", 0) or 0)
        has_totals = bool(
            app._input_tokens or app._cache_tokens or app._output_tokens
        )
        if has_totals:
            input_tokens = app._input_tokens
            cache_tokens = app._cache_tokens
            output_tokens = app._output_tokens
        elif last_in:
            input_tokens, cache_tokens, output_tokens = last_in, 0, 0
        else:
            input_tokens = cache_tokens = output_tokens = 0

        occupancy = format_context_occupancy_label(
            last_input_tokens=last_in,
            context_window=self.context_window_tokens(),
        )
        if not (has_totals or last_in or occupancy):
            return ""

        label = Text()
        if has_totals or last_in:
            label.append(format_token_count(input_tokens), style=_styles._C_FG)
            label.append("/", style=_styles._C_MUTED)
            label.append(format_token_count(cache_tokens), style=_styles._C_GREEN)
            label.append("/", style=_styles._C_MUTED)
            label.append(format_token_count(output_tokens), style=_styles._C_ORANGE)
        if occupancy:
            if label:
                label.append(" ", style=_styles._C_MUTED)
            label.append(occupancy, style=_styles._C_GREEN)
        return label

    def turn_stats_label(self) -> str | Text:
        """Render turn number, last-turn TTFT/throughput, session token usage
        and tool-output savings for the bottombar."""
        app = self._app
        ttft = app._last_ttft_s
        rate = app._output_tokens_per_second
        steps = app._last_model_calls
        turn = int(getattr(app, "_current_turn", 0) or 0)
        parts: list[str | Text] = []
        if turn:
            parts.append(f"turn {turn}")
        if ttft is not None:
            parts.append(f"TTFT {ttft:.1f}s")
        rate_label = format_token_rate(rate, estimated=app._token_rate_estimated)
        if rate_label:
            parts.append(rate_label)
        if steps:
            parts.append(f"{steps} step" + ("s" if steps != 1 else ""))
        usage = self.usage_right_label()
        if usage:
            parts.append(usage)
        saved = self.tool_output_label()
        if saved:
            parts.append(saved)
        if not parts:
            return ""
        label = Text()
        for i, part in enumerate(parts):
            if i:
                label.append(" · ", style=_styles._C_ORANGE)
            if isinstance(part, Text):
                label.append(part)
            else:
                # Plain segments keep the bottombar CENTER region's orange
                # fallback so the padded region style still applies (Text
                # chunks skip the region fallback in render_packed_line).
                label.append(part, style=_styles._C_ORANGE)
        return label

    def begin_turn_usage(self) -> None:
        """Mark session totals baseline for live per-call topbar updates."""
        app = self._app
        app._usage_base_input = int(app._input_tokens or 0)
        app._usage_base_output = int(app._output_tokens or 0)
        app._usage_base_cache = int(app._cache_tokens or 0)
        app._output_tokens_per_second = None
        app._last_ttft_s = None
        app._last_rate_basis = "end_to_end"
        app._token_rate_estimated = False
        app._last_model_calls = 0

    def apply_turn_usage(
        self,
        *,
        turn_input: int = 0,
        turn_output: int = 0,
        turn_cache: int = 0,
        last_input: int = 0,
        last_output: int = 0,
        last_cache: int = 0,
        output_tokens_per_second: float | None = None,
        ttft_s: float | None = None,
        rate_basis: str = "end_to_end",
        rate_estimated: bool = False,
        model_calls: int = 0,
    ) -> None:
        """Apply cumulative-in-turn usage (from stream) onto session chrome.

        ``turn_*`` are totals for the *current* stream/turn so far (not deltas).
        Session display = baseline + turn totals. Occupancy uses last call input.
        """
        app = self._app
        app._input_tokens = int(app._usage_base_input or 0) + max(
            0, int(turn_input or 0)
        )
        app._output_tokens = int(app._usage_base_output or 0) + max(
            0, int(turn_output or 0)
        )
        app._cache_tokens = int(app._usage_base_cache or 0) + max(
            0, int(turn_cache or 0)
        )
        if last_input or last_output or last_cache:
            app._context_tokens = int(last_input or 0)
            app._last_out_tokens = int(last_output or 0)
        if output_tokens_per_second is not None:
            app._output_tokens_per_second = float(output_tokens_per_second)
            app._last_ttft_s = float(ttft_s) if ttft_s is not None else None
            app._last_rate_basis = str(rate_basis or "end_to_end")
            app._token_rate_estimated = bool(rate_estimated)
        app._last_model_calls = max(0, int(model_calls or 0))
        self._refresh_chrome_both()

    def _refresh_chrome_both(self) -> None:
        """Refresh topbar then bottombar, each isolated so one failure never
        starves the other (usage/savings now render in the bottombar)."""
        app = self._app
        try:
            app._refresh_topbar()
        except Exception:  # noqa: BLE001 - chrome refresh must never break a turn
            pass
        self._refresh_bottombar_best_effort()

    def apply_restored_usage(self, messages: list[Any] | None) -> None:
        """Hydrate topbar totals from checkpoint messages (session open / switch)."""
        app = self._app
        try:
            from synapse.ui.stream import aggregate_usage_from_messages
        except Exception:  # noqa: BLE001
            return
        try:
            agg = aggregate_usage_from_messages(messages)
        except Exception:  # noqa: BLE001
            return
        app._input_tokens = int(agg.get("input_tokens") or 0)
        app._output_tokens = int(agg.get("output_tokens") or 0)
        app._cache_tokens = int(agg.get("cache_tokens") or 0)
        app._context_tokens = int(agg.get("last_input_tokens") or 0)
        app._last_out_tokens = int(agg.get("last_output_tokens") or 0)
        app._output_tokens_per_second = None
        app._last_ttft_s = None
        app._last_rate_basis = "end_to_end"
        app._token_rate_estimated = False
        app._last_model_calls = 0
        app._usage_base_input = app._input_tokens
        app._usage_base_output = app._output_tokens
        app._usage_base_cache = app._cache_tokens
        self._refresh_chrome_both()

    def apply_projected_usage(self, usage: TranscriptUsage) -> None:
        """Hydrate topbar totals from the O(1) transcript usage projection."""
        app = self._app
        app._input_tokens = max(0, int(usage.input_tokens))
        app._output_tokens = max(0, int(usage.output_tokens))
        app._cache_tokens = max(0, int(usage.cache_tokens))
        app._context_tokens = max(0, int(usage.last_input_tokens))
        app._last_out_tokens = max(0, int(usage.last_output_tokens))
        app._output_tokens_per_second = None
        app._last_ttft_s = None
        app._last_rate_basis = "end_to_end"
        app._token_rate_estimated = False
        app._last_model_calls = 0
        app._usage_base_input = app._input_tokens
        app._usage_base_output = app._output_tokens
        app._usage_base_cache = app._cache_tokens
        self._refresh_chrome_both()

    def reset_session_token_chrome(self) -> None:
        app = self._app
        app._input_tokens = 0
        app._cache_tokens = 0
        app._output_tokens = 0
        app._context_tokens = 0
        app._last_out_tokens = 0
        app._last_model_calls = 0
        app._usage_base_input = 0
        app._usage_base_output = 0
        app._usage_base_cache = 0
        # Turn chrome is session-scoped: reset on every switch (incl. /new) and
        # let the transcript restore path re-seed it from the projected turns.
        app._current_turn = 0
        self._refresh_bottombar_best_effort()

    def _refresh_bottombar_best_effort(self) -> None:
        """Refresh bottombar chrome; never let a refresh break session state."""
        app = self._app
        try:
            app._refresh_bottombar()
        except Exception:  # noqa: BLE001 - chrome refresh must never break a turn
            pass

    # -- tool-output stats ------------------------------------------------------

    def tool_output_label(self) -> str | Text:
        """Render the stable net tool-output saving for the active session."""
        app = self._app
        stats = app._tool_output_stats
        if app._tool_output_stats_thread_id != app.thread_id or not stats:
            return ""
        if not int(stats.get("transformed", 0) or 0):
            return ""
        saved = max(0, int(stats.get("effective_saved_bytes", 0) or 0))
        # Keep the chrome to a stable absolute metric. The ratio is cumulative and
        # changes whenever any later tool output is recorded, so it belongs in hover.
        return Text(
            f"saved {format_byte_count(saved)}",
            style=_styles._C_GREEN if saved else _styles._C_ORANGE,
        )

    def tool_output_hover_stats(self) -> dict[str, Any]:
        """Return a snapshot for the tool-output topbar hover popover."""
        app = self._app
        if app._tool_output_stats_thread_id != app.thread_id:
            return {}
        return dict(app._tool_output_stats or {})

    def reload_tool_output_stats(self) -> None:
        """Schedule persistent metrics loading outside Textual's UI thread."""
        app = self._app
        if app._tool_output_refresh_pending:
            return
        app._tool_output_refresh_pending = True
        if not bool(getattr(app, "is_running", False)):
            self.refresh_tool_output_stats_bg(app.thread_id, debounce=False)
            return
        app._refresh_tool_output_stats_bg(app.thread_id)

    def on_tool_output_metrics_changed(self, thread_id: str) -> None:
        """Worker-side metrics signal: forward to the UI thread only.

        ``_tool_output_refresh_pending`` / ``_tool_output_refresh_dirty`` are
        owned by the Textual UI thread. This worker entry never reads or writes
        them directly, which removes the cross-thread check-then-set race.
        Falls back to the inline handler when the app is not running yet
        (startup) or the call already happens on the UI thread.
        """
        app = self._app
        try:
            app.call_from_thread(self._on_tool_output_metrics_changed_ui, thread_id)
        except Exception:  # noqa: BLE001
            self._on_tool_output_metrics_changed_ui(thread_id)

    def _on_tool_output_metrics_changed_ui(self, thread_id: str) -> None:
        """UI-thread coalescing step: mark dirty or schedule a refresh."""
        app = self._app
        if thread_id != app.thread_id:
            return
        if app._tool_output_refresh_pending:
            app._tool_output_refresh_dirty = True
            return
        self.reload_tool_output_stats()

    def refresh_tool_output_stats_bg(self, thread_id: str, *, debounce: bool = True) -> None:
        """Worker body: aggregate metrics, then publish one immutable snapshot."""
        app = self._app
        if debounce:
            time.sleep(0.35)
        try:
            stats = app._tool_output_repo.chrome_stats(thread_id=thread_id)
        except Exception:  # noqa: BLE001
            stats = {}
        try:
            app.call_from_thread(self.apply_tool_output_stats, thread_id, stats)
        except RuntimeError:
            self.apply_tool_output_stats(thread_id, stats)

    def apply_tool_output_stats(self, thread_id: str, stats: dict[str, Any]) -> None:
        """UI-thread apply step for a completed metrics refresh."""
        app = self._app
        app._tool_output_refresh_pending = False
        dirty = bool(app._tool_output_refresh_dirty)
        app._tool_output_refresh_dirty = False
        if thread_id != app.thread_id:
            self.reload_tool_output_stats()
            return
        app._tool_output_stats = dict(stats or {})
        app._tool_output_stats_thread_id = thread_id
        self._refresh_chrome_both()
        if dirty:
            self.reload_tool_output_stats()

    # -- git chrome -------------------------------------------------------------

    def render_branch_chrome(self):
        """Styled branch + dirty/diff stats/ahead/behind for the topbar."""
        from synapse.ui.topbar.git_chrome import render_branch_chrome

        app = self._app
        return render_branch_chrome(
            app._git_chrome,
            mark=_TOPBAR_BRANCH_MARK,
            color_clean=_styles._C_GREEN,
            color_dirty=_styles._C_ERROR,
            color_ahead=_styles._C_USER,
            color_behind=_styles._C_ORANGE,
            color_diverged=_styles._C_FG,
            color_files=_styles._C_DIM,
            color_added=_styles._C_GREEN,
            color_deleted=_styles._C_ERROR,
        )

    def refresh_git_chrome(self) -> None:
        """UI-thread entry: coalesce and schedule an off-thread git probe.

        ``git`` subprocess probes are cheap individually but run up to four
        times in a row (rev-parse / status / rev-list / shortstat), each with
        a sub-second timeout. Running them on Textual's event loop stalls the
        UI; this entry only marks state and defers to the worker.
        """
        app = self._app
        if app._git_chrome_refresh_pending:
            app._git_chrome_refresh_dirty = True
            return
        app._git_chrome_refresh_pending = True
        app._refresh_git_chrome_bg()

    def refresh_git_chrome_bg(self) -> None:
        """Worker body: probe git outside Textual's event loop, then apply."""
        app = self._app
        try:
            ws = Path(getattr(app.settings, "workspace", Path.cwd()) or Path.cwd())
            info = probe_git_branch_chrome(ws)
        except Exception:  # noqa: BLE001 - chrome probing is best-effort
            info = None
        try:
            app.call_from_thread(self.apply_git_chrome, info)
        except RuntimeError:
            # Synchronous test hosts reject cross-thread scheduling.
            self.apply_git_chrome(info)

    def apply_git_chrome(self, info: Any) -> None:
        """UI-thread apply step for a completed git probe."""
        app = self._app
        app._git_chrome_refresh_pending = False
        dirty = bool(app._git_chrome_refresh_dirty)
        app._git_chrome_refresh_dirty = False
        app._git_chrome = info
        app._git_branch = info.name if info is not None else None
        try:
            bar = app.query_one("#topbar", TopBar)
            bar.invalidate_files_cache()
            if not (info and info.dirty):
                bar.dismiss()
        except Exception:  # noqa: BLE001
            pass
        app._refresh_topbar()
        if dirty:
            self.refresh_git_chrome()

    # -- install default components -------------------------------------------

    def install_default_topbar(self) -> None:
        """Register built-in workspace / title / branch components.

        Token usage and tool-output savings moved to the bottombar
        turn-stats chrome; the providers are still passed for custom
        topbar installers that opt back in.
        """
        app = self._app
        install_default_components(
            app._topbar,
            TopBarContext(
                workspace=lambda: short_workspace_label(app.settings.workspace),
                title=lambda: self.render_session_title(),
                branch=self.render_branch_chrome,
                usage=self.usage_right_label,
                tool_output=self.tool_output_label,
                branch_mark=_TOPBAR_BRANCH_MARK,
            ),
        )
        self.apply_topbar_region_bands()

    def render_session_title(self, *, max_len: int = 56) -> str:
        """Session title with a background-session count badge when applicable."""
        app = self._app
        title = (app._session_title or "").strip() or self.session_title_label(max_len=max_len)
        turn = getattr(app, "_turn", None)
        if turn is None:
            return title
        try:
            bg = turn.background_running_count()
        except Exception:  # noqa: BLE001 - chrome must never break on state probes
            return title
        if bg > 0:
            return f"[{bg} bg] {title}"
        return title

    def apply_topbar_region_bands(self) -> None:
        """Apply theme topbar metrics (gap/pad already in CSS; optional band bg).

        Built-in themes leave region backgrounds empty. ``top_gap`` controls
        spacing between left/center/right. Explicit ``top_left`` /
        ``top_center`` / ``top_right`` still enable optional color bands.
        Layout stays classic: left/right hug content, center flex-fills.
        """
        app = self._app
        gap = 0
        try:
            from synapse.ui.theme import get_theme

            theme = get_theme()
            bands = theme.topbar_region_bands()
            gap = max(0, int(getattr(theme, "top_gap", 0) or 0))
        except Exception:  # noqa: BLE001
            bands = {
                "left": (_styles._C_FG, ""),
                "center": (_styles._C_FG, ""),
                "right": (_styles._C_DIM, ""),
            }

        layout = {
            "left": {
                "flex": 0,
                "align": "left",
                "min_width": 0,
                "priority": 40,
                "gap_after": gap,
                # Fallback cap only: keep the left chrome (workspace + branch +
                # diff stats like "6f +314 -13") fully visible for normal
                # projects, and reserve room for the centered title on wide
                # terminals. A very long branch/workspace elides via
                # render_for_width only when it exceeds this fallback.
                "max_width": 80,
            },
            "center": {
                "flex": 1,
                "align": "center",
                "min_width": 4,
                "priority": 10,
                "gap_after": gap,
            },
            "right": {
                "flex": 0,
                "align": "right",
                "min_width": 0,
                "priority": 50,
                "gap_after": 0,
            },
        }
        for rid, (fg, bg) in bands.items():
            conf = layout.get(rid, {})
            # bg="" clears band (set_region_style treats None as "leave unchanged").
            app._topbar.set_region_style(
                rid,
                fg=fg or _styles._C_FG,
                bg=bg if bg else "",
                flex=int(conf.get("flex", 0)),
                align=str(conf.get("align", "left")),
                min_width=int(conf.get("min_width", 0)),
                priority=int(conf.get("priority", 0)),
                gap_after=int(conf.get("gap_after", 0)),
                max_width=int(conf.get("max_width", 0)),
            )

    def install_default_bottombar(self) -> None:
        """Register key_hints / mode / model / mcp / goal under the prompt."""
        app = self._app
        install_default_bottombar_components(
            app._bottombar,
            BottomBarContext(
                busy=lambda: bool(app._busy),
                thread=lambda: "",  # thread chrome disabled on bottombar
                mode=self.bottombar_mode_label,
                idle_hints=lambda: "",
                busy_hints=lambda: "",
                model=self.current_session_model_label,
                codex_usage=self.codex_usage_label,
                mcp=self.mcp_label,
                fast_mode=lambda: bool(
                    getattr(app.settings, "openai_fast_mode", False)
                )
                and self.has_codex_oauth_profile(),
                turbo=lambda: bool(getattr(app.settings, "turbo", False)),
                goal=self.goal_label,
                turn_stats=self.turn_stats_label,
            ),
        )

    # -- goal / codex / bottombar labels --------------------------------------

    def goal_label(self) -> str:
        """Render the long-running goal chrome（缓存状态，绝不阻塞渲染路径）。"""
        goal = getattr(self._app, "_current_goal", None)
        if goal is None:
            return ""
        try:
            from synapse.ui.bottombar.components.goal import goal_indicator_text

            return goal_indicator_text(goal)
        except Exception:  # noqa: BLE001 - chrome 渲染不能失败
            return ""

    def bind_goal_listener(self) -> None:
        """Subscribe to GoalService changes to refresh the bottombar."""
        app = self._app
        service = getattr(app.agent, "_coding_goal_service", None)
        if service is None:
            return
        if getattr(app, "_goal_listener_bound", False):
            return

        def _on_goal_changed(thread_id: str, goal: object | None) -> None:
            def _apply() -> None:
                current_thread_id = app.__dict__.get("thread_id")
                if thread_id and current_thread_id and thread_id != current_thread_id:
                    return
                previous = app.__dict__.get("_current_goal")
                app.__dict__["_current_goal"] = goal
                try:
                    app._bottombar.refresh()
                except Exception:  # noqa: BLE001
                    pass
                from synapse.goals.model import ThreadGoalStatus

                # 预算耗尽且回合仍在运行：注入收尾引导，让模型停止新工作。
                if goal is not None and app.__dict__.get("_busy", False):
                    if goal.status == ThreadGoalStatus.BUDGET_LIMITED:
                        from synapse.goals.steering import budget_limit_prompt

                        goal_runtime = service.runtime(thread_id)
                        if goal_runtime.mark_budget_reported(goal.goal_id):
                            app._turn.queue_guidance(budget_limit_prompt(goal))
                    elif (
                        previous is not None
                        and getattr(previous, "objective", None) != goal.objective
                    ):
                        from synapse.goals.steering import objective_updated_prompt

                        app._turn.queue_guidance(objective_updated_prompt(goal))
                    return
                # 目标变为 active 且线程空闲（/goal 设置、resume 等）：立即续跑。
                if goal is not None and goal.status == ThreadGoalStatus.ACTIVE:
                    try:
                        from synapse.goals.steering import GOAL_STEER_PREFIX, continuation_prompt
                        app._turn.queue_guidance(
                            f"{GOAL_STEER_PREFIX}\n{continuation_prompt(goal)}"
                        )
                    except Exception:  # noqa: BLE001
                        pass

            try:
                app.call_from_thread(_apply)
            except RuntimeError:
                # 已在 UI 线程（slash 处理中同步通知）：直接执行。
                _apply()
            except Exception:  # noqa: BLE001 - 非 Textual 线程环境同步回退
                try:
                    _apply()
                except Exception:  # noqa: BLE001 - 通知不能阻断 goal 持久化
                    pass

        app._goal_listener_bound = True
        app._goal_listener_fn = _on_goal_changed
        service.add_listener(_on_goal_changed)

    def load_current_goal(self) -> None:
        """Load the active thread's goal after startup / session switch."""
        app = self._app
        service = getattr(app.agent, "_coding_goal_service", None)
        if service is None:
            return
        try:
            goal = service.get(app.thread_id)
        except Exception:  # noqa: BLE001
            goal = None
        app._current_goal = goal
        try:
            app._bottombar.refresh()
        except Exception:  # noqa: BLE001
            pass

    def codex_usage_label(self) -> str | Text:
        """Render cached Codex usage; never block the UI render path."""
        if not self.has_codex_oauth_profile():
            return ""
        return self._app._codex.label

    def has_codex_oauth_profile(self) -> bool:
        """Return whether the selected profile uses Codex OAuth.

        Reuses the session agent's registry and caches the verdict on the
        Codex service so the label path never touches the filesystem.
        """
        agent = self._current_agent()
        registry = getattr(agent, "_coding_model_registry", None)
        if registry is None:
            self._app._codex.oauth_profile = False
            return False
        try:
            selected = getattr(agent, "_coding_model_profile", None) or registry.default
            value = registry.get(selected).auth == "openai_oauth"
        except Exception:  # noqa: BLE001
            value = False
        self._app._codex.oauth_profile = value
        return value

    def bottombar_thread_label(self) -> str:
        """Short thread id for the bottombar right slot."""
        tid = (getattr(self._app, "thread_id", "") or "").strip()
        if not tid:
            return ""
        if len(tid) <= 12:
            return tid
        return f"{tid[:4]}…{tid[-4:]}"

    def bottombar_mode_label(self) -> str:
        """Optional center mode badge. Steer count shown in status line only."""
        return ""

    def refresh_topbar(self, tokens: str | None = None) -> None:
        """Rebuild the topbar line from the registry layout."""
        del tokens  # legacy arg; usage is tracked on the app
        app = self._app
        usable = app._topbar_usable_width()
        line = layout_from_registry(
            app._topbar,
            usable_width=usable,
            left_style=_styles._C_FG,
            center_style=_styles._C_FG,
            right_style=_styles._C_DIM,
            gap_style=_styles._C_DIM,
        )
        app.query_one("#topbar", TopBar).update(line)

    # -- codex reset-credits popup --------------------------------------------

    def open_codex_reset_dialog(self) -> None:
        """Show reset-credit details in a popup; fetch details if needed."""
        app = self._app
        credits = app._codex.reset_credits
        sn = app._codex.snapshot
        available = (
            sn.reset_credits.available_count if sn and sn.reset_credits else 0
        )
        # When we have a count but no detail rows yet, fetch first then re-open.
        if available > 0 and credits is None:
            app.flash_status("fetching reset-credit details…", "dim")
            app._fetch_codex_reset_credits_for_dialog_bg()
            return
        dialog = CodexResetDialog(
            credits=list(credits.credits) if credits else [],
            available_count=available,
            on_reset=self.on_codex_reset_request,
        )
        app.push_screen(dialog, lambda _: None)

    def _thread_call(self, callback: Any, *args: Any, **kwargs: Any) -> None:
        """Run a UI callback, falling back to inline execution on the UI thread."""
        try:
            self._app.call_from_thread(callback, *args, **kwargs)
        except RuntimeError:
            callback(*args, **kwargs)
        except Exception:  # noqa: BLE001 - non-Textual hosts degrade to inline
            try:
                callback(*args, **kwargs)
            except Exception:  # noqa: BLE001 - best-effort UI update
                pass

    def fetch_codex_reset_credits_for_dialog_bg(self) -> None:
        try:
            self._app._codex.fetch_reset_credits()
        except Exception as exc:  # noqa: BLE001
            self._thread_call(
                self._app.flash_status,
                f"Codex reset-credits fetch failed: {exc}",
                "yellow",
            )
            return
        self._thread_call(self._app._open_codex_reset_dialog)

    def on_codex_reset_request(self, credit_id: str) -> None:
        """User clicked Reset on a specific credit."""
        app = self._app
        if app._codex.consuming:
            return
        app._codex.consuming = True
        app._refresh_bottombar()
        app.flash_status("redeeming Codex reset…", "dim")
        app._consume_codex_reset_bg(credit_id)

    def consume_codex_reset_bg(self, credit_id: str) -> None:
        try:
            result = self._app._codex.consume_reset(credit_id)
        except Exception as exc:  # noqa: BLE001
            self._thread_call(
                self._app.flash_status, f"Codex reset failed: {exc}", "yellow"
            )
            self._thread_call(self._app._on_codex_reset_consume_done)
            return
        self._thread_call(self._app._on_codex_reset_consumed, result)

    def on_codex_reset_consumed(self, result: ConsumeResetResult) -> None:
        app = self._app
        outcome = result.outcome
        if outcome in {"reset", "alreadyRedeemed"}:
            app.flash_status(f"Codex reset {outcome}; refreshing…", "dim")
            app._refresh_codex_usage(force=True)
        elif outcome == "nothingToReset":
            app.flash_status("Codex reset: no eligible window.", "yellow")
        elif outcome == "noCredit":
            app.flash_status("Codex reset: no credits available.", "yellow")
        else:
            app.flash_status(f"Codex reset: {outcome}", "yellow")
        app._on_codex_reset_consume_done()

    def on_codex_reset_consume_done(self) -> None:
        app = self._app
        app._codex.consuming = False
        app._codex.reset_credits = None  # force refetch next open
        app._refresh_bottombar()

    def refresh_codex_usage(self, *, force: bool = False) -> None:
        """Start a background usage fetch when an OAuth profile is active."""
        app = self._app
        oauth = self.has_codex_oauth_profile()
        if not app._codex.should_refresh(force=force):
            if not oauth:
                app._codex.invalidate()
                app._refresh_bottombar()
            return
        app._codex.loading = True
        app._refresh_bottombar()
        app._fetch_codex_usage_bg()

    def fetch_codex_usage_bg(self) -> None:
        self._app._codex.refresh_usage()
        self._thread_call(self._app._on_codex_usage_ready)

    def on_codex_usage_ready(self) -> None:
        app = self._app
        app._codex.loading = False
        app._refresh_bottombar()
