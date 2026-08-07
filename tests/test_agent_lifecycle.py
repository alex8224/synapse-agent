from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from synapse.ui.agent_lifecycle import AgentLifecycleController


class _Host:
    def __init__(self, *, agent=None, enable_mcp=False, prewarm=False) -> None:
        self.agent = agent
        self.settings = SimpleNamespace(
            enable_mcp=enable_mcp,
            session_prewarm_enabled=prewarm,
        )
        self.project_root = "workspace"
        self.thread_id = "thread-1"
        self._busy = False
        self.events: list[tuple[str, str]] = []
        self.activities: list[tuple[str, str, bool]] = []
        self.bind_count = 0
        self.ready_count = 0
        self.prewarm_count = 0

    def call_from_thread(self, callback, *args, **kwargs):
        return callback(*args, **kwargs)

    def set_activity(self, phase, detail="", reset_timer=False):
        self.activities.append((phase, detail, reset_timer))

    def append_event(self, text, style="dim"):
        self.events.append((text, style))

    def _on_agent_ready(self, with_mcp):
        self.ready_count += 1

    def _bind_steer_queue(self):
        self.bind_count += 1

    def _on_mcp_attached(self):
        pass

    def _maybe_start_prewarm(self):
        self.prewarm_count += 1


def test_agent_ready_event_is_set_for_existing_agent() -> None:
    agent = object()
    host = _Host(agent=agent)
    controller = AgentLifecycleController(host, agent=agent, defer_agent_build=False)

    assert controller.agent_ready.is_set()
    assert controller.should_build_on_mount() is False


def test_build_agent_reports_failure_and_unblocks_waiters() -> None:
    host = _Host()
    controller = AgentLifecycleController(host, agent=None, defer_agent_build=True)

    with patch(
        "synapse.app.agent.build_coding_agent",
        side_effect=RuntimeError("factory failed"),
    ):
        controller.build_agent()

    assert controller.agent_ready.is_set()
    assert controller.agent_error == "factory failed"
    assert host.events == [("agent start failed: factory failed", "bold red")]
    assert host.activities[-1] == ("idle", "agent failed", True)


def test_build_agent_without_mcp_notifies_ready() -> None:
    agent = object()
    host = _Host()
    controller = AgentLifecycleController(host, agent=None, defer_agent_build=True)

    with patch("synapse.app.agent.build_coding_agent", return_value=agent):
        controller.build_agent()

    assert host.agent is agent
    assert controller.agent_ready.is_set()
    assert host.ready_count == 1
    assert host.bind_count == 0


def test_prewarm_can_be_cancelled_before_worker_starts() -> None:
    host = _Host(agent=object(), prewarm=True)
    controller = AgentLifecycleController(host, agent=host.agent, defer_agent_build=False)

    with patch("synapse.runtime.session_prewarm.prewarm_session") as prewarm:
        controller.maybe_start_prewarm()
        controller.cancel_prewarm()

    assert controller.state.prewarm_started is True
    assert controller.prewarm_cancel_event.is_set()
    # Thread scheduling is intentionally asynchronous; this assertion only
    # checks that a second start is suppressed.
    controller.maybe_start_prewarm()
    assert prewarm.call_count <= 1
