"""A workspace-confined unified-diff editing tool."""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from synapse.runtime.pathing import is_virtual_path

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)$")


class ApplyPatchInput(BaseModel):
    """Arguments for applying a standard unified diff in the workspace."""

    patch: str = Field(
        description=(
            "A standard unified diff for multi-line or multi-file refactors. Every file must "
            "have '--- a/path' and '+++ b/path' headers, and every hunk header must include "
            "numeric ranges, for example '@@ -10,2 +10,3 @@'. Minimal example: "
            "'--- a/src/app.py\\n+++ b/src/app.py\\n@@ -1,2 +1,2 @@\\n-old\\n+new\\n "
            "unchanged'. Do not use Codex markers such as '*** Begin Patch', "
            "'*** Update File', or '*** End Patch'. A bare '@@' is invalid. Paths must remain "
            "workspace-relative; do not use host absolute paths or '..'."
        ),
        min_length=1,
    )


@dataclass(frozen=True)
class _FilePatch:
    old_path: str | None
    new_path: str | None
    hunks: tuple[tuple[str, ...], ...]


class PatchError(ValueError):
    """A patch that cannot be parsed or applied safely."""


def _header_path(value: str) -> str | None:
    value = value.split("\t", 1)[0].strip()
    if value == "/dev/null":
        return None
    if value.startswith("a/") or value.startswith("b/"):
        value = value[2:]
    if not value or not is_virtual_path("/" + value.lstrip("/")):
        raise PatchError(f"Invalid patch path: {value!r}")
    unsafe_part = any(part in {"", ".", ".."} for part in value.split("/"))
    if value.startswith("/") or "\\" in value or unsafe_part:
        raise PatchError(f"Unsafe patch path: {value!r}")
    return value


def _parse_unified_diff(patch: str) -> tuple[_FilePatch, ...]:
    lines = patch.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    files: list[_FilePatch] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("diff --git ") or line.startswith("index "):
            index += 1
            continue
        if not line.startswith("--- "):
            index += 1
            continue
        if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
            raise PatchError("Expected a +++ header immediately after --- header")
        old_path = _header_path(line[4:])
        new_path = _header_path(lines[index + 1][4:])
        index += 2
        hunks: list[tuple[str, ...]] = []
        while index < len(lines) and lines[index].startswith("@@ "):
            if _HUNK_HEADER.match(lines[index]) is None:
                raise PatchError(f"Invalid hunk header: {lines[index]!r}")
            index += 1
            hunk: list[str] = []
            while index < len(lines):
                row = lines[index]
                if row.startswith("@@ ") or row.startswith("--- ") or row.startswith("diff --git "):
                    break
                if row == "\\ No newline at end of file":
                    index += 1
                    continue
                if not row.startswith((" ", "+", "-")):
                    raise PatchError(f"Invalid hunk row: {row!r}")
                hunk.append(row)
                index += 1
            if not hunk:
                raise PatchError(
                    "A hunk must contain at least one context, removal, or addition row"
                )
            hunks.append(tuple(hunk))
        if not hunks:
            raise PatchError("Each file patch must contain at least one @@ hunk")
        if old_path is None and new_path is None:
            raise PatchError("A file patch cannot delete and create /dev/null")
        files.append(_FilePatch(old_path=old_path, new_path=new_path, hunks=tuple(hunks)))
    if not files:
        raise PatchError("No unified diff file patch found")
    return tuple(files)


def _apply_hunks(original: list[str], hunks: tuple[tuple[str, ...], ...]) -> list[str]:
    result = list(original)
    cursor = 0
    for hunk in hunks:
        expected = [row[1:] for row in hunk if row[0] in {" ", "-"}]
        matches = [
            start
            for start in range(cursor, len(result) - len(expected) + 1)
            if result[start : start + len(expected)] == expected
        ]
        if not matches:
            sample = "\n".join(expected[:5])
            raise PatchError(f"Failed to find expected hunk context:\n{sample}")
        if len(matches) > 1:
            raise PatchError("Patch hunk context is ambiguous; include more unchanged lines")
        start = matches[0]
        replacement = [row[1:] for row in hunk if row[0] in {" ", "+"}]
        result[start : start + len(expected)] = replacement
        cursor = start + len(replacement)
    return result


def _read_text(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PatchError(f"Patch supports UTF-8 text files only: {path.name}") from exc
    return text, "\r\n" if "\r\n" in text else "\n"


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def build_apply_patch_tools(backend: Any) -> list[Any]:
    """Create a unified-diff patch tool bound to a filesystem backend."""

    @tool("apply_patch", args_schema=ApplyPatchInput)
    def apply_patch(*, patch: str) -> str:
        """Apply a workspace-confined unified diff atomically per changed file."""
        try:
            file_patches = _parse_unified_diff(patch)
            resolved: list[tuple[_FilePatch, Path | None, Path | None]] = []
            for file_patch in file_patches:
                old = (
                    backend._resolve_path("/" + file_patch.old_path)
                    if file_patch.old_path
                    else None
                )
                new = (
                    backend._resolve_path("/" + file_patch.new_path)
                    if file_patch.new_path
                    else None
                )
                resolved.append((file_patch, old, new))

            prepared: list[tuple[_FilePatch, Path | None, Path | None, str | None]] = []
            for file_patch, old, new in resolved:
                if old is None:
                    if new is None or new.exists():
                        raise PatchError(f"Cannot create existing file: /{file_patch.new_path}")
                    original, newline = "", "\n"
                else:
                    if not old.exists() or not old.is_file():
                        raise PatchError(f"File not found: /{file_patch.old_path}")
                    original, newline = _read_text(old)
                original_lines = original.replace("\r\n", "\n").splitlines()
                updated_lines = _apply_hunks(original_lines, file_patch.hunks)
                if new is None:
                    prepared.append((file_patch, old, new, None))
                    continue
                updated = "\n".join(updated_lines)
                if original.endswith(("\n", "\r")) and updated:
                    updated += "\n"
                prepared.append((file_patch, old, new, updated.replace("\n", newline)))

            changed: list[str] = []
            for file_patch, old, new, updated in prepared:
                if new is None:
                    if old is None:
                        raise PatchError("Cannot delete a missing source file")
                    old.unlink()
                    changed.append(f"deleted /{file_patch.old_path}")
                    continue
                if updated is None:
                    raise PatchError("Missing replacement content for patch target")
                _write_atomic(new, updated)
                action = "created" if old is None else "updated"
                changed.append(f"{action} /{file_patch.new_path}")
            return "Applied patch: " + ", ".join(changed)
        except (OSError, RuntimeError, ValueError) as exc:
            return f"Error applying patch: {exc}"

    return [apply_patch]
