"""Durable attachment refs survive a checkpoint-driven transcript rebuild.

The end-to-end chain exercised here is the real one:

``build_turn_request`` -> LangGraph message serialization (``add_messages``) ->
``TranscriptProjection.replace_from_messages`` -> ``load_tail``.

Every test is temp-only: no network, no database server, no native wheel, and no
background thread.  The durable refs stay opaque metadata (ids + display fields,
never bytes), so the assertions also pin the "no base64 in the projection"
invariant.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import HumanMessage
from langgraph.graph.message import add_messages

from synapse.content.multimodal import (
    ATTACHMENT_REFS_KEY,
    MAX_DURABLE_REFS,
    Attachment,
    attachment_refs_metadata,
    extract_attachment_refs,
)
from synapse.models.rust_openai import messages_to_openai_dicts
from synapse.runtime.agent_loop import (
    TurnContext,
    TurnResult,
    TurnStatus,
    build_turn_request,
)
from synapse.runtime.sessions.persistence import SessionPersistence
from synapse.sessions.transcript import fold_messages_for_ui
from synapse.sessions.transcript_projection import TranscriptProjection

THREAD = "thread-a"
ATTACHMENT_ID = "0123456789abcdef0123456789abcdef"
PNG_RED = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"

EXPECTED_REF: dict[str, Any] = {
    "image_id": 1,
    "attachment_id": ATTACHMENT_ID,
    "name": "shot.png",
    "mime": "image/png",
    "size": len(PNG_RED),
}


def _settings() -> SimpleNamespace:
    return SimpleNamespace(max_concurrency=2, model="openai:gpt-4o")


def _durable_image() -> Attachment:
    return Attachment(
        id=1,
        name="shot.png",
        mime="image/png",
        data=PNG_RED,
        source="file",
        durable_id=ATTACHMENT_ID,
    )


def _human_from_request(request: Any) -> HumanMessage:
    """Serialize the frozen request payload through the LangGraph reducer."""
    message = add_messages([], [request.payload["messages"][0]])[0]
    assert isinstance(message, HumanMessage)
    return message


# --- request assembly -------------------------------------------------------


def test_build_turn_request_embeds_durable_refs_on_the_message() -> None:
    request = build_turn_request(
        text="look [image#1]",
        attachments=[_durable_image()],
        settings=_settings(),
        thread_id=THREAD,
    )
    message = request.payload["messages"][0]
    assert message["role"] == "user"
    assert message["additional_kwargs"] == {ATTACHMENT_REFS_KEY: [EXPECTED_REF]}
    # JSON-safe: the exact checkpoint serialization round-trips unchanged.
    assert json.loads(json.dumps(message)) == message
    assert request.attachment_refs == (EXPECTED_REF,)


def test_build_turn_request_without_durable_ids_keeps_a_plain_message() -> None:
    local = Attachment(id=1, name="clip.png", mime="image/png", data=PNG_RED)
    request = build_turn_request(
        text="look [image#1]",
        attachments=[local],
        settings=_settings(),
        thread_id=THREAD,
    )
    message = request.payload["messages"][0]
    assert "additional_kwargs" not in message
    assert request.attachment_refs == ()


def test_provider_serialization_omits_the_durable_ref_metadata() -> None:
    request = build_turn_request(
        text="look [image#1]",
        attachments=[_durable_image()],
        settings=_settings(),
        thread_id=THREAD,
    )
    wire = messages_to_openai_dicts([_human_from_request(request)])[0]
    # The refs are message metadata, not a provider field: the OpenAI-compatible
    # serialization forwards only known keys.
    assert "additional_kwargs" not in wire
    assert ATTACHMENT_REFS_KEY not in wire
    assert wire["role"] == "user"


# --- transcript rebuild -----------------------------------------------------


def test_rebuild_from_checkpoint_message_keeps_durable_refs(tmp_path: Any) -> None:
    request = build_turn_request(
        text="look [image#1]",
        attachments=[_durable_image()],
        settings=_settings(),
        thread_id=THREAD,
    )
    human = _human_from_request(request)

    folded = fold_messages_for_ui([human])
    assert folded[0].attachments == [EXPECTED_REF]
    # The legacy inline-image path is unchanged: bytes are still decoded for the
    # live fold even though the projection never persists them.
    assert len(folded[0].images) == 1

    projection = TranscriptProjection(tmp_path / "transcript.sqlite")
    try:
        projection.replace_from_messages(THREAD, [human])
        page = projection.load_tail(THREAD)
        user = page.events[0]
        assert user.kind == "user"
        assert user.attachments == [EXPECTED_REF]
        assert user.images == []
        assert (tmp_path / "transcript.sqlite").read_bytes().find(PNG_RED) == -1
    finally:
        projection.close()


def test_append_and_rebuild_agree_on_durable_refs(tmp_path: Any) -> None:
    settings = _settings()
    request = build_turn_request(
        text="look [image#1]",
        attachments=[_durable_image()],
        settings=settings,
        thread_id=THREAD,
    )
    projection = TranscriptProjection(tmp_path / "transcript.sqlite")
    try:
        persistence = SessionPersistence(
            transcript_projection=projection,
            summary_store=None,
            summary_mode="off",
            catalog_enabled=False,
        )
        persistence.persist(
            TurnContext(thread_id=THREAD, agent=object(), settings=settings, request=request),
            TurnResult(turn_id="turn-1", thread_id=THREAD, status=TurnStatus.COMPLETED),
        )
        appended = projection.load_tail(THREAD).events[0]
        assert appended.attachments == [EXPECTED_REF]

        # The same thread rebuilt from the checkpoint message must match the
        # incrementally persisted turn.
        projection.replace_from_messages(THREAD, [_human_from_request(request)])
        rebuilt = projection.load_tail(THREAD).events[0]
        assert rebuilt.attachments == appended.attachments
    finally:
        projection.close()


def test_persistence_falls_back_to_request_refs_without_message_metadata(
    tmp_path: Any,
) -> None:
    from synapse.runtime.agent_loop import TurnRequest

    settings = _settings()
    request = TurnRequest(
        payload={"messages": [{"role": "user", "content": "legacy"}]},
        config={},
        thread_id=THREAD,
        input="legacy",
        attachment_refs=(EXPECTED_REF,),
    )
    projection = TranscriptProjection(tmp_path / "transcript.sqlite")
    try:
        SessionPersistence(
            transcript_projection=projection,
            summary_store=None,
            summary_mode="off",
            catalog_enabled=False,
        ).persist(
            TurnContext(thread_id=THREAD, agent=object(), settings=settings, request=request),
            TurnResult(turn_id="turn-1", thread_id=THREAD, status=TurnStatus.COMPLETED),
        )
        assert projection.load_tail(THREAD).events[0].attachments == [EXPECTED_REF]
    finally:
        projection.close()


# --- legacy / hostile metadata ---------------------------------------------


def test_legacy_messages_without_metadata_default_to_no_refs(tmp_path: Any) -> None:
    legacy = HumanMessage(content="hello")
    assert fold_messages_for_ui([legacy])[0].attachments == []

    projection = TranscriptProjection(tmp_path / "transcript.sqlite")
    try:
        projection.replace_from_messages(THREAD, [legacy])
        user = projection.load_tail(THREAD).events[0]
        assert user.text == "hello"
        assert user.attachments == []
    finally:
        projection.close()


def test_malformed_metadata_is_ignored_and_bounded() -> None:
    assert extract_attachment_refs(None) == []
    assert extract_attachment_refs("nope") == []
    assert extract_attachment_refs({ATTACHMENT_REFS_KEY: "nope"}) == []
    assert extract_attachment_refs({ATTACHMENT_REFS_KEY: [1, "x", None, {}]}) == []
    # A missing, non-string, empty or oversized opaque id is dropped.
    assert extract_attachment_refs({ATTACHMENT_REFS_KEY: [{"attachment_id": 5}]}) == []
    assert extract_attachment_refs({ATTACHMENT_REFS_KEY: [{"attachment_id": "  "}]}) == []
    assert extract_attachment_refs({ATTACHMENT_REFS_KEY: [{"attachment_id": "a" * 300}]}) == []

    # A well-formed entry survives with bounded, typed display fields.
    refs = extract_attachment_refs(
        {
            ATTACHMENT_REFS_KEY: [
                {
                    "attachment_id": " id-1 ",
                    "image_id": -1,
                    "size": -5,
                    "name": "n" * 300,
                    "mime": 7,
                }
            ]
        }
    )
    assert refs == [
        {
            "image_id": 0,
            "attachment_id": "id-1",
            "name": "n" * 256,
            "mime": "",
            "size": 0,
        }
    ]

    # The extraction is capped, and non-dict refs never build metadata.
    many = [{"attachment_id": f"id-{i}"} for i in range(100)]
    assert len(extract_attachment_refs({ATTACHMENT_REFS_KEY: many})) == MAX_DURABLE_REFS
    assert attachment_refs_metadata([None, "x", {}]) == {}


def test_hostile_message_metadata_never_reaches_the_transcript(tmp_path: Any) -> None:
    human = HumanMessage(
        content="hi",
        additional_kwargs={
            ATTACHMENT_REFS_KEY: [
                {"attachment_id": 123},  # wrong type
                {"attachment_id": "ok-id", "image_id": True, "size": "big"},
                object(),  # not a mapping
            ],
            "other": object(),  # unrelated metadata must not break replay
        },
    )
    events = fold_messages_for_ui([human])
    assert events[0].attachments == [
        {"image_id": 0, "attachment_id": "ok-id", "name": "", "mime": "", "size": 0}
    ]

    projection = TranscriptProjection(tmp_path / "transcript.sqlite")
    try:
        projection.replace_from_messages(THREAD, [human])
        assert projection.load_tail(THREAD).events[0].attachments == [
            {"image_id": 0, "attachment_id": "ok-id", "name": "", "mime": "", "size": 0}
        ]
    finally:
        projection.close()


@pytest.mark.parametrize("metadata", [None, [], {"attachment_id": "x"}, 5])
def test_non_ref_metadata_is_not_a_durable_ref(metadata: Any) -> None:
    assert extract_attachment_refs(metadata) == []


def test_durable_ref_cap_matches_the_composer_and_service_budget() -> None:
    from synapse.content.multimodal import ImageBank
    from synapse.runtime.service.attachments import MAX_ATTACHMENTS_PER_SUBMIT

    # One turn can never carry more refs than the composer/service allow, so the
    # metadata extraction cap never drops a legitimate reference.
    assert MAX_DURABLE_REFS == ImageBank.max_images == MAX_ATTACHMENTS_PER_SUBMIT
