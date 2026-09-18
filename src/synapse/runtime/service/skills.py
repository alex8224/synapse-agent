"""Pure skill-enumeration DTOs for the Agent Runtime Service (v1 additive).

This module belongs to the contract layer: it declares frozen dataclasses
and bounded limits. It imports no catalog, transport, settings, UI,
or session-execution module, so the wire decoder, the in-process service,
and the daemon composition root can all depend on it without introducing a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "MAX_PROJECT_ID_BYTES",
    "ListSkillsQuery",
    "SkillEntry",
    "SkillListPage",
]

#: An optional project_id is bounded before it is resolved.
MAX_PROJECT_ID_BYTES = 128


@dataclass(frozen=True, slots=True)
class ListSkillsQuery:
    """Enumerate discoverable Agent Skills.

    ``project_id`` is optional: when present, skills from that project's
    configured ``skills_paths`` are discovered; when null, the default repository
    or host skills paths are searched.
    """

    project_id: str | None = None

    def __post_init__(self) -> None:
        if self.project_id is not None:
            if type(self.project_id) is not str:
                raise ValueError("project_id must be a string or null")
            text = self.project_id.strip()
            if not text or "\x00" in text:
                raise ValueError("project_id must be a non-empty string without NUL")
            if len(text.encode("utf-8")) > MAX_PROJECT_ID_BYTES:
                raise ValueError("project_id exceeds the size limit")
            object.__setattr__(self, "project_id", text)


@dataclass(frozen=True, slots=True)
class SkillEntry:
    """One discoverable Agent Skill with its metadata."""

    name: str
    description: str
    path: str
    source: str


@dataclass(frozen=True, slots=True)
class SkillListPage:
    """Bounded collection of discoverable Agent Skills."""

    skills: tuple[SkillEntry, ...]
