# synapse-core-tool

Required Rust/PyO3 implementation of Synapse filesystem operations.

The Python module `synapse_core_tool` exports:

- `grep` and `glob` for ignore-aware workspace search;
- `read` for bounded file or directory reads;
- `edit` for exact string replacement with line-ending compatibility;
- `patch` for unified-diff updates;
- `render_math_png` for bounded, in-process LaTeX math rendering through RaTeX.

Text edits preserve detected UTF-8/UTF-16 BOMs and legacy encodings. The Python backend resolves and authorizes paths before calling this module; the native package accepts only absolute host paths.

`render_math_png` returns transparent PNG bytes by default and supports display/text math styles, foreground/background colors, font size, padding, and device pixel ratio. It embeds the KaTeX fonts and does not require Node.js, a browser, or a system TeX installation. Formula source and output dimensions are bounded before rasterization.

```python
from synapse_core_tool import render_math_png

png = render_math_png(
    r"\frac{-b \pm \sqrt{b^2 - 4ac}}{2a}",
    display=True,
    color="#e8eaed",
    background=None,
    device_pixel_ratio=2.0,
)
```

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
