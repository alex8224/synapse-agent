"""Serve the Synapse Textual TUI in a web browser via textual-serve.

The TUI keeps running as a server-side process; the browser renders it
through xterm.js over a WebSocket (same Python code, zero changes).

This module is also the ``synapse-web`` console-script entry point so the
packaged wheel can start the web server without any source checkout:

    synapse-web --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import argparse
import sys

from textual_serve.server import Server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="synapse-web",
        description="Serve the Synapse TUI in a web browser",
    )
    parser.add_argument("--host", default="localhost", help="Bind host (default: localhost)")
    parser.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000)")
    parser.add_argument(
        "--workspace",
        default=None,
        help="Workspace directory passed to the TUI (default: current dir)",
    )
    parser.add_argument(
        "--public-url",
        default=None,
        help=(
            "External URL (scheme://host[:port][/path]) used in the served page "
            "for WebSocket/static links. Required when behind an nginx/TLS "
            "reverse proxy; otherwise derived from --host/--port."
        ),
    )
    return parser


def build_command(workspace: str | None = None) -> str:
    """Command textual-serve spawns for each browser session."""
    command = f'"{sys.executable}" -m synapse tui'
    if workspace:
        command += f' --workspace "{workspace}"'
    return command


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    server = Server(
        build_command(args.workspace),
        host=args.host,
        port=args.port,
        public_url=args.public_url,
    )
    server.serve()


if __name__ == "__main__":
    main()
