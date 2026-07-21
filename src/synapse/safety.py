"""Command safety helpers and named safety profiles.

Default product behavior: **no approval gate**, auto-pass (dev-autopass).
Profiles make the product posture explicit:

- dev-autopass: full tools, no HITL (default)
- dev-approve: HITL on execute/write/edit
- readonly: exclude write/execute tools via harness
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

SafetyProfileName = Literal["dev-autopass", "dev-approve", "readonly"]

# Patterns considered dangerous on developer machines.
DANGEROUS_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\b", re.I),
    re.compile(r"\brm\s+-rf\s+/", re.I),
    re.compile(r"\b(del|rmdir)\s+/s\b", re.I),
    re.compile(r"\bformat\s+[a-z]:", re.I),
    re.compile(r"\bmkfs\b", re.I),
    re.compile(r"\bdd\s+if=", re.I),
    re.compile(r":\s*\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;", re.I),  # fork bomb
    re.compile(r"\bshutdown\b", re.I),
    re.compile(r"\breboot\b", re.I),
    re.compile(r"\bgit\s+push\s+.*--force\b", re.I),
    re.compile(r"\bgit\s+reset\s+--hard\b", re.I),
    re.compile(r"\bgit\s+clean\s+-fdx\b", re.I),
    re.compile(r">\s*/dev/sd[a-z]", re.I),
    re.compile(r"\bRemove-Item\b.*-Recurse\b.*-Force\b", re.I),
]


@dataclass(frozen=True)
class SafetyVerdict:
    allowed: bool
    reason: str | None = None
    matched_pattern: str | None = None


@dataclass(frozen=True)
class SafetyProfile:
    """Named product posture for approval / readonly / blacklist."""

    name: SafetyProfileName
    require_approval: bool
    readonly: bool
    auto_approve: bool
    enable_command_blacklist: bool
    description: str


SAFETY_PROFILES: dict[str, SafetyProfile] = {
    "dev-autopass": SafetyProfile(
        name="dev-autopass",
        require_approval=False,
        readonly=False,
        auto_approve=True,
        enable_command_blacklist=True,
        description="Local dev default: full tools, no HITL, blacklist advisory.",
    ),
    "dev-approve": SafetyProfile(
        name="dev-approve",
        require_approval=True,
        readonly=False,
        auto_approve=False,
        enable_command_blacklist=True,
        description="Local dev with HITL on execute/write/edit.",
    ),
    "readonly": SafetyProfile(
        name="readonly",
        require_approval=False,
        readonly=True,
        auto_approve=True,
        enable_command_blacklist=True,
        description="Read-only: exclude write_file/edit_file/execute via harness.",
    ),
}


def get_safety_profile(name: str | None) -> SafetyProfile:
    key = (name or "dev-autopass").strip().casefold()
    if key in SAFETY_PROFILES:
        return SAFETY_PROFILES[key]
    # aliases
    if key in {"approve", "hitl", "dev_approve"}:
        return SAFETY_PROFILES["dev-approve"]
    if key in {"ro", "read-only", "read_only"}:
        return SAFETY_PROFILES["readonly"]
    if key in {"auto", "autopass", "dev_autopass", "off"}:
        return SAFETY_PROFILES["dev-autopass"]
    return SAFETY_PROFILES["dev-autopass"]


def apply_safety_to_settings(settings: Any, profile: SafetyProfile | str) -> list[str]:
    """Mutate settings fields from a named profile. Returns change notes."""
    prof = get_safety_profile(profile if isinstance(profile, str) else profile.name)
    notes: list[str] = []
    mapping = {
        "require_approval": prof.require_approval,
        "auto_approve": prof.auto_approve,
        "readonly": prof.readonly,
        "enable_command_blacklist": prof.enable_command_blacklist,
        "safety_profile": prof.name,
    }
    for key, val in mapping.items():
        if not hasattr(settings, key):
            continue
        old = getattr(settings, key)
        if old != val:
            try:
                setattr(settings, key, val)
            except Exception:  # noqa: BLE001
                # pydantic Settings may be frozen-ish; try model_copy pattern
                pass
            else:
                notes.append(f"{key}: {old!r} -> {val!r}")
    # pydantic BaseSettings often allows setattr on instance
    if hasattr(settings, "model_copy") and not notes:
        # try replace via object.__setattr__ for model fields
        for key, val in mapping.items():
            if hasattr(settings, key) and getattr(settings, key) != val:
                object.__setattr__(settings, key, val)
                notes.append(f"{key} -> {val!r}")
    if not notes:
        notes.append(f"profile already active: {prof.name}")
    else:
        notes.insert(0, f"safety profile: {prof.name}")
        notes.append(prof.description)
    return notes


def check_command(command: str, *, enable_blacklist: bool = True) -> SafetyVerdict:
    """Return whether a shell command is considered safe enough to run."""
    if not enable_blacklist:
        return SafetyVerdict(allowed=True)

    text = command.strip()
    if not text:
        return SafetyVerdict(allowed=False, reason="empty command")

    for pattern in DANGEROUS_PATTERNS:
        if pattern.search(text):
            return SafetyVerdict(
                allowed=False,
                reason="command matches dangerous pattern",
                matched_pattern=pattern.pattern,
            )
    return SafetyVerdict(allowed=True)


def build_interrupt_on(*, require_approval: bool) -> dict[str, bool] | None:
    """Map product settings to deepagents `interrupt_on`.

    Default: require_approval=False => no interrupt middleware.
    """
    if not require_approval:
        return None
    return {
        "execute": True,
        "write_file": True,
        "edit_file": True,
    }


def format_safety_status(settings: Any) -> list[str]:
    profile = getattr(settings, "safety_profile", None) or (
        "dev-approve"
        if getattr(settings, "require_approval", False)
        else ("readonly" if getattr(settings, "readonly", False) else "dev-autopass")
    )
    return [
        f"safety profile: {profile}",
        f"  require_approval: {getattr(settings, 'require_approval', False)}",
        f"  auto_approve: {getattr(settings, 'auto_approve', True)}",
        f"  readonly: {getattr(settings, 'readonly', False)}",
        f"  command_blacklist: {getattr(settings, 'enable_command_blacklist', True)}",
        "profiles: dev-autopass | dev-approve | readonly",
        "switch: /safety <profile>   (rebuilds agent)",
    ]
