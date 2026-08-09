"""Agent construction, MCP attachment, and session prewarm lifecycle.

The controller owns background-agent state that used to be mixed into
``CodingAgentApp``.  Textual scheduling remains on the host app; controller
methods perform the worker body and use the host only for UI callbacks.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentLifecycleState:
    """Mutable state shared by startup, turn, and prewarm workers."""

    defer_agent_build: bool = False
    agent_ready: threading.Event = field(default_factory=threading.Event)
    agent_error: str | None = None
    mcp_attaching: bool = False
    prewarm_cancel_event: threading.Event = field(default_factory=threading.Event)
    prewarm_started: bool = False


class AgentLifecycleController:
    """Build and finalize the live coding agent without blocking Textual."""

    def __init__(self, app: Any, *, agent: Any, defer_agent_build: bool) -> None:
        self._app = app
        self.state = AgentLifecycleState(
            defer_agent_build=bool(defer_agent_build and agent is None)
        )
        if agent is not None:
            self.state.agent_ready.set()

    @property
    def agent_ready(self) -> threading.Event:
        return self.state.agent_ready

    @property
    def agent_error(self) -> str | None:
        return self.state.agent_error

    @property
    def mcp_attaching(self) -> bool:
        return self.state.mcp_attaching

    def set_mcp_attaching(self, value: bool) -> None:
        self.state.mcp_attaching = bool(value)

    @property
    def prewarm_cancel_event(self) -> threading.Event:
        return self.state.prewarm_cancel_event

    def should_build_on_mount(self) -> bool:
        return self.state.defer_agent_build or self._app.agent is None

    def build_agent(self) -> None:
        """Build phase one, then attach MCP in the same worker."""
        from synapse.app.agent import attach_mcp_to_agent, build_coding_agent
        from synapse.observability.startup_trace import duration

        app = self._app
        startup_started = time.perf_counter()

        def report_progress(detail: str) -> None:
            app.call_from_thread(app.set_activity, "starting", detail, False)

        try:
            agent = build_coding_agent(
                app.settings,
                project_root=app.project_root,
                load_mcp=False,
                progress=report_progress,
                prompt_cache_key=lambda: app.thread_id,
            )
            app.agent = agent
            turn = getattr(app, "_turn", None)
            if turn is not None:
                turn.bind_agent(app.thread_id, agent)
            self.state.agent_ready.set()
            duration("agent.ready", startup_started, phase="startup")
            app.call_from_thread(app._on_agent_ready, False)
        except Exception as exc:  # noqa: BLE001
            self.state.agent_error = str(exc)
            self.state.agent_ready.set()
            app.call_from_thread(
                app.append_event,
                f"agent start failed: {exc}",
                "bold red",
            )
            app.call_from_thread(app.set_activity, "idle", "agent failed", True)
            return

        if not bool(getattr(app.settings, "enable_mcp", True)):
            return
        if getattr(agent, "_coding_mcp_attached", False):
            return
        mcp_started = time.perf_counter()
        try:
            self.state.mcp_attaching = True
            app.call_from_thread(
                app.set_activity, "starting", "connecting MCP…", False
            )
            agent2 = attach_mcp_to_agent(
                app.settings,
                agent,
                project_root=app.project_root,
            )
            if app.agent is not agent:
                # A model switch replaced phase one while MCP was connecting.
                # Rebuild the current graph with the now-live pool; this path
                # reuses tools and performs no second network connection.
                current = app.agent
                if current is None:
                    return
                current_with_mcp = attach_mcp_to_agent(
                    app.settings,
                    current,
                    project_root=app.project_root,
                )
                if app.agent is current:
                    app.agent = current_with_mcp
                    turn = getattr(app, "_turn", None)
                    if turn is not None:
                        turn.bind_agent(app.thread_id, current_with_mcp)
                    app.call_from_thread(app._bind_steer_queue)
                    app.call_from_thread(app._on_mcp_attached)
                return
            app.agent = agent2
            turn = getattr(app, "_turn", None)
            if turn is not None:
                turn.bind_agent(app.thread_id, agent2)
            app.call_from_thread(app._bind_steer_queue)
            if not app._busy:
                app.call_from_thread(app._on_mcp_attached)
            else:
                app.call_from_thread(
                    app.append_event,
                    "MCP tools attached (will apply next turn)",
                    "dim",
                )
        except Exception as exc:  # noqa: BLE001
            app.call_from_thread(
                app.append_event,
                f"MCP attach failed (agent still usable): {exc}",
                "yellow",
            )
        finally:
            duration("mcp.attach", mcp_started, phase="startup")
            self.state.mcp_attaching = False
            if not app._busy:
                app.call_from_thread(app.set_activity, "idle", "ready", True)
                # MCP attach failed or was skipped: the phase-one agent is the
                # final shape, so prewarm it (no-op if already started).
                app.call_from_thread(app._maybe_start_prewarm)

    def on_agent_ready(self, with_mcp: bool) -> None:
        """Apply the UI-side initialization after phase-one construction."""
        app = self._app
        label = "agent ready" + (" + MCP" if with_mcp else " (MCP pending)")
        app.append_event(label, "dim")
        app.set_activity("idle", "ready", True)
        app._bind_steer_queue()
        app._bind_goal_listener()
        app._load_current_goal()
        app._restore_session_transcript(announce=True)
        # Do not prewarm before MCP has finalized the tool schema.
        if not bool(getattr(app.settings, "enable_mcp", True)):
            self.maybe_start_prewarm()

    def maybe_start_prewarm(self) -> None:
        """Start one optional provider prewarm after the agent is final."""
        app = self._app
        if not bool(getattr(app.settings, "session_prewarm_enabled", False)):
            return
        if self.state.prewarm_started or app.agent is None or not app.thread_id:
            return
        if self.state.mcp_attaching:
            return
        self.state.prewarm_started = True
        cancel_event = threading.Event()
        self.state.prewarm_cancel_event = cancel_event
        agent = app.agent
        thread_id = app.thread_id

        def _worker() -> None:
            from synapse.runtime.session_prewarm import prewarm_session

            try:
                prewarm_session(
                    agent,
                    thread_id,
                    cancel_event=cancel_event,
                    notify=lambda text: app.call_from_thread(
                        app.append_event, text, "dim"
                    ),
                )
            except Exception:  # noqa: BLE001 - prewarm must never break the session
                pass

        threading.Thread(target=_worker, name="session-prewarm", daemon=True).start()

    def cancel_prewarm(self) -> None:
        """Stop background prewarm before a real user turn starts."""
        self.state.prewarm_cancel_event.set()

    def on_mcp_attached(self) -> None:
        """Publish MCP diagnostics and start prewarm on the finalized agent."""
        from synapse.app.agent import build_coding_agent

        app = self._app
        servers = list(getattr(build_coding_agent, "last_mcp_servers", []) or [])
        tools = list(getattr(build_coding_agent, "last_mcp_tool_names", []) or [])
        warnings = list(getattr(build_coding_agent, "last_mcp_warnings", []) or [])
        app.append_event(
            f"MCP ready: servers={servers or '-'} tools={len(tools)}",
            "dim",
        )
        for warning in warnings:
            app.append_event(f"mcp: {warning}", "yellow")
        app.set_activity("idle", "ready", True)
        self.maybe_start_prewarm()
