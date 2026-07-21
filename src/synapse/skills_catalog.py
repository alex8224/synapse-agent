"""Discover local Agent Skills and memory files for /skills /memory."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)


@dataclass
class SkillInfo:
    name: str
    description: str = ""
    path: str = ""
    source: str = ""


def _parse_frontmatter(text: str) -> dict[str, str]:
    m = _FRONT_MATTER_RE.match(text or "")
    if not m:
        return {}
    data: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        data[key.strip().casefold()] = val.strip().strip("\"'")
    return data


def discover_skills(skills_paths: list[str] | None) -> list[SkillInfo]:
    """Scan skills directories for SKILL.md files."""
    found: list[SkillInfo] = []
    seen: set[str] = set()
    for raw in skills_paths or []:
        root = Path(raw).expanduser()
        if not root.exists():
            continue
        if root.is_file() and root.name.upper() == "SKILL.MD":
            candidates = [root]
            source = str(root.parent)
        else:
            candidates = sorted(root.rglob("SKILL.md"))
            source = str(root.resolve())
        for skill_md in candidates:
            try:
                text = skill_md.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001
                continue
            meta = _parse_frontmatter(text)
            name = meta.get("name") or skill_md.parent.name
            desc = meta.get("description") or ""
            key = str(skill_md.resolve())
            if key in seen:
                continue
            seen.add(key)
            found.append(
                SkillInfo(
                    name=name,
                    description=desc,
                    path=str(skill_md.resolve()),
                    source=source,
                )
            )
    return found


def format_skills_lines(skills: list[SkillInfo]) -> list[str]:
    if not skills:
        return ["skills: (none found)", "tip: put SKILL.md under skills/<name>/"]
    lines = [f"skills: {len(skills)}"]
    for s in skills:
        desc = s.description
        if len(desc) > 70:
            desc = desc[:69] + "…"
        lines.append(f"  - {s.name}: {desc or '(no description)'}")
        lines.append(f"    {s.path}")
    return lines


def list_memory_files(memory_paths: list[str] | None) -> list[tuple[str, bool, int]]:
    """Return (path, exists, size_bytes)."""
    out: list[tuple[str, bool, int]] = []
    for raw in memory_paths or []:
        p = Path(raw).expanduser()
        if p.exists() and p.is_file():
            try:
                size = p.stat().st_size
            except Exception:  # noqa: BLE001
                size = 0
            out.append((str(p.resolve()), True, size))
        else:
            out.append((str(p), False, 0))
    return out


def format_memory_lines(entries: list[tuple[str, bool, int]]) -> list[str]:
    if not entries:
        return ["memory: (no paths configured)"]
    lines = [f"memory files: {len(entries)}"]
    for path, exists, size in entries:
        if exists:
            lines.append(f"  - {path}  ({size} bytes)")
        else:
            lines.append(f"  - {path}  (missing)")
    lines.append("note: existing files are injected via create_deep_agent(memory=...)")
    return lines


def skills_paths_from_settings(settings: Any, project_root: Path | None = None) -> list[str]:
    root = project_root or Path.cwd()
    fn = getattr(settings, "resolved_skills_paths", None)
    if callable(fn):
        return list(fn(root) or [])
    raw = getattr(settings, "skills_paths", None) or []
    return [str((root / p).resolve()) if not Path(p).is_absolute() else p for p in raw]


def memory_paths_from_settings(settings: Any, project_root: Path | None = None) -> list[str]:
    root = project_root or Path.cwd()
    fn = getattr(settings, "resolved_memory_paths", None)
    if callable(fn):
        return list(fn(root) or [])
    raw = getattr(settings, "memory_paths", None) or []
    return [str((root / p).resolve()) if not Path(p).is_absolute() else p for p in raw]
