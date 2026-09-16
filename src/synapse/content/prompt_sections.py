"""Prompt section registry for the coding agent.

A *section* is the unit of prompt assembly. Each one carries a stable identity
(``name``/``source``) plus two rendering hints that request-time middleware can
act on:

* ``cache_hint`` - ``"stable"`` content is byte-identical across turns and may
  carry a prompt-cache breakpoint; ``"dynamic"`` content changes per turn.
* ``injection_target`` - ``"system"`` sections form the system message;
  ``"meta_user"`` sections are attached on the user side instead.

The model itself is deliberately content-free: it only describes and renders.
Content assembly lives in :mod:`synapse.content.prompts`.

Sections are normalized on construction (surrounding blank lines removed) and
``render_system_prompt`` joins them with a blank line, which reproduces the
historical single-string prompt byte for byte.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

SYSTEM_TARGET = "system"
META_USER_TARGET = "meta_user"
STABLE = "stable"
DYNAMIC = "dynamic"

#: Rendered sections are separated by exactly one blank line.
SECTION_SEPARATOR = "\n\n"

_CACHE_HINTS = frozenset({STABLE, DYNAMIC})
_INJECTION_TARGETS = frozenset({SYSTEM_TARGET, META_USER_TARGET})


def normalize_section_content(content: str) -> str:
    """Strip surrounding blank lines so separators stay exact."""
    return content.strip()


@dataclass(frozen=True)
class PromptSection:
    """One named block of prompt text with its rendering hints."""

    name: str
    source: str
    content: str
    cache_hint: str = STABLE
    injection_target: str = SYSTEM_TARGET

    def __post_init__(self) -> None:
        if self.cache_hint not in _CACHE_HINTS:
            raise ValueError(
                f"cache_hint must be one of {sorted(_CACHE_HINTS)}, got {self.cache_hint!r}"
            )
        if self.injection_target not in _INJECTION_TARGETS:
            raise ValueError(
                "injection_target must be one of "
                f"{sorted(_INJECTION_TARGETS)}, got {self.injection_target!r}"
            )
        normalized = normalize_section_content(self.content)
        if normalized != self.content:
            object.__setattr__(self, "content", normalized)

    @property
    def chars(self) -> int:
        """Character count of the normalized content."""
        return len(self.content)

    def as_dict(self) -> dict[str, object]:
        """Flatten the section for logging and diagnostics."""
        return {
            "name": self.name,
            "source": self.source,
            "injection_target": self.injection_target,
            "cache_hint": self.cache_hint,
            "chars": self.chars,
            "preview": self.content[:100],
        }


def _join_sections(sections: Iterable[PromptSection]) -> str:
    """Join non-empty section bodies with a blank line, without a terminator."""
    return SECTION_SEPARATOR.join(s.content for s in sections if s.content)


def render_system_prompt(sections: Iterable[PromptSection]) -> str:
    """Render sections into the final prompt string.

    Empty sections are skipped so an optional block that produced no content
    cannot introduce a stray blank line. The result is terminated by a single
    newline, matching the historical prompt's trailing newline.
    """
    body = _join_sections(sections)
    return f"{body}\n" if body else ""


def stable_prefix(sections: Sequence[PromptSection]) -> str:
    """Return the cacheable leading run of stable system sections.

    The result is always a literal prefix of :func:`render_system_prompt` for
    the same section list, including the separator that follows it, so a caller
    can split the rendered prompt at ``len(stable_prefix(sections))``.
    """
    ordered = [section for section in sections if section.content]
    count = 0
    for section in ordered:
        if section.injection_target != SYSTEM_TARGET or section.cache_hint != STABLE:
            break
        count += 1
    if count == 0:
        return ""
    prefix = _join_sections(ordered[:count])
    if not prefix:
        return ""
    if count < len(ordered):
        return prefix + SECTION_SEPARATOR
    return f"{prefix}\n"


def section_stats(sections: Iterable[PromptSection]) -> list[dict[str, object]]:
    """Return per-section diagnostics for logs and debugging surfaces."""
    return [section.as_dict() for section in sections]
