"""AST-based architecture boundary tests for runtime packages.

Validates that core runtime packages (agent_loop, service, streaming) do not
import presentation-layer modules (synapse.ui, synapse.cli, synapse.acp, or
textual), and that service contract files (ports, commands, errors, events,
queries) do not import transport, LangGraph, langchain, or deepagents.
"""

from __future__ import annotations

import ast
import importlib.util
import os
from pathlib import Path

# Absolute path to src/synapse based on *this* test file's location so the
# scan works regardless of the working directory pytest is invoked from.
SRC = Path(__file__).resolve().parents[1] / "src" / "synapse"


def _package_for_file(py: Path) -> str:
    """Derive the dotted package name for *py* relative to the ``src`` root."""
    rel = py.resolve().relative_to(SRC.parent)  # e.g. synapse/runtime/agent_loop/turn.py
    parts = list(rel.with_suffix("").parts)      # ['synapse', 'runtime', 'agent_loop', 'turn']
    # Package is the parent of the module.
    return ".".join(parts[:-1]) if len(parts) > 1 else parts[0]


def _collect_imports(tree: ast.AST, *, package: str) -> list[str]:
    """Return resolved module strings from import/from statements.

    Relative imports are resolved against *package* using
    ``importlib.util.resolve_name``.  For ``ImportFrom`` nodes each imported
    name is also recorded as ``<base>.<alias>`` so that ``from synapse import
    ui`` correctly produces ``synapse.ui``.
    """
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            # Build the raw reference string (e.g. "...ui" or ".sub").
            dots = "." * (node.level or 0)
            raw = dots + (node.module or "")
            if node.level and node.level > 0:
                try:
                    base = importlib.util.resolve_name(raw, package)
                except (ImportError, ValueError):
                    base = raw  # unresolvable -- keep raw string
            else:
                base = node.module or ""
            modules.append(base)
            # Record base.alias for each imported name so that
            # ``from synapse import ui`` yields ``synapse.ui``.
            for alias in node.names:
                modules.append(f"{base}.{alias.name}")
    return modules


def _py_files(directory: Path) -> list[Path]:
    """All .py files under *directory*, recursively."""
    result = []
    for root, _dirs, files in os.walk(directory):
        for f in files:
            if f.endswith(".py"):
                result.append(Path(root) / f)
    return sorted(result)


# ---------------------------------------------------------------------------
# Forbidden import sets
# ---------------------------------------------------------------------------

RUNTIME_FORBIDDEN = {
    "synapse.ui",
    "synapse.cli",
    "synapse.acp",
    "textual",
    # Textual and Rich are presentation-only dependencies: neither may leak
    # into the headless execution/streaming core (phase 3 boundary).
    "rich",
}

CONTRACT_FILES = {
    "ports.py",
    "commands.py",
    "errors.py",
    "events.py",
    "queries.py",
    "history.py",
    "runtime_config.py",
}
CONTRACT_FORBIDDEN = {
    "synapse.runtime.transport",
    "langgraph",
    "langchain",
    "langchain_core",
    "langchain_openai",
    "deepagents",
}


def _matches_any(module: str, prefixes: set[str]) -> str | None:
    """Return the matched prefix if *module* starts with any of *prefixes*."""
    for prefix in prefixes:
        if module == prefix or module.startswith(prefix + "."):
            return prefix
    return None


# ---------------------------------------------------------------------------
# Task 1: runtime/{agent_loop,service,streaming} must not import UI/CLI/ACP
# ---------------------------------------------------------------------------

RUNTIME_PACKAGES = [
    SRC / "runtime" / "agent_loop",
    SRC / "runtime" / "service",
    SRC / "runtime" / "streaming",
]


def _scan_violations(
    directories: list[Path],
    forbidden: set[str],
    *,
    file_filter: set[str] | None = None,
) -> list[tuple[Path, str]]:
    """Scan *directories* for imports matching *forbidden* prefixes.

    When *file_filter* is given, only files whose name is in that set are
    checked (used for contract-file scanning).
    """
    violations: list[tuple[Path, str]] = []
    for pkg in directories:
        for py in _py_files(pkg):
            if file_filter is not None and py.name not in file_filter:
                continue
            package = _package_for_file(py)
            tree = ast.parse(py.read_text("utf-8"), filename=str(py))
            for mod in _collect_imports(tree, package=package):
                hit = _matches_any(mod, forbidden)
                if hit is not None:
                    violations.append((py, hit))
    return violations


def test_runtime_packages_have_no_forbidden_imports() -> None:
    """Runtime packages must not import presentation-layer modules."""
    for pkg in RUNTIME_PACKAGES:
        assert pkg.is_dir(), f"Expected runtime package directory does not exist: {pkg}"
    violations = _scan_violations(RUNTIME_PACKAGES, RUNTIME_FORBIDDEN)
    assert violations == [], f"Unexpected imports: {violations}"


# ---------------------------------------------------------------------------
# Task 2: service contract files do not import transport/LangGraph/langchain/deepagents
# ---------------------------------------------------------------------------

SERVICE_DIR = SRC / "runtime" / "service"


def test_contract_files_have_no_forbidden_imports() -> None:
    """Contract files must not import transport, LangGraph, langchain, or deepagents."""
    assert SERVICE_DIR.is_dir(), f"Service directory does not exist: {SERVICE_DIR}"
    existing = {f.name for f in SERVICE_DIR.iterdir() if f.suffix == ".py"}
    assert CONTRACT_FILES <= existing, f"Missing contract files: {CONTRACT_FILES - existing}"
    violations = _scan_violations([SERVICE_DIR], CONTRACT_FORBIDDEN, file_filter=CONTRACT_FILES)
    assert violations == [], f"Unexpected imports: {violations}"


# ---------------------------------------------------------------------------
# Task 3: the headless turn runtime must stay renderer-free
# ---------------------------------------------------------------------------

_HEADLESS_RENDERER_SEAMS = {
    # Renderer seams removed in phase 3 must not be reintroduced into the
    # headless execution runtime.
    "StreamRunnerOptions",
    "_HeadlessRenderer",
    "_NoopRenderer",
    "runner_options",
    "renderer",
}


def test_headless_turn_runtime_has_no_renderer_seams() -> None:
    """``agent_loop/turn.py`` must not name or carry any renderer seam."""
    turn = SRC / "runtime" / "agent_loop" / "turn.py"
    tree = ast.parse(turn.read_text("utf-8"), filename=str(turn))
    identifiers = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
        elif isinstance(node, ast.arg):
            identifiers.add(node.arg)
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef)):
            identifiers.add(node.name)
    assert not (identifiers & _HEADLESS_RENDERER_SEAMS), (
        f"turn.py reintroduced renderer seams: {sorted(identifiers & _HEADLESS_RENDERER_SEAMS)}"
    )


# ---------------------------------------------------------------------------
# Helper unit tests: _collect_imports and _matches_any
# ---------------------------------------------------------------------------


class TestCollectImports:
    def test_import_statement(self) -> None:
        tree = ast.parse("import os")
        assert "os" in _collect_imports(tree, package="synapse")

    def test_from_import(self) -> None:
        tree = ast.parse("from synapse.ui.tui import Foo")
        result = _collect_imports(tree, package="synapse.runtime")
        assert "synapse.ui.tui" in result
        assert "synapse.ui.tui.Foo" in result

    def test_relative_import_triple_dot_ui(self) -> None:
        """``from ...ui import X`` inside synapse.runtime.agent_loop resolves to synapse.ui."""
        tree = ast.parse("from ...ui import SomeWidget")
        result = _collect_imports(tree, package="synapse.runtime.agent_loop")
        assert "synapse.ui" in result
        assert "synapse.ui.SomeWidget" in result

    def test_relative_import_bare_from_dots(self) -> None:
        """``from ... import ui`` inside synapse.runtime.agent_loop resolves to synapse.ui."""
        tree = ast.parse("from ... import ui")
        result = _collect_imports(tree, package="synapse.runtime.agent_loop")
        assert "synapse" in result
        assert "synapse.ui" in result

    def test_from_synapse_import_ui(self) -> None:
        """``from synapse import ui`` must produce synapse.ui."""
        tree = ast.parse("from synapse import ui")
        result = _collect_imports(tree, package="synapse.runtime")
        assert "synapse.ui" in result

    def test_relative_import_double_dot_transport(self) -> None:
        """``from .. import transport`` in service pkg resolves to synapse.runtime.transport."""
        tree = ast.parse("from .. import transport")
        result = _collect_imports(tree, package="synapse.runtime.service")
        assert "synapse.runtime" in result
        assert "synapse.runtime.transport" in result

    def test_multiple_imports(self) -> None:
        code = "import os\nimport sys\nfrom pathlib import Path"
        tree = ast.parse(code)
        result = _collect_imports(tree, package="synapse")
        assert "os" in result
        assert "sys" in result
        assert "pathlib" in result


class TestMatchesAny:
    def test_exact_match(self) -> None:
        assert _matches_any("textual", {"textual"}) == "textual"

    def test_prefix_match(self) -> None:
        assert _matches_any("synapse.ui.tui", {"synapse.ui"}) == "synapse.ui"

    def test_no_match(self) -> None:
        assert _matches_any("synapse.runtime.service", {"synapse.ui"}) is None

    def test_partial_name_no_false_positive(self) -> None:
        # "synapse.uix" should NOT match "synapse.ui"
        assert _matches_any("synapse.uix", {"synapse.ui"}) is None

    def test_langchain_match(self) -> None:
        assert _matches_any("langchain_core.messages", {"langchain_core"}) == "langchain_core"

    def test_deepagents_match(self) -> None:
        assert _matches_any("deepagents.backends", {"deepagents"}) == "deepagents"

    def test_allowed_runtime_import(self) -> None:
        assert _matches_any("synapse.runtime.service.events", {"synapse.ui", "textual"}) is None
