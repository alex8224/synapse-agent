"""Compatibility launcher for ``synapse.web``.

Usage:
    uv run --no-sync python scripts/serve_web.py [--host HOST] [--port PORT]
"""

from __future__ import annotations

from synapse.web import main

if __name__ == "__main__":
    main()
