"""P2 ACP event bridge tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from synapse.acp.events import ACPEventBridge


@dataclass(frozen=True)
class _Kind:
    value: str


@dataclass(frozen=True)
class _Payload:
    text: str


@dataclass(frozen=True)
class _Event:
    kind: _Kind
    payload: _Payload


@dataclass(frozen=True)
class _Envelope:
    event: _Event


def test_bridge_coalesces_adjacent_preview_events_in_order() -> None:
    async def run() -> None:
        bridge = ACPEventBridge(asyncio.get_running_loop(), max_preview_events=8)
        received: list[_Envelope] = []
        async def consume(envelope: _Envelope) -> None:
            received.append(envelope)

        bridge.start(consume)
        bridge.publish(_Envelope(_Event(_Kind("answer_delta"), _Payload("a"))))
        bridge.publish(_Envelope(_Event(_Kind("answer_delta"), _Payload("b"))))
        await bridge.wait_drained()
        await asyncio.sleep(0)
        await bridge.close()
        assert [item.event.payload.text for item in received] == ["ab"]
    asyncio.run(run())


def test_bridge_bounds_preview_queue_but_preserves_terminal_event() -> None:
    async def run() -> None:
        bridge = ACPEventBridge(asyncio.get_running_loop(), max_preview_events=1)
        received: list[_Envelope] = []

        async def consume(envelope: _Envelope) -> None:
            await asyncio.sleep(0.01)
            received.append(envelope)

        bridge.start(consume)
        for text in ("a", "b", "c", "d"):
            bridge.publish(_Envelope(_Event(_Kind("answer_delta"), _Payload(text))))
        terminal = _Envelope(_Event(_Kind("turn_completed"), _Payload("done")))
        bridge.publish(terminal, terminal=True)
        await bridge.close()
        assert received[-1] == terminal
        assert bridge.stats.dropped_preview_events > 0

    asyncio.run(run())
