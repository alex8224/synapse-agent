"""Turn state machine: submit, run, resume, settle, follow-up steer, goals.

Owns the turn lifecycle logic that used to live directly on ``CodingAgentApp``.
The Textual host keeps event wiring (``@on``, ``@work``) and forwards here, so
the controller can be exercised against a fake host surface.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from textual.widgets import Input

from synapse.content.multimodal import find_placeholders
from synapse.runtime.steer import (
    SteerQueue,
    format_steer_message,
    get_agent_steer_queue,
)
from synapse.sessions.transcript_projection import TranscriptUsage
from synapse.subagent_monitor import MONITOR_CONFIG_KEY
from synapse.ui.stream import extract_last_ai_text, stream_agent
from synapse.ui.textual_stream_sink import TextualStreamSink
from synapse.ui.turn.request import build_turn_request


class TurnController:
    """One graph run: from user submit through turn end and goal settlement."""

    def __init__(self, app: Any) -> None:
        self._app = app

    # -- submit ------------------------------------------------------------

    def submit(self, event: Any) -> None:
        """Handle one ``Input.Submitted`` on #prompt."""
        app = self._app
        text = (event.value or "").strip()
        event.input.value = ""
        if not text:
            return
        # A real turn supersedes the background prewarm; stop it so two huge
        # requests never queue on the provider at the same time.
        app._prewarm_cancel_event.set()

        # 渲染用文本保留粘贴占位符（大块内容在显示时压缩），推理/历史用完整展开文本。
        text, display = app._prompt.expand_paste(text)

        # Parse [image#N] placeholders from text and resolve to attachments.
        ids = find_placeholders(text)
        attachments: list[Any] = []
        if ids:
            seen: set[int] = set()
            for pid in ids:
                if pid in seen:
                    continue
                seen.add(pid)
                att = app._image_bank.items.get(pid)
                if att is not None:
                    attachments.append(att)

        app._prompt.add_history(text)
        if app._handle_slash(text):
            app._image_bank.clear()
            return
        if app._busy:
            # Mid-run guidance: queue only (panel + prompt mode). No transcript/status.
            app._bind_steer_queue()
            q = app._turn_steer_queue()
            if q is not None:
                pending = q.push(text)
                if pending:
                    return
            app.append_event("still running previous turn…", "yellow")
            return
        try:
            from synapse.sessions.store import SessionStore

            SessionStore(app.settings.resolved_sessions_path()).touch(
                app.thread_id,
                title_hint=text,
                model=str(app.settings.model),
            )
            app._reload_session_title()
            app._refresh_topbar()
        except Exception:  # noqa: BLE001
            pass

        # Snapshot image bank BEFORE clear so run_turn retains data.
        turn_images = list(attachments)
        resolved_ids = {a.id for a in attachments}
        not_found = [f"[image#{pid}]" for pid in ids if pid not in resolved_ids]
        if not_found:
            # Keep bank + restore prompt; do not send a half-image turn.
            app.append_event(
                f"missing images: {' '.join(not_found)} (not sent)",
                "yellow",
            )
            prompt = app.query_one("#prompt", Input)
            prompt.value = text
            prompt.focus()
            return

        app._image_bank.clear()

        app.append_user(display, images=turn_images or None, full_text=text)
        self.capture_turn_context()
        app._busy = True
        app._skip_steer_followup = False
        app._cancel_event = threading.Event()
        app._transcript.reset_for_turn()
        app._subagent_monitor.reset()
        app._subagent_monitor_auto_opened = False
        app._clear_subagent_status()
        app.clear_stream()
        app.set_activity("thinking", "starting", True)
        app._sync_prompt_placeholder()
        # Notify debug store of a new turn
        try:
            from synapse.observability.llm_debug import get_debug_store

            get_debug_store().begin_turn()
        except Exception:  # noqa: BLE001
            pass
        app.run_turn(text, turn_images or None)

    # -- run ---------------------------------------------------------------

    def run_turn(self, text: str, attachments: list[Any] | None = None) -> None:
        """Run one agent turn off the UI thread (host wraps with @work)."""
        app = self._app
        if not app._agent_ready.wait(timeout=180):
            app.call_from_thread(
                app.append_event,
                "agent start timeout (180s)",
                "bold red",
            )
            app.call_from_thread(app._turn_done)
            return
        turn_agent = app._active_turn_agent or app.agent
        turn_thread_id = app._active_turn_thread_id or app.thread_id
        if app._agent_error or turn_agent is None:
            app.call_from_thread(
                app.append_event,
                f"agent unavailable: {app._agent_error or 'not built'}",
                "bold red",
            )
            app.call_from_thread(app._turn_done)
            return

        app._begin_turn_usage()
        transcript_generation = app._transcript_generation
        sink = TextualStreamSink(app)
        request = build_turn_request(
            text=text,
            attachments=attachments,
            settings=app.settings,
            thread_id=turn_thread_id,
            monitor_id=app._subagent_monitor.monitor_id,
            max_concurrency=app.settings.max_concurrency,
        )
        try:
            result = stream_agent(
                turn_agent,
                request.payload,
                request.config,
                token_stream=app.settings.token_stream,
                prefer_async=True,
                max_concurrency=app.settings.max_concurrency,
                sink=sink,
                cancel_event=app._cancel_event,
                show_reasoning_placeholders=bool(
                    getattr(app.settings, "show_reasoning_placeholders", True)
                ),
            )
            if self.apply_stream_result(
                result, transcript_generation=transcript_generation
            ):
                return
        except Exception as exc:  # noqa: BLE001
            app._call_for_transcript(
                transcript_generation,
                app.append_event,
                f"ERROR: {exc}",
                "bold red",
            )
        finally:
            app.call_from_thread(app._turn_done)

    def run_resume(self, action: str, message: str | None = None) -> None:
        """Resume graph after /approve or /reject (host wraps with @work)."""
        from synapse.runtime.hitl import (
            build_decisions,
            build_resume_payload,
            extract_pending_interrupt,
            format_interrupt_lines,
        )

        app = self._app
        turn_agent = app._active_turn_agent or app.agent
        turn_thread_id = app._active_turn_thread_id or app.thread_id
        if turn_agent is None:
            app.call_from_thread(app.append_event, "agent unavailable", "bold red")
            app.call_from_thread(app._turn_done)
            return
        app._begin_turn_usage()
        sink = TextualStreamSink(app)
        # Allow Esc to abort resume stream as well.
        app._cancel_event = threading.Event()
        config = {
            "configurable": {
                "thread_id": turn_thread_id,
                MONITOR_CONFIG_KEY: app._subagent_monitor.monitor_id,
            },
            "max_concurrency": app.settings.max_concurrency,
        }
        try:
            pending = extract_pending_interrupt(turn_agent, config)
            if pending is None or (not pending.actions and not pending.raw):
                app.call_from_thread(app.append_event, "no pending approval", "yellow")
                return
            for line in format_interrupt_lines(pending):
                app.call_from_thread(app.append_event, line, "dim")
            decisions = build_decisions(pending, action=action, message=message)
            payload = build_resume_payload(decisions)
            result = stream_agent(
                turn_agent,
                payload,
                config,
                token_stream=app.settings.token_stream,
                prefer_async=True,
                max_concurrency=app.settings.max_concurrency,
                sink=sink,
                cancel_event=app._cancel_event,
                show_reasoning_placeholders=bool(
                    getattr(app.settings, "show_reasoning_placeholders", True)
                ),
            )
            if self.apply_stream_result(result, transcript_generation=None, resume=True):
                return
        except Exception as exc:  # noqa: BLE001
            app.call_from_thread(app.append_event, f"ERROR: {exc}", "bold red")
        finally:
            app.call_from_thread(app._turn_done)

    def apply_stream_result(
        self,
        result: Any,
        *,
        transcript_generation: int | None,
        resume: bool = False,
    ) -> bool:
        """Apply one ``stream_agent`` result to the transcript/chrome.

        Returns True when the run was cancelled and the caller should return
        early.
        """
        app = self._app

        def ui(fn: Any, *args: Any, **kwargs: Any) -> None:
            if transcript_generation is None:
                app.call_from_thread(fn, *args, **kwargs)
            else:
                app._call_for_transcript(transcript_generation, fn, *args, **kwargs)

        if getattr(result, "cancelled", False):
            app._skip_steer_followup = True
            ui(app.append_event, "已终止（上下文已保留）。可继续输入。", "yellow")
            return True
        # Session token totals for chrome: input / cache / output.
        if (
            result.input_tokens
            or result.output_tokens
            or getattr(result, "cache_tokens", 0)
            or result.total_tokens
            or getattr(result, "last_input_tokens", 0)
        ):
            # Idempotent with live note_usage: baseline + turn totals.
            ui(
                app.apply_turn_usage,
                turn_input=int(result.input_tokens or 0),
                turn_output=int(result.output_tokens or 0),
                turn_cache=int(getattr(result, "cache_tokens", 0) or 0),
                last_input=int(
                    getattr(result, "last_input_tokens", 0)
                    or result.input_tokens
                    or 0
                ),
                last_output=int(
                    getattr(result, "last_output_tokens", 0)
                    or result.output_tokens
                    or 0
                ),
                last_cache=int(getattr(result, "last_cache_tokens", 0) or 0),
                output_tokens_per_second=getattr(
                    result, "last_output_tokens_per_second", None
                ),
                ttft_s=getattr(result, "last_ttft_s", None),
                rate_basis=str(getattr(result, "last_rate_basis", "end_to_end")),
                rate_estimated=False,
            )

        if not resume and getattr(result, "compact_events", 0):
            ui(app.append_event, f"context compacted ×{result.compact_events}", "dim")

        if not result.streamed_answer:
            answer = result.final_text or extract_last_ai_text(result.state)
            if answer:
                ui(app.commit_answer, answer)
            elif resume:
                pass
            elif getattr(result, "interrupted", False):
                ui(app.append_event, "HITL: use /approve or /reject", "yellow")
            else:
                ui(app.append_event, "(empty response)", "dim")
        elif getattr(result, "interrupted", False):
            ui(
                app.append_event,
                "still waiting for approval — /approve or /reject"
                if resume
                else "HITL: use /approve or /reject",
                "yellow",
            )
        return False

    # -- turn context -------------------------------------------------------

    def capture_turn_context(self) -> None:
        """Freeze the agent, thread, and queue used by one graph run."""
        app = self._app
        turn_agent = app.agent
        app._active_turn_agent = turn_agent
        app._active_turn_thread_id = app.thread_id
        app._active_steer_queue = get_agent_steer_queue(turn_agent)

    def clear_turn_context(self) -> None:
        app = self._app
        app._active_turn_agent = None
        app._active_turn_thread_id = None
        app._active_steer_queue = None

    # -- turn end -----------------------------------------------------------

    def turn_done(self) -> None:
        app = self._app
        completed_queue = app._active_steer_queue or get_agent_steer_queue(app.agent)
        app._busy = False
        app._sync_prompt_placeholder()
        # An immediate middleware drain retains the panel while the turn is
        # active. Reconcile it now so applied guidance disappears at turn end.
        if completed_queue is not None:
            app._on_steer_items_changed(completed_queue.peek_items())
        try:
            app._commit_live_tools_to_log()
        except Exception:  # noqa: BLE001
            pass
        app.clear_stream()
        app.set_activity("idle", "ready", True)
        try:
            app._refresh_git_chrome()
        except Exception:  # noqa: BLE001
            pass
        app._clear_subagent_status()
        app.query_one("#prompt", Input).focus()
        # If the model finished without another tool/model step, apply leftover
        # guidance as a follow-up turn (unless the run was Esc-cancelled).
        if getattr(app, "_skip_steer_followup", False):
            app._skip_steer_followup = False
            # Esc supersedes guidance already queued for this run, including a
            # delayed goal continuation callback.
            if completed_queue is not None:
                completed_queue.clear()
            # Consume the cancelled token so a later resume starts cleanly.
            app._cancel_event = threading.Event()
            self.clear_turn_context()
            app._bind_steer_queue()
            self.note_session_recap_turn()
            return
        # 长程目标：最终结算，并按需自动发起续跑回合。
        self.settle_goal_turn(completed_queue)
        # Capture snapshot before steer follow-up may start another busy turn.
        self.note_session_recap_turn()
        if self.schedule_followup_steer(completed_queue):
            return
        self.clear_turn_context()
        app._bind_steer_queue()

    # -- goals --------------------------------------------------------------

    def settle_goal_turn(self, completed_queue: SteerQueue | None) -> None:
        """回合结束后的 goal 结算与自动继续（长程执行核心）。

        - 结算本回合 token/时间用量并刷新 bottombar；
        - 若目标仍 active、未设置自动继续上限、用户无待处理输入，
          向 steer 队列推送 continuation 引导，由既有 follow-up 机制
          自动开启下一回合。
        """
        app = self._app
        service = getattr(app.agent, "_coding_goal_service", None)
        if service is None:
            return
        try:
            goal = service.on_turn_end(app.thread_id)
        except Exception:  # noqa: BLE001 - 结算失败不阻断 UI
            goal = service.get(app.thread_id) if app.thread_id else None
        app._current_goal = goal
        try:
            app._bottombar.refresh()
        except Exception:  # noqa: BLE001
            pass
        if goal is None:
            return
        if completed_queue is not None and completed_queue.peek_count() > 0:
            return  # 用户 steer 优先，不叠加自动续跑
        self.maybe_continue_goal(completed_queue)

    def maybe_continue_goal(self, queue: SteerQueue | None = None) -> bool:
        """若当前 thread 存在 active goal 且线程空闲，调度一次续跑回合。

        复用 steer follow-up 机制：向 steer 队列推送 continuation 引导
        （模型可见、不进面板），由 ``schedule_followup_steer`` 自动开启
        新回合。返回是否已调度。已存在未消费的 goal continuation 时
        不重复推送。
        """
        app = self._app
        if app.__dict__.get("_busy", False):
            return False
        settings = app.__dict__.get("settings")
        if not bool(getattr(settings, "goal_auto_continue", True)):
            return False
        agent = app.__dict__.get("agent")
        service = getattr(agent, "_coding_goal_service", None)
        thread_id = app.__dict__.get("thread_id")
        if service is None or not thread_id:
            return False
        from synapse.goals.model import ThreadGoalStatus
        from synapse.goals.steering import GOAL_STEER_PREFIX, continuation_prompt

        goal = service.get(thread_id)
        if goal is None or goal.status != ThreadGoalStatus.ACTIVE:
            return False
        q = queue or app._turn_steer_queue()
        if q is None:
            return False
        if any(
            str(item).strip().startswith(GOAL_STEER_PREFIX) for item in q.peek_items()
        ):
            return False
        try:
            q.push(f"{GOAL_STEER_PREFIX}\n{continuation_prompt(goal)}")
        except Exception:  # noqa: BLE001
            return False
        return True

    # -- recap / persistence ------------------------------------------------

    def note_session_recap_turn(self) -> None:
        """Remember latest turn facts for idle recap."""
        app = self._app
        transcript = getattr(app, "_transcript", None)
        if transcript is None:
            # Compatibility path for lightweight hosts that exercise turn
            # cleanup without constructing the full Textual transcript.
            return
        state = transcript.state
        user_text = ""
        if state.user_turns:
            user_text = getattr(state.user_turns[-1], "full_text", "") or ""
        try:
            app._session_recap.note_turn_done(
                time.monotonic(),
                user_text=user_text,
                tool_summary=state.last_tool_summary or "",
                tool_items=list(state.last_tool_items or []),
                answer_text=state.last_answer_text or "",
                turn_count=len(state.user_turns),
            )
        except Exception:  # noqa: BLE001
            pass
        self.persist_transcript_turn(user_text=user_text)
        self.persist_turn_summary(user_text=user_text)
        self.project_session_into_catalog()

    def persist_transcript_turn(self, *, user_text: str) -> None:
        """Append one bounded visual turn and cumulative usage to the projection."""
        app = self._app
        if not user_text:
            return
        from synapse.sessions.transcript import UiTranscriptEvent

        events = [UiTranscriptEvent(kind="user", text=user_text)]
        state = app._transcript.state
        thought_text = ""
        if state.thought_blocks:
            thought_text = str(
                getattr(state.thought_blocks[-1], "body", "") or ""
            ).strip()
        if thought_text:
            events.append(UiTranscriptEvent(kind="thought", text=thought_text))
        if state.last_tool_items:
            calls: list[dict[str, Any]] = []
            results: list[dict[str, Any]] = []
            for index, item in enumerate(state.last_tool_items):
                item_id = str(item.id or f"tool-{index}")
                calls.append(
                    {
                        "id": item_id,
                        "name": item.name or "tool",
                        "args": {"label": item.label, "path": item.path},
                    }
                )
                results.append(
                    {
                        "id": item_id,
                        "name": item.name or "tool",
                        "content": item.preview or "",
                        "status": "error" if item.error else "ok",
                    }
                )
            events.append(
                UiTranscriptEvent(
                    kind="tools",
                    tool_calls=calls,
                    tool_results=results,
                )
            )
        if state.last_answer_text:
            events.append(UiTranscriptEvent(kind="answer", text=state.last_answer_text))
        usage = TranscriptUsage(
            input_tokens=int(app._input_tokens or 0),
            output_tokens=int(app._output_tokens or 0),
            cache_tokens=int(app._cache_tokens or 0),
            last_input_tokens=int(app._context_tokens or 0),
            last_output_tokens=int(app._last_out_tokens or 0),
        )
        try:
            app._transcript_projection.append_turn(
                app.thread_id,
                events,
                usage=usage,
            )
        except Exception:  # noqa: BLE001 - checkpoint remains source of truth
            pass

    def persist_turn_summary(self, *, user_text: str) -> None:
        """Deterministic local digest per completed turn (no model call)."""
        app = self._app
        mode = getattr(app.settings, "session_summary_mode", "local")
        if mode == "off" or not user_text or app._busy:
            return
        try:
            if app._summary_store is None:
                from synapse.sessions.store import SessionStore

                app._summary_store = SessionStore(
                    app.settings.resolved_sessions_path()
                )
            from synapse.sessions.summary import persist_local_summary

            persist_local_summary(
                app._summary_store,
                app.thread_id,
                user_text=user_text,
                tool_summary=app._transcript.state.last_tool_summary or "",
                answer_text=app._transcript.state.last_answer_text or "",
                max_chars=int(
                    getattr(app.settings, "session_summary_max_chars", 600) or 600
                ),
            )
        except Exception:  # noqa: BLE001 - summaries are best-effort
            pass

    def project_session_into_catalog(self) -> None:
        """Mirror the current session row into the global catalog."""
        app = self._app
        if not bool(getattr(app.settings, "project_catalog_enabled", True)):
            return
        if app._project_catalog is None:
            return
        try:
            if app._summary_store is None:
                from synapse.sessions.store import SessionStore

                app._summary_store = SessionStore(
                    app.settings.resolved_sessions_path()
                )
            info = app._summary_store.get(app.thread_id)
            if info is None:
                return
            app._project_catalog.upsert_session(
                app.settings.workspace,
                thread_id=info.thread_id,
                title=info.title,
                model=info.model or info.active_model,
                summary=info.summary,
                updated_at=info.updated_at,
                created_at=info.created_at,
                tags=info.tags,
            )
        except Exception:  # noqa: BLE001 - projection is best-effort
            pass

    def prompt_has_draft(self) -> bool:
        try:
            prompt = self._app.query_one("#prompt", Input)
            return bool((prompt.value or "").strip())
        except Exception:  # noqa: BLE001
            return False

    def maybe_show_session_recap(self) -> None:
        """After idle, mount one recap line (no slash command)."""
        app = self._app
        if app._busy:
            return
        try:
            line = app._session_recap.try_fire(
                time.monotonic(),
                busy=app._busy,
                draft_nonempty=self.prompt_has_draft(),
            )
        except Exception:  # noqa: BLE001
            return
        if not line:
            return
        app.append_event(line, "dim")

    # -- follow-up steer ------------------------------------------------------

    def schedule_followup_steer(self, queue: SteerQueue | None) -> bool:
        app = self._app
        if queue is None or queue.peek_count() <= 0:
            return False
        scheduled_cancel_event = app._cancel_event
        app._busy = True
        app._sync_prompt_placeholder()
        if app.call_after_refresh(
            self.start_followup_steer, queue, scheduled_cancel_event
        ):
            return True
        app._busy = False
        app._sync_prompt_placeholder()
        return False

    def start_followup_steer(
        self,
        queue: SteerQueue,
        scheduled_cancel_event: threading.Event | None = None,
    ) -> None:
        app = self._app
        cancel_event = scheduled_cancel_event or app._cancel_event
        if cancel_event.is_set():
            app._skip_steer_followup = True
            app._turn_done()
            return
        if queue.peek_count() <= 0:
            app._busy = False
            app._sync_prompt_placeholder()
            self.clear_turn_context()
            app._bind_steer_queue()
            app.set_activity("idle", "ready", True)
            return
        self.maybe_followup_steer(queue)

    def maybe_followup_steer(self, queue: SteerQueue | None = None) -> None:
        app = self._app
        q = queue or get_agent_steer_queue(app.agent)
        if q is None or q.peek_count() <= 0:
            return
        items = q.drain()
        content = format_steer_message(items)
        if not content:
            return
        # Silent follow-up: model gets content; no transcript/status steer copy.
        if app._active_turn_agent is None:
            self.capture_turn_context()
        app._active_steer_queue = q
        app._busy = True
        app._skip_steer_followup = False
        app._cancel_event = threading.Event()
        app.clear_stream()
        app.set_activity("thinking", "", True)
        app._sync_prompt_placeholder()
        app.run_turn(content, None)
