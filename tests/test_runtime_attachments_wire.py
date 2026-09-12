"""Runtime attachment wiring: wire roundtrip, ACL scoping, and durable history.

Every test is temp-only: it uses a throwaway workspace, needs no network, no
database server, and no background thread.  The store itself is exercised through
the real ``LocalAgentRuntimeService`` so the wiring (ports -> ACL -> store ->
wire) is covered end to end, while the persistence test writes and re-reads the
real transcript projection.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest

from synapse.runtime.agent_loop import TurnContext, TurnRequest, TurnResult, TurnStatus
from synapse.runtime.service import (
    ATTACHMENTS_READ,
    ATTACHMENTS_WRITE,
    AbortAttachmentCommand,
    AclAuthorizer,
    AclGrant,
    AppendAttachmentChunkCommand,
    AttachmentChunk,
    AttachmentMetadata,
    AttachmentRef,
    BeginAttachmentCommand,
    BeginAttachmentResult,
    FinishAttachmentCommand,
    InvalidRequestError,
    LocalAgentRuntimeService,
    PermissionDeniedError,
    Principal,
    ReadAttachmentQuery,
    ReadSessionHistoryQuery,
    StatAttachmentQuery,
    SubmitTurnCommand,
    bind_access,
)
from synapse.runtime.service.attachments import (
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS_PER_SESSION,
    MAX_CHUNK_BASE64_CHARS,
    MAX_CHUNK_BYTES,
    AttachmentNotFoundError,
)
from synapse.runtime.service.history_store import (
    _attachment_tuple,
    read_session_history_page,
)
from synapse.runtime.sessions import RuntimeManager
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.transport import client as wire_client
from synapse.runtime.transport import protocol
from synapse.sessions.transcript_projection import TranscriptProjection

REF = SessionRef(project_id="project-a", thread_id="thread-a")
PRINCIPAL = Principal("subject-a")

# A valid 1x1 PNG generated once with Pillow and embedded as a literal, so the
# suite stays dependency-free while still exercising the optional decode check.
PNG_RED = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)
ATTACHMENT_ID = "0123456789abcdef0123456789abcdef"


def _authorizer(*capabilities: str) -> AclAuthorizer:
    return AclAuthorizer(
        [AclGrant(PRINCIPAL.subject, REF.project_id, frozenset(capabilities), None)]
    )


def _workspace_settings(tmp_path: Any) -> SimpleNamespace:
    return SimpleNamespace(
        workspace=tmp_path,
        sessions_path=tmp_path / "sessions.sqlite",
        checkpoint_path=None,
        deny_fs_paths=[],
        max_concurrency=2,
        model="test",
    )


def _service(tmp_path: Any) -> LocalAgentRuntimeService:
    manager = RuntimeManager(
        settings=_workspace_settings(tmp_path),
        agent_factory=lambda thread_id, shared: SimpleNamespace(thread_id=thread_id),
        project_id=REF.project_id,
    )
    return LocalAgentRuntimeService(
        lambda project: manager if project == REF.project_id else None
    )


# --- wire: request decoders -------------------------------------------------


def test_attachment_request_decoders_round_trip_and_bound() -> None:
    session = {"project_id": REF.project_id, "thread_id": REF.thread_id}
    ref = {"session": session, "attachment_id": ATTACHMENT_ID}

    begin = protocol.decode_params(
        "runtime.attachments.begin",
        {"session": session, "size": 68, "mime": "image/png", "display_name": "shot.png"},
    )
    assert isinstance(begin, BeginAttachmentCommand)
    assert (begin.size, begin.mime, begin.display_name) == (68, "image/png", "shot.png")

    append = protocol.decode_params(
        "runtime.attachments.append",
        {"ref": ref, "expected_offset": 0, "data_base64": "AA=="},
    )
    assert isinstance(append, AppendAttachmentChunkCommand)
    assert append.expected_offset == 0

    finish = protocol.decode_params(
        "runtime.attachments.finish",
        {"ref": ref, "expected_size": 68, "expected_mime": "image/png"},
    )
    assert isinstance(finish, FinishAttachmentCommand)

    abort = protocol.decode_params("runtime.attachments.abort", {"ref": ref})
    assert isinstance(abort, AbortAttachmentCommand)

    stat = protocol.decode_params("runtime.attachments.stat", {"ref": ref})
    assert isinstance(stat, StatAttachmentQuery)
    assert stat.ref == AttachmentRef(REF, ATTACHMENT_ID)

    read = protocol.decode_params("runtime.attachments.read", {"ref": ref})
    assert isinstance(read, ReadAttachmentQuery)
    assert (read.offset, read.limit) == (0, 64 * 1024)

    for method, params in (
        ("runtime.attachments.begin", {"session": session, "size": 0, "mime": "image/png"}),
        (
            "runtime.attachments.begin",
            {"session": session, "size": MAX_ATTACHMENT_BYTES + 1, "mime": "image/png"},
        ),
        ("runtime.attachments.begin", {"session": session, "size": 1, "mime": "text/plain"}),
        (
            "runtime.attachments.append",
            {"ref": ref, "expected_offset": -1, "data_base64": "AA=="},
        ),
        (
            "runtime.attachments.append",
            {
                "ref": ref,
                "expected_offset": 0,
                "data_base64": "A" * (MAX_CHUNK_BASE64_CHARS + 1),
            },
        ),
        ("runtime.attachments.stat", {"ref": {"session": session, "attachment_id": "nope"}}),
        ("runtime.attachments.stat", {"ref": {"session": session}}),
        (
            "runtime.attachments.read",
            {"ref": ref, "limit": 0},
        ),
        ("runtime.attachments.abort", {"ref": ref, "extra": 1}),
    ):
        with pytest.raises(protocol.ProtocolError) as caught:
            protocol.decode_params(method, params)
        assert caught.value.service_code == "invalid_params"


def test_submit_attachment_refs_wire_rules() -> None:
    session = {"project_id": REF.project_id, "thread_id": REF.thread_id}
    decoded = protocol.decode_params(
        "runtime.turn.submit",
        {"session": session, "text": "", "attachment_refs": [ATTACHMENT_ID]},
    )
    assert isinstance(decoded, SubmitTurnCommand)
    assert decoded.attachment_refs == (ATTACHMENT_ID,)
    assert decoded.text == ""

    # At least one of text / attachment_refs is required.
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_params("runtime.turn.submit", {"session": session, "text": ""})
    # The in-process attachment objects stay unsupported on the wire.
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_params(
            "runtime.turn.submit",
            {"session": session, "text": "hi", "attachments": [{"path": "a.png"}]},
        )
    # A bounded, opaque id list.
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_params(
            "runtime.turn.submit",
            {"session": session, "text": "hi", "attachment_refs": ["not-hex"]},
        )
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_params(
            "runtime.turn.submit",
            {
                "session": session,
                "text": "hi",
                "attachment_refs": [ATTACHMENT_ID] * (MAX_ATTACHMENTS_PER_SESSION + 1),
            },
        )


def test_attachment_results_project_to_the_client_field_sets() -> None:
    ref = AttachmentRef(REF, ATTACHMENT_ID)
    metadata = AttachmentMetadata(
        ref=ref,
        size=68,
        mime="image/png",
        revision=hashlib.sha256(PNG_RED).hexdigest(),
        display_name="shot.png",
        created_at="2026-01-01T00:00:00+00:00",
        finalized=True,
    )
    assert set(protocol.project_result(metadata)) == wire_client._ATTACHMENT_METADATA_FIELDS
    assert wire_client._attachment_metadata(protocol.project_result(metadata)) == metadata

    chunk = AttachmentChunk(
        ref=ref,
        offset=0,
        data_base64=base64.b64encode(PNG_RED).decode("ascii"),
        byte_length=len(PNG_RED),
        next_offset=len(PNG_RED),
        eof=True,
        metadata=metadata,
    )
    projected = protocol.project_result(chunk)
    assert set(projected) == wire_client._ATTACHMENT_CHUNK_FIELDS
    assert wire_client._attachment_ref(projected["ref"]) == ref
    assert wire_client._attachment_metadata(projected["metadata"]) == metadata

    begin = BeginAttachmentResult(
        ref=ref,
        chunk_bytes=MAX_CHUNK_BYTES,
        chunk_base64_chars=4 * ((MAX_CHUNK_BYTES + 2) // 3),
        expires_at="2026-01-01T00:00:00+00:00",
        next_offset=0,
    )
    assert set(protocol.project_result(begin)) == wire_client._BEGIN_ATTACHMENT_RESULT_FIELDS
    assert wire_client._attachment_ref(protocol.project_result(begin)["ref"]) == ref


# --- service + ACL + store --------------------------------------------------


def test_attachment_upload_read_round_trip_and_acl_scope(tmp_path: Any) -> None:
    async def run() -> None:
        service = _service(tmp_path)
        writer = bind_access(service, PRINCIPAL, _authorizer(ATTACHMENTS_WRITE))
        reader = bind_access(service, PRINCIPAL, _authorizer(ATTACHMENTS_READ))

        begin = await writer.begin_attachment(
            BeginAttachmentCommand(REF, len(PNG_RED), "image/png", "shot.png")
        )
        assert begin.chunk_bytes == MAX_CHUNK_BYTES
        ref = begin.ref

        offset = 0
        for start in range(0, len(PNG_RED), 16):
            payload = base64.b64encode(PNG_RED[start : start + 16]).decode("ascii")
            appended = await writer.append_attachment_chunk(
                AppendAttachmentChunkCommand(ref, offset, payload)
            )
            offset = appended.next_offset
        finished = await writer.finish_attachment(
            FinishAttachmentCommand(ref, len(PNG_RED), "image/png")
        )
        assert finished.revision == hashlib.sha256(PNG_RED).hexdigest()

        meta = await reader.stat_attachment(StatAttachmentQuery(ref))
        assert meta.finalized is True
        assert (meta.mime, meta.size, meta.display_name) == ("image/png", len(PNG_RED), "shot.png")
        chunk = await reader.read_attachment(ReadAttachmentQuery(ref, 0, 64 * 1024))
        assert base64.b64decode(chunk.data_base64) == PNG_RED
        assert chunk.eof is True

        # Read and write are separate capabilities: a write-only grant cannot
        # read, and a read-only grant cannot abort.
        with pytest.raises(PermissionDeniedError):
            await writer.stat_attachment(StatAttachmentQuery(ref))
        with pytest.raises(PermissionDeniedError):
            await reader.abort_attachment(AbortAttachmentCommand(ref))

        # A second, freshly constructed service over the same workspace reads the
        # same ref with no in-process cache: the durable store is the source.
        restarted = bind_access(_service(tmp_path), PRINCIPAL, _authorizer(ATTACHMENTS_READ))
        again = await restarted.read_attachment(ReadAttachmentQuery(ref, 0, 64 * 1024))
        assert base64.b64decode(again.data_base64) == PNG_RED

    asyncio.run(run())


def test_submit_rejects_mixed_or_empty_sources(tmp_path: Any) -> None:
    service = _service(tmp_path)

    async def run() -> None:
        with pytest.raises(InvalidRequestError):
            await service.submit_turn(
                SubmitTurnCommand(
                    REF,
                    "hi",
                    attachments=(object(),),
                    attachment_refs=(ATTACHMENT_ID,),
                )
            )
        with pytest.raises(InvalidRequestError):
            await service.submit_turn(SubmitTurnCommand(REF, ""))

    asyncio.run(run())


def test_submit_resolves_attachment_refs_into_durable_images(tmp_path: Any) -> None:
    async def run() -> None:
        manager = RuntimeManager(
            settings=_workspace_settings(tmp_path),
            agent_factory=lambda thread_id, shared: SimpleNamespace(thread_id=thread_id),
            project_id=REF.project_id,
        )
        service = LocalAgentRuntimeService(
            lambda project: manager if project == REF.project_id else None
        )
        writer = bind_access(service, PRINCIPAL, _authorizer(ATTACHMENTS_WRITE))
        begin = await writer.begin_attachment(
            BeginAttachmentCommand(REF, len(PNG_RED), "image/png", "shot.png")
        )
        ref = begin.ref
        await writer.append_attachment_chunk(
            AppendAttachmentChunkCommand(ref, 0, base64.b64encode(PNG_RED).decode("ascii"))
        )
        await writer.finish_attachment(
            FinishAttachmentCommand(ref, len(PNG_RED), "image/png")
        )

        captured: dict[str, Any] = {}

        async def fake_submit_ref(session: SessionRef, turn: Any, **kwargs: Any) -> Any:
            captured["session"] = session
            captured["turn"] = turn
            return SimpleNamespace(turn_id="turn-1")

        manager.submit_ref = fake_submit_ref  # type: ignore[method-assign]
        submitter = bind_access(service, PRINCIPAL, _authorizer("turn.submit"))
        receipt = await submitter.submit_turn(
            SubmitTurnCommand(REF, "look [image#1]", attachment_refs=(ref.attachment_id,))
        )
        assert receipt.turn_id == "turn-1"
        turn = captured["turn"]
        image = turn.attachments[0]
        # The durable id is carried on the resolved image and in the metadata the
        # persistence layer records; the placeholder id is renumbered 1..N.
        assert image.durable_id == ref.attachment_id
        assert image.id == 1
        assert turn.attachment_refs[0] == {
            "image_id": 1,
            "attachment_id": ref.attachment_id,
            "name": "shot.png",
            "mime": "image/png",
            "size": len(PNG_RED),
        }

    asyncio.run(run())


def test_unknown_attachment_ref_is_a_typed_error(tmp_path: Any) -> None:
    async def run() -> None:
        reader = bind_access(_service(tmp_path), PRINCIPAL, _authorizer(ATTACHMENTS_READ))
        with pytest.raises(AttachmentNotFoundError) as caught:
            await reader.stat_attachment(
                StatAttachmentQuery(AttachmentRef(REF, "f" * 32))
            )
        # The typed attachment error never leaks a workspace path.
        assert tmp_path.as_posix() not in str(caught.value)

    asyncio.run(run())


# --- durable history persistence -------------------------------------------


def test_history_projection_persists_durable_attachment_refs(tmp_path: Any) -> None:
    settings = _workspace_settings(tmp_path)
    projection = TranscriptProjection(tmp_path / "transcript.sqlite")
    try:
        from synapse.runtime.sessions.persistence import SessionPersistence

        persistence = SessionPersistence(
            transcript_projection=projection,
            summary_store=None,
            summary_mode="off",
        )
        metadata = {
            "image_id": 1,
            "attachment_id": ATTACHMENT_ID,
            "name": "shot.png",
            "mime": "image/png",
            "size": len(PNG_RED),
        }
        request = TurnRequest(
            payload={"messages": []},
            config={},
            thread_id=REF.thread_id,
            input="look [image#1]",
            attachment_refs=(metadata,),
        )
        context = TurnContext(
            thread_id=REF.thread_id, agent=object(), settings=settings, request=request
        )
        result = TurnResult(
            turn_id="turn-1", thread_id=REF.thread_id, status=TurnStatus.COMPLETED
        )
        persistence.persist(context, result)

        page = read_session_history_page(
            settings, ReadSessionHistoryQuery(session=REF)
        )
        assert page.available is True
        assert page.total_turns == 1
        user = page.events[0]
        assert user.kind == "user"
        assert user.text == "look [image#1]"
        assert len(user.attachments) == 1
        attachment = user.attachments[0]
        assert attachment.attachment_id == ATTACHMENT_ID
        assert attachment.image_id == 1
        assert (attachment.name, attachment.mime, attachment.size) == (
            "shot.png",
            "image/png",
            len(PNG_RED),
        )
        # No base64 image bytes leak into the projection.
        assert base64.b64encode(PNG_RED) not in (tmp_path / "transcript.sqlite").read_bytes()
    finally:
        projection.close()


def test_legacy_history_rows_without_attachments_default_to_empty(tmp_path: Any) -> None:
    settings = _workspace_settings(tmp_path)
    projection = TranscriptProjection(tmp_path / "transcript.sqlite")
    try:
        with sqlite3.connect(str(projection.path)) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO transcript_meta"
                "(thread_id,total_turns,total_events,source_message_count,updated_at) "
                "VALUES (?,?,?,?,CURRENT_TIMESTAMP)",
                (REF.thread_id, 1, 1, 0),
            )
            conn.execute(
                "INSERT INTO transcript_events(thread_id,event_seq,turn_seq,kind,payload_json) "
                "VALUES (?,?,?,?,?)",
                (
                    REF.thread_id,
                    0,
                    1,
                    "user",
                    json.dumps(
                        {"kind": "user", "text": "legacy", "tool_calls": [], "tool_results": []}
                    ),
                ),
            )
        page = read_session_history_page(settings, ReadSessionHistoryQuery(session=REF))
        assert page.events[0].attachments == ()
        assert _attachment_tuple(None) == ()
    finally:
        projection.close()
