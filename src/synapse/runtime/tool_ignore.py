"""Gitignore-based path filters for filesystem tools.

Used by ``CodingLocalShellBackend`` so ``glob`` / ``grep`` skip paths that
developers already marked ignored (``.venv``, build caches, agent state, ...).

Practical gitwildmatch subset (not a full git clone):
- root ``.gitignore`` only
- ``*``, ``?``, ``**``, trailing ``/`` directory rules, ``!`` negation
- extra deny patterns OR-ed via config

Not a security sandbox: ``execute`` can still touch ignored paths.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


def _strip_comment(line: str) -> str:
    out: list[str] = []
    escaped = False
    for ch in line:
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            out.append(ch)
            continue
        if ch == "#":
            break
        out.append(ch)
    return "".join(out).rstrip().replace("\\ ", " ").replace("\\#", "#")


def _gitignore_pattern_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile one gitignore pattern to regex over relative POSIX paths."""
    pat = pattern
    dir_only = pat.endswith("/")
    if dir_only:
        pat = pat[:-1]

    anchored = pat.startswith("/")
    if anchored:
        pat = pat[1:]
    elif "/" in pat:
        anchored = True

    parts: list[str] = ["^"]
    if not anchored:
        parts.append("(?:.*/)?")

    i = 0
    n = len(pat)
    while i < n:
        c = pat[i]
        if c == "*":
            if i + 1 < n and pat[i + 1] == "*":
                i += 2
                if i < n and pat[i] == "/":
                    i += 1
                    parts.append("(?:.*/)?")
                else:
                    parts.append(".*")
            else:
                parts.append("[^/]*")
                i += 1
        elif c == "?":
            parts.append("[^/]")
            i += 1
        elif c == "[":
            j = i + 1
            if j < n and pat[j] in {"!", "]"}:
                j += 1
            while j < n and pat[j] != "]":
                j += 1
            if j >= n:
                parts.append(re.escape(c))
                i += 1
            else:
                class_body = pat[i + 1 : j]
                if class_body.startswith("!"):
                    class_body = "^" + class_body[1:]
                parts.append("[" + class_body + "]")
                i = j + 1
        else:
            parts.append(re.escape(c))
            i += 1

    # Directory-only and plain-name rules both exclude trees under the name.
    parts.append("(?:/.*)?")
    parts.append("$")
    return re.compile("".join(parts))


@dataclass(frozen=True)
class _Rule:
    regex: re.Pattern[str]
    negate: bool
    raw: str


_BUILTIN_DENY_PATTERNS = (".git/",)
_DEFAULT_IGNORE_PATTERNS = (
    "target/",
    ".venv/",
    ".node_modules/",
    "node_modules/",
    "__pycache__/",
)
_BUILTIN_DENY_RULES = tuple(
    _Rule(regex=_gitignore_pattern_to_regex(pattern), negate=False, raw=pattern)
    for pattern in _BUILTIN_DENY_PATTERNS
)
_DEFAULT_IGNORE_RULES = tuple(
    _Rule(regex=_gitignore_pattern_to_regex(pattern), negate=False, raw=pattern)
    for pattern in _DEFAULT_IGNORE_PATTERNS
)


class ToolIgnoreMatcher:
    """Match workspace-relative paths against built-in, gitignore, and deny rules."""

    def __init__(self, rules: list[_Rule]) -> None:
        self._rules = list(rules)

    @property
    def rule_count(self) -> int:
        """Number of configured gitignore or extra-deny rules."""
        return len(self._rules)

    @property
    def has_rules(self) -> bool:
        """Whether built-in or configured filters can exclude a path."""
        return bool(_BUILTIN_DENY_RULES or self._rules)

    @classmethod
    def from_workspace(
        cls,
        root: Path | str,
        *,
        extra_deny: list[str] | None = None,
    ) -> ToolIgnoreMatcher:
        root_path = Path(root).expanduser().resolve()
        rules: list[_Rule] = []
        gi = root_path / ".gitignore"
        if gi.is_file():
            try:
                text = gi.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            rules.extend(cls._parse_lines(text.splitlines()))
        for raw in extra_deny or []:
            rules.extend(cls._parse_lines([str(raw)]))
        return cls(rules)

    @classmethod
    def from_patterns(cls, patterns: list[str]) -> ToolIgnoreMatcher:
        rules: list[_Rule] = []
        for raw in patterns:
            rules.extend(cls._parse_lines([raw]))
        return cls(rules)

    @staticmethod
    def _parse_lines(lines: list[str]) -> list[_Rule]:
        out: list[_Rule] = []
        for line in lines:
            raw = line.strip()
            if not raw:
                continue
            body = _strip_comment(raw)
            if not body:
                continue
            negate = body.startswith("!")
            if negate:
                body = body[1:].strip()
            if not body:
                continue
            try:
                regex = _gitignore_pattern_to_regex(body)
            except re.error:
                continue
            out.append(_Rule(regex=regex, negate=negate, raw=raw))
        return out

    @staticmethod
    def normalize(path: str | Path) -> str:
        text = str(path).replace("\\", "/").strip()
        if not text or text in {".", "./"}:
            return ""
        while text.startswith("./"):
            text = text[2:]
        if text.startswith("/"):
            text = text[1:]
        if len(text) >= 2 and text[1] == ":":
            text = text[2:].lstrip("/")
        text = PurePosixPath(text).as_posix()
        return "" if text == "." else text.rstrip("/")

    def is_ignored(self, path: str | Path, *, is_dir: bool = False) -> bool:
        del is_dir  # tree match is encoded in regex via (?:/.*)?
        rel = self.normalize(path)
        if not rel:
            return False
        if any(rule.regex.match(rel) for rule in _BUILTIN_DENY_RULES):
            return True
        ignored = any(rule.regex.match(rel) for rule in _DEFAULT_IGNORE_RULES)
        for rule in self._rules:
            if rule.regex.match(rel):
                ignored = not rule.negate
        return ignored


def relative_to_root(path: str | Path, root: Path) -> str:
    """Best-effort workspace-relative POSIX path for ignore checks."""
    text = str(path).replace("\\", "/")
    try:
        p = Path(path)
        if p.is_absolute():
            return p.resolve().relative_to(root.resolve()).as_posix()
    except Exception:  # noqa: BLE001
        pass
    if text.startswith("/"):
        return text[1:]
    return text
