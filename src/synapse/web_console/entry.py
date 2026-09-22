"""Console entry point for the loopback Synapse Web Console host."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from synapse.runtime.daemon.launcher import DaemonHandle, ensure_daemon
from synapse.web_console.config import (
    DEFAULT_MAX_BODY_BYTES,
    DEFAULT_MAX_CONCURRENT_SOCKETS,
    DEFAULT_MESSAGE_BYTES,
    DEFAULT_PAIR_TTL_SECONDS,
    DEFAULT_PORT,
    DEFAULT_SESSION_TTL_SECONDS,
    DEFAULT_WS_HEARTBEAT_SECONDS,
    WebConsoleConfig,
)
from synapse.web_console.host import WebConsoleHost, resolve_project


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="synapse web-console",
        description=(
            "Serve the built React Web console and relay JSON-RPC WebSockets to "
            "the runtime daemon. Loopback, single-user only."
        ),
    )
    parser.add_argument("--host", default="127.0.0.1", help="Loopback bind host")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Bind port")
    parser.add_argument("--workspace", type=Path, help="Project workspace (default: cwd)")
    parser.add_argument(
        "--static-dir",
        type=Path,
        help=(
            "Directory with the built console assets (default: bundled wheel assets, "
            "or <workspace>/web/dist from a source checkout)"
        ),
    )
    parser.add_argument("--state-dir", type=Path, help="Daemon state dir (discovery + token)")
    parser.add_argument("--token-file", type=Path, help="Daemon token file (server-side only)")
    parser.add_argument("--catalog-path", type=Path, help="Project catalog database path")
    parser.add_argument(
        "--project-scope",
        choices=("workspace", "all"),
        default="all",
        help=(
            "Projects the console may switch to: 'workspace' keeps the single-project "
            "boundary, 'all' allows every project in the same user catalog (default)"
        ),
    )
    parser.add_argument("--runtime-host", default="127.0.0.1", help="Daemon WS host")
    parser.add_argument("--runtime-port", type=int, help="Daemon WS port (default: daemon.json)")
    parser.add_argument(
        "--start-runtime",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Start a runtime daemon for --state-dir when none is published, and stop it "
            "again on exit (default). Use --no-start-runtime to require a daemon the user "
            "started themselves; --runtime-port always means 'use that daemon'."
        ),
    )
    parser.add_argument(
        "--max-message-bytes",
        type=int,
        default=DEFAULT_MESSAGE_BYTES,
        help="Maximum WebSocket frame size relayed (default 1 MiB)",
    )
    parser.add_argument(
        "--session-ttl-seconds",
        type=int,
        default=DEFAULT_SESSION_TTL_SECONDS,
        help="Session cookie lifetime in seconds (default 12h)",
    )
    parser.add_argument(
        "--pair-ttl-seconds",
        type=int,
        default=DEFAULT_PAIR_TTL_SECONDS,
        help="Pairing code lifetime in seconds (default 300)",
    )
    parser.add_argument(
        "--pairing",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Require the one-time pairing code printed on stderr (default). "
            "--no-pairing mints a session for any same-origin loopback browser "
            "without a code: local debugging only, never for a console you share"
        ),
    )
    parser.add_argument(
        "--max-sockets",
        type=int,
        default=DEFAULT_MAX_CONCURRENT_SOCKETS,
        help="Maximum concurrent relayed WebSockets (default 16)",
    )
    parser.add_argument(
        "--max-body-bytes",
        type=int,
        default=DEFAULT_MAX_BODY_BYTES,
        help="Maximum HTTP request body size for /api/* (default 4096)",
    )
    parser.add_argument(
        "--ws-heartbeat-seconds",
        type=int,
        default=DEFAULT_WS_HEARTBEAT_SECONDS,
        help="WebSocket ping interval in seconds; 0 disables (default 30)",
    )
    parser.add_argument(
        "--auto-register",
        action="store_true",
        help="Auto-register workspace in catalog if not registered",
    )
    return parser


def should_start_runtime(*, start_runtime: bool, runtime_port: int | None) -> bool:
    """Whether the console host may start a daemon for itself.

    An explicit ``--runtime-port`` means the caller is pointing at a specific
    daemon; that daemon is never replaced by one we started, even when nothing
    answers on the port yet.
    """
    return start_runtime and runtime_port is None


def main(argv: list[str] | None = None) -> int:
    launched: DaemonHandle | None = None
    try:
        args = build_parser().parse_args(argv)
        state_dir = Path(args.state_dir) if args.state_dir is not None else WebConsoleConfig(
            workspace=Path(".").resolve()
        ).state_dir
        config = WebConsoleConfig(
            workspace=Path(args.workspace).resolve() if args.workspace else Path.cwd(),
            host=args.host,
            port=args.port,
            static_dir=Path(args.static_dir) if args.static_dir else None,
            state_dir=state_dir,
            token_file=Path(args.token_file) if args.token_file else None,
            catalog_path=Path(args.catalog_path) if args.catalog_path else None,
            project_scope=args.project_scope,
            runtime_host=args.runtime_host,
            runtime_port=args.runtime_port,
            max_message_bytes=args.max_message_bytes,
            session_ttl_seconds=args.session_ttl_seconds,
            pair_ttl_seconds=args.pair_ttl_seconds,
            pairing_required=args.pairing,
            auto_register=args.auto_register,
            max_concurrent_sockets=args.max_sockets,
            max_body_bytes=args.max_body_bytes,
            ws_heartbeat_seconds=args.ws_heartbeat_seconds,
        )
        # One command should be enough to get a usable console: reuse the daemon
        # published for this state dir, otherwise start one and stop it again on
        # exit.  `--runtime-port` means the caller is pointing at a specific
        # daemon, so that daemon is never replaced by one we started.
        if should_start_runtime(start_runtime=args.start_runtime, runtime_port=config.runtime_port):
            launched = ensure_daemon(config.state_dir)
            if launched is not None:
                # stderr on purpose: stdout is the single JSON metadata line.
                print(
                    "synapse web-console: started runtime daemon on "
                    f"{launched.endpoint.host}:{launched.endpoint.port} "
                    f"(state dir {config.state_dir})",
                    file=sys.stderr,
                    flush=True,
                )
        project = resolve_project(config)
        return asyncio.run(_run(config, project))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001 - entry point reports and exits
        print(f"synapse web-console: unable to start: {exc}", file=sys.stderr)
        return 2
    finally:
        # Only ever stops a daemon this call started; a reused one keeps running.
        if launched is not None:
            launched.stop()


async def _run(config: WebConsoleConfig, project: object) -> int:
    host = WebConsoleHost(config, project)
    metadata = await host.start()
    print(json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), flush=True)
    stop = asyncio.Event()
    restore = _install_stop_handlers(stop)
    try:
        await stop.wait()
    finally:
        restore()
        await host.close()
    return 0


def _install_stop_handlers(stop: asyncio.Event) -> Callable[[], None]:
    """Install the interrupt handlers that resolve ``stop`` on this platform.

    POSIX: ``loop.add_signal_handler``, which runs the callback on the loop
    itself.  Windows: the Proactor loop raises ``NotImplementedError``, and the
    previous implementation silently skipped installation, so a console control
    event (``CTRL_BREAK_EVENT``) reached the OS default handler, which terminated
    the process with ``0xC000013A`` before ``host.close()`` ever ran.  The
    fallback installs a Python-level handler with ``signal.signal`` and hands the
    event to the loop with ``loop.call_soon_threadsafe`` -- required because
    ``Event.set()`` alone cannot wake a selector that is blocked in its wait.
    """
    loop = asyncio.get_running_loop()
    restore_actions: list[Callable[[], None]] = []
    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None), getattr(signal, "SIGBREAK", None)):
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError, ValueError):
            # Degradation boundary: this loop cannot own signal handlers (Windows
            # Proactor loop).  Fall back to a Python-level handler so a console
            # control event still stops the host gracefully instead of killing it.
            try:
                previous = signal.signal(sig, _loop_waking_handler(loop, stop))
            except (OSError, RuntimeError, ValueError):
                # ``signal.signal`` is main-thread only.  Without it the platform
                # default handler stays in place for this signal (the pre-fix
                # behaviour), which is recorded as a residual limitation rather
                # than silently presented as an installed handler.
                continue
            restore_actions.append(_restore_signal(sig, previous))
        else:
            restore_actions.append(_remove_loop_handler(loop, sig))

    def restore() -> None:
        for action in restore_actions:
            try:
                action()
            except (NotImplementedError, OSError, RuntimeError, ValueError):
                # Restoring is best effort and must never mask the shutdown path;
                # the loop is usually already closing here.
                pass

    return restore


def _loop_waking_handler(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> Any:
    """A ``signal.signal`` handler that hands ``stop`` to the (possibly blocked) loop."""

    def handler(signum: int, frame: Any) -> None:  # noqa: ARG001 - signal signature
        try:
            loop.call_soon_threadsafe(stop.set)
        except RuntimeError:
            # The loop is already closed: there is nothing left to stop.
            pass

    return handler


def _remove_loop_handler(
    loop: asyncio.AbstractEventLoop, sig: signal.Signals
) -> Callable[[], None]:
    def action() -> None:
        loop.remove_signal_handler(sig)

    return action


def _restore_signal(sig: signal.Signals, previous: Any) -> Callable[[], None]:
    def action() -> None:
        signal.signal(sig, previous)

    return action


if __name__ == "__main__":
    raise SystemExit(main())
