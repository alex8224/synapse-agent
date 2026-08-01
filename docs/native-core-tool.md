# Native Core Tool

`synapse-core-tool` is the required PyO3 package that implements Synapse filesystem search, reading, exact editing, and unified-diff patching.

## Package Boundary

```text
rust/synapse-core-tool/
  Cargo.toml
  pyproject.toml
  src/
    lib.rs
    search.rs
    file_access.rs
    file_tools.rs
  python/synapse_core_tool/__init__.py
.github/workflows/native-core-tool-wheels.yml
```

`CodingLocalShellBackend` retains responsibility for resolving agent paths, enforcing workspace boundaries, applying configured deny paths, and converting host paths back to virtual paths. The native package receives only absolute authorized host paths.

The backend's existing `read_file` and `edit_file` model tools call the native `read` and `edit`
implementations. Synapse registers `find_files`, `search_files`, and `patch` as model-facing tools
backed by native `glob`, `grep`, and unified-diff implementations. DeepAgents' generic `ls`,
`glob`, and `grep` tools are hidden from model requests, and an authoritative system-prompt suffix
documents only the active Synapse schemas to prevent conflicting tool guidance.

## Distribution

A `synapse-core-tool-v*` tag triggers `.github/workflows/native-core-tool-wheels.yml`, which builds abi3 wheels for Windows x86_64, macOS Apple Silicon arm64, and Linux x86_64/aarch64, attaches them to a GitHub Release, and publishes them to PyPI.

For repository development, `uv` maps the dependency to `rust/synapse-core-tool`. Published Synapse wheels still declare the normal `synapse-core-tool` package dependency, so the native package must be published before a Synapse release that depends on it.

## Local Development

```powershell
maturin develop --manifest-path rust/synapse-core-tool/Cargo.toml
cargo test --manifest-path rust/synapse-core-tool/Cargo.toml
cargo fmt --manifest-path rust/synapse-core-tool/Cargo.toml --check
uv run --no-sync python -m pytest tests/test_backends.py -q
```