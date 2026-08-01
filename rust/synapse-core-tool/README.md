# synapse-core-tool

Required Rust/PyO3 implementation of Synapse filesystem operations.

The Python module `synapse_core_tool` exports:

- `grep` and `glob` for ignore-aware workspace search;
- `read` for bounded file or directory reads;
- `edit` for exact string replacement with line-ending compatibility;
- `patch` for unified-diff updates.

Text edits preserve detected UTF-8/UTF-16 BOMs and legacy encodings. The Python backend resolves and authorizes paths before calling this module; the native package accepts only absolute host paths.

## Distribution

Release tags use the format `synapse-core-tool-v{version}`. The `native-core-tool-wheels` workflow builds abi3 wheels for Windows x86_64, macOS Apple Silicon arm64, and Linux x86_64/aarch64, then publishes them to PyPI.

Build and install locally:

```powershell
maturin develop --manifest-path rust/synapse-core-tool/Cargo.toml
```

Run Rust checks:

```powershell
cargo test --manifest-path rust/synapse-core-tool/Cargo.toml
cargo fmt --manifest-path rust/synapse-core-tool/Cargo.toml --check
```
