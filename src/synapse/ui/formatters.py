"""Pure formatting helpers for TUI chrome and transcript previews."""

from __future__ import annotations

import re
from pathlib import Path

_FINISHED_RE = re.compile(r"^finished in ([\d.]+)s\b", re.I)


def format_answer_divider(
    width: int,
    *,
    diamond: str = "◇",
    rule_ratio: float = 0.80,
) -> list[str]:
    """Centered thin rule with a diamond between tools and final answer."""
    usable = max(28, min(int(width or 56), 200))
    gem = diamond or "◇"
    ratio = min(0.95, max(0.3, float(rule_ratio or 0.80)))
    rule_len = max(21, int(usable * ratio))
    if (rule_len - len(gem)) % 2:
        rule_len += 1
    side = max(4, (rule_len - len(gem)) // 2)
    rule = ("─" * side) + gem + ("─" * side)
    pad = max(0, (usable - len(rule)) // 2)
    line = (" " * pad) + rule
    trail = max(0, usable - len(line))
    if trail:
        line += " " * trail
    return ["", line, ""]


def format_token_count(n: int) -> str:
    """Compact token count for chrome (14K, 1.2M)."""
    n = max(0, int(n or 0))
    if n < 1000:
        return str(n)
    if n < 10_000:
        s = f"{n / 1000:.1f}K"
        return s.replace(".0K", "K")
    if n < 1_000_000:
        return f"{(n + 500) // 1000}K"
    if n < 10_000_000:
        s = f"{n / 1_000_000:.1f}M"
        return s.replace(".0M", "M")
    return f"{(n + 500_000) // 1_000_000}M"


def format_byte_count(n: int) -> str:
    """Compact binary byte count with an explicit unit."""
    size = max(0, int(n or 0))
    for unit, divisor in (("MiB", 1024**2), ("KiB", 1024)):
        if size >= divisor:
            value = size / divisor
            rendered = f"{value:.1f}" if value < 10 else f"{value:.0f}"
            return f"{rendered.rstrip('0').rstrip('.')} {unit}"
    return f"{size} B"


def format_usage_label(
    *,
    input_tokens: int = 0,
    cache_tokens: int = 0,
    output_tokens: int = 0,
) -> str:
    """Token chrome as compact ``in/cache/out`` counts."""
    return (
        f"{format_token_count(input_tokens)}/"
        f"{format_token_count(cache_tokens)}/"
        f"{format_token_count(output_tokens)}"
    )


def format_context_occupancy_label(
    *,
    last_input_tokens: int = 0,
    context_window: int | None = None,
) -> str:
    """Format the latest model-call context fill as tokens and an optional ratio."""
    used = max(0, int(last_input_tokens or 0))
    if used <= 0:
        return ""
    used_s = format_token_count(used)
    try:
        window = int(context_window) if context_window is not None else None
    except (TypeError, ValueError):
        window = None
    if window is not None and window > 0:
        pct = min(999, int(round(100.0 * used / window)))
        return f"{used_s}/{pct}%"
    return used_s


def format_mcp_status_label(
    *,
    enabled: bool,
    servers: list[str] | None = None,
    tools: list[str] | None = None,
    warnings: list[str] | None = None,
    deferred: bool = False,
) -> str:
    """Format MCP status as ``mcp on``, ``mcp off`` or ``mcp err``."""
    if not enabled:
        return "mcp off"
    servers = list(servers or [])
    tools = list(tools or [])
    warnings = list(warnings or [])
    server_count = len(servers)
    tool_count = len(tools)
    if warnings and server_count == 0:
        if deferred:
            return "mcp off"
        return "mcp err"
    if server_count == 0 and tool_count == 0:
        return "mcp off"
    return "mcp on"


def short_model_name(model: str) -> str:
    from synapse.models.registry import short_model_id

    return short_model_id(model)


def model_status_label(settings: object) -> str:
    """Idle status / subtitle for the configured model."""
    from synapse.models.registry import format_model_status

    return format_model_status(settings)


def short_workspace_label(path: Path | str, *, max_len: int = 42) -> str:
    """Prefer the last two path segments and ellipsize long absolute paths."""
    pth = Path(path)
    parts = [part for part in pth.parts if part not in {"/", "\\"}]
    if len(parts) >= 2:
        label = f"{parts[-2]}/{parts[-1]}"
    else:
        label = pth.name or str(pth)
    if len(label) <= max_len:
        return label
    return "…" + label[-(max_len - 1) :]


def soften_turn_footer(message: str) -> str:
    """Convert a CLI-style completion message to a soft transcript footer."""
    text = (message or "").strip()
    match = _FINISHED_RE.match(text)
    if match:
        return f"Worked for {match.group(1)}s."
    return text


def stream_tail_preview(
    body: str,
    *,
    max_lines: int = 28,
    max_chars: int = 3500,
) -> str:
    """Return only the newest tail of a growing answer for live preview."""
    if not body:
        return ""
    text = body
    truncated = False
    lines = text.splitlines()
    if len(lines) > max_lines:
        text = "\n".join(lines[-max_lines:])
        truncated = True
    if len(text) > max_chars:
        text = text[-max_chars:]
        nl = text.find("\n")
        if 0 <= nl < 120:
            text = text[nl + 1 :]
        truncated = True
    if truncated:
        return "…\n" + text.lstrip("\n")
    return text


__all__ = [
    "format_answer_divider",
    "format_byte_count",
    "format_context_occupancy_label",
    "format_mcp_status_label",
    "format_token_count",
    "format_usage_label",
    "model_status_label",
    "short_model_name",
    "short_workspace_label",
    "soften_turn_footer",
    "stream_tail_preview",
]
