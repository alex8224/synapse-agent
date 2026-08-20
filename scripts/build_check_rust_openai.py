"""Rebuild synapse-core-tool and verify native OpenAI Responses API support."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def run_build() -> None:
    """Rebuild and reinstall the native extension into the current uv environment."""
    command = ["uv", "sync", "--reinstall-package", "synapse-core-tool"]
    print("$", " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def check_support() -> int:
    """Print native Responses API capabilities and return a shell status code."""
    expected_venv = (ROOT / ".venv").resolve()
    current_venv = Path(sys.prefix).resolve()
    if current_venv != expected_venv:
        print(
            "ERROR: run this script with the project environment, for example: "
            "uv run --no-sync python scripts/build_check_rust_openai.py"
        )
        print(f"current_python={sys.executable}")
        print(f"expected_venv={expected_venv}")
        return 2

    try:
        import synapse_core_tool
    except (ImportError, OSError) as exc:
        print(f"ERROR: cannot import synapse_core_tool: {exc}")
        return 2

    client = getattr(synapse_core_tool, "RustOpenAIClient", None)
    has_client = client is not None
    has_complete = bool(has_client and hasattr(client, "complete_responses"))
    has_stream = bool(has_client and hasattr(client, "stream_responses"))
    disabled = os.environ.get("SYNAPSE_DISABLE_RUST_OPENAI", "").strip().casefold() in {
        "1",
        "true",
        "yes",
    }

    print(f"module={synapse_core_tool.__file__}")
    print(f"RustOpenAIClient={has_client}")
    print(f"complete_responses={has_complete}")
    print(f"stream_responses={has_stream}")
    print(f"SYNAPSE_DISABLE_RUST_OPENAI={disabled}")

    enabled = has_client and has_complete and has_stream and not disabled
    print(f"RESULT={'ENABLED' if enabled else 'NOT_ENABLED'}")
    return 0 if enabled else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Skip rebuilding and only inspect the currently installed extension.",
    )
    args = parser.parse_args()

    if not args.check_only:
        run_build()
    return check_support()


if __name__ == "__main__":
    raise SystemExit(main())
