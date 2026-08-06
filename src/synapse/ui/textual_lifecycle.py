"""Compatibility helpers for releasing detached Textual widget state."""

from __future__ import annotations


def clear_textual_style_cache_refs() -> None:
    """Release detached widgets retained by Textual's instance-keyed style LRU.

    Textual 8.2.8 decorates ``StylesCache.get_inner_outer`` with a process-wide
    LRU whose key contains the cache instance. Clear it after removing a complete
    widget tree so detached transcript/dialog widgets can become unreachable.
    """
    try:
        from textual._styles_cache import StylesCache

        cache_clear = getattr(StylesCache.get_inner_outer, "cache_clear", None)
        if callable(cache_clear):
            cache_clear()
    except Exception:  # noqa: BLE001 - private Textual compatibility boundary
        pass
