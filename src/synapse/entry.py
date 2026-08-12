"""Process entry point for the ``synapse`` console script.

Starts the startup trace *before* ``synapse.cli`` is imported so the report
covers the CLI module-tree import (typer, settings, stream modules) that
previously fell outside the trace — the gap between interpreter startup and
``cli.main``.  ``synapse.cli.main`` keeps its own ``ensure_started`` call,
which is a no-op here (the trace already has stages).
"""

from __future__ import annotations


def main() -> None:
    """Console-script entry point: start the trace, then delegate to the CLI."""
    from synapse.observability.startup_trace import ensure_started, mark

    ensure_started()
    mark("entry:start")
    from synapse.cli import main as cli_main

    mark("cli:imported")
    cli_main()


if __name__ == "__main__":
    main()
