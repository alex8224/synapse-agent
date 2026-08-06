"""Unit tests for the TUI turn request builder (payload/config construction)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from synapse.ui.turn.request import TurnRequest, build_turn_request


def test_build_turn_request_constructs_payload_and_config() -> None:
    settings = SimpleNamespace(
        max_concurrency=3,
    )
    with (
        patch(
            "synapse.ui.turn.request.compose_user_content",
            return_value="composed-content",
        ) as compose,
        patch(
            "synapse.ui.turn.request.provider_from_settings",
            return_value="provider-x",
        ) as provider,
    ):
        req = build_turn_request(
            text="hello",
            attachments=None,
            settings=settings,
            thread_id="t1",
            monitor_id="m1",
        )

    assert isinstance(req, TurnRequest)
    assert req.thread_id == "t1"
    assert req.payload == {"messages": [{"role": "user", "content": "composed-content"}]}
    assert req.config == {
        "configurable": {"thread_id": "t1", "subagent_monitor_id": "m1"},
        "max_concurrency": 3,
    }
    provider.assert_called_once_with(settings)
    compose.assert_called_once_with("hello", attachments=None, provider="provider-x")


def test_build_turn_request_keeps_plain_string_without_attachments() -> None:
    with (
        patch(
            "synapse.ui.turn.request.compose_user_content",
            return_value={"type": "text", "text": "plain"},
        ),
        patch("synapse.ui.turn.request.provider_from_settings", return_value=None),
    ):
        req = build_turn_request(
            text="plain",
            attachments=None,
            settings=SimpleNamespace(max_concurrency=2),
            thread_id="t",
            monitor_id="m",
        )

    assert req.payload["messages"][0]["content"] == {"type": "text", "text": "plain"}


def test_build_turn_request_passes_attachments_through() -> None:
    attachments = [SimpleNamespace(id=1)]
    with (
        patch(
            "synapse.ui.turn.request.compose_user_content",
            return_value="with-attachments",
        ) as compose,
        patch("synapse.ui.turn.request.provider_from_settings", return_value="p"),
    ):
        req = build_turn_request(
            text="t",
            attachments=attachments,
            settings=SimpleNamespace(max_concurrency=1),
            thread_id="t",
            monitor_id="m",
        )

    compose.assert_called_once_with("t", attachments=attachments, provider="p")
    assert req.payload["messages"][0]["content"] == "with-attachments"
