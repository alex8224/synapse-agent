"""Pure project-enumeration DTOs for the Agent Runtime Service (v1 additive).

This module belongs to the contract layer: it declares frozen dataclasses, the
bounded pagination constants, and one read-only provider port.  It imports no
catalog, transport, settings, UI, or session-execution module, so the wire
decoder, the in-process service, and the daemon composition root can all depend
on it without introducing a cycle.

Visibility is *not* a client input.  ``ListProjectsQuery.visible_project_ids``
is computed server-side (the principal's ACL visibility intersected with the
trusted connection scope) and is rejected by the wire decoder, so a client can
never widen the set of projects it is allowed to enumerate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

__all__ = [
    "ListProjectsQuery",
    "PROJECT_LIST_LIMIT_DEFAULT",
    "PROJECT_LIST_LIMIT_MAX",
    "PROJECT_LIST_LIMIT_MIN",
    "PROJECT_LIST_OFFSET_MAX",
    "PROJECT_LIST_VISIBILITY_MAX",
    "ProjectListItem",
    "ProjectListPage",
    "ProjectListProvider",
]

PROJECT_LIST_LIMIT_MIN = 1
PROJECT_LIST_LIMIT_MAX = 100
PROJECT_LIST_LIMIT_DEFAULT = 50
PROJECT_LIST_OFFSET_MAX = 100_000
#: Upper bound of one provider scan.  The user-layer project catalog is itself
#: capped (``synapse.projects.catalog`` caps at 500 rows), so one bounded read
#: yields an exact visible total instead of an unbounded enumeration.
PROJECT_LIST_VISIBILITY_MAX = 500


def _validate_int(value: object, *, name: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer, got {type(value).__name__!r}")
    if not (minimum <= value <= maximum):
        raise ValueError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


@dataclass(frozen=True, slots=True)
class ListProjectsQuery:
    """Bounded project-enumeration request with a server-computed visibility set.

    ``visible_project_ids`` is empty for "no restriction" and otherwise carries
    the exact project ids the caller may see.  It is filled in by the ACL/scope
    layer, never by the wire decoder (``transport.protocol.decode_params`` rejects
    an explicit value), and providers must apply it *before* paginating so
    ``limit`` / ``offset`` always describe the caller's own visible slice.
    """

    limit: int = PROJECT_LIST_LIMIT_DEFAULT
    offset: int = 0
    visible_project_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "limit",
            _validate_int(
                self.limit,
                name="limit",
                minimum=PROJECT_LIST_LIMIT_MIN,
                maximum=PROJECT_LIST_LIMIT_MAX,
            ),
        )
        object.__setattr__(
            self,
            "offset",
            _validate_int(
                self.offset,
                name="offset",
                minimum=0,
                maximum=PROJECT_LIST_OFFSET_MAX,
            ),
        )
        try:
            visible = tuple(self.visible_project_ids)
        except TypeError:
            raise ValueError("visible_project_ids must be an iterable of strings") from None
        if len(visible) > PROJECT_LIST_VISIBILITY_MAX:
            raise ValueError("visible_project_ids exceeds the visibility limit")
        seen: set[str] = set()
        for project_id in visible:
            if type(project_id) is not str or not project_id or "\x00" in project_id:
                raise ValueError("visible_project_ids must contain non-empty strings")
            seen.add(project_id)
        object.__setattr__(self, "visible_project_ids", tuple(sorted(seen)))


@dataclass(frozen=True, slots=True)
class ProjectListItem:
    """One registered project, projected to non-secret identity fields.

    ``workspace_path`` is included because the loopback console already exposes
    it for the console's own project (``GET /api/session``) and for the
    deprecated switchable list; it is never a token, a credential, or a
    file-level secret.
    """

    project_id: str
    workspace_name: str | None
    git_branch: str | None
    workspace_path: str


@dataclass(frozen=True, slots=True)
class ProjectListPage:
    """One bounded page of project identity items with a continuation offset."""

    projects: tuple[ProjectListItem, ...]
    next_offset: int | None
    total: int


class ProjectListProvider(Protocol):
    """Read-only project enumeration.

    Implementations read a bounded registered-project source, apply
    ``query.visible_project_ids`` first, and only then slice ``offset`` /
    ``limit``.  The provider never opens a session, builds an agent, or
    registers a project.
    """

    def __call__(self, query: ListProjectsQuery) -> ProjectListPage: ...
