"""Generate or verify the runtime contract manifest and generated TypeScript.

The registry in ``src/synapse/runtime/service/contract_registry.py`` is the
authority; this script only renders it.  Run without arguments to write the two
generated artifacts, and with ``--check`` to fail (exit code 1) when a committed
artifact is missing or drifted.  The generated output is deterministic: no local
paths, no timestamps, and LF line endings on every platform.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_export_module():
    """Import the export module with the source tree on ``sys.path``."""
    src = ROOT / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from synapse.runtime.service import contract_export

    return contract_export


def _targets(export) -> tuple[tuple[Path, str], ...]:
    """Return the (path, expected text) pairs for both generated artifacts."""
    return (
        (ROOT / export.MANIFEST_RELATIVE_PATH, export.render_manifest_json()),
        (ROOT / export.TYPESCRIPT_RELATIVE_PATH, export.render_typescript()),
    )


def _read(path: Path) -> str | None:
    """Read one committed artifact as LF text, or ``None`` when it is missing."""
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def generate() -> int:
    """Write both generated artifacts and report their sizes."""
    export = _load_export_module()
    for path, text in _targets(export):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        relative = path.relative_to(ROOT).as_posix()
        print(f"wrote {relative} ({len(text.splitlines())} lines)")
    return 0


def check() -> int:
    """Verify both committed artifacts match the registry."""
    export = _load_export_module()
    problems: list[str] = []
    for path, expected in _targets(export):
        relative = path.relative_to(ROOT).as_posix()
        actual = _read(path)
        if actual is None:
            problems.append(f"missing generated artifact: {relative}")
        elif actual != expected:
            problems.append(f"drifted generated artifact: {relative}")
    if problems:
        for problem in problems:
            print(f"ERROR: {problem}")
        print("regenerate with: uv run --no-sync python scripts/export_contract_manifest.py")
        return 1
    print("runtime contract artifacts are up to date")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the exporter in write mode (default) or ``--check`` mode."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the committed artifacts instead of writing them",
    )
    args = parser.parse_args(argv)
    return check() if args.check else generate()


if __name__ == "__main__":
    raise SystemExit(main())
