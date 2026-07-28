# Native Compression Core

Rust-only compression algorithms must ship as a separate package, not as part of the Synapse Python build.

## Package Boundary

The standalone package lives at `rust/synapse-tool-compress-core` in this repository. It has its own `Cargo.toml`, `pyproject.toml`, Python package, tests, and wheel workflow:

```text
rust/synapse-tool-compress-core/
  Cargo.toml
  pyproject.toml
  src/lib.rs
  python/synapse_tool_compress_core/__init__.py
.github/workflows/native-compression-wheels.yml
```

The PyO3 abi3 API exposes deterministic detection and compression:

```python
from synapse_tool_compress_core import (
    compress_code,
    compress_diff,
    compress_log,
    compress_search,
    crush_json,
    detect_content_type,
)
```

`search`, `log`, `diff`, `SmartCrusher` JSON compression, AST-aware code compression, BM25 relevance, anchor selection, adaptive sizing, and the deterministic keyword detector are direct Headroom-derived ports. The package is therefore licensed under Apache-2.0 and includes Headroom's `LICENSE` and `NOTICE`; every derived Rust source file has an Apache-2.0 SPDX header and source attribution.

The port deliberately excludes `Kompress`, `Magika`, `EmbeddingScorer`, `HybridScorer`'s embedding branch, `fastembed`, ONNX Runtime, HuggingFace Hub/model downloads, Redis, proxy code, and network/provider code. `SmartCrusher` uses the copied pure-Rust `BM25Scorer` as its default relevance scorer.

The API exchanges plain strings and JSON-compatible metadata only. It does not contain proxy logic, providers, credentials, model routing, or session storage.

## Distribution

- Build wheels with `maturin` in the core package CI, never in Synapse CI.
- Publish abi3 wheels for CPython 3.12+ on `win_amd64`, `macosx_arm64`, `macosx_x86_64`, and `manylinux_x86_64` / `manylinux_aarch64`.
- Synapse declares a normal wheel dependency once releases are available.
- End users run `uv sync`; no Rust compiler, Cargo, C++ compiler, or local native toolchain is required.
- Source distributions are not a supported installation path for Synapse releases. Missing-wheel platforms should receive a clear compatibility error or use Synapse's Python fallback.

## Integration

Synapse owns content routing, middleware, SQLite reversible storage, thread isolation, retrieval, metrics, and safety fallbacks. The native package owns only deterministic algorithm execution.

```text
ToolOutputTransformPipeline
  -> Python fallback algorithms
  -> optional native Headroom-derived search/log/diff/JSON/code algorithms
  -> Synapse safety validation
  -> Synapse reversible storage
```

Synapse now discovers this package at runtime when its wheel is installed and prefers it for search, log, diff, JSON, and code. Synapse still validates critical facts and byte savings, and keeps original data in its own SQLite repository. If import or a native transform fails, the matching Python transformer runs instead.

The package is deliberately not listed in Synapse's `pyproject.toml` until it is published to the configured package index, so `uv sync` never attempts to build it from source or require a Rust toolchain. For local development, install the built wheel explicitly with `uv pip install rust/synapse-tool-compress-core/dist/<wheel>.whl`.
