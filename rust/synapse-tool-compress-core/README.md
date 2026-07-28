# synapse-tool-compress-core

A standalone Rust/PyO3 package for deterministic native tool-output compression.

## License and attribution

This package contains modified source derived from Headroom. It is licensed under Apache-2.0; see `LICENSE` and `NOTICE`. Directly derived Rust files under `src/headroom_port/` carry Apache-2.0 SPDX headers and source attribution.

## Included algorithms

- Content detection
- Headroom SearchCompressor
- Headroom LogCompressor
- Headroom DiffCompressor
- Headroom SmartCrusher JSON compression, including lossless compaction
- Headroom tree-sitter AST CodeCompressor
- BM25 relevance, keyword importance, anchor selection, adaptive sizing

The default SmartCrusher scorer is BM25 only.

## Explicit exclusions

This package does not include `Kompress`, Magika, embedding relevance, `fastembed`, ONNX Runtime, HuggingFace Hub/model downloads, Redis, Headroom proxy code, provider code, or network calls.

## Python API

- `detect_content_type(content)`
- `compress_search(content, query=None, max_files=15, max_matches_per_file=5, max_total_matches=30)`
- `compress_log(content, context_lines=3, max_lines=100, min_lines_for_compression=50, max_warnings=5)`
- `compress_diff(content, context_lines=2)`
- `crush_json(content, query="", bias=1.0)`
- `compress_code(content, language=None, context="")`

The package is intentionally not a Synapse development dependency. Release CI builds abi3 wheels, so end users install a prebuilt wheel and do not need Rust, Cargo, or a C/C++ compiler. Synapse continues to own middleware, reversible SQLite storage, metrics, and safety fallbacks.
