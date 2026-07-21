"""Slash command completion candidates for TUI / CLI.

Returns full completed strings (not just suffixes) so Textual Input can
render ghost-text and accept with Right/Tab.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from synapse.mcp_client import load_mcp_server_configs
from synapse.models_registry import registry_from_settings
from synapse.sessions import SessionStore

ROOT_COMMANDS: list[str] = [
    "/help",
    "/?",
    "/thread",
    "/id",
    "/clear",
    "/exit",
    "/quit",
    "/sessions",
    "/session",
    "/new",
    "/switch",
    "/rename",
    "/export",
    "/compact",
    "/context",
    "/safety",
    "/approve",
    "/reject",
    "/skills",
    "/memory",
    "/subagents",
    "/mcp",
    "/model",
    "/theme",
]

SESSION_SUBCOMMANDS: list[str] = [
    "list",
    "ls",
    "show",
    "new",
    "switch",
    "rename",
    "delete",
    "search",
    "export",
]

MCP_SUBCOMMANDS: list[str] = [
    "list",
    "ls",
    "status",
    "tools",
    "test",
    "reload",
    "enable",
    "on",
    "disable",
    "off",
    "config",
]

EXPORT_FORMATS: list[str] = ["md", "json"]


@dataclass
class SessionChoice:
    """One session option for switch/delete completion."""

    thread_id: str
    title: str = ""

    def label(self) -> str:
        title = (self.title or "").strip()
        if not title or title == self.thread_id or title.startswith("session "):
            return self.thread_id
        short = title if len(title) <= 40 else title[:39] + "…"
        return f"{self.thread_id} · {short}"

    def matches(self, partial: str) -> bool:
        p = " ".join((partial or "").strip().split()).casefold()
        if not p:
            return True
        if self.thread_id.casefold().startswith(p):
            return True
        title = (self.title or "").strip().casefold()
        if not title or title.startswith("session ") or title == self.thread_id.casefold():
            return False
        return title.startswith(p) or p in title


@dataclass
class SlashCompleteContext:
    """Runtime data for dynamic completions."""

    settings: Any | None = None
    thread_ids: list[str] = field(default_factory=list)
    session_titles: list[str] = field(default_factory=list)
    sessions: list[SessionChoice] = field(default_factory=list)
    model_names: list[str] = field(default_factory=list)
    mcp_server_names: list[str] = field(default_factory=list)


def build_complete_context(settings: Any | None) -> SlashCompleteContext:
    ctx = SlashCompleteContext(settings=settings)
    if settings is None:
        return ctx
    try:
        store = SessionStore(settings.resolved_sessions_path())
        sessions = store.list(limit=50)
        ctx.sessions = [
            SessionChoice(thread_id=s.thread_id, title=s.title or "") for s in sessions
        ]
        ctx.thread_ids = [s.thread_id for s in sessions]
        ctx.session_titles = [
            s.title
            for s in sessions
            if s.title
            and not s.title.startswith("session ")
            and s.title != s.thread_id
        ]
    except Exception:  # noqa: BLE001
        pass
    try:
        reg = registry_from_settings(settings)
        ctx.model_names = list(reg.list_names())
    except Exception:  # noqa: BLE001
        pass
    try:
        servers = load_mcp_server_configs(
            path=getattr(settings, "mcp_config_path", None),
            json_blob=getattr(settings, "mcp_servers_json", None),
        )
        ctx.mcp_server_names = [s.name for s in servers]
    except Exception:  # noqa: BLE001
        pass
    return ctx


def _sessions_from_ctx(ctx: SlashCompleteContext) -> list[SessionChoice]:
    if ctx.sessions:
        return list(ctx.sessions)
    return [SessionChoice(thread_id=tid) for tid in ctx.thread_ids]


def _filter_sessions(sessions: list[SessionChoice], partial: str) -> list[SessionChoice]:
    return [s for s in sessions if s.matches(partial)]


def _session_complete_lines(
    cmd: str,
    used: list[str],
    sessions: list[SessionChoice],
    *,
    partial: str,
) -> list[str]:
    """Complete session refs by id/title; insert thread_id into the command line."""
    matched = _filter_sessions(sessions, partial)
    p = " ".join((partial or "").strip().split()).casefold()
    id_first: list[str] = []
    title_hits: list[str] = []
    for s in matched:
        line = " ".join([cmd, *used, s.thread_id]) if used else f"{cmd} {s.thread_id}"
        if not p or s.thread_id.casefold().startswith(p):
            id_first.append(line)
        else:
            title_hits.append(line)
    return _unique_keep_order(id_first + title_hits)


def _filter_prefix(options: list[str], prefix: str, *, casefold: bool = True) -> list[str]:
    if casefold:
        p = prefix.casefold()
        return [o for o in options if o.casefold().startswith(p)]
    return [o for o in options if o.startswith(prefix)]


def _unique_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def complete_slash(
    value: str,
    ctx: SlashCompleteContext | None = None,
) -> list[str]:
    """Return full-line completion candidates for the current input value."""
    raw = value or ""
    if not raw.startswith("/"):
        return []

    ctx = ctx or SlashCompleteContext()
    trailing_space = raw.endswith(" ")
    parts = raw.split()
    if not parts:
        return []
    cmd = parts[0]
    rest = parts[1:]

    if not rest and not trailing_space:
        return _unique_keep_order(_filter_prefix(ROOT_COMMANDS, cmd))

    cmd_cf = cmd.casefold()

    def with_prefix(options: list[str], used: list[str]) -> list[str]:
        out: list[str] = []
        for opt in options:
            line = " ".join([cmd, *used, opt]) if used else f"{cmd} {opt}"
            if line.casefold().startswith(raw.casefold()):
                out.append(line)
        return _unique_keep_order(out)

    # /session <sub>
    if cmd_cf == "/session":
        if not rest and trailing_space:
            return with_prefix(SESSION_SUBCOMMANDS, [])
        if len(rest) == 1 and not trailing_space:
            return with_prefix(_filter_prefix(SESSION_SUBCOMMANDS, rest[0]), [])
        sub = rest[0].casefold() if rest else ""
        if sub in {"switch", "delete", "show"}:
            sessions = _sessions_from_ctx(ctx)
            if len(rest) == 1 and trailing_space:
                return _session_complete_lines(cmd, [rest[0]], sessions, partial="")
            if len(rest) >= 2 and not trailing_space:
                return _session_complete_lines(
                    cmd, [rest[0]], sessions, partial=" ".join(rest[1:])
                )
            return []
        if sub == "export":
            arg = rest[1] if len(rest) >= 2 else ""
            if len(rest) == 1 and trailing_space:
                return with_prefix(EXPORT_FORMATS, [rest[0]])
            if len(rest) == 2 and not trailing_space:
                return with_prefix(_filter_prefix(EXPORT_FORMATS, arg), [rest[0]])
        if sub == "list" and len(rest) == 1 and trailing_space:
            return [f"{cmd} list 20", f"{cmd} list 50"]
        return []

    # /sessions [list|search]
    if cmd_cf == "/sessions":
        subs = ["list", "search"]
        if not rest and trailing_space:
            return with_prefix(subs, [])
        if len(rest) == 1 and not trailing_space:
            return with_prefix(_filter_prefix(subs, rest[0]), [])
        if rest and rest[0].casefold() == "search":
            titles = ctx.session_titles
            if len(rest) == 1 and trailing_space:
                return [f"{cmd} search {t}" for t in titles[:10]]
            if len(rest) >= 2 and not trailing_space:
                partial = " ".join(rest[1:]).casefold()
                return [
                    f"{cmd} search {t}"
                    for t in titles
                    if t.casefold().startswith(partial) or partial in t.casefold()
                ][:10]
        return []

    # /switch <thread_id|title>
    if cmd_cf == "/switch":
        sessions = _sessions_from_ctx(ctx)
        if not rest and trailing_space:
            return _session_complete_lines(cmd, [], sessions, partial="")
        if rest and not trailing_space:
            return _session_complete_lines(cmd, [], sessions, partial=" ".join(rest))
        return []

    # /export [md|json]
    if cmd_cf == "/export":
        if not rest and trailing_space:
            return with_prefix(EXPORT_FORMATS, [])
        if len(rest) == 1 and not trailing_space:
            return with_prefix(_filter_prefix(EXPORT_FORMATS, rest[0]), [])
        return []

    # /mcp <sub>
    if cmd_cf == "/mcp":
        if not rest and trailing_space:
            return with_prefix(MCP_SUBCOMMANDS, [])
        if len(rest) == 1 and not trailing_space:
            return with_prefix(_filter_prefix(MCP_SUBCOMMANDS, rest[0]), [])
        return []

    # /model <alias> [thinking-level] | /model thinking <level>
    if cmd_cf == "/model":
        names = list(ctx.model_names) + ["thinking", "effort"]
        levels = ["off", "minimal", "low", "medium", "high", "max"]
        if not rest and trailing_space:
            return with_prefix(names, [])
        if len(rest) == 1 and not trailing_space:
            return with_prefix(_filter_prefix(names, rest[0]), [])
        if len(rest) == 1 and trailing_space:
            return with_prefix(levels + ["thinking"], [rest[0]])
        if len(rest) == 2 and not trailing_space:
            return with_prefix(_filter_prefix(levels + ["thinking"], rest[1]), [rest[0]])
        if rest and rest[0].casefold() in {"thinking", "effort"}:
            if len(rest) == 1 and trailing_space:
                return with_prefix(levels, [rest[0]])
            if len(rest) == 2 and not trailing_space:
                return with_prefix(_filter_prefix(levels, rest[1]), [rest[0]])
        if (
            len(rest) >= 2
            and rest[1].casefold() == "thinking"
            and len(rest) == 2
            and trailing_space
        ):
            return with_prefix(levels, rest[:2])
        if len(rest) == 3 and rest[1].casefold() == "thinking" and not trailing_space:
            return with_prefix(_filter_prefix(levels, rest[2]), rest[:2])
        return []

    # /theme [list|<name>]
    if cmd_cf == "/theme":
        try:
            from synapse.ui.theme import list_theme_names

            theme_names = list(list_theme_names())
        except Exception:  # noqa: BLE001
            theme_names = []
        options = ["list", "ls", *theme_names]
        if not rest and trailing_space:
            return with_prefix(options, [])
        if len(rest) == 1 and not trailing_space:
            return with_prefix(_filter_prefix(options, rest[0]), [])
        return []

    # /safety [profile]
    if cmd_cf == "/safety":
        profiles = ["dev-autopass", "dev-approve", "readonly", "hitl", "auto", "ro"]
        if not rest and trailing_space:
            return with_prefix(profiles, [])
        if len(rest) == 1 and not trailing_space:
            return with_prefix(_filter_prefix(profiles, rest[0]), [])
        return []

    # /rename <title>
    if cmd_cf == "/rename":
        titles = ctx.session_titles
        if not rest and trailing_space:
            return with_prefix(titles[:10], [])
        partial = " ".join(rest)
        if rest and not trailing_space:
            return [
                f"{cmd} {t}"
                for t in titles
                if t.casefold().startswith(partial.casefold())
            ][:10]
        return []

    return []


def best_completion(value: str, ctx: SlashCompleteContext | None = None) -> str | None:
    cands = complete_slash(value, ctx)
    if not cands:
        return None
    return cands[0]


def cycle_completion(
    value: str,
    current: str | None,
    ctx: SlashCompleteContext | None = None,
) -> str | None:
    """Return next candidate after current (wrap). If current not in list, first."""
    cands = complete_slash(value, ctx)

    if value in cands or (len(cands) == 1 and cands[0] == value):
        if " " in value.rstrip():
            parent = value.rstrip().rsplit(" ", 1)[0] + " "
            siblings = complete_slash(parent, ctx)
            if siblings:
                cands = siblings
        else:
            prefix = value[:2] if value.startswith("/") else value
            siblings = complete_slash(prefix, ctx)
            if siblings:
                cands = siblings

    if not cands:
        if " " in value.rstrip():
            parent = value.rstrip().rsplit(" ", 1)[0] + " "
            cands = complete_slash(parent, ctx)
        if not cands:
            return None

    pivot = current if current in cands else (value if value in cands else None)
    if pivot is not None:
        idx = cands.index(pivot)
        return cands[(idx + 1) % len(cands)]
    return cands[0]


def format_completion_hint(
    value: str,
    ctx: SlashCompleteContext | None = None,
    *,
    limit: int = 8,
) -> str:
    ctx = ctx or SlashCompleteContext()
    cands = complete_slash(value, ctx)
    if not cands:
        return ""

    id_to_label = {s.thread_id: s.label() for s in _sessions_from_ctx(ctx)}
    shown = cands[:limit]
    tails: list[str] = []
    for c in shown:
        token = c.rsplit(" ", 1)[-1] if " " in c else c
        if token in id_to_label and (
            c.startswith("/switch ")
            or " switch " in c
            or " delete " in c
            or " show " in c
        ):
            tails.append(id_to_label[token])
            continue
        if c.casefold().startswith(value.casefold()) and len(c) > len(value):
            tails.append(c[len(value) :])
        else:
            tails.append(c)
    extra = f" +{len(cands) - limit}" if len(cands) > limit else ""
    return "tab: " + " | ".join(tails) + extra


def make_textual_suggester(
    context_provider: Callable[[], SlashCompleteContext],
    workspace: str | Path | None = None,
):
    """Build a Textual Suggester instance (lazy import).

    When *workspace* is provided the suggester also handles ``@``-prefixed
    file/directory path completion.
    """
    from textual.suggester import Suggester

    _ws = Path(workspace).resolve() if workspace else None

    class SlashSuggester(Suggester):
        def __init__(self) -> None:
            # No cache: session/model lists change at runtime.
            super().__init__(use_cache=False, case_sensitive=True)

        async def get_suggestion(self, value: str) -> str | None:
            if value.startswith("/"):
                return best_completion(value, context_provider())
            if _ws and "@" in value:
                return best_at_completion(value, _ws)
            return None

    return SlashSuggester()


# ---------------------------------------------------------------------------
# @ path completion — workspace-relative file/directory completions
# ---------------------------------------------------------------------------

_AT_HINT_LIMIT = 12
"""Max candidates shown in the completion hint bar."""


def _find_at(value: str) -> tuple[int, str] | None:
    """Find the last ``@`` and extract the path token after it.

    Returns ``(at_index, path_prefix)``, e.g. for ``"cat @src/main"``
    returns ``(4, "src/main")``.  Returns ``None`` when there is no ``@``.
    """
    idx = value.rfind("@")
    if idx < 0:
        return None
    # Everything after @ until end or next whitespace.
    rest = value[idx + 1 :]
    # Split on whitespace to get the path token.
    parts = rest.split(maxsplit=1)
    token = parts[0] if parts else ""
    return idx, token


def _at_path_prefix(token: str) -> str:
    """Normalise a user-typed path token to a relative ``Path`` string.

    Strips leading ``./`` and normalises separators.
    """
    t = token.replace("\\", "/").lstrip("/")
    if t.startswith("./"):
        t = t[2:]
    return t


_RECURSIVE_SCAN_LIMIT = 2000
"""Max entries scanned during recursive fallback."""

# Directories skipped during recursive search (common VCS / cache / env dirs).
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".venv",
        "venv",
        ".tox",
        "node_modules",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".hypothesis",
        ".eggs",
        "build",
        "dist",
        ".idea",
        ".vscode",
    }
)


def _glob_at_candidates(
    token: str,
    workspace: Path,
    *,
    limit: int = 100,
) -> list[str]:
    """Return matching relative path suffixes under *workspace*.

    Results are relative to *workspace* so they can be plugged directly into
    the ``@prefix`` slot.  At most *limit* candidates are returned to avoid
    excessive filesystem scanning.

    When *token* ends with ``/`` (or ``\\``) the token is treated as a
    directory and its immediate children are listed, so the user can drill
    down without remembering exact file names.

    When the user types a bare prefix (no directory separators) and direct
    children produce few results, the function falls back to a shallow
    recursive scan so that ``@syn`` can match ``src/synapse/`` without
    requiring the user to type every directory level.
    """
    trailing_slash = token.endswith("/") or token.endswith("\\")
    prefix = _at_path_prefix(token)

    if prefix:
        parent_path = Path(prefix)
        if trailing_slash:
            # Directory mode — list the children of this directory.
            parent_part = parent_path.as_posix().rstrip("/")
            base_part = ""
            ls_dir = (workspace / parent_path).resolve()
        else:
            # File/partial mode — list parent dir, filter by base name.
            parent_part = str(parent_path.parent).replace("\\", "/")
            if parent_part == ".":
                parent_part = ""
            base_part = parent_path.name
            ls_dir = (workspace / parent_path.parent).resolve() if parent_part else workspace.resolve()
    else:
        parent_part = ""
        base_part = ""
        ls_dir = workspace.resolve()

    if not ls_dir.is_dir():
        return []

    seen: set[str] = set()
    candidates: list[str] = []

    # -- direct children first (fast path) --
    try:
        all_entries: list[Path] = []
        for i, ent in enumerate(ls_dir.iterdir()):
            if i >= 500:
                break
            all_entries.append(ent)
        entries = sorted(all_entries, key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError:
        entries = []

    for ent in entries:
        if base_part and not ent.name.lower().startswith(base_part.lower()):
            continue
        suffix = ent.name + ("/" if ent.is_dir() else "")
        rel = f"{parent_part}/{suffix}" if parent_part else suffix
        rel = rel.replace("\\", "/")
        if rel.startswith("./"):
            rel = rel[2:]
        if rel not in seen:
            seen.add(rel)
            candidates.append(rel)
            if len(candidates) >= limit:
                return candidates

    # -- recursive fallback: search subdirectories for prefix matches --
    # Only when the user typed a bare prefix (no explicit parent dir) and is
    # not in directory-browsing mode.
    if trailing_slash:
        return candidates
    if not base_part:
        return candidates
    if len(candidates) >= limit // 2:
        return candidates

    try:
        workspace_resolved = workspace.resolve()
        pattern = f"**/{base_part}*"
        scanned = 0
        for p in workspace_resolved.rglob(pattern):
            if scanned >= _RECURSIVE_SCAN_LIMIT:
                break
            scanned += 1
            # Skip hidden / common-ignore dirs anywhere in the path.
            if any(part in _SKIP_DIRS for part in p.parts):
                continue
            try:
                rel = p.relative_to(workspace_resolved).as_posix()
            except ValueError:
                continue
            # Skip hidden entries and entries inside hidden dirs.
            if rel.startswith(".") or "/." in rel:
                continue
            suffix = "/" if p.is_dir() else ""
            key = rel + suffix
            if key in seen:
                continue
            seen.add(key)
            candidates.append(key)
            if len(candidates) >= limit:
                break
    except PermissionError:
        pass

    # Sort: directories first, then by path.
    candidates.sort(key=lambda c: (not c.endswith("/"), c.lower()))
    return candidates[:limit]


def complete_at_line(value: str, workspace: Path) -> list[str]:
    """Return full-line completion candidates for an ``@`` path reference.

    Each candidate is the original *value* with the ``@token`` portion
    replaced by the matched path.
    """
    found = _find_at(value)
    if found is None:
        return []
    at_idx, token = found
    cands = _glob_at_candidates(token, workspace)
    if not cands:
        return []
    prefix = value[:at_idx]  # everything before @
    # Build full-line replacement for every candidate.
    result: list[str] = []
    for c in cands:
        result.append(f"{prefix}@{c}")
    return result


def best_at_completion(value: str, workspace: Path) -> str | None:
    """Return the first match for ``@`` path completion, or None."""
    cands = complete_at_line(value, workspace)
    return cands[0] if cands else None


def cycle_at_completion(
    value: str,
    current: str | None,
    workspace: Path,
) -> str | None:
    """Return next candidate after *current* (wraps around).

    When *current* is not in the candidate list, return the first candidate.
    """
    cands = complete_at_line(value, workspace)
    if not cands:
        return None
    if current in cands:
        idx = cands.index(current)
        return cands[(idx + 1) % len(cands)]
    return cands[0]


def format_at_hint(value: str, workspace: Path, *, limit: int = _AT_HINT_LIMIT) -> str:
    """Return a short hint string for the completion bar."""
    cands = complete_at_line(value, workspace)
    if not cands:
        return ""
    shown = cands[:limit]
    # Extract only the tail after @ for compact display.
    at_idx, _ = _find_at(value)
    tails: list[str] = []
    for c in shown:
        tail = c[at_idx:] if at_idx is not None else c
        tails.append(tail)
    extra = f" +{len(cands) - limit}" if len(cands) > limit else ""
    return "tab: " + " | ".join(tails) + extra
