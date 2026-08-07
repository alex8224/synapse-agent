"""Cross-project session identity and strict resolution.

``SessionRef(project_id, thread_id)`` is the routing key for commands and
events once RuntimeManager spans multiple projects (P6).  ``project_id`` is a
stable per-workspace identity (see ``synapse.projects.catalog``), so a moved
directory keeps its sessions; ``thread_id`` is the project-local session id.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class SessionRef:
    """Stable global reference to one session in one project."""

    project_id: str
    thread_id: str

    @property
    def global_id(self) -> str:
        return f"{self.project_id}:{self.thread_id}"

    def __str__(self) -> str:
        return self.global_id


class CatalogLike(Protocol):
    """Minimal surface needed by the resolver (see ProjectCatalog)."""

    def resolve_project(self, ref: str) -> Any | None: ...
    def resolve_session(self, project_id: str, ref: str) -> Any | None: ...


class SessionResolutionError(ValueError):
    """Raised when a global reference cannot be resolved uniquely."""


def parse_global_id(value: str) -> SessionRef:
    """Parse ``project_id:thread_id``.

    Splits on the first ``:`` because ``project_id`` is a stable UUID while a
    thread id may itself contain colons.
    """
    if not isinstance(value, str) or not value.strip():
        raise SessionResolutionError(f"empty session reference: {value!r}")
    text = value.strip()
    if ":" not in text:
        raise SessionResolutionError(
            f"global session reference must be '<project_id>:<thread_id>', got {value!r}"
        )
    project, thread = text.split(":", 1)
    project = project.strip()
    thread = thread.strip()
    if not project or not thread:
        raise SessionResolutionError(f"invalid global session reference: {value!r}")
    return SessionRef(project_id=project, thread_id=thread)


def resolve_session_ref(
    value: str,
    *,
    catalog: CatalogLike,
    verify: bool = False,
) -> SessionRef:
    """Resolve a user-supplied global reference with strict ambiguity handling.

    - A full ``project_id:thread_id`` resolves directly.
    - A unique project prefix + thread ref resolves when unambiguous.
    - Ambiguous project or thread matches raise ``SessionResolutionError``.
    - When ``verify=True`` the catalog hit is re-checked against project-local
      data before the ref is returned (best-effort; a missing local row is an
      error only if the catalog row itself is stale).
    """
    if ":" in value:
        ref = parse_global_id(value)
        return _finalize(ref, catalog=catalog, verify=verify)
    # Single token: must be a unique project prefix; no thread is given.
    matches = _match_projects(value, catalog)
    if len(matches) == 1:
        # Allow the shorthand only for lookups that do not require a thread.
        return SessionRef(project_id=matches[0], thread_id="")
    if not matches:
        raise SessionResolutionError(f"no project matches {value!r}")
    raise SessionResolutionError(
        f"ambiguous project reference {value!r}: matches {len(matches)} projects"
    )


def _match_projects(value: str, catalog: CatalogLike) -> list[str]:
    """Return project ids whose id or name starts with ``value``."""
    try:
        items = catalog.list_projects(limit=500)  # type: ignore[attr-defined]
    except AttributeError:
        items = []
    ids: list[str] = []
    lowered = value.casefold()
    for item in items:
        project_id = getattr(item, "project_id", "")
        name = getattr(item, "name", "") or ""
        if (
            project_id.casefold().startswith(lowered)
            or name.casefold().startswith(lowered)
        ):
            ids.append(project_id)
    return ids


def _finalize(ref: SessionRef, *, catalog: CatalogLike, verify: bool) -> SessionRef:
    if ref.project_id.startswith(":") or " " in ref.project_id:
        raise SessionResolutionError(f"invalid project id: {ref.project_id!r}")
    project = _resolve_one_project(ref.project_id, catalog)
    if project is None:
        raise SessionResolutionError(f"unknown project: {ref.project_id}")
    canonical = SessionRef(project_id=project.project_id, thread_id=ref.thread_id)
    if verify and ref.thread_id:
        row = _verify_thread(canonical, catalog)
        if row is None:
            raise SessionResolutionError(
                f"session {ref.thread_id!r} not found in project "
                f"{canonical.project_id}"
            )
    return canonical


def _resolve_one_project(prefix: str, catalog: CatalogLike) -> Any | None:
    try:
        items = catalog.list_projects(limit=500)  # type: ignore[attr-defined]
    except AttributeError:
        items = []
    lowered = prefix.casefold()
    exact = [i for i in items if getattr(i, "project_id", "") == prefix]
    if exact:
        return exact[0]
    matches = [
        i
        for i in items
        if getattr(i, "project_id", "").casefold().startswith(lowered)
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def _verify_thread(ref: SessionRef, catalog: CatalogLike) -> Any | None:
    try:
        rows = catalog.list_sessions(project_id=ref.project_id, limit=500)  # type: ignore[attr-defined]
    except AttributeError:
        return None
    for row in rows:
        if getattr(row, "thread_id", "") == ref.thread_id:
            return row
    return None
