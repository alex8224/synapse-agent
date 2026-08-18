# Synapse Collaboration Guide

## Repository layout

| Path | Responsibility |
| --- | --- |
| `src/synapse/app/` | Agent assembly and `AGENTS.md` injection |
| `src/synapse/commands/` | Slash command parsing, completion, and results |
| `src/synapse/content/` | Prompts, skills, input history, multimodal content |
| `src/synapse/goals/` | Goal accounting, steering, and persistence |
| `src/synapse/integrations/` | MCP, model transport, Codex import, vision |
| `src/synapse/memory/` | Long-term memory (SQLite + embeddings) |
| `src/synapse/models/` | Model profiles, registry, config helpers |
| `src/synapse/observability/` | Startup and runtime observability |
| `src/synapse/planner/` | LLM-driven task decomposition |
| `src/synapse/projects/` | User-layer project catalog |
| `src/synapse/rag/` | Project document index and retrieval |
| `src/synapse/runtime/` | Backends, middleware, compression, steer, safety, async runtime |
| `src/synapse/sessions/` | Sessions, transcripts, recap, persistence |
| `src/synapse/settings/` | Layered config paths, Settings schema, loading |
| `src/synapse/tool_output/` | Reversible tool output detection, transforms, storage, metrics |
| `src/synapse/tools/` | Custom tools injected into the agent |
| `src/synapse/ui/` | Textual TUI, stream, timeline, dialogs, topbar/bottombar |
| `tests/` | Python tests, usually mirroring domain modules |
| `rust/synapse-core-tool/` | Native filesystem tools (read/edit/patch/search) and math rendering; published to PyPI as `synapse-core-tool` |
| `rust/synapse-tool-compress-core/` | Native tool-output compression core; required dependency, published to PyPI as `synapse-tool-compress-core` |
| `docs/`, `mkdocs.yml` | User docs and MkDocs config |
| `.github/workflows/` | CI, docs, Python release, native wheel builds |

## Architecture & compatibility

- `src/synapse/app/agent.py` is the assembly layer; keep domain algorithms in their own packages.
- New features belong in the matching domain package; cross-domain wiring goes in `app/` or an explicit runtime middleware.
- `src/synapse/config.py` is a compatibility re-export layer. Import from `synapse.settings` in new code; keep legacy import paths working.
- `__init__.py` exports are public API. Keep necessary re-exports when moving implementations.
- Config merges user and project layers. When changing Settings, update `src/synapse/settings/schema.py`, `src/synapse/settings/config_paths.py`, `tests/test_config.py`, `tests/test_layered_config.py`, and the user docs (`README.md`, `docs/config.md`).
- `AGENTS.md` is statically injected by the agent-md middleware (`src/synapse/app/agent_md.py`), independent of writable memory; never route it back into memory writes.

## Coding standards

- Ruff is the only automated Python baseline: line length 100, target Python 3.12, rules `E/F/I/B/UP`.
- Annotate new or modified public functions, complex state transitions, and compatibility branches.
- Catch broad exceptions only at explicit degradation boundaries, and explain the fallback; never swallow core business errors silently.
- Avoid unbounded reads, searches, and terminal output; cap logs, tool results, and external data.
- Never leak API keys, tokens, `.env` contents, or private user config in code, tests, docs, or output.
- Update README/docs when user-visible behavior changes; skip doc churn for internal-only changes.

## Development & testing

Run `uv sync` after the first install or dependency changes.

Verification order: narrowest test → domain tests → full checks.

```powershell
uv run --no-sync pytest tests/test_x.py -q
uv run --no-sync pytest tests/test_x.py::test_case_name -q
```

| Change area | Preferred tests |
| --- | --- |
| settings/models | `tests/test_config.py`, `tests/test_layered_config.py`, model tests |
| backend/safety/runtime | `tests/test_backends.py`, `tests/test_safety.py`, middleware tests |
| tool output/compression | `tests/test_tool_output.py`, `tests/test_tool_output_*`, request compression tests |
| sessions/Codex import | `tests/test_session_*`, `tests/test_transcript.py`, `tests/test_codex_*` |
| CLI/slash commands | `tests/test_cli.py`, `tests/test_slash_*` |
| TUI/widgets/dialogs | `tests/test_tui_*`, `tests/test_stream_*`, `tests/test_dialogs.py`, component tests |

Full checks:

```powershell
uv run --no-sync ruff check .
uv run --no-sync pytest -q
```

CI runs lint on ubuntu-latest and tests on Windows/Linux with Python 3.12/3.13. Platform-specific changes must at least pass on the current machine and be reviewed for the other platform.

Docs and packaging:

```powershell
uv run --no-sync mkdocs build   # after touching docs/, README.md, or mkdocs.yml
uv build                        # after packaging/entry-point changes
```

## Rust/PyO3 native crates

One optional crate; the Python app must work without its wheel and must keep `ImportError`/`OSError` fallbacks.

- `rust/synapse-core-tool` (`synapse-core-tool` on PyPI): native filesystem tools (read/edit/patch/search) and math rendering. Release with tag `synapse-core-tool-v*`; `native-core-tool-wheels.yml` builds wheels and publishes to GitHub Release + PyPI. Keep the `pyproject.toml` version in sync with `Cargo.toml` — maturin names wheels from `pyproject.toml`.
- `rust/synapse-tool-compress-core` (`synapse-tool-compress-core` on PyPI): native tool-output compression core. **Required dependency** of `synapse-cli-agent`. Release with tag `synapse-tool-compress-core-v*`; `native-compression-wheels.yml` builds wheels and publishes to GitHub Release + PyPI. Keep the `pyproject.toml` version in sync with `Cargo.toml`. Because the main package hard-depends on it, publish the compress-core wheels to PyPI before tagging the main `v*` release.

When changing either crate, at least run:

```powershell
cargo test --manifest-path rust/<crate>/Cargo.toml
cargo fmt --manifest-path rust/<crate>/Cargo.toml --check
```

If Python bindings/APIs change, build or install the local extension and run the related Python tests.

Keep the Apache-2.0 SPDX headers, attribution, `LICENSE`, and `NOTICE` under `rust/synapse-tool-compress-core/src/headroom_port/`; do not introduce excluded network calls or model downloads.

## Release process

Before any `git push`, ask the user: "Do we need to tag a release this time?"

If releasing:

1. Read the current version from `pyproject.toml` and let the user confirm or override it.
2. Review changes since the last `v*` tag with `git log`.
3. Add a `## v{version}` section at the top of `CHANGELOG.md`; the heading must match the tag exactly, group entries by New Features / Bug Fixes / Engineering, and write entries in English (the section becomes the GitHub Release notes).
4. Update `pyproject.toml` if the version changed; sync `uv.lock` when needed.
5. Run relevant tests, Ruff, and `uv build`.
6. Commit with `release: bump to v{version}`.
7. Run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/release.ps1
```

8. The script creates and pushes the `v{version}` tag; `release.yml` extracts the matching CHANGELOG section, runs `uv build`, and creates the GitHub Release.

## cdp_take_screenshot usage

### Known limits
- `filePath` cannot write into the workspace (browser sandbox); any attempt returns `Access denied`.
- Without `filePath`, the screenshot is stored as text (with base64) under `/large_tool_results/call_xx_xxx`.

### Standard flow: screenshot → decode → describe
1. Ensure `/decode_screenshot.py` exists (it picks the latest `large_tool_results/call_*`, extracts the base64, and writes `.tmp/screenshot_*.png`).
2. Run `python decode_screenshot.py`.
3. Call `describe_image(image_path="/.tmp/screenshot_<timestamp>.png", ...)` with the printed filename.

### Notes
- Every screenshot creates a new file; the script always picks the latest.
- Output goes to `/.tmp/`, which is gitignored.
- Use the script instead of decoding manually.