"""Runtime attachment upload slice: DTO gates, store bounds, and image safety.

Every test is temp-only: no test reads or writes the real workspace and no test
needs a network or a database.  The concurrency tests use short-lived threads
(and one short-lived subprocess) only to prove the store's OS-level locking.
"""

from __future__ import annotations

import base64
import concurrent.futures
import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import synapse.runtime.service.attachment_image as image
import synapse.runtime.service.attachment_store as store
import synapse.runtime.service.attachments as dto
from synapse.content.multimodal import ALLOWED_MIME, Attachment
from synapse.runtime.service.attachment_image import validate_image_payload
from synapse.runtime.service.attachments import (
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS_PER_SUBMIT,
    MAX_CHUNK_BASE64_CHARS,
    MAX_READ_BYTES,
    AttachmentConflictError,
    AttachmentForbiddenError,
    AttachmentNotFoundError,
    AttachmentQuotaError,
    AttachmentRef,
    AttachmentUnavailableError,
    AttachmentUnsafeError,
    BeginAttachmentCommand,
    FinishAttachmentCommand,
    ReadAttachmentQuery,
    StatAttachmentQuery,
)
from synapse.runtime.service.errors import InvalidRequestError
from synapse.runtime.sessions.ref import SessionRef
from synapse.settings.config_paths import SYNAPSE_DIRNAME

REF = SessionRef(project_id="project", thread_id="thread")
OTHER_THREAD = SessionRef(project_id="project", thread_id="other-thread")
OTHER_PROJECT = SessionRef(project_id="other-project", thread_id="thread")

# Valid 1x1 images generated once with Pillow and embedded as literals, so the
# suite stays dependency-free while still exercising the optional decode check.
PNG_RED = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)
PNG_GREEN = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNg+M8AAAICAQB7CYF4AAAAAElFTkSuQmCC"
)
PNG_BLUE = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNgYPgPAAEDAQAIicLsAAAAAElFTkSuQmCC"
)
GIF_1X1 = base64.b64decode("R0lGODdhAQABAIEAAAD/AAAAAAAAAAAAACwAAAAAAQABAAAIBAABBAQAOw==")


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


def _begin(
    workspace: Path,
    *,
    data: bytes = PNG_RED,
    mime: str = "image/png",
    name: str = "shot.png",
    size: int | None = None,
    session: SessionRef = REF,
) -> dto.BeginAttachmentResult:
    return store.begin_attachment(
        workspace,
        BeginAttachmentCommand(
            session=session,
            size=len(data) if size is None else size,
            mime=mime,
            display_name=name,
        ),
    )


def _upload(
    workspace: Path,
    *,
    data: bytes = PNG_RED,
    mime: str = "image/png",
    name: str = "shot.png",
    session: SessionRef = REF,
    chunk_size: int = 16,
) -> tuple[AttachmentRef, dto.FinishAttachmentResult]:
    started = _begin(workspace, data=data, mime=mime, name=name, session=session)
    offset = 0
    while offset < len(data):
        result = store.append_attachment_chunk(
            workspace,
            dto.AppendAttachmentChunkCommand(
                ref=started.ref,
                expected_offset=offset,
                data_base64=base64.b64encode(data[offset : offset + chunk_size]).decode("ascii"),
            ),
        )
        offset = result.next_offset
    finished = store.finish_attachment(
        workspace,
        FinishAttachmentCommand(
            ref=started.ref,
            expected_size=len(data),
            expected_mime=mime,
        ),
    )
    return started.ref, finished


def _attachment_dir(workspace: Path, ref: AttachmentRef) -> Path:
    matches = [
        path
        for path in store.attachments_root(workspace).rglob(ref.attachment_id)
        if path.is_dir()
    ]
    assert len(matches) == 1
    return matches[0]


def _attachment_exists(workspace: Path, ref: AttachmentRef) -> bool:
    return any(
        path.is_dir() for path in store.attachments_root(workspace).rglob(ref.attachment_id)
    )


def _age(attachment_dir: Path, *, seconds: float) -> None:
    old = time.time() - seconds
    for name in ("meta.json", "data.part"):
        path = attachment_dir / name
        if path.exists():
            os.utime(path, (old, old))


def _stale_upload(workspace: Path, *, session: SessionRef = REF) -> AttachmentRef:
    """Begin one upload, write a first chunk, and age it past the TTL."""
    started = _begin(workspace, session=session)
    store.append_attachment_chunk(
        workspace,
        dto.AppendAttachmentChunkCommand(
            ref=started.ref,
            expected_offset=0,
            data_base64=base64.b64encode(PNG_RED[:8]).decode("ascii"),
        ),
    )
    _age(_attachment_dir(workspace, started.ref), seconds=dto.INCOMPLETE_TTL_SECONDS + 60)
    return started.ref


def _symlink_or_skip(target: Path, link: Path, *, target_is_directory: bool) -> None:
    try:
        os.symlink(target, link, target_is_directory=target_is_directory)
    except (AttributeError, NotImplementedError, OSError):
        pytest.skip("symlink creation is not permitted on this platform")


def _leaf_values(value: Any) -> list[Any]:
    if isinstance(value, dict):
        return [leaf for item in value.values() for leaf in _leaf_values(item)]
    if isinstance(value, list):
        return [leaf for item in value for leaf in _leaf_values(item)]
    return [value]


def _session_dir(workspace: Path, session: SessionRef = REF) -> Path:
    return (
        store.attachments_root(workspace)
        / store._project_key(session.project_id)
        / store._session_key(session.thread_id)
    )


def _run_parallel(call: Any, arguments: list[Any]) -> list[Any]:
    """Run ``call`` for each argument on its own thread, returning results/errors."""
    barrier = threading.Barrier(len(arguments))

    def _one(argument: Any) -> Any:
        barrier.wait()
        try:
            return call(argument)
        except Exception as exc:  # noqa: BLE001 - the caller inspects the failure
            return exc

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(arguments)) as pool:
        return list(pool.map(_one, arguments))


# --- DTO shape and pure validation ------------------------------------------


def test_attachment_dtos_are_frozen_slotted_and_json_safe() -> None:
    ref = AttachmentRef(session=REF, attachment_id="a" * 32)
    metadata = dto.AttachmentMetadata(
        ref=ref,
        size=10,
        mime="image/png",
        revision=None,
        display_name="shot.png",
        created_at="2026-01-01T00:00:00+00:00",
        finalized=False,
    )
    instances = [
        ref,
        dto.BeginAttachmentCommand(session=REF, size=10, mime="image/png"),
        dto.BeginAttachmentResult(
            ref=ref, chunk_bytes=1, chunk_base64_chars=2, expires_at="2026-01-01T00:00:00+00:00"
        ),
        dto.AppendAttachmentChunkCommand(ref=ref, expected_offset=0, data_base64="AA=="),
        dto.AppendAttachmentChunkResult(ref=ref, received_bytes=1, next_offset=1),
        dto.FinishAttachmentCommand(ref=ref, expected_size=10, expected_mime="image/png"),
        dto.FinishAttachmentResult(ref=ref, size=10, mime="image/png", revision="b" * 64),
        dto.AbortAttachmentCommand(ref=ref),
        dto.AbortAttachmentResult(ref=ref, removed=True),
        metadata,
        dto.StatAttachmentQuery(ref=ref),
        dto.ReadAttachmentQuery(ref=ref),
        dto.AttachmentChunk(
            ref=ref,
            offset=0,
            data_base64="",
            byte_length=0,
            next_offset=0,
            eof=True,
            metadata=metadata,
        ),
    ]
    for instance in instances:
        assert dataclasses.is_dataclass(instance)
        assert hasattr(instance, "__slots__")
        payload = dataclasses.asdict(instance)
        json.dumps(payload)
        assert not any(isinstance(leaf, (Path, bytes)) for leaf in _leaf_values(payload))
    with pytest.raises(dataclasses.FrozenInstanceError):
        instances[0].attachment_id = "c" * 32  # type: ignore[misc]


def test_allowlists_and_state_dir_match_their_source_of_truth() -> None:
    assert dto.IMAGE_MIME_ALLOWED == ALLOWED_MIME
    assert store._STATE_DIRNAME == SYNAPSE_DIRNAME


@pytest.mark.parametrize(
    "attachment_id",
    [
        "",
        "..",
        "../../etc/passwd",
        "a/b",
        "A" * 32,
        "g" * 32,
        "0" * 31,
        "0" * 33,
        "0" * 32 + "\x00",
    ],
)
def test_attachment_id_validation_rejects_non_server_ids(attachment_id: str) -> None:
    with pytest.raises(InvalidRequestError) as caught:
        dto.validate_attachment_id(attachment_id)
    if attachment_id:
        assert attachment_id not in str(caught.value)
    assert "attachment id" in str(caught.value)


def test_begin_validation_rejects_bad_size_and_type() -> None:
    for size in (0, -1, True, "69", None, MAX_ATTACHMENT_BYTES + 1):
        with pytest.raises(InvalidRequestError):
            dto.validate_begin_command(
                BeginAttachmentCommand(session=REF, size=size, mime="image/png")  # type: ignore[arg-type]
            )
    for mime in ("", "text/plain", "image/svg+xml", "IMAGE/PNG/", None):
        with pytest.raises(InvalidRequestError):
            dto.validate_begin_command(
                BeginAttachmentCommand(session=REF, size=10, mime=mime)  # type: ignore[arg-type]
            )


def test_begin_normalizes_jpg_alias_and_sanitizes_display_name() -> None:
    assert dto.normalize_mime("  IMAGE/JPG ") == "image/jpeg"
    assert dto.sanitize_display_name(r"C:\Users\me\shot.png") == "shot.png"
    assert dto.sanitize_display_name("/etc/passwd") == "passwd"
    assert dto.sanitize_display_name("bad\nname.png") == "badname.png"
    assert dto.sanitize_display_name("x" * 500) == "x" * dto.MAX_DISPLAY_NAME_CHARS
    assert dto.sanitize_display_name("") == ""
    assert dto.sanitize_display_name(None) == ""


def test_chunk_budget_rejects_oversized_and_malformed_payloads(monkeypatch) -> None:
    with pytest.raises(InvalidRequestError):
        dto.decode_chunk_base64("A" * (MAX_CHUNK_BASE64_CHARS + 1))
    with pytest.raises(InvalidRequestError):
        dto.decode_chunk_base64("!!not base64!!")
    with pytest.raises(InvalidRequestError):
        dto.decode_chunk_base64("")
    with pytest.raises(InvalidRequestError):
        dto.decode_chunk_base64(None)  # type: ignore[arg-type]
    monkeypatch.setattr(dto, "MAX_CHUNK_BYTES", 8)
    with pytest.raises(InvalidRequestError):
        dto.decode_chunk_base64(base64.b64encode(b"0123456789").decode("ascii"))
    assert dto.decode_chunk_base64(base64.b64encode(b"01234567").decode("ascii")) == b"01234567"


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (PNG_RED, "image/png"),
        (GIF_1X1, "image/gif"),
        (b"\xff\xd8\xff\xe0" + b"\x00" * 8, "image/jpeg"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b"BM\x00\x00\x00\x00", "image/bmp"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'/>", None),
        (b"", None),
    ],
)
def test_sniff_image_mime_reads_bytes_never_names(data: bytes, expected: str | None) -> None:
    assert image.sniff_image_mime(data) == expected


def test_image_payload_check_rejects_mismatched_and_non_image_bytes() -> None:
    verified = validate_image_payload(PNG_RED, expected_mime="image/png")
    assert verified.mime == "image/png"
    assert verified.level in dto.VERIFICATION_LEVELS
    with pytest.raises(AttachmentUnsafeError):
        validate_image_payload(GIF_1X1, expected_mime="image/png")
    with pytest.raises(AttachmentUnsafeError):
        validate_image_payload(PNG_RED, expected_mime="image/gif")
    with pytest.raises(AttachmentUnsafeError):
        validate_image_payload(b"plain text pretending to be a png", expected_mime="image/png")
    with pytest.raises(AttachmentUnsafeError):
        validate_image_payload(b"", expected_mime="image/png")
    with pytest.raises(InvalidRequestError):
        validate_image_payload(PNG_RED, expected_mime="application/pdf")


def test_structural_check_reports_its_level_and_caps_pixels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Force the no-Pillow path: the bounded header parse must still verify.
    monkeypatch.setattr(image, "_pillow_verify", lambda data: False)
    verified = validate_image_payload(PNG_RED, expected_mime="image/png")
    assert verified == image.ImageVerification(mime="image/png", level=dto.VERIFICATION_STRUCTURE)

    def _bomb(width: int, height: int) -> bytes:
        return (
            image._PNG_SIGNATURE
            + (13).to_bytes(4, "big")
            + b"IHDR"
            + width.to_bytes(4, "big")
            + height.to_bytes(4, "big")
            + b"\x08\x02\x00\x00\x00"
        )

    for width, height in ((40_000, 40_000), (image.MAX_IMAGE_DIMENSION + 1, 1), (0, 0)):
        with pytest.raises(AttachmentUnsafeError):
            validate_image_payload(_bomb(width, height), expected_mime="image/png")
    assert image.MAX_IMAGE_PIXELS < 40_000 * 40_000


def test_unparseable_structure_is_a_typed_unavailable_without_pillow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(image, "_pillow_verify", lambda data: False)
    magic_only = image._PNG_SIGNATURE + b"\x00" * 8
    with pytest.raises(AttachmentUnavailableError):
        validate_image_payload(magic_only, expected_mime="image/png")


def test_error_text_never_echoes_untrusted_values() -> None:
    with pytest.raises(InvalidRequestError) as bad_id:
        dto.validate_attachment_id("../../secret")
    assert "secret" not in str(bad_id.value)
    with pytest.raises(InvalidRequestError) as bad_chunk:
        dto.decode_chunk_base64("!!leaked-payload!!")
    assert "leaked-payload" not in str(bad_chunk.value)
    with pytest.raises(InvalidRequestError) as bad_name:
        dto.sanitize_display_name("leaked\x00name.png")
    assert "leaked" not in str(bad_name.value)


# --- store: happy path, durability, ordering --------------------------------


def test_begin_append_finish_persists_a_finalized_attachment(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    started = _begin(workspace)
    assert started.next_offset == 0
    assert started.chunk_bytes == dto.MAX_CHUNK_BYTES
    assert started.chunk_base64_chars == MAX_CHUNK_BASE64_CHARS
    half = len(PNG_RED) // 2
    first = store.append_attachment_chunk(
        workspace,
        dto.AppendAttachmentChunkCommand(
            ref=started.ref,
            expected_offset=0,
            data_base64=base64.b64encode(PNG_RED[:half]).decode("ascii"),
        ),
    )
    assert first.next_offset == half
    second = store.append_attachment_chunk(
        workspace,
        dto.AppendAttachmentChunkCommand(
            ref=started.ref,
            expected_offset=half,
            data_base64=base64.b64encode(PNG_RED[half:]).decode("ascii"),
        ),
    )
    assert second.received_bytes == len(PNG_RED)

    finished = store.finish_attachment(
        workspace,
        FinishAttachmentCommand(
            ref=started.ref,
            expected_size=len(PNG_RED),
            expected_mime="image/png",
        ),
    )
    assert finished.size == len(PNG_RED)
    assert finished.mime == "image/png"
    assert finished.revision == hashlib.sha256(PNG_RED).hexdigest()

    attachment_dir = _attachment_dir(workspace, started.ref)
    # Exact bytes on disk: no platform newline translation may alter the image.
    assert (attachment_dir / "data.bin").read_bytes() == PNG_RED
    assert not (attachment_dir / "data.part").exists()
    meta = json.loads((attachment_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["finalized"] is True
    assert meta["revision"] == finished.revision
    assert meta["project_id"] == REF.project_id
    assert meta["thread_id"] == REF.thread_id
    assert meta["size"] == len(PNG_RED)
    assert meta["mime"] == "image/png"
    assert meta["display_name"] == "shot.png"


def test_finalized_attachment_is_durable_and_bound_to_its_session(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    ref, finished = _upload(workspace, name=r"C:\Users\me\shot.png")
    metadata = store.stat_attachment(workspace, StatAttachmentQuery(ref=ref))
    assert metadata.finalized is True
    assert metadata.revision == finished.revision
    assert metadata.display_name == "shot.png"
    # No original client path is stored anywhere.
    raw_meta = (store.attachments_root(workspace)).rglob("meta.json")
    for path in raw_meta:
        text = path.read_text(encoding="utf-8")
        assert "Users" not in text
        assert "C:" not in text
    # A stateless lookup (no in-memory cache) still finds the same bytes.
    chunk = store.read_attachment(workspace, ReadAttachmentQuery(ref=ref, limit=MAX_READ_BYTES))
    assert base64.b64decode(chunk.data_base64) == PNG_RED
    assert chunk.eof is True
    for other in (OTHER_THREAD, OTHER_PROJECT):
        with pytest.raises(AttachmentNotFoundError):
            store.stat_attachment(
                workspace,
                StatAttachmentQuery(
                    ref=AttachmentRef(session=other, attachment_id=ref.attachment_id)
                ),
            )


def test_stat_reports_pending_then_finalized_state(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    started = _begin(workspace)
    pending = store.stat_attachment(workspace, StatAttachmentQuery(ref=started.ref))
    assert pending.finalized is False
    assert pending.revision is None
    with pytest.raises(AttachmentConflictError):
        store.read_attachment(workspace, ReadAttachmentQuery(ref=started.ref))
    with pytest.raises(AttachmentConflictError):
        store.resolve_attachments(workspace, [started.ref])
    ref, finished = _upload(workspace)
    assert store.stat_attachment(workspace, StatAttachmentQuery(ref=ref)).revision == (
        finished.revision
    )


def test_revision_binds_content_and_attachment_ids_are_unique(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    ref_a, done_a = _upload(workspace)
    ref_b, done_b = _upload(workspace)
    ref_c, done_c = _upload(workspace, data=PNG_GREEN)
    assert ref_a.attachment_id != ref_b.attachment_id
    assert done_a.revision == done_b.revision
    assert done_c.revision != done_a.revision


def test_read_is_bounded_and_pages_through_the_attachment(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    ref, _ = _upload(workspace)
    with pytest.raises(InvalidRequestError):
        store.read_attachment(workspace, ReadAttachmentQuery(ref=ref, limit=0))
    with pytest.raises(InvalidRequestError):
        store.read_attachment(workspace, ReadAttachmentQuery(ref=ref, limit=MAX_READ_BYTES + 1))
    with pytest.raises(InvalidRequestError):
        store.read_attachment(workspace, ReadAttachmentQuery(ref=ref, offset=len(PNG_RED) + 1))
    collected = bytearray()
    offset = 0
    while True:
        chunk = store.read_attachment(
            workspace, ReadAttachmentQuery(ref=ref, offset=offset, limit=16)
        )
        assert chunk.byte_length <= 16
        assert chunk.next_offset == offset + chunk.byte_length
        collected.extend(base64.b64decode(chunk.data_base64))
        offset = chunk.next_offset
        if chunk.eof:
            break
    assert bytes(collected) == PNG_RED


def test_resolve_attachments_renumbers_in_reference_order(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    refs = [_upload(workspace, data=data)[0] for data in (PNG_RED, PNG_GREEN, PNG_BLUE)]
    assert store.resolve_attachments(workspace, []) == ()
    resolved = store.resolve_attachments(workspace, list(reversed(refs)))
    assert [item.id for item in resolved] == [1, 2, 3]
    assert all(isinstance(item, Attachment) for item in resolved)
    assert resolved[0].data == PNG_BLUE
    assert resolved[0].source == "attachment"
    assert resolved[0].mime == "image/png"
    with pytest.raises(AttachmentQuotaError):
        store.resolve_attachments(workspace, [refs[0]] * (MAX_ATTACHMENTS_PER_SUBMIT + 1))


# --- store: protocol violations ---------------------------------------------


def test_out_of_order_chunk_is_rejected_without_touching_bytes(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    started = _begin(workspace)
    store.append_attachment_chunk(
        workspace,
        dto.AppendAttachmentChunkCommand(
            ref=started.ref, expected_offset=0, data_base64=base64.b64encode(PNG_RED[:8]).decode()
        ),
    )
    part = _attachment_dir(workspace, started.ref) / "data.part"
    assert part.stat().st_size == 8
    for offset in (0, 9, 100):
        with pytest.raises(AttachmentConflictError):
            store.append_attachment_chunk(
                workspace,
                dto.AppendAttachmentChunkCommand(
                    ref=started.ref,
                    expected_offset=offset,
                    data_base64=base64.b64encode(PNG_RED[8:16]).decode(),
                ),
            )
    assert part.stat().st_size == 8


def test_append_and_finish_enforce_the_declaration(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    started = _begin(workspace, data=PNG_RED, size=8)
    with pytest.raises(dto.AttachmentTooLargeError):
        store.append_attachment_chunk(
            workspace,
            dto.AppendAttachmentChunkCommand(
                ref=started.ref,
                expected_offset=0,
                data_base64=base64.b64encode(PNG_RED[:9]).decode(),
            ),
        )
    store.append_attachment_chunk(
        workspace,
        dto.AppendAttachmentChunkCommand(
            ref=started.ref, expected_offset=0, data_base64=base64.b64encode(PNG_RED[:5]).decode()
        ),
    )
    with pytest.raises(AttachmentConflictError):
        store.finish_attachment(
            workspace,
            FinishAttachmentCommand(
                ref=started.ref, expected_size=5, expected_mime="image/png"
            ),
        )
    with pytest.raises(AttachmentConflictError):
        store.finish_attachment(
            workspace,
            FinishAttachmentCommand(
                ref=started.ref, expected_size=8, expected_mime="image/gif"
            ),
        )
    with pytest.raises(InvalidRequestError):
        store.finish_attachment(
            workspace,
            FinishAttachmentCommand(
                ref=started.ref, expected_size=8, expected_mime="image/svg+xml"
            ),
        )
    with pytest.raises(AttachmentConflictError):
        store.finish_attachment(
            workspace,
            FinishAttachmentCommand(
                ref=started.ref, expected_size=8, expected_mime="image/png"
            ),
        )


def test_finish_rejects_bytes_that_do_not_match_the_declared_type(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    started = _begin(workspace, data=GIF_1X1, mime="image/png")
    store.append_attachment_chunk(
        workspace,
        dto.AppendAttachmentChunkCommand(
            ref=started.ref,
            expected_offset=0,
            data_base64=base64.b64encode(GIF_1X1).decode("ascii"),
        ),
    )
    with pytest.raises(AttachmentUnsafeError):
        store.finish_attachment(
            workspace,
            FinishAttachmentCommand(
                ref=started.ref, expected_size=len(GIF_1X1), expected_mime="image/png"
            ),
        )
    attachment_dir = _attachment_dir(workspace, started.ref)
    assert not (attachment_dir / "data.bin").exists()
    assert json.loads((attachment_dir / "meta.json").read_text(encoding="utf-8"))["finalized"] is (
        False
    )


def test_append_and_finish_reject_an_already_finalized_upload(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    ref, _ = _upload(workspace)
    with pytest.raises(AttachmentConflictError):
        store.append_attachment_chunk(
            workspace,
            dto.AppendAttachmentChunkCommand(ref=ref, expected_offset=0, data_base64="AA=="),
        )
    with pytest.raises(AttachmentConflictError):
        store.finish_attachment(
            workspace,
            FinishAttachmentCommand(
                ref=ref, expected_size=len(PNG_RED), expected_mime="image/png"
            ),
        )


def test_unknown_attachment_reference_is_not_found(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    unknown = AttachmentRef(session=REF, attachment_id="f" * 32)
    with pytest.raises(AttachmentNotFoundError):
        store.stat_attachment(workspace, StatAttachmentQuery(ref=unknown))
    with pytest.raises(AttachmentNotFoundError):
        store.read_attachment(workspace, ReadAttachmentQuery(ref=unknown))


def test_abort_removes_partial_bytes_and_is_idempotent(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    started = _begin(workspace)
    store.append_attachment_chunk(
        workspace,
        dto.AppendAttachmentChunkCommand(
            ref=started.ref, expected_offset=0, data_base64=base64.b64encode(PNG_RED[:8]).decode()
        ),
    )
    attachment_dir = _attachment_dir(workspace, started.ref)
    assert attachment_dir.exists()
    first = store.abort_attachment(workspace, dto.AbortAttachmentCommand(ref=started.ref))
    assert first.removed is True
    assert not attachment_dir.exists()
    second = store.abort_attachment(workspace, dto.AbortAttachmentCommand(ref=started.ref))
    assert second.removed is False
    unknown = AttachmentRef(session=REF, attachment_id="f" * 32)
    assert store.abort_attachment(workspace, dto.AbortAttachmentCommand(ref=unknown)).removed is (
        False
    )


def test_abort_never_deletes_a_finalized_attachment(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    ref, finished = _upload(workspace)
    attachment_dir = _attachment_dir(workspace, ref)
    assert (attachment_dir / "data.bin").read_bytes() == PNG_RED

    # A finalized attachment is durable and referenced by session history, so
    # abort is a no-op that reports ``removed=False`` instead of deleting it.
    assert store.abort_attachment(workspace, dto.AbortAttachmentCommand(ref=ref)).removed is False
    assert attachment_dir.exists()
    assert (attachment_dir / "data.bin").read_bytes() == PNG_RED
    metadata = store.stat_attachment(workspace, StatAttachmentQuery(ref=ref))
    assert metadata.finalized is True
    assert metadata.revision == finished.revision == hashlib.sha256(PNG_RED).hexdigest()
    chunk = store.read_attachment(workspace, ReadAttachmentQuery(ref=ref))
    assert base64.b64decode(chunk.data_base64) == PNG_RED
    assert store.resolve_attachments(workspace, [ref])[0].data == PNG_RED
    # Repeating the abort stays idempotent: the result never flips to removed.
    assert store.abort_attachment(workspace, dto.AbortAttachmentCommand(ref=ref)).removed is False


def test_abort_recovers_a_payload_awaiting_finalize_instead_of_deleting(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    started = _begin(workspace)
    store.append_attachment_chunk(
        workspace,
        dto.AppendAttachmentChunkCommand(
            ref=started.ref,
            expected_offset=0,
            data_base64=base64.b64encode(PNG_RED).decode("ascii"),
        ),
    )
    attachment_dir = _attachment_dir(workspace, started.ref)
    # The crash window of ``finish_attachment``: the payload was renamed, the
    # metadata was not, so the upload still reads as pending but is complete.
    os.replace(attachment_dir / "data.part", attachment_dir / "data.bin")

    # Abort must complete that finalize, not reclaim a fully written payload.
    assert (
        store.abort_attachment(workspace, dto.AbortAttachmentCommand(ref=started.ref)).removed
        is False
    )
    assert (attachment_dir / "data.bin").read_bytes() == PNG_RED
    metadata = store.stat_attachment(workspace, StatAttachmentQuery(ref=started.ref))
    assert metadata.finalized is True
    assert metadata.revision == hashlib.sha256(PNG_RED).hexdigest()
    chunk = store.read_attachment(workspace, ReadAttachmentQuery(ref=started.ref))
    assert base64.b64decode(chunk.data_base64) == PNG_RED


def test_abort_leaves_an_unreadable_payload_whose_data_bin_exists(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    started = _begin(workspace)
    store.append_attachment_chunk(
        workspace,
        dto.AppendAttachmentChunkCommand(
            ref=started.ref,
            expected_offset=0,
            data_base64=base64.b64encode(PNG_RED).decode("ascii"),
        ),
    )
    attachment_dir = _attachment_dir(workspace, started.ref)
    os.replace(attachment_dir / "data.part", attachment_dir / "data.bin")
    # Unusable metadata over an existing payload: this is not a partial upload,
    # so abort must not reclaim bytes that a history reference may resolve.
    (attachment_dir / "meta.json").write_text("{ not json", encoding="utf-8")

    assert (
        store.abort_attachment(workspace, dto.AbortAttachmentCommand(ref=started.ref)).removed
        is False
    )
    assert attachment_dir.exists()
    assert (attachment_dir / "data.bin").read_bytes() == PNG_RED


# --- store: quotas, TTL sweep, and path safety ------------------------------


def test_a_store_past_the_former_count_caps_still_accepts_uploads(tmp_path: Path) -> None:
    """Regression: the removed cumulative count caps refused whole projects.

    A project that had accumulated 128 images (the former
    ``MAX_ATTACHMENTS_PER_PROJECT``, equal to the former per-session cap) refused
    every later upload - in every session, including brand-new ones - with
    ``attachment_quota``, and finalized attachments are never reclaimed, so that
    wall was permanent and could only be removed by deleting files by hand.
    Only the byte caps may refuse an upload now.
    """
    assert not hasattr(dto, "MAX_ATTACHMENTS_PER_PROJECT")
    assert not hasattr(dto, "MAX_STORED_ATTACHMENTS_PER_SESSION")
    workspace = _workspace(tmp_path)
    seed_ref, _ = _upload(workspace)
    seed_dir = _attachment_dir(workspace, seed_ref)
    for index in range(129):
        shutil.copytree(seed_dir, seed_dir.parent / ("%032x" % (index + 1)))
    assert len(list(store.attachments_root(workspace).rglob("meta.json"))) == 130
    started = _begin(workspace)
    assert _attachment_exists(workspace, started.ref)


def test_per_submit_cap_does_not_limit_a_session_lifetime(tmp_path: Path) -> None:
    # The image bank's 8 is a per-submit budget; a session keeps accepting new
    # uploads across turns, and the storage side has no cumulative count cap.
    workspace = _workspace(tmp_path)
    refs = [_upload(workspace)[0] for _ in range(MAX_ATTACHMENTS_PER_SUBMIT + 2)]
    assert len(store.resolve_attachments(workspace, refs[:MAX_ATTACHMENTS_PER_SUBMIT])) == (
        MAX_ATTACHMENTS_PER_SUBMIT
    )
    with pytest.raises(AttachmentQuotaError):
        store.resolve_attachments(workspace, refs)


def test_session_and_project_byte_quotas_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    _upload(workspace)
    monkeypatch.setattr(store, "MAX_SESSION_ATTACHMENT_BYTES", len(PNG_RED))
    with pytest.raises(AttachmentQuotaError):
        _begin(workspace)
    monkeypatch.setattr(store, "MAX_SESSION_ATTACHMENT_BYTES", dto.MAX_SESSION_ATTACHMENT_BYTES)
    # Another session of the same project is still refused by the project cap.
    monkeypatch.setattr(store, "MAX_PROJECT_ATTACHMENT_BYTES", len(PNG_RED))
    with pytest.raises(AttachmentQuotaError):
        _begin(workspace, session=OTHER_THREAD)


def test_sweep_reclaims_only_inactive_unfinished_uploads(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    stale = _begin(workspace)
    store.append_attachment_chunk(
        workspace,
        dto.AppendAttachmentChunkCommand(
            ref=stale.ref, expected_offset=0, data_base64=base64.b64encode(PNG_RED[:8]).decode()
        ),
    )
    fresh = _begin(workspace)
    store.append_attachment_chunk(
        workspace,
        dto.AppendAttachmentChunkCommand(
            ref=fresh.ref, expected_offset=0, data_base64=base64.b64encode(PNG_RED[:8]).decode()
        ),
    )
    finalized_ref, _ = _upload(workspace)
    # Age only after every begin: an operation-time sweep already reclaims
    # expired uploads, which is asserted separately.
    _age(_attachment_dir(workspace, stale.ref), seconds=dto.INCOMPLETE_TTL_SECONDS + 60)

    assert store.sweep_incomplete(workspace) == 1
    assert not _attachment_exists(workspace, stale.ref)
    assert _attachment_exists(workspace, fresh.ref)
    assert _attachment_exists(workspace, finalized_ref)

    later = time.time() + dto.INCOMPLETE_TTL_SECONDS + 60
    assert store.sweep_incomplete(workspace, now=later) == 1
    assert not _attachment_exists(workspace, fresh.ref)
    assert _attachment_exists(workspace, finalized_ref)


def test_sweep_is_bounded_per_call(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    refs = []
    for _ in range(3):
        started = _begin(workspace)
        store.append_attachment_chunk(
            workspace,
            dto.AppendAttachmentChunkCommand(
                ref=started.ref,
                expected_offset=0,
                data_base64=base64.b64encode(PNG_RED[:8]).decode(),
            ),
        )
        refs.append(started.ref)
    for ref in refs:
        _age(_attachment_dir(workspace, ref), seconds=dto.INCOMPLETE_TTL_SECONDS + 60)
    assert store.sweep_incomplete(workspace, max_entries=2) == 2
    assert sum(_attachment_exists(workspace, ref) for ref in refs) == 1
    assert store.sweep_incomplete(workspace, max_entries=2) == 1
    with pytest.raises(InvalidRequestError):
        store.sweep_incomplete(workspace, max_entries=0)


def test_begin_sweeps_expired_uploads_before_enforcing_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    # Without the sweep the three stale slots would fill the whole session budget.
    monkeypatch.setattr(store, "MAX_SESSION_ATTACHMENT_BYTES", len(PNG_RED))
    for _ in range(3):
        started = _begin(workspace)
        store.append_attachment_chunk(
            workspace,
            dto.AppendAttachmentChunkCommand(
                ref=started.ref,
                expected_offset=0,
                data_base64=base64.b64encode(PNG_RED[:8]).decode(),
            ),
        )
        _age(_attachment_dir(workspace, started.ref), seconds=dto.INCOMPLETE_TTL_SECONDS + 60)
    started = _begin(workspace)
    assert _attachment_exists(workspace, started.ref)
    remaining = [path for path in store.attachments_root(workspace).rglob("meta.json")]
    assert len(remaining) == 1


def test_sweep_budget_is_shared_across_sessions(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    sessions = [
        SessionRef(project_id="shared", thread_id=f"thread-{index}") for index in range(3)
    ]
    refs = [_stale_upload(workspace, session=session) for session in sessions]
    # ``max_entries`` bounds the whole call, not each session it visits.
    assert store.sweep_incomplete(workspace, max_entries=2) == 2
    assert sum(_attachment_exists(workspace, ref) for ref in refs) == 1
    assert store.sweep_incomplete(workspace) == 1


def test_sweep_scope_selects_the_project_and_session_to_visit(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    target = _stale_upload(workspace)
    sibling = _stale_upload(workspace, session=OTHER_THREAD)
    foreign = _stale_upload(workspace, session=OTHER_PROJECT)

    assert store.sweep_incomplete(workspace, session=REF) == 1
    assert not _attachment_exists(workspace, target)
    assert _attachment_exists(workspace, sibling)
    assert _attachment_exists(workspace, foreign)

    assert store.sweep_incomplete(workspace, project_id=REF.project_id) == 1
    assert not _attachment_exists(workspace, sibling)
    assert _attachment_exists(workspace, foreign)

    assert store.sweep_incomplete(workspace, project_id="absent-project") == 0
    assert store.sweep_incomplete(workspace) == 1
    assert not _attachment_exists(workspace, foreign)


def test_sweep_never_removes_a_payload_awaiting_finalize_recovery(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    started = _begin(workspace)
    store.append_attachment_chunk(
        workspace,
        dto.AppendAttachmentChunkCommand(
            ref=started.ref,
            expected_offset=0,
            data_base64=base64.b64encode(PNG_RED).decode("ascii"),
        ),
    )
    attachment_dir = _attachment_dir(workspace, started.ref)
    # The crash window of ``finish_attachment``: the payload was renamed, the
    # metadata was not, so the upload still reads as pending but is complete.
    os.replace(attachment_dir / "data.part", attachment_dir / "data.bin")
    _age(attachment_dir, seconds=dto.INCOMPLETE_TTL_SECONDS + 60)

    # The next operation repairs it; the sweep must leave it for that repair
    # instead of reclaiming a fully written payload as a partial upload.
    assert store.sweep_incomplete(workspace) == 0
    assert _attachment_exists(workspace, started.ref)
    metadata = store.stat_attachment(workspace, StatAttachmentQuery(ref=started.ref))
    assert metadata.finalized is True
    assert metadata.revision == hashlib.sha256(PNG_RED).hexdigest()
    chunk = store.read_attachment(workspace, ReadAttachmentQuery(ref=started.ref))
    assert base64.b64decode(chunk.data_base64) == PNG_RED


def test_sweep_reports_an_unreadable_store_as_unavailable(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    root = store.attachments_root(workspace)
    root.parent.mkdir(parents=True, exist_ok=True)
    # A file where the store root belongs: the bounded scan must report the
    # store as unusable (a typed error) instead of silently sweeping nothing.
    root.write_bytes(b"not a directory")
    with pytest.raises(AttachmentUnavailableError):
        store.sweep_incomplete(workspace)


def test_quota_scan_fails_closed_instead_of_under_counting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    _upload(workspace)
    _upload(workspace)
    monkeypatch.setattr(store, "MAX_QUOTA_SCAN_ENTRIES", 1)
    with pytest.raises(AttachmentQuotaError):
        _begin(workspace)


def test_corrupt_metadata_is_charged_the_worst_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    corrupt = (
        store.attachments_root(workspace)
        / store._project_key(REF.project_id)
        / store._session_key(REF.thread_id)
        / ("e" * 32)
    )
    corrupt.mkdir(parents=True)
    (corrupt / "meta.json").write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(store, "MAX_SESSION_ATTACHMENT_BYTES", MAX_ATTACHMENT_BYTES)
    with pytest.raises(AttachmentQuotaError):
        _begin(workspace)


def test_store_paths_are_derived_digests_not_wire_values(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    hostile = SessionRef(project_id="../../escape", thread_id="..\\..\\win")
    started = _begin(workspace, session=hostile)
    root = store.attachments_root(workspace)
    assert root == workspace / SYNAPSE_DIRNAME / "attachments"
    names = [path.name for path in root.rglob("*")]
    assert all(".." not in name and "escape" not in name and "win" not in name for name in names)
    assert started.ref.attachment_id in names
    assert sorted(path.name for path in workspace.iterdir()) == [SYNAPSE_DIRNAME]


def test_symlinked_project_dir_is_rejected_and_nothing_escapes(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    attachments = store.attachments_root(workspace)
    attachments.mkdir(parents=True)
    _symlink_or_skip(
        outside, attachments / store._project_key(REF.project_id), target_is_directory=True
    )
    with pytest.raises(AttachmentForbiddenError):
        _begin(workspace)
    assert list(outside.iterdir()) == []


def test_symlinked_finalized_file_is_rejected(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    ref, _ = _upload(workspace)
    data_path = _attachment_dir(workspace, ref) / "data.bin"
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG_RED)
    data_path.unlink()
    _symlink_or_skip(outside, data_path, target_is_directory=False)
    with pytest.raises(AttachmentForbiddenError):
        store.read_attachment(workspace, ReadAttachmentQuery(ref=ref))


def test_store_requires_a_real_trusted_workspace(tmp_path: Path) -> None:
    with pytest.raises(dto.AttachmentUnavailableError):
        store.attachments_root(tmp_path / "missing")
    with pytest.raises(dto.AttachmentUnavailableError):
        store.attachments_root("")
    with pytest.raises(dto.AttachmentUnavailableError):
        store.attachments_root(None)  # type: ignore[arg-type]


def test_store_module_has_no_threads_event_loop_or_mutable_state() -> None:
    source = Path(store.__file__).read_text(encoding="utf-8")
    assert "import threading" not in source
    assert "import asyncio" not in source
    assert "atexit" not in source
    assert "async def" not in source
    module_state = {
        name: value for name, value in vars(store).items() if not name.startswith("__")
    }
    assert not any(isinstance(value, (dict, list, set)) for value in module_state.values())


# --- store: concurrency, atomic quota/append, and crash recovery -------------


def _slow_meta(monkeypatch: pytest.MonkeyPatch) -> None:
    """Widen the quota-scan/create window so a missing lock would over-create."""
    original = store._write_meta

    def _write(attachment_dir: Path, meta: Any) -> None:
        time.sleep(0.05)
        original(attachment_dir, meta)

    monkeypatch.setattr(store, "_write_meta", _write)


def test_parallel_begin_cannot_exceed_the_session_byte_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    monkeypatch.setattr(store, "MAX_SESSION_ATTACHMENT_BYTES", 3 * len(PNG_RED))
    _slow_meta(monkeypatch)
    results = _run_parallel(lambda _: _begin(workspace), [None] * 10)
    created = [item for item in results if isinstance(item, dto.BeginAttachmentResult)]
    rejected = [item for item in results if isinstance(item, AttachmentQuotaError)]
    assert len(created) == 3
    assert len(rejected) == 7
    assert len(list(store.attachments_root(workspace).rglob("meta.json"))) == 3


def test_parallel_append_at_the_same_offset_has_one_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    started = _begin(workspace)
    payload = base64.b64encode(PNG_RED[:16]).decode("ascii")
    original = store._write_all

    def _write(fd: int, data: bytes) -> None:
        time.sleep(0.05)
        original(fd, data)

    monkeypatch.setattr(store, "_write_all", _write)

    def _append(_: Any) -> Any:
        return store.append_attachment_chunk(
            workspace,
            dto.AppendAttachmentChunkCommand(
                ref=started.ref, expected_offset=0, data_base64=payload
            ),
        )

    results = _run_parallel(_append, [None, None])
    winners = [item for item in results if isinstance(item, dto.AppendAttachmentChunkResult)]
    losers = [item for item in results if isinstance(item, AttachmentConflictError)]
    assert len(winners) == 1
    assert len(losers) == 1
    # The loser must not have overwritten the winner's bytes.
    assert (_attachment_dir(workspace, started.ref) / "data.part").read_bytes() == PNG_RED[:16]


def test_project_quota_is_atomic_across_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    monkeypatch.setattr(store, "MAX_PROJECT_ATTACHMENT_BYTES", 4 * len(PNG_RED))
    _slow_meta(monkeypatch)
    sessions = [SessionRef(project_id="shared", thread_id=f"thread-{i}") for i in range(12)]
    results = _run_parallel(lambda session: _begin(workspace, session=session), sessions)
    created = [item for item in results if isinstance(item, dto.BeginAttachmentResult)]
    rejected = [item for item in results if isinstance(item, AttachmentQuotaError)]
    assert len(created) == 4
    assert len(rejected) == 8


def test_lock_wait_is_bounded_and_never_deadlocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    started = _begin(workspace)
    monkeypatch.setattr(store, "_LOCK_TIMEOUT_SECONDS", 0.2)
    # Holding the session lock here proves the lock is a real OS lock: the same
    # process cannot re-enter it, and the waiter fails fast instead of hanging.
    with store._session_lock(_session_dir(workspace)):
        began = time.monotonic()
        with pytest.raises(AttachmentConflictError):
            store.append_attachment_chunk(
                workspace,
                dto.AppendAttachmentChunkCommand(
                    ref=started.ref,
                    expected_offset=0,
                    data_base64=base64.b64encode(PNG_RED[:8]).decode("ascii"),
                ),
            )
        assert time.monotonic() - began < 5.0
    # Once the lock is free the same call succeeds.
    result = store.append_attachment_chunk(
        workspace,
        dto.AppendAttachmentChunkCommand(
            ref=started.ref,
            expected_offset=0,
            data_base64=base64.b64encode(PNG_RED[:8]).decode("ascii"),
        ),
    )
    assert result.received_bytes == 8


def test_sweep_cannot_delete_a_fresh_upload_under_a_stale_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sweep running while an upload looks expired must not delete it.

    The upload is aged past the TTL, then a real ``append`` refreshes it while
    the sweep is already in flight.  The sweep has to wait for the session lock
    and re-read the activity before removing anything, so the refreshed upload
    survives instead of being reclaimed from a listing that went stale.
    """
    workspace = _workspace(tmp_path)
    started = _begin(workspace)
    store.append_attachment_chunk(
        workspace,
        dto.AppendAttachmentChunkCommand(
            ref=started.ref,
            expected_offset=0,
            data_base64=base64.b64encode(PNG_RED[:4]).decode("ascii"),
        ),
    )
    _age(_attachment_dir(workspace, started.ref), seconds=dto.INCOMPLETE_TTL_SECONDS + 60)

    entered = threading.Event()
    release = threading.Event()
    original_write = store._write_all

    def _write(fd: int, data: bytes) -> None:
        entered.set()
        assert release.wait(timeout=30)
        original_write(fd, data)

    monkeypatch.setattr(store, "_write_all", _write)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        append = pool.submit(
            store.append_attachment_chunk,
            workspace,
            dto.AppendAttachmentChunkCommand(
                ref=started.ref,
                expected_offset=4,
                data_base64=base64.b64encode(PNG_RED[4:8]).decode("ascii"),
            ),
        )
        assert entered.wait(timeout=30)
        sweep = pool.submit(store.sweep_incomplete, workspace)
        # The append holds the session lock, so the sweep must be blocked: a
        # stale listing must never translate into a deletion.
        time.sleep(0.3)
        assert _attachment_exists(workspace, started.ref)
        release.set()
        assert append.result(timeout=30).received_bytes == 8
        assert sweep.result(timeout=30) == 0

    assert _attachment_exists(workspace, started.ref)
    attachment_dir = _attachment_dir(workspace, started.ref)
    assert (attachment_dir / "data.part").read_bytes() == PNG_RED[:8]
    # The whole upload directory is still consistent: a sweep that half-removed
    # it would leave the pending metadata missing or the bytes truncated.
    pending = store.stat_attachment(workspace, StatAttachmentQuery(ref=started.ref))
    assert pending.finalized is False
    assert pending.size == len(PNG_RED)


def test_sweep_skips_a_session_whose_lock_a_peer_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    busy = _stale_upload(workspace)
    idle = _stale_upload(workspace, session=OTHER_THREAD)
    monkeypatch.setattr(store, "_LOCK_TIMEOUT_SECONDS", 0.2)
    # A session in active use is skipped (its upload is not stale anyway) while
    # the rest of the sweep still makes progress instead of failing outright.
    with store._session_lock(_session_dir(workspace)):
        assert store.sweep_incomplete(workspace) == 1
    assert _attachment_exists(workspace, busy)
    assert not _attachment_exists(workspace, idle)


def test_pending_metadata_repair_waits_for_the_session_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The readonly paths repair pending metadata under the session lock."""
    workspace = _workspace(tmp_path)
    started = _begin(workspace)
    store.append_attachment_chunk(
        workspace,
        dto.AppendAttachmentChunkCommand(
            ref=started.ref,
            expected_offset=0,
            data_base64=base64.b64encode(PNG_RED).decode("ascii"),
        ),
    )
    attachment_dir = _attachment_dir(workspace, started.ref)
    os.replace(attachment_dir / "data.part", attachment_dir / "data.bin")
    monkeypatch.setattr(store, "_LOCK_TIMEOUT_SECONDS", 0.2)
    with store._session_lock(_session_dir(workspace)):
        with pytest.raises(AttachmentConflictError):
            store.stat_attachment(workspace, StatAttachmentQuery(ref=started.ref))
        with pytest.raises(AttachmentConflictError):
            store.read_attachment(workspace, ReadAttachmentQuery(ref=started.ref))
    metadata = store.stat_attachment(workspace, StatAttachmentQuery(ref=started.ref))
    assert metadata.finalized is True
    assert metadata.revision == hashlib.sha256(PNG_RED).hexdigest()


def test_crash_between_rename_and_meta_is_recovered(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    started = _begin(workspace)
    store.append_attachment_chunk(
        workspace,
        dto.AppendAttachmentChunkCommand(
            ref=started.ref,
            expected_offset=0,
            data_base64=base64.b64encode(PNG_RED).decode("ascii"),
        ),
    )
    attachment_dir = _attachment_dir(workspace, started.ref)
    # Simulate the crash window: the payload was renamed, the metadata was not.
    os.replace(attachment_dir / "data.part", attachment_dir / "data.bin")
    assert json.loads((attachment_dir / "meta.json").read_text(encoding="utf-8"))["finalized"] is (
        False
    )
    assert not (attachment_dir / "data.part").exists()

    metadata = store.stat_attachment(workspace, StatAttachmentQuery(ref=started.ref))
    assert metadata.finalized is True
    assert metadata.revision == hashlib.sha256(PNG_RED).hexdigest()
    raw = json.loads((attachment_dir / "meta.json").read_text(encoding="utf-8"))
    assert raw["verification"] in dto.VERIFICATION_LEVELS
    chunk = store.read_attachment(workspace, ReadAttachmentQuery(ref=started.ref))
    assert base64.b64decode(chunk.data_base64) == PNG_RED
    resolved = store.resolve_attachments(workspace, [started.ref])
    assert resolved[0].data == PNG_RED
    # The repair is durable, so a later operation sees the same finalized state.
    assert json.loads((attachment_dir / "meta.json").read_text(encoding="utf-8"))["finalized"] is (
        True
    )
    assert store.stat_attachment(workspace, StatAttachmentQuery(ref=started.ref)).revision == (
        metadata.revision
    )


def test_finish_records_the_verification_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    monkeypatch.setattr(image, "_pillow_verify", lambda data: False)
    ref, _ = _upload(workspace)
    # The level is durable store metadata, not a wire DTO field: the frozen
    # contract keeps ``FinishAttachmentResult`` / ``AttachmentMetadata`` intact.
    raw = json.loads((_attachment_dir(workspace, ref) / "meta.json").read_text(encoding="utf-8"))
    assert raw["verification"] == dto.VERIFICATION_STRUCTURE
    assert store.stat_attachment(workspace, StatAttachmentQuery(ref=ref)).finalized is True


def test_cross_process_append_waits_for_the_session_lock(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    started = _begin(workspace)
    payload = base64.b64encode(PNG_RED[:16]).decode("ascii")
    child = (
        "import sys, time\n"
        "import synapse.runtime.service.attachment_store as store\n"
        "from synapse.runtime.service.attachments import AppendAttachmentChunkCommand, "
        "AttachmentRef\n"
        "from synapse.runtime.sessions.ref import SessionRef\n"
        "workspace, attachment_id, data = sys.argv[1:4]\n"
        "ref = AttachmentRef(session=SessionRef(project_id='project', thread_id='thread'), "
        "attachment_id=attachment_id)\n"
        "start = time.monotonic()\n"
        "try:\n"
        "    result = store.append_attachment_chunk(workspace, "
        "AppendAttachmentChunkCommand(ref=ref, expected_offset=0, data_base64=data))\n"
        "    print('ok', result.received_bytes, round(time.monotonic() - start, 2))\n"
        "except Exception as exc:\n"
        "    print('err', type(exc).__name__, round(time.monotonic() - start, 2))\n"
    )
    hold = 0.6
    with store._session_lock(_session_dir(workspace)):
        process = subprocess.Popen(
            [sys.executable, "-c", child, str(workspace), started.ref.attachment_id, payload],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(hold)
    out, err = process.communicate(timeout=30)
    if process.returncode != 0 or not out.startswith("ok"):
        pytest.skip(f"child process could not run the store: {err.strip() or out.strip()}")
    _, received, elapsed = out.split()
    assert received == "16"
    # The child only proceeded once this process released the lock.
    assert float(elapsed) >= hold / 2
