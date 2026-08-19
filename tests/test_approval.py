"""ApprovalBlock interactive widget tests."""

from __future__ import annotations

import asyncio

from textual.app import App
from textual.widgets import Button

from synapse.runtime.hitl import PendingAction, PendingInterrupt
from synapse.ui.approval import ApprovalBlock


def _pending(
    *actions: PendingAction,
    allowed: tuple[str, ...] | None = None,
) -> PendingInterrupt:
    if not actions:
        actions = (
            PendingAction(
                name="execute",
                args={"command": "rm -rf /tmp/x"},
                description="run a command",
                allowed_decisions=list(allowed) if allowed else ["approve", "reject"],
            ),
        )
    return PendingInterrupt(actions=list(actions))


def _accept(decisions: list[tuple[str, str | None]]):
    def decide(action: str, message: str | None) -> bool:
        decisions.append((action, message))
        return True

    return decide


def _run(
    decisions: list[tuple[str, str | None]],
    press_id: str,
    *,
    on_decide=None,
    pending: PendingInterrupt | None = None,
) -> None:
    decide = on_decide or _accept(decisions)

    class HostApp(App[None]):
        def compose(self):
            yield ApprovalBlock(pending or _pending(), on_decide=decide)

    async def exercise() -> None:
        app = HostApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            button = app.query_one(press_id, Button)
            assert button.disabled is False
            button.press()
            await pilot.pause()
            # Buttons disable once a decision is accepted.
            assert app.query_one("#approval-approve", Button).disabled is True
            assert app.query_one("#approval-reject", Button).disabled is True

    asyncio.run(exercise())


def test_approve_button_dispatches_decision() -> None:
    decisions: list[tuple[str, str | None]] = []
    _run(decisions, "#approval-approve")
    assert decisions == [("approve", None)]


def test_reject_button_dispatches_decision() -> None:
    decisions: list[tuple[str, str | None]] = []
    _run(decisions, "#approval-reject")
    assert decisions == [("reject", None)]


def test_second_click_is_ignored() -> None:
    decisions: list[tuple[str, str | None]] = []

    class HostApp(App[None]):
        def compose(self):
            yield ApprovalBlock(_pending(), on_decide=_accept(decisions))

    async def exercise() -> None:
        app = HostApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.query_one("#approval-approve", Button).press()
            await pilot.pause()
            # Pressing again after resolution must not dispatch a second decision.
            app.query_one("#approval-approve", Button).press()
            await pilot.pause()
            app.query_one("#approval-reject", Button).press()
            await pilot.pause()

    asyncio.run(exercise())
    assert decisions == [("approve", None)]


def test_multiple_actions_render_with_single_decision_row() -> None:
    pending = PendingInterrupt(
        actions=[
            PendingAction(name="execute", args={"command": "ls"}),
            PendingAction(name="write_file", args={"path": "/tmp/a.py"}),
        ]
    )
    decisions: list[tuple[str, str | None]] = []

    class HostApp(App[None]):
        def compose(self):
            yield ApprovalBlock(pending, on_decide=_accept(decisions))

    async def exercise() -> None:
        app = HostApp()
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            app.query_one("#approval-approve", Button).press()
            await pilot.pause()

    asyncio.run(exercise())
    assert decisions == [("approve", None)]


def test_refused_resume_reenables_buttons() -> None:
    """A refused resume (session still settling) must not dead-end the widget."""
    decisions: list[tuple[str, str | None]] = []

    class HostApp(App[None]):
        def compose(self):
            yield ApprovalBlock(
                _pending(),
                on_decide=lambda a, m: (decisions.append((a, m)) or False),
            )

    async def exercise() -> None:
        app = HostApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.query_one("#approval-approve", Button).press()
            await pilot.pause()
            # Refused → buttons come back for a retry.
            assert app.query_one("#approval-approve", Button).disabled is False
            assert app.query_one("#approval-reject", Button).disabled is False
            # Retry succeeds.
            app.query_one("#approval-approve", Button).press()
            await pilot.pause()

    asyncio.run(exercise())
    assert decisions == [("approve", None), ("approve", None)]


def test_allowed_decisions_filter_buttons() -> None:
    """Only decisions listed in allowed_decisions are rendered."""
    pending = _pending(allowed=("approve",))

    class HostApp(App[None]):
        def compose(self):
            yield ApprovalBlock(pending, on_decide=lambda a, m: True)

    async def exercise() -> None:
        app = HostApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            # Reject button is absent; approve is present and functional.
            assert app.query_one("#approval-approve", Button) is not None
            assert len(app.query("#approval-reject")) == 0

    asyncio.run(exercise())
