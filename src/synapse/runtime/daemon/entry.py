"""Console entry point for the foreground S8 runtime daemon."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from synapse.runtime.daemon.application import run_daemon
from synapse.runtime.daemon.config import DaemonConfig, ensure_directory


def _configure_error_log(state_dir: Path) -> RotatingFileHandler:
    """Persist bounded daemon errors even when launched without a console."""
    ensure_directory(state_dir)
    log_path = state_dir / "errors.log"
    if log_path.is_symlink():
        raise ValueError("daemon error log must not be a symlink")
    handler = RotatingFileHandler(
        log_path, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
    )
    if os.name != "nt":
        log_path.chmod(0o600)
    handler.setLevel(logging.ERROR)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger = logging.getLogger("synapse.runtime.codex_usage")
    logger.addHandler(handler)
    logger.propagate = False
    return handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="synapse-runtime")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--token-file", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    handler: RotatingFileHandler | None = None
    try:
        args = build_parser().parse_args(argv)
        config = DaemonConfig(
            host=args.host,
            port=args.port,
            state_dir=args.state_dir or DaemonConfig().state_dir,
            token_file=args.token_file,
        )
        handler = _configure_error_log(config.state_dir)
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
    finally:
        if handler is not None:
            logging.getLogger("synapse.runtime.codex_usage").removeHandler(handler)
            handler.close()


if __name__ == "__main__":
    raise SystemExit(main())
