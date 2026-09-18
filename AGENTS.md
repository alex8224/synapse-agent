# Synapse Collaboration Guide

## Architecture

- Domain packages live under `src/synapse/` (each `synapse.<package>`); their tests are flat files named after the domain under `tests/` (`tests/test_<area>*.py`, plus `tests/fixtures/`) — do not add per-package subdirectories there. Rust/PyO3 crates live under `rust/`; the MkDocs site lives under `docs/` + `mkdocs.yml`.
- Repository Agent Skills live in `skills/<name>/SKILL.md` (YAML front matter: `name`, `description`, `license`, `compatibility`, `allowed_tools`). The default `skills_paths` is `["skills"]` relative to the project root (`src/synapse/settings/schema.py`); `src/synapse/content/skills_catalog.py` discovers them and feeds `/skills`, and `docs/skills.md` documents the feature. `skills/` is excluded from the sdist (`[tool.hatch.build.targets.sdist]`), so the skills travel with the checkout, not with the wheel. Keep the front matter accurate — it is what the catalog surfaces.
- `src/synapse/app/agent.py` is the assembly composition root; the reusable assembly contracts (middleware build, typed resource wiring) live in `src/synapse/app/agent_assembly.py`. Keep domain algorithms in their own packages.
- New features belong in the matching domain package; cross-domain wiring goes in `app/` or an explicit runtime middleware.
- Console-script entry points live in `src/synapse/entry.py` (`synapse`), `src/synapse/web.py` (`synapse-web`, textual-serve TUI in the browser), `src/synapse/web_console/entry.py` (`synapse-web-console`, loopback React console host), `src/synapse/acp/server.py` (`synapse-acp`), and `src/synapse/runtime/daemon/entry.py` (`synapse-runtime`). Keep entry modules thin; delegate to a domain package. These scripts only exist after an install/sync step (`uv sync`); in a stale source checkout run the module form (`python -m synapse.web_console.entry`) instead, and start the console host/daemon detached with redirected stdout/stderr — see `README.md` section "Web 控制台".
- `config.py`, `models_registry.py`, and `subagents.py` are compatibility re-export layers. Import from `synapse.settings`, `synapse.models.registry`, or `synapse.runtime.subagent_specs`/`synapse.runtime.subagents` in new code; keep legacy import paths working.
- `__init__.py` exports are public API. Keep necessary re-exports when moving implementations.
- Config merges user and project layers. When changing Settings, update `src/synapse/settings/schema.py`, `src/synapse/settings/config_paths.py`, `tests/test_config.py`, `tests/test_layered_config.py`, and the user docs (`README.md`, `docs/config.md`).
- `AGENTS.md` is statically injected by the agent-md middleware (`src/synapse/app/agent_md.py`), independent of writable memory; never route it back into memory writes.
- The browser console frontend lives in `web/` (React 19 + TypeScript + Vite, built to `web/dist/`); its conventions are in `web/AGENTS.md`. Read that file before changing anything under `web/` — this root file is the only one the agent-md middleware injects, so `web/AGENTS.md` is not loaded automatically.

## Coding standards

- Ruff is the only automated Python baseline: line length 100, target Python 3.12, rules `E/F/I/B/UP` with `B008` ignored (see `[tool.ruff]` in `pyproject.toml`).
- Annotate new or modified public functions, complex state transitions, and compatibility branches.
- Catch broad exceptions only at explicit degradation boundaries, and explain the fallback; never swallow core business errors silently.
- Avoid unbounded reads, searches, and terminal output; cap logs, tool results, and external data.
- Never leak API keys, tokens, `.env` contents, or private user config in code, tests, docs, or output.
- Update README/docs when user-visible behavior changes; skip doc churn for internal-only changes.

## Development & testing

Run `uv sync` after the first install or dependency changes.

Verification order: narrowest test → domain tests

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

CI runs lint on ubuntu-latest and tests on Windows/Linux with Python 3.12/3.13. Platform-specific changes must at least pass on the current machine and be reviewed for the other platform.

A separate ubuntu `contract` job is the only gate on the generated contract: it runs `uv run --no-sync python scripts/export_contract_manifest.py --check`, the targeted `tests/test_runtime_contract_manifest.py`, `tests/test_runtime_architecture_boundaries.py`, `tests/test_runtime_service_import_purity.py`, `tests/test_runtime_transport_client_compatibility.py` subset, and then `npx tsc -b` plus the web console's `runtimeContractFixture` / `runtimeClientBoundary` / `sourceGuard` tests. A wire-contract change has to keep all of those green.

Docs and packaging:

```powershell
uv run --no-sync mkdocs build   # after touching docs/, README.md, or mkdocs.yml
uv build                        # after packaging/entry-point changes
```

## Rust/PyO3 native crates

The Python app must work without the optional wheels and must keep `ImportError`/`OSError` fallbacks.

- `rust/synapse-core-tool` (`synapse-core-tool` on PyPI): native filesystem tools (read/edit/patch/search), math rendering, and the native OpenAI-compatible chat client (`RustOpenAIClient`, backed by `async-openai` byot). Release with tag `synapse-core-tool-v*`; `native-core-tool-wheels.yml` builds wheels and publishes to GitHub Release + PyPI. Keep the `pyproject.toml` version in sync with `Cargo.toml` — maturin names wheels from `pyproject.toml`.
- `rust/synapse-tool-compress-core` (`synapse-tool-compress-core` on PyPI): native tool-output compression core. **Required dependency** of `synapse-cli-agent`. Release with tag `synapse-tool-compress-core-v*`; `native-compression-wheels.yml` builds wheels and publishes to GitHub Release + PyPI. Keep the `pyproject.toml` version in sync with `Cargo.toml`. Because the main package hard-depends on it, publish the compress-core wheels to PyPI before tagging the main `v*` release.

### Building and installing a native crate locally

`uv` builds the path dependencies declared in `[tool.uv.sources]` (`rust/synapse-core-tool` and `rust/synapse-tool-compress-core`). After changing Rust code, rebuild + reinstall the affected crate with:

```powershell
uv sync --reinstall-package synapse-core-tool
```

This runs maturin under the hood and installs the freshly built wheel into the venv. Do **not** rely on `maturin develop` — a plain `uv sync` afterwards re-copies the cached (stale) wheel over the develop install. To avoid that surprise, either always rebuild via `uv sync --reinstall-package ...`, or run every command with `uv run --no-sync` after a manual `maturin develop`.

When changing either crate, at least run:

```powershell
cargo test --manifest-path rust/<crate>/Cargo.toml
cargo fmt --manifest-path rust/<crate>/Cargo.toml --check
```

If Python bindings/APIs change, rebuild via `uv sync --reinstall-package <crate>` and run the related Python tests.

Keep the Apache-2.0 SPDX headers in `rust/synapse-tool-compress-core/src/headroom_port/*.rs`, the crate's `LICENSE` and `NOTICE` at `rust/synapse-tool-compress-core/`, and the upstream attribution intact; do not introduce excluded network calls or model downloads.

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
