"""Deterministic and optional native tool-output transformers."""
from __future__ import annotations

import importlib
import json
import re
from typing import Any, Protocol

from synapse.tool_output.detection import _ERROR_LINE, _LOG_SUMMARY, _SEARCH_LINE
from synapse.tool_output.models import ContentType, TransformContext, TransformResult

_WARNING_LINE = re.compile(r"\b(warn|warning|todo|fixme)\b", re.I)
_STACK_LINE = re.compile(r"(^\s+at\s+|^\s*File .+, line \d+|^\s*\d+\s*\||^\s*-->\s+)")
_NUMBER_OR_PATH = re.compile(r"\b\d+\b|(?:[A-Za-z]:)?[/\\][\w./\\-]+")
_TOKEN = re.compile(r"[\w.-]+", re.UNICODE)

class ToolOutputTransformer(Protocol):
    name: str

    def transform(self, content: str, context: TransformContext) -> TransformResult: ...

def _critical_lines(content: str, content_type: ContentType) -> list[str]:
    lines = content.splitlines()
    if content_type is ContentType.LOG:
        return [
            line
            for line in lines
            if _ERROR_LINE.search(line) or re.search(r"\b[A-Za-z_]+(?:Error|Exception):", line)
        ]
    if content_type is ContentType.DIFF:
        return [
            line
            for line in lines
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        ]
    if content_type is ContentType.GIT_SUMMARY:
        return [
            line
            for line in lines
            if re.match(
                r"^(?:Merge made by the .+ strategy\.|\s*\d+ files? changed(?:,|$)|"
                r"\s*\d+ insertions?\(\+\)|\s*\d+ deletions?\(-\))",
                line,
            )
        ]
    return []


def _result(
    content: str,
    body: str,
    *,
    name: str,
    content_type: ContentType,
    metadata: dict[str, Any] | None = None,
) -> TransformResult:
    critical = _critical_lines(content, content_type)
    return TransformResult(
        body,
        name,
        content_type,
        len(critical),
        sum(item in body for item in critical),
        metadata or {},
    )


class SearchTransformer:
    """Headroom-inspired search parsing, scoring, and bounded selection."""

    name = "search-v1"
    content_types = frozenset({ContentType.SEARCH})

    def __init__(
        self, *, max_files: int = 15, max_matches_per_file: int = 5, max_total_matches: int = 30
    ) -> None:
        self.max_files = max_files
        self.max_matches_per_file = max_matches_per_file
        self.max_total_matches = max_total_matches

    def transform(self, content: str, context: TransformContext) -> TransformResult:
        grouped: dict[str, list[tuple[int, str]]] = {}
        for raw in content.splitlines():
            match = _SEARCH_LINE.match(raw)
            if match:
                grouped.setdefault(match.group("path"), []).append((int(match.group("line")), raw))
        if not grouped:
            return TransformResult(content, self.name, ContentType.SEARCH, 0, 0)
        terms = {term.casefold() for term in _TOKEN.findall(context.query) if len(term) > 2}

        def score(raw: str) -> float:
            value = sum(0.3 for term in terms if term in raw.casefold())
            if _ERROR_LINE.search(raw):
                value += 0.5
            elif _WARNING_LINE.search(raw):
                value += 0.3
            return min(1.0, value)

        files = sorted(
            grouped.items(), key=lambda item: sum(score(raw) for _, raw in item[1]), reverse=True
        )[: self.max_files]
        selected: list[tuple[str, int, str]] = []
        summaries: list[str] = []
        for path, matches in files:
            if len(selected) >= self.max_total_matches:
                summaries.append(f"{path}: {len(matches)} matches omitted")
                continue
            ranked = sorted(matches, key=lambda item: score(item[1]), reverse=True)
            keep = {matches[0], matches[-1], *ranked[: self.max_matches_per_file]}
            shown = sorted(keep)[: self.max_total_matches - len(selected)]
            selected.extend((path, line, raw) for line, raw in shown)
            if len(shown) < len(matches):
                summaries.append(f"{path}: {len(matches) - len(shown)} matches omitted")
        selected.sort(key=lambda item: (item[0], item[1]))
        omitted = sum(int(item.split(": ")[-1].split()[0]) for item in summaries)
        body = "\n".join(
            [
                f"[search results compressed: {len(grouped)} files, {omitted} matches omitted]",
                *(raw for _, _, raw in selected),
                *summaries,
            ]
        )
        return _result(
            content,
            body,
            name=self.name,
            content_type=ContentType.SEARCH,
            metadata={
                "files": len(grouped),
                "omitted_matches": omitted,
                "file_summaries": summaries,
            },
        )


class PathListTransformer:
    """Losslessly fold repeated parent directories in plain file-path listings.

    This follows Headroom's reversible ``path_heading`` approach. It only accepts
    every-line path listings and verifies that expansion exactly restores input.
    """

    name = "path-list-v1"
    content_types = frozenset({ContentType.PATHS})
    _path_row = re.compile(
        r"^(?P<directory>(?:(?:[A-Za-z]:)?[/\\]|\.{0,2}[/\\])?"
        r"(?:[^/\\\s:]+[/\\])+)(?P<base>[^/\\\s:]+)$"
    )

    @classmethod
    def _expand(cls, lines: list[str]) -> list[str] | None:
        restored: list[str] = []
        directory: str | None = None
        for line in lines:
            if line.endswith("/") or line.endswith("\\"):
                directory = line
            elif directory is not None and "/" not in line and "\\" not in line:
                restored.append(directory + line)
            else:
                return None
        return restored

    def transform(self, content: str, context: TransformContext) -> TransformResult:
        lines = content.splitlines()
        trailing_newline = (
            "\r\n" if content.endswith("\r\n") else "\n" if content.endswith("\n") else ""
        )
        if len(lines) < 3:
            return TransformResult(content, self.name, ContentType.PATHS, 0, 0)
        grouped: list[str] = []
        current_directory: str | None = None
        for line in lines:
            match = self._path_row.match(line)
            if match is None:
                return TransformResult(content, self.name, ContentType.PATHS, 0, 0)
            directory = match.group("directory")
            if directory != current_directory:
                grouped.append(directory)
                current_directory = directory
            grouped.append(match.group("base"))
        if self._expand(grouped) != lines:
            return TransformResult(content, self.name, ContentType.PATHS, 0, 0)
        directory_count = sum(line.endswith(("/", "\\")) for line in grouped)
        line_ending = "\r\n" if "\r\n" in content else "\n"
        body = line_ending.join(
            [
                f"[paths compressed: {len(lines)} paths / {directory_count} dirs]",
                *grouped,
            ]
        ) + trailing_newline
        return _result(
            content,
            body,
            name=self.name,
            content_type=ContentType.PATHS,
            metadata={
                "paths": len(lines),
                "directories": sum(line.endswith(("/", "\\")) for line in grouped),
                "reversible": True,
            },
        )


class LogTransformer:
    """Headroom-inspired log parsing, warning dedupe, ranking, and context."""

    name = "log-v1"
    content_types = frozenset({ContentType.LOG})

    def __init__(
        self,
        *,
        context_lines: int = 3,
        max_lines: int = 100,
        min_lines_for_compression: int = 50,
        max_warnings: int = 5,
    ) -> None:
        self.context_lines = context_lines
        self.max_lines = max_lines
        self.min_lines_for_compression = min_lines_for_compression
        self.max_warnings = max_warnings

    @staticmethod
    def _normalise(line: str) -> str:
        return _NUMBER_OR_PATH.sub("<value>", line.casefold())

    def transform(self, content: str, context: TransformContext) -> TransformResult:
        lines = content.splitlines()
        if len(lines) < self.min_lines_for_compression:
            return TransformResult(content, self.name, ContentType.LOG, 0, 0)
        errors = [
            i
            for i, line in enumerate(lines)
            if _ERROR_LINE.search(line) or re.search(r"\b[A-Za-z_]+(?:Error|Exception):", line)
        ]
        warnings = [
            i for i, line in enumerate(lines) if _WARNING_LINE.search(line) and i not in errors
        ]
        summaries = [i for i, line in enumerate(lines) if _LOG_SUMMARY.search(line)]
        selected: set[int] = (
            set(range(min(3, len(lines))))
            | set(range(max(0, len(lines) - 3), len(lines)))
            | set(summaries)
        )
        for index in errors:
            selected.update(
                range(
                    max(0, index - self.context_lines),
                    min(len(lines), index + self.context_lines + 1),
                )
            )
        seen: set[str] = set()
        warning_count = 0
        for index in warnings:
            key = self._normalise(lines[index])
            if key in seen or warning_count >= self.max_warnings:
                continue
            seen.add(key)
            warning_count += 1
            selected.add(index)
        for index, line in enumerate(lines):
            if _STACK_LINE.search(line) and any(abs(index - error) <= 20 for error in errors):
                selected.add(index)
        ordered = sorted(selected)
        retained_warning_keys: set[str] = set()
        filtered: list[int] = []
        for index in ordered:
            line = lines[index]
            if _WARNING_LINE.search(line) and index not in errors:
                key = self._normalise(line)
                if key in retained_warning_keys or len(retained_warning_keys) >= self.max_warnings:
                    continue
                retained_warning_keys.add(key)
            filtered.append(index)
        ordered = filtered
        if len(ordered) > self.max_lines:
            priority = sorted(ordered, key=lambda i: (i not in errors, i not in summaries, i))
            ordered = sorted(priority[: self.max_lines])
        omitted = len(lines) - len(ordered)
        stats = {
            "errors": len(errors),
            "warnings": len(warnings),
            "selected": len(ordered),
            "omitted_lines": max(0, omitted),
        }
        summary = (
            f"[log compressed: {max(0, omitted)} lines omitted; "
            f"ERROR={len(errors)} WARN={len(warnings)}]"
        )
        body = "\n".join([summary, *(lines[index] for index in ordered)])
        return _result(content, body, name=self.name, content_type=ContentType.LOG, metadata=stats)


class DiffTransformer:
    name = "diff-v1"
    content_types = frozenset({ContentType.DIFF})

    def __init__(self, *, context_lines: int = 2) -> None:
        self.context_lines = context_lines

    def transform(self, content: str, context: TransformContext) -> TransformResult:
        lines = content.splitlines()
        keep: set[int] = set()
        for index, line in enumerate(lines):
            structural = line.startswith(("diff --git", "index ", "--- ", "+++ ", "@@"))
            changed = line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
            if structural:
                keep.add(index)
            if changed:
                keep.update(
                    range(
                        max(0, index - self.context_lines),
                        min(len(lines), index + self.context_lines + 1),
                    )
                )
        ordered = sorted(keep)
        omitted = max(0, len(lines) - len(ordered))
        body = "\n".join(
            [f"[diff compressed: {omitted} context lines omitted]", *(lines[i] for i in ordered)]
        )
        return _result(
            content,
            body,
            name=self.name,
            content_type=ContentType.DIFF,
            metadata={"omitted_lines": omitted},
        )


class GitSummaryTransformer:
    """Keep Git operation status and a bounded, representative file-stat view."""

    name = "git-summary-v1"
    content_types = frozenset({ContentType.GIT_SUMMARY})

    def __init__(
        self, *, head_lines: int = 8, tail_lines: int = 8, max_file_entries: int = 30
    ) -> None:
        self.head_lines = head_lines
        self.tail_lines = tail_lines
        self.max_file_entries = max_file_entries

    def transform(self, content: str, context: TransformContext) -> TransformResult:
        lines = content.splitlines()
        file_entries = [
            index
            for index, line in enumerate(lines)
            if re.match(
                r"^(?:\s+.+\s+\|\s+\d+\s+[+\-]+$|(?:create|delete) mode \d+ |rename )", line
            )
        ]
        if len(file_entries) <= self.max_file_entries:
            return TransformResult(content, self.name, ContentType.GIT_SUMMARY, 0, 0)
        selected = set(range(min(self.head_lines, len(lines))))
        selected.update(range(max(0, len(lines) - self.tail_lines), len(lines)))
        selected.update(
            index
            for index, line in enumerate(lines)
            if re.match(
                r"^\s*\d+ files? changed|^\s*\d+ (?:insertions?\(\+\)|deletions?\(-\))",
                line,
            )
        )
        selected.update(file_entries[: self.max_file_entries // 2])
        selected.update(file_entries[-(self.max_file_entries - self.max_file_entries // 2) :])
        ordered = sorted(selected)
        omitted_entries = len(file_entries) - len(set(file_entries) & selected)
        omitted_lines = len(lines) - len(ordered)
        body = "\n".join(
            [
                "[git summary compressed: "
                f"{omitted_entries} file entries and {omitted_lines} lines omitted]",
                *(lines[index] for index in ordered),
            ]
        )
        return _result(
            content,
            body,
            name=self.name,
            content_type=ContentType.GIT_SUMMARY,
            metadata={"file_entries": len(file_entries), "omitted_file_entries": omitted_entries},
        )


class JsonTransformer:
    name = "json-v1"
    content_types = frozenset({ContentType.JSON})

    def __init__(self, *, max_items: int = 20) -> None:
        self.max_items = max_items

    @staticmethod
    def _score(value: Any, query_terms: set[str]) -> int:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).casefold()
        score = sum(term in text for term in query_terms)
        return (
            score + 5
            if any(word in text for word in ("error", "failed", "fatal", "exception"))
            else score
        )

    def transform(self, content: str, context: TransformContext) -> TransformResult:
        try:
            data = json.loads(content)
        except (TypeError, ValueError):
            return TransformResult(content, self.name, ContentType.JSON, 0, 0)
        query_terms = {term.casefold() for term in _TOKEN.findall(context.query) if len(term) > 1}
        if isinstance(data, list) and len(data) > self.max_items:
            ranked = sorted(
                enumerate(data),
                key=lambda pair: (self._score(pair[1], query_terms), -pair[0]),
                reverse=True,
            )
            indexes = sorted({0, len(data) - 1, *(index for index, _ in ranked[: self.max_items])})
            selected = [data[index] for index in indexes]
            body_data: Any = {
                "_summary": {
                    "original_items": len(data),
                    "shown_items": len(selected),
                    "omitted_items": len(data) - len(selected),
                },
                "items": selected,
            }
        elif isinstance(data, dict) and len(data) > self.max_items:
            ranked_keys = sorted(
                data,
                key=lambda key: (self._score({key: data[key]}, query_terms), key),
                reverse=True,
            )
            selected_keys = ranked_keys[: self.max_items]
            body_data = {
                "_summary": {
                    "original_keys": len(data),
                    "shown_keys": len(selected_keys),
                    "omitted_keys": len(data) - len(selected_keys),
                },
                "values": {key: data[key] for key in selected_keys},
            }
        else:
            return TransformResult(content, self.name, ContentType.JSON, 0, 0)
        body = json.dumps(body_data, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        return _result(
            content,
            body,
            name=self.name,
            content_type=ContentType.JSON,
            metadata={"structured": True},
        )


class CodeTransformer:
    name = "code-v1"
    content_types = frozenset({ContentType.CODE})
    _signature = re.compile(
        r"^(?P<prefix>\s*\d+(?:\.\d+)?\t)?(?P<indent>\s*)"
        r"(?:async\s+def|def|class|function|func|fn|export\s+(?:async\s+)?function)\s+[^:{(]+"
    )
    _import = re.compile(
        r"^\s*(?:\d+(?:\.\d+)?\t)?\s*(?:import|from|use|package|#include)\b"
    )

    def __init__(self, *, body_lines: int = 3) -> None:
        self.body_lines = body_lines

    def transform(self, content: str, context: TransformContext) -> TransformResult:
        lines = content.splitlines()
        signatures = [index for index, line in enumerate(lines) if self._signature.match(line)]
        if len(signatures) < 3:
            return TransformResult(content, self.name, ContentType.CODE, 0, 0)
        keep: set[int] = set()
        for index, line in enumerate(lines):
            if self._import.match(line):
                keep.add(index)
        for position, start in enumerate(signatures):
            end = signatures[position + 1] if position + 1 < len(signatures) else len(lines)
            keep.update(range(start, min(end, start + self.body_lines + 1)))
        ordered = sorted(keep)
        omitted = len(lines) - len(ordered)
        body = "\n".join(
            [f"[code compressed: {omitted} body lines omitted]", *(lines[i] for i in ordered)]
        )
        return _result(
            content,
            body,
            name=self.name,
            content_type=ContentType.CODE,
            metadata={"omitted_lines": omitted},
        )


class GenericTransformer:
    name = "generic-v1"
    content_types = frozenset({ContentType.TEXT})

    def __init__(
        self, *, head_lines: int = 40, tail_lines: int = 16, max_anchor_lines: int = 12
    ) -> None:
        self.head_lines = head_lines
        self.tail_lines = tail_lines
        self.max_anchor_lines = max_anchor_lines

    def transform(self, content: str, context: TransformContext) -> TransformResult:
        lines = content.splitlines()
        if len(lines) <= self.head_lines + self.tail_lines:
            return TransformResult(content, self.name, ContentType.TEXT, 0, 0)
        anchors = [
            line for line in lines[self.head_lines : -self.tail_lines] if _ERROR_LINE.search(line)
        ][: self.max_anchor_lines]
        omitted = len(lines) - self.head_lines - self.tail_lines - len(anchors)
        body = "\n".join(
            [
                *lines[: self.head_lines],
                f"...[{max(0, omitted)} lines omitted]...",
                *anchors,
                *lines[-self.tail_lines :],
            ]
        )
        return _result(
            content,
            body,
            name=self.name,
            content_type=ContentType.TEXT,
            metadata={"omitted_lines": max(0, omitted)},
        )


def load_transformer_plugins(specs: list[str] | tuple[str, ...]) -> list[ToolOutputTransformer]:
    """Load optional local transformers from ``module:attribute`` specifications."""
    plugins: list[ToolOutputTransformer] = []
    for spec in specs:
        module_name, separator, attribute = str(spec).partition(":")
        if not separator or not module_name or not attribute:
            raise ValueError(f"invalid tool-output transformer plugin: {spec!r}")
        factory = getattr(importlib.import_module(module_name), attribute)
        plugin = factory() if isinstance(factory, type) else factory
        if not callable(getattr(plugin, "transform", None)) or not getattr(plugin, "name", ""):
            raise TypeError(f"invalid tool-output transformer plugin: {spec!r}")
        plugins.append(plugin)
    return plugins


class NativeTransformer:
    """Adapter for the optional prebuilt Apache-2.0 native compression wheel.

    Native code receives only text and returns a compact view. Synapse retains
    responsibility for critical-fact validation, fallback, metrics, and SQLite
    reversible storage in ``ToolOutputTransformPipeline``.
    """

    def __init__(
        self,
        *,
        name: str,
        content_type: ContentType,
        native_transform: Any,
    ) -> None:
        self.name = name
        self.content_types = frozenset({content_type})
        self._content_type = content_type
        self._native_transform = native_transform

    def transform(self, content: str, context: TransformContext) -> TransformResult:
        try:
            payload = self._native_transform(content, context)
            transformed = payload.get("content") if isinstance(payload, dict) else None
            if not isinstance(transformed, str):
                raise TypeError("native compressor returned no text content")
            metadata = {
                "native": True,
                **{key: value for key, value in payload.items() if key != "content"},
            }
            return _result(
                content,
                transformed,
                name=str(payload.get("transformer") or self.name),
                content_type=self._content_type,
                metadata=metadata,
            )
        except Exception as exc:  # noqa: BLE001
            return TransformResult(
                content,
                self.name,
                self._content_type,
                0,
                0,
                {"native": True, "fallback": "native_error", "error": type(exc).__name__},
            )


def load_native_transformers(*, enabled: bool = True) -> list[ToolOutputTransformer]:
    """Load native transformers when the optional wheel is installed.

    Import failure is expected on unsupported platforms and leaves the Python
    deterministic transformers active. The package is never built at install
    time by Synapse itself.
    """
    if not enabled:
        return []
    try:
        import synapse_tool_compress_core as native
    except (ImportError, OSError):
        return []

    def compress_native_diff(content: str, context: TransformContext) -> dict[str, Any]:
        try:
            result = native.compress_diff(content, context=context.query)
        except TypeError as exc:
            if "unexpected keyword argument 'context'" not in str(exc):
                raise
            result = native.compress_diff(content)
            result["context_supported"] = False
        else:
            result["context_supported"] = True
        return result

    return [
        NativeTransformer(
            name="headroom-search-v1",
            content_type=ContentType.SEARCH,
            native_transform=lambda content, context: native.compress_search(
                content, query=context.query
            ),
        ),
        NativeTransformer(
            name="headroom-log-v1",
            content_type=ContentType.LOG,
            native_transform=lambda content, _context: native.compress_log(content),
        ),
        NativeTransformer(
            name="headroom-diff-v1",
            content_type=ContentType.DIFF,
            native_transform=compress_native_diff,
        ),
        NativeTransformer(
            name="headroom-smart-crusher-v1",
            content_type=ContentType.JSON,
            native_transform=lambda content, context: native.crush_json(
                content, query=context.query
            ),
        ),
        NativeTransformer(
            name="headroom-code-v1",
            content_type=ContentType.CODE,
            native_transform=lambda content, context: native.compress_code(
                content, context=context.query
            ),
        ),
    ]
