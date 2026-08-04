"""Serve the Synapse Textual TUI in a web browser via textual-serve.

The TUI keeps running as a server-side process; the browser renders it
through xterm.js over a WebSocket (same Python code, zero changes).

Usage:
    uv run --no-sync python scripts/serve_web.py [--host HOST] [--port PORT]
"""

from __future__ import annotations

import argparse
import sys

from textual_serve.server import Server


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Serve the Synapse TUI in a web browser"
    )
    parser.add_argument("--host", default="localhost", help="Bind host (default: localhost)")
    parser.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000)")
    parser.add_argument(
        "--workspace",
        default=None,
        help="Workspace directory passed to the TUI (default: current dir)",
    )
    args = parser.parse_args()

    # Launch the TUI with the same interpreter that runs this script, so the
    # subprocess inherits the venv without depending on any console script.
    command = f'"{sys.executable}" -m synapse tui'
    if args.workspace:
        command += f' --workspace "{args.workspace}"'

    server = Server(command, host=args.host, port=args.port)
    server.serve()


if __name__ == "__main__":
    main()
