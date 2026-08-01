# Native Search Core

`synapse-search-core` is the required PyO3 package that implements Synapse workspace `grep` and `glob` operations.

## Package Boundary

```text
rust/synapse-search-core/
  Cargo.toml
  pyproject.toml
  src/lib.rs
  python/synapse_search_core/__init__.py
.github/workflows/native-search-wheels.yml
```

The package performs only filesystem traversal, glob matching, and regular-expression search. `CodingLocalShellBackend` retains responsibility for resolving agent paths, enforcing workspace boundaries, applying configured deny paths, and returning virtual paths.

## Distribution

`synapse-search-core` is a required normal dependency of the main package. It is not mapped to a local source in `pyproject.toml`.

A `synapse-search-core-v*` tag triggers `.github/workflows/native-search-wheels.yml`, which:

1. builds abi3 wheels for Windows x86_64, macOS Apple Silicon arm64, and Linux x86_64/aarch64; Intel macOS x86_64 is unsupported;
2. attaches wheels to the GitHub Release;
3. publishes wheels to PyPI using the `pypi` trusted-publishing environment.

After `synapse-search-core` version `0.1.0` is published to PyPI, regenerate and commit `uv.lock`. Then `uv sync` downloads the matching platform wheel. User machines do not need a Rust toolchain, Cargo, Maturin, or a C/C++ compiler.

## Local Development

The main project and the native core use different development installation mechanisms:

- `uv run synapse` loads Synapse Python code from the repository's `src/synapse/` editable installation.
- A normal `uv sync` installs `synapse-search-core` from PyPI because the project does not define a local source override.
- To test changes under `rust/synapse-search-core/`, compile and install the extension into the project `.venv` with Maturin.

### 1. Stop running Synapse processes

Exit every Synapse instance started from this workspace before replacing the extension. Windows locks a loaded `_native.pyd`, so an active process can prevent Maturin from updating it.

### 2. Prepare the project environment

Run from the repository root:

```powershell
uv sync
```

Confirm that the main Python package resolves to the repository source:

```powershell
uv run python -c "import synapse; print(synapse.__file__)"
```

The result should point to:

```text
<workspace>\src\synapse\__init__.py
```

### 3. Compile and install the local native core

Do not use `uv run --with maturin maturin develop` for this step. The `--with` command runs Maturin in a temporary uv environment and changes `VIRTUAL_ENV` to that temporary directory, so the extension is not installed into the project `.venv`.

Activate the project environment and invoke an already installed Maturin directly:

```powershell
.\.venv\Scripts\Activate.ps1
maturin develop --uv `
  --manifest-path rust/synapse-search-core/Cargo.toml
```

Before building, verify the target environment:

```powershell
python -c "import sys; print(sys.prefix)"
$env:VIRTUAL_ENV
```

Both values must point to `<workspace>\.venv`. Keep the `--uv` flag: uv-created environments may not contain `pip`, and without `--uv` Maturin can finish compiling without installing the editable package into `.venv`.

If `maturin` is not available, install it into the project environment first, then invoke that exact executable:

```powershell
uv pip install --python .\.venv\Scripts\python.exe maturin
.\.venv\Scripts\maturin.exe develop --uv `
  --manifest-path rust/synapse-search-core/Cargo.toml
```

After installation, do not run `uv sync` or `uv sync --reinstall-package synapse-search-core` before testing, because uv can restore the locked PyPI wheel over the local development build.

`cargo build` alone is insufficient: it builds the Rust library but does not install the Python extension into `.venv`.

### 4. Confirm which extension Python loads

Print the package and native module paths:

```powershell
uv run --no-sync python -c "import synapse_search_core, synapse_search_core._native as n; print(synapse_search_core.__file__); print(n.__file__)"
```

Maturin may install the locally built module under `.venv/Lib/site-packages`, which is also where a PyPI wheel is installed. Therefore, the path alone may not prove its origin. Inspect the loaded binary's hash:

```powershell
$loaded = uv run --no-sync python -c "import synapse_search_core._native as n; print(n.__file__)"
Get-FileHash $loaded -Algorithm SHA256
```

When Maturin also writes `rust/synapse-search-core/python/synapse_search_core/_native.pyd`, compare both binaries:

```powershell
Get-FileHash `
  $loaded, `
  rust/synapse-search-core/python/synapse_search_core/_native.pyd `
  -Algorithm SHA256
```

Matching hashes confirm that the loaded extension is the local build. A modified timestamp immediately after `maturin develop` is another useful signal.

### 5. Test the native core directly

This isolates Rust search behavior from the Synapse backend and tool schema:

```powershell
@'
from pathlib import Path

import synapse_search_core
import synapse_search_core._native as native

base = Path("src/synapse/tools").resolve()
print("native:", native.__file__)

for glob in [None, "*.py", "**/*.py", "filesystem_search.py"]:
    result = synapse_search_core.grep(
        str(base),
        r"\bField\b",
        include_glob=glob,
        max_results=30,
    )
    print(
        repr(glob),
        len(result["matches"]),
        sorted({item["path"] for item in result["matches"]}),
    )
'@ | uv run --no-sync python -
```

Every listed glob should return matches. If `None` returns matches but every non-empty glob returns zero, the failure is in the native include-glob path.

### 6. Test the complete Synapse tool chain

This verifies the actual path used by the Agent:

```text
search_files StructuredTool
  -> CodingLocalShellBackend
  -> synapse_search_core
```

Run:

```powershell
@'
from pathlib import Path

from synapse.runtime.backends import CodingLocalShellBackend
from synapse.tools.filesystem_search import build_filesystem_search_tools

backend = CodingLocalShellBackend(
    root_dir=Path(".").resolve(),
    virtual_mode=True,
    inherit_env=False,
    env={},
)
search_files = {
    tool.name: tool
    for tool in build_filesystem_search_tools(backend)
}["search_files"]

for glob in [None, "*.py", "**/*.py", "filesystem_search.py"]:
    args = {
        "pattern": r"\bField\b",
        "path": "/src/synapse/tools",
        "output_mode": "count",
        "max_results": 30,
        "head_limit": 20,
        "context_lines": 0,
    }
    if glob is not None:
        args["glob"] = glob

    print(repr(glob))
    print(search_files.invoke(args))
    print()
'@ | uv run --no-sync python -
```

The calls with `*.py`, `**/*.py`, and `filesystem_search.py` should all return matching files. The `glob` argument is an include-only path filter, not an exclusion rule.

### 7. Run automated checks

Run the focused Python tests:

```powershell
uv run --no-sync pytest `
  tests/test_backends.py::test_native_grep_supports_regex_glob_and_virtual_paths `
  tests/test_backends.py::test_native_grep_retries_empty_include_glob_result `
  tests/test_backends.py::test_search_files_tool_applies_glob_as_include_filter `
  tests/test_filesystem_search_tools.py `
  -q
```

Run the Rust checks:

```powershell
cargo test --manifest-path rust/synapse-search-core/Cargo.toml
cargo fmt --manifest-path rust/synapse-search-core/Cargo.toml --check
```

### 8. Start Synapse from the workspace

After installing the local core, start a new process without dependency synchronization:

```powershell
uv run --no-sync synapse tui -w .
```

Do not use plain `uv run synapse` for this local-core test. Its automatic environment check can restore the lockfile's PyPI `synapse-search-core` package and remove the Maturin editable installation before Synapse starts.

The resulting development stack is:

```text
Synapse Python code:       <workspace>/src/synapse
Native search core:        locally compiled PyO3 extension installed in <workspace>/.venv
Python environment:        <workspace>/.venv
```

After changing Rust code, rerun `maturin develop` and restart Synapse. A running Python process does not reload `_native.pyd` automatically.

Running `uv sync --reinstall-package synapse-search-core` can replace the local development build with the PyPI wheel. If that happens, rerun `maturin develop` before testing local Rust changes.
