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

Install the extension for local backend tests:

```powershell
maturin develop --manifest-path rust/synapse-search-core/Cargo.toml
```

Validate it with:

```powershell
cargo test --manifest-path rust/synapse-search-core/Cargo.toml
cargo fmt --manifest-path rust/synapse-search-core/Cargo.toml --check
uv run --no-sync python -m pytest tests/test_backends.py -q
```
