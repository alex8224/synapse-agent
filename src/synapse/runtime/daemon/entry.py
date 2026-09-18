"""Console entry point for the foreground S8 runtime daemon."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from synapse.runtime.daemon.application import run_daemon
from synapse.runtime.daemon.config import DaemonConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="synapse-runtime")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--token-file", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        config = DaemonConfig(
            host=args.host,
            port=args.port,
            state_dir=args.state_dir or DaemonConfig().state_dir,
            token_file=args.token_file,
        )
        asyncio.run(run_daemon(config, stdout=sys.stdout))
        return 0
    except asyncio.CancelledError:
        print("synapse-runtime: unable to start daemon", file=sys.stderr)
        return 2
    except Exception:
        print("synapse-runtime: unable to start daemon", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
