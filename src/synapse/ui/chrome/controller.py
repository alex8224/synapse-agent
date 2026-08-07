"""Chrome state and rendering: usage, tool-output stats, git, session title.

Owns the topbar/bottombar rendering helpers that used to live directly on
``CodingAgentApp``. The Textual host keeps the registries and Textual wiring
and forwards here; the controller reads/writes host state through ``_app``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rich.text import Text

import synapse.ui.tui_styles as _styles
from synapse.integrations.openai_usage import ConsumeResetResult
from synapse.runtime.steer import get_agent_steer_queue
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

    def mcp_snapshot(self) -> tuple[bool, list[str], list[str], list[str], bool]:
        from synapse.app.agent import build_coding_agent

        enabled = bool(getattr(self._app.settings, "enable_mcp", True))
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

    def reload_session_title(self) -> None:
        """Load human title for the active thread into chrome state."""
        app = self._app
        title = ""
        try:
            from synapse.sessions.store import SessionStore

            info = SessionStore(app.settings.resolved_sessions_path()).get(
                app.thread_id
            )
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
        rate_label = format_token_rate(
            app._output_tokens_per_second,
            estimated=app._token_rate_estimated,
        )
        if rate_label:
            if label:
                label.append(" ", style=_styles._C_MUTED)
            label.append(rate_label, style=_styles._C_ORANGE)
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
        app._refresh_topbar()

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
        app._usage_base_input = app._input_tokens
        app._usage_base_output = app._output_tokens
        app._usage_base_cache = app._cache_tokens
        app._refresh_topbar()

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
        app._usage_base_input = app._input_tokens
        app._usage_base_output = app._output_tokens
        app._usage_base_cache = app._cache_tokens
        app._refresh_topbar()

    def reset_session_token_chrome(self) -> None:
        app = self._app
        app._input_tokens = 0
        app._cache_tokens = 0
        app._output_tokens = 0
        app._context_tokens = 0
        app._last_out_tokens = 0
        app._usage_base_input = 0
        app._usage_base_output = 0
        app._usage_base_cache = 0

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
        """Load persistent metrics for the active session outside the render path."""
        app = self._app
        try:
            stats = app._tool_output_repo.stats(thread_id=app.thread_id)
        except Exception:  # noqa: BLE001
            stats = {}
        app._tool_output_stats = stats
        app._tool_output_stats_thread_id = app.thread_id
        app._refresh_topbar()

    def on_tool_output_metrics_changed(self, thread_id: str) -> None:
        """Receive a worker-thread metric write and coalesce UI refreshes."""
        app = self._app
        if thread_id != app.thread_id or app._tool_output_refresh_pending:
            return
        app._tool_output_refresh_pending = True
        try:
            app.call_from_thread(self.refresh_tool_output_stats)
        except Exception:  # noqa: BLE001
            app._tool_output_refresh_pending = False

    def refresh_tool_output_stats(self) -> None:
        app = self._app
        app._tool_output_refresh_pending = False
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
        """Re-probe local git status for the topbar (cheap, local-only)."""
        app = self._app
        try:
            ws = Path(getattr(app.settings, "workspace", Path.cwd()) or Path.cwd())
            app._git_chrome = probe_git_branch_chrome(ws)
            app._git_branch = app._git_chrome.name if app._git_chrome else None
        except Exception:  # noqa: BLE001
            pass
        try:
            bar = app.query_one("#topbar", TopBar)
            bar.invalidate_files_cache()
            if not (app._git_chrome and app._git_chrome.dirty):
                bar.dismiss()
        except Exception:  # noqa: BLE001
            pass
        app._refresh_topbar()

    # -- install default components -------------------------------------------

    def install_default_topbar(self) -> None:
        """Register built-in workspace / title / branch / usage components."""
        app = self._app
        install_default_components(
            app._topbar,
            TopBarContext(
                workspace=lambda: short_workspace_label(app.settings.workspace),
                title=lambda: (app._session_title or "").strip()
                or self.session_title_label(max_len=56),
                branch=self.render_branch_chrome,
                usage=self.usage_right_label,
                tool_output=self.tool_output_label,
                branch_mark=_TOPBAR_BRANCH_MARK,
            ),
        )
        self.apply_topbar_region_bands()

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
                idle_hints=lambda: (
                    "Tab complete · / · Alt+C copy · F2 model · F4 sessions · F9 agents"
                ),
                busy_hints=lambda: "Esc cancel · Enter queue · Alt+C copy · F9 agents",
                model=lambda: model_status_label(app.settings),
                codex_usage=self.codex_usage_label,
                mcp=self.mcp_label,
                fast_mode=lambda: bool(
                    getattr(app.settings, "openai_fast_mode", False)
                )
                and self.has_codex_oauth_profile(),
                goal=self.goal_label,
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

                        runtime = service.runtime(thread_id)
                        queue = (
                            getattr(app, "_active_steer_queue", None)
                            or app._turn_steer_queue()
                            if runtime.mark_budget_reported(goal.goal_id)
                            else None
                        )
                        if queue is not None:
                            try:
                                queue.push(budget_limit_prompt(goal))
                            except Exception:  # noqa: BLE001
                                pass
                    elif (
                        previous is not None
                        and getattr(previous, "objective", None) != goal.objective
                    ):
                        from synapse.goals.steering import objective_updated_prompt

                        queue = (
                            getattr(app, "_active_steer_queue", None)
                            or app._turn_steer_queue()
                        )
                        if queue is not None:
                            try:
                                queue.push(objective_updated_prompt(goal))
                            except Exception:  # noqa: BLE001
                                pass
                    return
                # 目标变为 active 且线程空闲（/goal 设置、resume 等）：立即续跑。
                if goal is not None and goal.status == ThreadGoalStatus.ACTIVE:
                    try:
                        queue = app.__dict__.get("_active_steer_queue")
                        if queue is None:
                            queue = get_agent_steer_queue(app.__dict__.get("agent"))
                        if queue is not None:
                            from synapse.goals.steering import (
                                GOAL_STEER_PREFIX,
                                continuation_prompt,
                            )

                            if not any(
                                str(item).strip().startswith(GOAL_STEER_PREFIX)
                                for item in queue.peek_items()
                            ):
                                queue.push(
                                    f"{GOAL_STEER_PREFIX}\n{continuation_prompt(goal)}"
                                )
                                schedule = getattr(app, "_schedule_followup_steer", None)
                                if schedule is not None:
                                    schedule(queue)
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
        return self._app._codex.label

    def has_codex_oauth_profile(self) -> bool:
        """Return whether the currently selected profile uses Codex OAuth."""
        return self._app._codex.has_oauth_profile()

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

    def fetch_codex_reset_credits_for_dialog_bg(self) -> None:
        try:
            self._app._codex.fetch_reset_credits()
        except Exception as exc:  # noqa: BLE001
            self._app.call_from_thread(
                self._app.flash_status,
                f"Codex reset-credits fetch failed: {exc}",
                "yellow",
            )
            return
        self._app.call_from_thread(self._app._open_codex_reset_dialog)

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
            self._app.call_from_thread(
                self._app.flash_status, f"Codex reset failed: {exc}", "yellow"
            )
            self._app.call_from_thread(self._app._on_codex_reset_consume_done)
            return
        self._app.call_from_thread(self._app._on_codex_reset_consumed, result)

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
        if not app._codex.should_refresh(force=force):
            if not app._codex.has_oauth_profile():
                app._codex.invalidate()
                app._refresh_bottombar()
            return
        app._codex.loading = True
        app._refresh_bottombar()
        app._fetch_codex_usage_bg()

    def fetch_codex_usage_bg(self) -> None:
        self._app._codex.refresh_usage()
        self._app.call_from_thread(self._app._on_codex_usage_ready)

    def on_codex_usage_ready(self) -> None:
        app = self._app
        app._codex.loading = False
        app._refresh_bottombar()
