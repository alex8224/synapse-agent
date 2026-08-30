"""S4 read-only workspace artifact port gates."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import os
import threading
from builtins import open as builtin_open
from pathlib import Path
from types import SimpleNamespace

import pytest

import synapse.runtime.service.artifacts as artifact_module
from synapse.runtime.service import (
    ArtifactForbiddenError,
    ArtifactNotFoundError,
    ArtifactRef,
    CloseSessionCommand,
    GetSessionQuery,
    InvalidArtifactPathError,
    InvalidSessionError,
    ListArtifactsQuery,
    LocalAgentRuntimeService,
    OpenSessionCommand,
    ReadArtifactQuery,
    StatArtifactQuery,
)
from synapse.runtime.service.artifacts import (
    MAX_CHUNK_BYTES,
    MAX_CURSOR_BYTES,
    MAX_EXPECTED_REVISION_BYTES,
    MAX_PATH_BYTES,
    validate_artifact_path,
)
from synapse.runtime.service.errors import (
    ArtifactChangedError,
    ArtifactUnavailableError,
    InvalidArtifactCursorError,
    InvalidRequestError,
)
from synapse.runtime.service.ports import AgentRuntimeService
from synapse.runtime.sessions import RuntimeManager, SessionRef

REF = SessionRef(project_id="project", thread_id="thread")


def _service(
    workspace: Path | None, *, deny: list[str] | None = None
) -> LocalAgentRuntimeService:
    settings = SimpleNamespace(
        workspace=workspace,
        deny_fs_paths=deny or [],
        max_concurrency=2,
        model="test",
    )
    manager = RuntimeManager(
        settings=settings,
        agent_factory=lambda thread_id, shared: SimpleNamespace(thread_id=thread_id),
        project_id=REF.project_id,
    )
    service = LocalAgentRuntimeService(
        lambda project_id: manager if project_id == REF.project_id else None
    )
    return service


async def _opened_service(
    tmp_path: Path, *, deny: list[str] | None = None
) -> LocalAgentRuntimeService:
    service = _service(tmp_path, deny=deny)
    await service.open_session(OpenSessionCommand(session=REF))
    return service


@pytest.mark.parametrize(
    "path",
    [
        "/secret",
        "C:/secret",
        "C:\\secret",
        "//server/share",
        "a\\b",
        "a/../b",
        "a/./b",
        "a\x00b",
        "",
    ],
)
def test_artifact_path_validation_is_relative_posix_and_redacted(path: str) -> None:
    with pytest.raises(InvalidArtifactPathError) as caught:
        validate_artifact_path(path)
    if path:
        assert path not in str(caught.value)
    assert "artifact path" in str(caught.value)


def test_artifact_dtos_are_frozen_slotted_and_json_safe() -> None:
    ref = ArtifactRef(REF, "file.bin")
    query = ReadArtifactQuery(ref)
    assert dataclasses.is_dataclass(query)
    assert json.dumps(dataclasses.asdict(query))
    assert hasattr(query, "__slots__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        query.limit = 10  # type: ignore[misc]
    assert not any(
        isinstance(value, (Path, bytes)) for value in dataclasses.asdict(query).values()
    )
    assert hasattr(AgentRuntimeService, "read_artifact")


def test_stat_list_and_binary_read_are_bounded_and_json_safe(tmp_path: Path) -> None:
    async def run() -> None:
        (tmp_path / "z.txt").write_text("ignored ordering", encoding="utf-8")
        (tmp_path / "a.bin").write_bytes(bytes(range(256)) * 20)
        (tmp_path / "folder").mkdir()
        service = await _opened_service(tmp_path)

        metadata = await service.stat_artifact(StatArtifactQuery(ArtifactRef(REF, "a.bin")))
        assert metadata.kind == "file"
        assert metadata.media_type == "application/octet-stream"
        assert metadata.modified_at and metadata.revision

        page = await service.list_artifacts(ListArtifactsQuery(REF, limit=1))
        assert [entry.path for entry in page.entries] == ["a.bin"]
        assert page.next_cursor
        second = await service.list_artifacts(
            ListArtifactsQuery(REF, cursor=page.next_cursor, limit=10)
        )
        assert [entry.path for entry in second.entries] == ["folder", "z.txt"]
        folder = await service.stat_artifact(
            StatArtifactQuery(ArtifactRef(REF, "folder"))
        )
        assert folder.kind == "directory"

        first = await service.read_artifact(
            ReadArtifactQuery(ArtifactRef(REF, "a.bin"), limit=1024)
        )
        assert base64.b64decode(first.data_base64) == bytes(range(256)) * 4
        assert first.byte_length == 1024 and not first.eof
        end = await service.read_artifact(
            ReadArtifactQuery(ArtifactRef(REF, "a.bin"), offset=4096, limit=1024)
        )
        assert end.eof
        empty = await service.read_artifact(
            ReadArtifactQuery(ArtifactRef(REF, "a.bin"), offset=5120, limit=1024)
        )
        assert empty.eof and empty.byte_length == 0
        with pytest.raises(InvalidRequestError):
            await service.read_artifact(
                ReadArtifactQuery(ArtifactRef(REF, "a.bin"), offset=5121)
            )

    asyncio.run(run())


def test_broken_symlink_is_not_found_and_open_races_are_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        (tmp_path / "data").write_bytes(b"old")
        service = await _opened_service(tmp_path)
        try:
            (tmp_path / "broken").symlink_to(tmp_path / "missing")
        except (NotImplementedError, OSError):
            pytest.skip("symlinks unavailable")
        with pytest.raises(ArtifactNotFoundError) as missing:
            await service.stat_artifact(StatArtifactQuery(ArtifactRef(REF, "broken")))
        assert "broken" not in str(missing.value)

        original_open = artifact_module.open

        def replace_before_open(path: object, *args: object, **kwargs: object) -> object:
            Path(path).unlink()
            Path(path).write_bytes(b"replacement")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(artifact_module, "open", replace_before_open)
        with pytest.raises(ArtifactChangedError) as changed:
            await service.read_artifact(ReadArtifactQuery(ArtifactRef(REF, "data")))
        assert "data" not in str(changed.value)

    asyncio.run(run())


def test_close_reopen_recreates_artifact_session_without_old_state(tmp_path: Path) -> None:
    async def run() -> None:
        (tmp_path / "data").write_text("data", encoding="utf-8")
        service = await _opened_service(tmp_path)
        before = await service.get_session(GetSessionQuery(REF))
        result = await service.close_session(CloseSessionCommand(REF))
        assert result.closed
        with pytest.raises(Exception) as missing:
            await service.stat_artifact(StatArtifactQuery(ArtifactRef(REF, "data")))
        assert getattr(missing.value, "code", None) == "not_found"
        reopened = await service.open_session(OpenSessionCommand(session=REF))
        assert reopened.created
        after = await service.get_session(GetSessionQuery(REF))
        assert before.latest_sequence == after.latest_sequence == 0
        metadata = await service.stat_artifact(StatArtifactQuery(ArtifactRef(REF, "data")))
        assert metadata.kind == "file"

    asyncio.run(run())


def test_artifact_session_and_workspace_error_boundaries(tmp_path: Path) -> None:
    async def run() -> None:
        service = _service(tmp_path)
        missing = SessionRef(project_id=REF.project_id, thread_id="missing")
        with pytest.raises(Exception) as missing_error:
            await service.stat_artifact(StatArtifactQuery(ArtifactRef(missing, "x")))
        assert getattr(missing_error.value, "code", None) == "not_found"

        wrong_project = SessionRef(project_id="other", thread_id=REF.thread_id)
        wrong_service = _service(tmp_path)
        await wrong_service.open_session(OpenSessionCommand(session=REF))
        with pytest.raises(Exception) as project_error:
            await wrong_service.stat_artifact(
                StatArtifactQuery(ArtifactRef(wrong_project, "x"))
            )
        assert getattr(project_error.value, "code", None) == "not_found"

        with pytest.raises(InvalidSessionError):
            await wrong_service.stat_artifact(
                StatArtifactQuery(ArtifactRef(object(), "x"))  # type: ignore[arg-type]
            )

        root_file = tmp_path / "root-file"
        root_file.write_text("not a directory", encoding="utf-8")
        file_service = _service(root_file)
        await file_service.open_session(OpenSessionCommand(session=REF))
        with pytest.raises(ArtifactUnavailableError):
            await file_service.list_artifacts(ListArtifactsQuery(REF))

        missing_root_service = _service(tmp_path / "gone")
        await missing_root_service.open_session(OpenSessionCommand(session=REF))
        with pytest.raises(ArtifactUnavailableError):
            await missing_root_service.read_artifact(
                ReadArtifactQuery(ArtifactRef(REF, "x"))
            )

    asyncio.run(run())


@pytest.mark.parametrize("path", [None, 42, True, "a/" + "x" * 256])
def test_artifact_query_path_shape_is_rejected(path: object) -> None:
    query = ListArtifactsQuery(REF, path=path)  # type: ignore[arg-type]
    with pytest.raises(InvalidArtifactPathError):
        artifact_module.validate_list_query(query)


def test_path_segment_and_total_lengths_use_utf8_bytes() -> None:
    with pytest.raises(InvalidArtifactPathError):
        validate_artifact_path("é" * 128)
    with pytest.raises(InvalidArtifactPathError):
        validate_artifact_path("/".join(["segment"] * 600))


def test_cursor_binds_session_and_directory_and_detects_directory_changes(tmp_path: Path) -> None:
    async def run() -> None:
        (tmp_path / "a").write_text("a", encoding="utf-8")
        (tmp_path / "b").write_text("b", encoding="utf-8")
        (tmp_path / "folder").mkdir()
        service = await _opened_service(tmp_path)
        page = await service.list_artifacts(ListArtifactsQuery(REF, limit=1))
        assert page.next_cursor
        other_session = SessionRef(project_id=REF.project_id, thread_id="other")
        await service.open_session(OpenSessionCommand(session=other_session))
        with pytest.raises(InvalidArtifactCursorError):
            await service.list_artifacts(
                ListArtifactsQuery(other_session, cursor=page.next_cursor)
            )
        with pytest.raises(InvalidArtifactCursorError):
            await service.list_artifacts(
                ListArtifactsQuery(REF, path="folder", cursor=page.next_cursor)
            )
        (tmp_path / "changed").write_text("changed", encoding="utf-8")
        with pytest.raises(ArtifactChangedError):
            await service.list_artifacts(
                ListArtifactsQuery(REF, cursor=page.next_cursor, limit=1)
            )

    asyncio.run(run())


def test_list_pagination_has_no_duplicates_or_gaps(tmp_path: Path) -> None:
    async def run() -> None:
        for name in ("a", "b", "c", "d", "e"):
            (tmp_path / name).write_text(name, encoding="utf-8")
        service = await _opened_service(tmp_path)
        cursor = None
        seen: list[str] = []
        while True:
            page = await service.list_artifacts(ListArtifactsQuery(REF, cursor=cursor, limit=2))
            seen.extend(entry.path for entry in page.entries)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        assert seen == ["a", "b", "c", "d", "e"]


    asyncio.run(run())


def test_read_requests_exactly_limit_plus_one_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        (tmp_path / "data").write_bytes(b"x" * 2048)
        service = await _opened_service(tmp_path)
        observed: list[int] = []
        original_open = builtin_open

        class RecordingFile:
            def __init__(self, wrapped: object) -> None:
                self._wrapped = wrapped

            def __enter__(self) -> RecordingFile:
                self._wrapped.__enter__()  # type: ignore[attr-defined]
                return self

            def __exit__(self, *args: object) -> None:
                self._wrapped.__exit__(*args)  # type: ignore[attr-defined]

            def __getattr__(self, name: str) -> object:
                return getattr(self._wrapped, name)

            def read(self, size: int = -1) -> bytes:
                observed.append(size)
                return self._wrapped.read(size)  # type: ignore[attr-defined]

        def recording_open(*args: object, **kwargs: object) -> RecordingFile:
            return RecordingFile(original_open(*args, **kwargs))

        monkeypatch.setattr(artifact_module, "open", recording_open)
        await service.read_artifact(
            ReadArtifactQuery(ArtifactRef(REF, "data"), limit=1024)
        )
        assert observed == [1025]

    asyncio.run(run())


def test_blocked_filesystem_work_is_offloaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        (tmp_path / "data").write_bytes(b"data")
        service = await _opened_service(tmp_path)
        entered = threading.Event()
        release = threading.Event()
        original_stat = artifact_module.os.stat

        def blocked_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
            entered.set()
            release.wait(timeout=2)
            return original_stat(path, *args, **kwargs)

        monkeypatch.setattr(artifact_module, "_stat_path", lambda path: blocked_stat(path))
        operation = asyncio.create_task(
            service.stat_artifact(StatArtifactQuery(ArtifactRef(REF, "data")))
        )
        assert await asyncio.to_thread(entered.wait, 1)
        ticks = 0
        for _ in range(5):
            ticks += 1
            await asyncio.sleep(0)
        release.set()
        await operation
        assert ticks == 5

    asyncio.run(run())


def test_read_detects_post_fstat_revision_without_returning_chunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        (tmp_path / "data").write_bytes(b"data")
        service = await _opened_service(tmp_path)
        original_fstat = artifact_module.os.fstat
        calls = 0

        def changing_fstat(fd: int) -> os.stat_result:
            nonlocal calls
            calls += 1
            result = original_fstat(fd)
            if calls == 2:
                values = list(result)
                values[8] += 1
                return os.stat_result(values)
            return result

        monkeypatch.setattr(artifact_module.os, "fstat", changing_fstat)
        with pytest.raises(ArtifactChangedError):
            await service.read_artifact(ReadArtifactQuery(ArtifactRef(REF, "data")))

    asyncio.run(run())


def test_workspace_session_and_project_resolution_errors() -> None:
    async def run() -> None:
        service = _service(None)
        await service.open_session(OpenSessionCommand(session=REF))
        with pytest.raises(ArtifactUnavailableError) as unavailable:
            await service.stat_artifact(StatArtifactQuery(ArtifactRef(REF, "file.txt")))
        assert unavailable.value.code == "artifact_unavailable"

    asyncio.run(run())


def test_ignore_symlink_and_revision_guards(tmp_path: Path) -> None:
    async def run() -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "secret").write_text("secret", encoding="utf-8")
        (tmp_path / ".gitignore").write_text(
            "ignored/\n!ignored/public.txt\n", encoding="utf-8"
        )
        (tmp_path / "ignored").mkdir()
        (tmp_path / "ignored" / "public.txt").write_text("public", encoding="utf-8")
        (tmp_path / "denied.txt").write_text("denied", encoding="utf-8")
        outside = tmp_path.parent / (tmp_path.name + "-outside")
        outside.mkdir(exist_ok=True)
        (outside / "escape.txt").write_text("escape", encoding="utf-8")
        service = await _opened_service(tmp_path, deny=["denied.txt"])

        for name in (".git/secret", ".gitignore", "denied.txt"):
            with pytest.raises(ArtifactForbiddenError):
                await service.stat_artifact(StatArtifactQuery(ArtifactRef(REF, name)))
        public = await service.stat_artifact(
            StatArtifactQuery(ArtifactRef(REF, "ignored/public.txt"))
        )
        assert public.kind == "file"
        listed = await service.list_artifacts(ListArtifactsQuery(REF, limit=100))
        assert all(
            entry.path not in {".git", ".gitignore", "ignored", "denied.txt"}
            for entry in listed.entries
        )
        assert "ignored/public.txt" not in {entry.path for entry in listed.entries}

        try:
            (tmp_path / "inside.txt").symlink_to(tmp_path / "denied.txt")
            (tmp_path / "escape.txt").symlink_to(outside / "escape.txt")
        except (NotImplementedError, OSError):
            pytest.skip("symlinks unavailable")
        with pytest.raises(ArtifactForbiddenError):
            await service.stat_artifact(StatArtifactQuery(ArtifactRef(REF, "inside.txt")))
        with pytest.raises(ArtifactForbiddenError):
            await service.stat_artifact(StatArtifactQuery(ArtifactRef(REF, "escape.txt")))

    asyncio.run(run())


def test_resolved_aliases_reapply_policy_for_stat_read_and_list(tmp_path: Path) -> None:
    async def run() -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "secret.txt").write_text("secret", encoding="utf-8")
        (tmp_path / "ignored.txt").write_text("ignored", encoding="utf-8")
        (tmp_path / "cache").mkdir()
        (tmp_path / "cache" / "data.txt").write_text("cache", encoding="utf-8")
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "data.txt").write_text("cache", encoding="utf-8")
        (tmp_path / "denied.txt").write_text("denied", encoding="utf-8")
        (tmp_path / "allowed.txt").write_text("allowed", encoding="utf-8")
        (tmp_path / ".gitignore").write_text("ignored.txt\ncache/\n", encoding="utf-8")
        aliases = {
            "deny-alias.txt": "denied.txt",
            "git-alias.txt": ".git/secret.txt",
            "gitignored-alias.txt": "ignored.txt",
            "cache-alias.txt": "cache/data.txt",
            "default-cache-alias.txt": "__pycache__/data.txt",
        }
        try:
            for alias, target in aliases.items():
                (tmp_path / alias).symlink_to(tmp_path / target)
            (tmp_path / "allowed-alias.txt").symlink_to(tmp_path / "allowed.txt")
        except (NotImplementedError, OSError):
            pytest.skip("symlinks unavailable")
        service = await _opened_service(tmp_path, deny=["denied.txt"])

        for alias in aliases:
            with pytest.raises(ArtifactForbiddenError):
                await service.stat_artifact(StatArtifactQuery(ArtifactRef(REF, alias)))
            with pytest.raises(ArtifactForbiddenError):
                await service.read_artifact(
                    ReadArtifactQuery(ArtifactRef(REF, alias), limit=1024)
                )
        allowed = await service.stat_artifact(
            StatArtifactQuery(ArtifactRef(REF, "allowed-alias.txt"))
        )
        assert allowed.kind == "file"
        assert await service.read_artifact(
            ReadArtifactQuery(ArtifactRef(REF, "allowed-alias.txt"), limit=1024)
        )
        listed = await service.list_artifacts(ListArtifactsQuery(REF, limit=100))
        paths = {entry.path for entry in listed.entries}
        assert not paths.intersection(aliases)
        assert "allowed-alias.txt" in paths

    asyncio.run(run())


def test_artifact_ref_root_is_not_a_file_target_but_list_root_is_valid(tmp_path: Path) -> None:
    async def run() -> None:
        (tmp_path / "file.txt").write_text("data", encoding="utf-8")
        service = await _opened_service(tmp_path)
        root_ref = ArtifactRef(REF, ".")
        for operation in (
            service.stat_artifact(StatArtifactQuery(root_ref)),
            service.read_artifact(ReadArtifactQuery(root_ref)),
        ):
            with pytest.raises(InvalidArtifactPathError) as caught:
                await operation
            assert caught.value.code == "invalid_artifact_path"
            assert "." not in str(caught.value)
        page = await service.list_artifacts(ListArtifactsQuery(REF, path="."))
        assert page.path == "."

    asyncio.run(run())


def test_cursor_validation_is_utf8_bounded_and_strict() -> None:
    from synapse.runtime.service.artifacts import _decode_cursor

    for cursor in ("é" * MAX_CURSOR_BYTES, "\ud800", "not base64!"):
        with pytest.raises(InvalidArtifactCursorError) as caught:
            _decode_cursor(cursor)
        assert caught.value.code == "invalid_artifact_cursor"
        assert cursor not in str(caught.value)


def test_expected_revision_is_bounded_in_utf8_bytes() -> None:
    with pytest.raises(InvalidRequestError):
        from synapse.runtime.service.artifacts import validate_read_query

        validate_read_query(
            ReadArtifactQuery(
                ArtifactRef(REF, "file.txt"),
                expected_revision="é" * MAX_EXPECTED_REVISION_BYTES,
            )
        )


def test_missing_and_limits_do_not_block_event_loop(tmp_path: Path) -> None:
    async def run() -> None:
        (tmp_path / "data").write_bytes(b"data")
        service = await _opened_service(tmp_path)
        with pytest.raises(ArtifactNotFoundError):
            await service.stat_artifact(StatArtifactQuery(ArtifactRef(REF, "gone")))
        with pytest.raises(InvalidRequestError):
            await service.list_artifacts(ListArtifactsQuery(REF, limit=0))
        with pytest.raises(InvalidRequestError):
            await service.read_artifact(
                ReadArtifactQuery(ArtifactRef(REF, "data"), limit=MAX_CHUNK_BYTES + 1)
            )

        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            for _ in range(20):
                ticks += 1
                await asyncio.sleep(0)

        await asyncio.gather(
            service.read_artifact(ReadArtifactQuery(ArtifactRef(REF, "data"))), ticker()
        )
        assert ticks == 20

    asyncio.run(run())


def test_explicit_length_limits() -> None:
    with pytest.raises(InvalidArtifactPathError):
        validate_artifact_path("a" * (MAX_PATH_BYTES + 1))
