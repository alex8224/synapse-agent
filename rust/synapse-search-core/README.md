# synapse-search-core

Required Rust/PyO3 implementation of Synapse workspace `grep` and `glob` operations.

- `grep` uses ripgrep's Rust crates with regular-expression matching.
- `glob` uses `globset` matching.
- Both operations traverse with `ignore::WalkBuilder`, honoring `.gitignore`, `.ignore`, Git global excludes, and `.git/info/exclude`.
- The Python backend resolves and authorizes paths before calling this module; this package receives only absolute host paths.

## Distribution

Release tags use the format `synapse-search-core-v{version}`. The `native-search-wheels` workflow builds abi3 wheels for Windows x86_64, macOS Apple Silicon arm64, and Linux x86_64/aarch64, then publishes them to PyPI. Intel macOS x86_64 is unsupported. Synapse declares `synapse-search-core` as a normal package dependency, so user machines download a matching prebuilt wheel with `uv sync` and do not need Rust or Cargo.

Build and install locally:

```powershell
maturin develop --manifest-path rust/synapse-search-core/Cargo.toml
```

Run Rust tests:

```powershell
cargo test --manifest-path rust/synapse-search-core/Cargo.toml
cargo fmt --manifest-path rust/synapse-search-core/Cargo.toml --check
```
