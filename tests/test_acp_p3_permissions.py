"""P3 ACP permission coordinator tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from synapse.acp.permissions import ACPPermissionError, PermissionCoordinator


@dataclass(frozen=True)
class _Option:
    option_id: str
    kind: str


@dataclass(frozen=True)
class _Outcome:
    outcome: str
    option_id: str = ""


async def _resolve(
    coordinator: PermissionCoordinator,
    *,
    session_id: str = "sess-1",
    prompt_id: str = "prompt-1",
    tool_call_id: str = "call-1",
    outcome: _Outcome = _Outcome("selected", "allow"),
    options: list[_Option] | None = None,
) -> object:
    options = options or [_Option("allow", "allow_once")]

    async def request(_request: object, _options: list[_Option]) -> _Outcome:
        return outcome

    return await coordinator.resolve(
        session_id=session_id,
        prompt_id=prompt_id,
        turn_id="turn-1",
        tool_call_id=tool_call_id,
        action_name="write_file",
        request=object(),
        options=options,
        request_permission=request,
    )


def test_permission_decodes_selected_option_and_rejects_unknown_option() -> None:
    async def run() -> None:
        coordinator = PermissionCoordinator()
        decision = await _resolve(coordinator)
        assert decision.kind == "allow_once"
        with pytest.raises(ACPPermissionError, match="unknown option"):
            await _resolve(
                coordinator,
                prompt_id="prompt-2",
                outcome=_Outcome("selected", "missing"),
            )

    asyncio.run(run())


def test_permission_timeout_fails_closed_and_cleans_registry() -> None:
    async def run() -> None:
        coordinator = PermissionCoordinator(request_timeout=0.01)

        async def request(_request: object, _options: list[_Option]) -> _Outcome:
            await asyncio.sleep(1)
            return _Outcome("selected", "allow")

        with pytest.raises(ACPPermissionError, match="timed out"):
            await coordinator.resolve(
                session_id="sess-1",
                prompt_id="p-timeout",
                turn_id="t1",
                tool_call_id="c1",
                action_name="write_file",
                request=object(),
                options=[_Option("allow", "allow_once")],
                request_permission=request,
            )
        assert coordinator.pending_count == 0

    asyncio.run(run())


def test_duplicate_permission_id_is_rejected_without_overwriting_pending() -> None:
    async def run() -> None:
        coordinator = PermissionCoordinator()
        started = asyncio.Event()
        release = asyncio.Event()

        async def request(_request: object, _options: list[_Option]) -> _Outcome:
            started.set()
            await release.wait()
            return _Outcome("selected", "allow")

        first = asyncio.create_task(
            coordinator.resolve(
                session_id="sess-1",
                prompt_id="same",
                turn_id="t1",
                tool_call_id="same-call",
                action_name="write_file",
                request=object(),
                options=[_Option("allow", "allow_once")],
                request_permission=request,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        with pytest.raises(ACPPermissionError, match="duplicate"):
            await coordinator.resolve(
                session_id="sess-1",
                prompt_id="same",
                turn_id="t1",
                tool_call_id="same-call",
                action_name="write_file",
                request=object(),
                options=[_Option("allow", "allow_once")],
                request_permission=request,
            )
        release.set()
        assert (await first).kind == "allow_once"

    asyncio.run(run())


def test_allow_always_is_scoped_to_one_session() -> None:
    async def run() -> None:
        coordinator = PermissionCoordinator()
        calls = 0

        async def request(_request: object, _options: list[_Option]) -> _Outcome:
            nonlocal calls
            calls += 1
            return _Outcome("selected", "always")

        options = [_Option("always", "allow_always")]
        first = await coordinator.resolve(
            session_id="sess-1",
            prompt_id="p1",
            turn_id="t1",
            tool_call_id="c1",
            action_name="write_file",
            request=object(),
            options=options,
            request_permission=request,
        )
        second = await coordinator.resolve(
            session_id="sess-1",
            prompt_id="p2",
            turn_id="t2",
            tool_call_id="c2",
            action_name="write_file",
            request=object(),
            options=options,
            request_permission=request,
        )
        third = await coordinator.resolve(
            session_id="sess-2",
            prompt_id="p3",
            turn_id="t3",
            tool_call_id="c3",
            action_name="write_file",
            request=object(),
            options=options,
            request_permission=request,
        )
        assert first.kind == second.kind == third.kind == "allow_always"
        assert calls == 2

    asyncio.run(run())


def test_cancel_session_cancels_pending_permission_and_clears_policy() -> None:
    async def run() -> None:
        coordinator = PermissionCoordinator()
        started = asyncio.Event()
        release = asyncio.Event()

        async def request(_request: object, _options: list[_Option]) -> _Outcome:
            started.set()
            await release.wait()
            return _Outcome("selected", "allow")

        task = asyncio.create_task(
            coordinator.resolve(
                session_id="sess-1",
                prompt_id="p1",
                turn_id="t1",
                tool_call_id="c1",
                action_name="write_file",
                request=object(),
                options=[_Option("allow", "allow_once")],
                request_permission=request,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        assert coordinator.pending_count == 1
        await coordinator.cancel_session("sess-1")
        release.set()
        assert (await asyncio.wait_for(task, timeout=1)).kind == "cancelled"
        assert coordinator.pending_count == 0

    asyncio.run(run())
