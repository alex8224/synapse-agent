"""Shared parsing and presentation helpers for slash-command handlers."""

from __future__ import annotations


def parts(text: str) -> list[str]:
    return text.strip().split()


def format_bytes(value: int | float) -> str:
    """Format byte counts compactly for command tables."""
    amount = max(0, int(value or 0))
    for unit, size in (("G", 1024**3), ("M", 1024**2), ("K", 1024)):
        if amount >= size:
            rendered = amount / size
            return f"{rendered:.1f}{unit}" if rendered < 10 else f"{rendered:.0f}{unit}"
    return f"{amount}B"


def markdown_escape(text: str) -> str:
    """Escape pipe and backtick for Markdown table cells."""
    return str(text).replace("|", "\\|").replace("`", "\\`")
