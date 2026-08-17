# Changelog

All notable changes to this project are documented in this file.

Each release section starts with `## v{version}` and ends before the next `## ` heading.
The release workflow automatically extracts the matching section as release notes.
All entries are written in English.

---

## v0.1.35

### New Features

- Added a TUI model manager for add/edit/delete/set-default model profiles with instant hot reload (`/model manage`, `m` in the model picker).
- Added Codex config import: detects `~/.codex/config.toml` and `auth.json`, maps model_providers/profiles into `models.json` with an OpenAI-compatible fallback and conflict preview (`/model import-codex`).
- Added a supported provider catalog dialog (`/model providers`).
- Show turn number in bottombar turn-stats chrome.
- Added accurate token-rate/TTFT stats to bottombar turn-stats.

### Bug Fixes

- Localized bottombar turn chrome to English.
- Rendered mermaid text on Linux via DejaVu Sans fallback.
- Added built-in osascript fallback for macOS clipboard image paste.

### Engineering

- Added `synapse.models.persist`, an atomic CRUD layer for layered `models.json`.
- `ModelProfile` gains `provider`/`wire_api` metadata fields with backward-compatible parsing.

---

## v0.1.34

### Bug Fixes

- Fixed clipboard image paste on Linux/WSLg: WSLg syncs Windows screenshots as `image/bmp`, so the Linux reader now tries `image/png`, `image/bmp`, and `image/jpeg` and normalizes non-PNG data to PNG.
- Fixed pasted image bytes being rendered as a text placeholder (`[BP... chars]`) instead of an image attachment by detecting mangled image signatures with binary noise and falling back to a clipboard image re-read.
- Fixed multi-line and long Shift+Insert pastes being truncated to the first line; full content is preserved and expanded on submit.

### Engineering

- Added clipboard unit tests for BMP normalization, explicit text MIME requests, and mangled-prefix detection.

---

## v0.1.33

### New Features

- Added per-subagent model and reasoning-effort configuration in the TUI, with layered configuration and display metadata for subagent tool calls.
- Improved subagent activity rendering with live reasoning, tool-calling, and answering phases, including attribution for concurrent activity.

### Bug Fixes

- Fixed subagent tool-call inheritance and model selection behavior.
- Enforced non-overridable file-tool path rules in subagent prompts.
- Fixed TUI model and subagent picker keyboard interactions.
- Fixed subagent sub-call display windows so recent calls remain visible as new calls arrive.
- Fixed long-line Ruff violations in selectable-text and cancellation status rendering.

### Engineering

- Simplified subagent specifications and removed the obsolete DAG parallel-subagent runtime.
- Improved TUI responsiveness by batching session replay and moving blocking work off the event loop.
- Updated the bundled `synapse-core-tool` dependency metadata to v0.1.3.

---

## v0.1.32

### New Features

- Added the ACP v1 adapter: a full agent collaboration protocol server exposing session lifecycle, permissions, content/prompt handling, per-session MCP servers, client services, and streaming plan/diff events. See `docs/acp-adapter/` for the capability matrix and phase documentation.
- ACP provider management and model selection: clients can manage providers, select the active model per session, and send image prompts.
- ACP sessions are bridged to the project-level TUI session store, so TUI and ACP clients share the same per-project recent-session history.
- The herdr integration now reports agent lifecycle state (startup, turn, shutdown) to the herdr backend.

### Bug Fixes

- Fixed ACP ignoring the project's `mcp.json`: project MCP servers are now always merged with MCP servers supplied by the client.
- Fixed placeholder-titled sessions staying invisible in the TUI recent-sessions view: listing ACP sessions now repairs sessions created before the TUI bridge existed, and Zed's global history request (without `cwd`) includes TUI sessions through the user-level project projection.
- Fixed the transcript projection migration worker failing under the packaged Windows executable: frozen builds now route through a hidden `transcript-migration-worker` CLI subcommand instead of `python -m ... --worker`.

### Engineering

- Extracted the shared `run_transcript_migration_worker` worker body so source installs and frozen binaries execute identical migration logic.
- Extended ACP bridge and migration tests to cover placeholder-title repair, global history merging, and the frozen worker command path.

---

## v0.1.31

### New Features

- The `Ctrl+O` / `Ctrl+Tab` recent-sessions switcher now lists the most recently changed sessions across every registered project (from the user-layer catalog) instead of current-project history only, and marks each row's live runtime status (`running` / `queued` / `idle` / `cold`, etc.). Sessions without an in-process runtime are shown as cold history, and the list degrades gracefully to the legacy view when the catalog is unavailable.

### Bug Fixes

- Fixed the standalone Windows executable exiting immediately after launch: the PyInstaller entry script only defined `main()` without invoking it, so the packaged binary ran the top-level code and quit. `synapse.entry` now has an `if __name__ == "__main__"` guard and the packaged executable runs the CLI correctly.
- Fixed the release executable assets: the Linux and macOS binaries shared the same filename (`synapse`) and overwrote each other when merged into the release. Each platform is now uploaded under a distinct name: `synapse-windows-x64.exe`, `synapse-macos-arm64`, and `synapse-linux-x64`.

---

## v0.1.30

### New Features

- TUI startup timing trace: the startup report now covers the full CLI module-tree import (typer, settings, stream modules) that previously fell outside the trace, making startup bottlenecks visible.
- Faster TUI startup: the model registry prewarm is optimized so the interface appears sooner.

### Engineering

- Release workflow now builds standalone single-file executables for Windows, Linux, and macOS (PyInstaller) and attaches them to the GitHub Release; the GitHub Release creation was moved to a dedicated job that waits for both the Python distributions and the executables, while PyPI publishing stays independent.
- Translated AGENTS.md and CHANGELOG.md to English; updated the README homepage demo media with an animated GIF and an mp4 fallback source.

---

## v0.1.29

### New Features

- TUI image viewer: clicking an image in the transcript opens it enlarged in a modal window; fixed accidental dismissal when the click races with modal stack switches.
- TUI Mermaid rendering optimization: Mermaid images are rendered on a background thread with a placeholder shown meanwhile, preventing native rendering from blocking the event loop.
- Notify the foreground session when background sessions finish; carry the cancellation reason across event boundaries.

### Bug Fixes

- Fixed the image viewer click-to-dismiss possibly removing the default screen and raising ScreenStackError.
- Fixed a race in Mermaid placeholder rendering during session switches; results are swapped in only when still current and mounted.

### Engineering

- Published synapse-core-tool native extension 0.1.2 (fixed the PyPI upload failure caused by an unsynced pyproject.toml version).
- Added `make_mermaid_widget_from_png` to decouple PNG rendering from widget construction and make the UI-thread boundary explicit.

---

## v0.1.28

### New Features

- TUI in-terminal LaTeX math rendering: display math (`$$...$$`) blocks in the transcript are rendered to PNG images via the native RaTeX kernel, with configurable foreground/background colors, font size, padding, and device pixel ratio; no Node.js, browser, or system TeX required.
- TUI image rendering enhancements: pasted images can be previewed in the terminal (textual-image), transcript images are shown, and clicking an image opens it enlarged in a modal window.
- Session switcher: Ctrl+Tab quickly switches among the 10 most recent sessions with the current item highlighted; cold history is merged into recents.
- Project drawer bound to F12 with interaction support; readability and rendering stability improvements.
- Cross-project in-process switching and concurrent multi-session support (runtime/TUI decoupling refactor).

### Bug Fixes

- Fixed the exit deadlock and added exit-time diagnostics.
- Fixed turn rotation content loss after session switches, replay terminal-state closure, and broken-link rendering freezes.
- Fixed event rendering blocking input.
- Isolated per-session model and MCP state to avoid cross-session interference.
- Fixed the reservation/settlement race in goal follow-up turns.

### Engineering

- Runtime/TUI decoupling: the stream parser moved out of the UI package; tool events are emitted structurally with tool item lifecycle preserved.
- Added `render_math_png` to the synapse-core-tool native extension (RaTeX kernel + embedded KaTeX fonts).

---

## v0.1.27

### New Features

- TUI topbar shows token output rate and time-to-first-token (TTFT) in real time.
- README redesigned and split into Chinese and English versions; `docs/sessions.md` updated with session docs.

### Bug Fixes

- Fixed Esc cancellation and goal state not being preserved after the TUI controller split (goal cancel state kept across controllers; active goal paused on Esc).
- Fixed legacy transcript projections migration blocking the main thread; now runs in a subprocess.
- Fixed a second scrollbar appearing in dialog modal screens and duplicate scrollbars in the git changes popover.

### Engineering

- Split `tui.py` into domain controllers (ChromeController, TranscriptController, SlashController, PromptController, TurnController); completed the controller extraction and fixed the Codex usage threading model.
- Limited TUI session and tool output memory usage to reduce long-session resource consumption.

---

## v0.1.26

### New Features

- Restored the `synapse run` one-shot command: headless mode runs a single task then exits, with `-w/--workspace`, `-m/--model`, `--readonly`, `--thread-id`, `--debug`, and `--stream/--no-stream` flags; adapted for async-only model clients (`ainvoke`).
- `build_coding_agent` gained `backend=` and `system_prompt=` injection parameters, letting evaluation harnesses bridge the agent to remote/container backends and override the default coding prompt.
- TUI session transcripts now use paginated rendering (compact projection) for better long-conversation browsing performance.
- Added heap dump export (`observability/heap_dump.py`) for diagnosing long-session memory issues.

### Bug Fixes

- Fixed TUI session switches not releasing the previous session's resources (stream sink / rail widgets).
- Fixed the goal accounting disabled middleware constructor crash with `enable_goals=False` (`goal_middleware_disabled() takes no arguments`).

### Engineering

- Git Explore dialog and turn rail components updated with matching unit tests (transcript projection, heap dump, selection, dialogs, etc.).

---

## v0.1.25

### New Features

- `synapse-web` gained a `--public-url` argument: specifies the WebSocket and static asset URLs of the page generated by `textual-serve`, supporting nginx/TLS reverse proxy setups (`https://` auto-derives `wss://`).

### Bug Fixes

- Fixed the browser being unable to connect when WebSocket/static asset URLs inside the page used `http://<host>:<port>` behind an nginx reverse proxy.

### Engineering

- Docker deployment docs added a config example for binding the container to the loopback interface plus an nginx 443 reverse proxy.

---

## v0.1.24

### New Features

- Added the `synapse-web` console entry point (`synapse.web:main`): start the Web TUI from an installed wheel without source code; supports `--host`, `--port`, and `--workspace`.
- Added the minimal `Dockerfile.web` image: based on `python:3.12-slim`, no Rust toolchain, all runtime deps installed from prebuilt wheels; supports `pypi` (install the published version from PyPI) and `local` (install the local wheel in the build context) build modes.
- Added `.dockerignore` and Docker deployment docs (`docs/docker-web.md`) covering remote-machine builds, running, port mapping, and `/workspace` data persistence.

### Engineering

- `scripts/serve_web.py` became a thin compatibility wrapper around `synapse.web`.
- Docs navigation gained a Docker Web TUI page.

---

## v0.1.23

### New Features

- Rich Markdown rendering integrated with the Synapse theme system: built-in themes provide a full Markdown style mapping, so headings, paragraphs, emphasis, inline code, quotes, links, lists, and tables follow theme colors.
- `themes.json` supports overriding Rich styles for Markdown elements via a dedicated `markdown` field, with theme inheritance and layered config.
- Switching themes at runtime redraws displayed answers, thoughts, and standalone Markdown blocks; the theme designer preserves Markdown style config when saving.

### Bug Fixes

- Fixed Rich Markdown using built-in default colors so Markdown content did not follow Synapse theme switches.
- Fixed the theme designer possibly dropping hand-written Markdown style overrides when saving an existing theme.

### Engineering

- Added tests for Markdown theme parsing, Rich style injection, theme inheritance, runtime redraw, and built-in theme completeness.
- Added Markdown theme config docs, keeping the note that code blocks use `code_theme` for syntax highlighting.

---

## v0.1.22

### New Features

- TUI session restoration now loads paginated history: startup renders only the most recent `AGENT_HISTORY_TAIL_TURNS` turns (default 20); scrolling to the transcript top asynchronously loads older history while preserving scroll position, avoiding startup lag on very long sessions.
- Added the `AGENT_HISTORY_TAIL_TURNS` setting controlling how many recent visible turns the TUI renders at startup (environment variable and layered `settings.json` supported).

### Bug Fixes

- Fixed the request-generation race in asynchronous history pagination: after switching/reloading sessions, stale pagination worker callbacks no longer clear the current request's loading state or insert outdated data.

### Engineering

- Pinned direct dependency versions to the corresponding `uv.lock` versions.

---

## v0.1.21

### New Features

- Refactored the session tool `list_sessions` into `search_session`: added a local incremental full-text index (`SessionSearchIndex`) matching session message bodies as well as title/summary/model; an empty `query` lists recent sessions with `limit`/`offset` pagination and summaries.
- `read_session` supports `include_tools` (tool messages dropped by default, output size reduced by up to 13x) and `offset`/`limit` turn pagination.
- Added the native `patch` tool; `read_file`/`edit_file`/`patch` all route to the encoding-preserving native implementations in `synapse-core-tool`.
- Added the `AGENT_EXPAND_THINKING` setting controlling expansion/collapse of reasoning blocks.

### Bug Fixes

- Fixed `load_messages_from_checkpointer` reading only the latest checkpoint so messages were missing under the newer SqliteSaver (delta storage): delta reconstruction first, old logic as fallback.
- Fixed concurrent write-lock contention in the `search_session` index (`database is locked`): WAL + busy_timeout + degraded sync fallback.
- Fixed JSON type detection running after LOG and failing to recognize bracket-style logs.
- Fixed long dialog line labels being truncated.

### Engineering

- Migrated and renamed the filesystem core from `synapse-search-core` to `synapse-core-tool` (added native read/edit/patch); the CI workflow migrated to `native-core-tool-wheels.yml`.
- Hid DeepAgents built-in `ls`/`glob`/`grep` and added a prompt middleware injecting authoritative file-tool guidance.
- Adjusted the main TUI welcome logo animation timing.
- Cleaned up `list_sessions` legacy name remnants (docstrings, error messages, prompts, docs).

---

## v0.1.20

### New Features

- LLM Debug Inspector can capture and show raw HTTP request/response payloads for diagnosing model transport issues.
- The main TUI displays sub-agent run status inline and dynamically picks the DAG parallel execution path based on task complexity.
- System prompts now always inject the current shell syntax rules to reduce PowerShell/Bash/cmd mixing.

### Bug Fixes

- Improved `search_files` ripgrep-compatible pattern examples, parameter bounds, and `glob` include-only semantics; when the native include-glob unexpectedly returns empty results, the same Rust core enumerates candidates and degrades the search, avoiding false negatives from valid `*.py` / `**/*.py` filters.
- Fixed reasoning content extraction and streaming display across Responses API, Anthropic thinking blocks, and more, and avoided duplicating the answer after reasoning.
- Enabled SOCKS proxy support for the HTTP client and fixed related proxy config unavailability.

### Engineering

- Added full glob regression tests for the `search_files` StructuredTool against the Rust native core, and verified that documented regex examples compile with the actual matcher.
- Updated tests for agent assembly, streaming UI, HTTP transport, prompt injection, and sub-agent state.

---

## v0.1.19

### New Features

- Codex OAuth usage bottom-bar component: shows the 5h/1d usage window, reset remaining time, and account expiry; turns red below 50%.
- Codex rate-limit reset capability: reads available reset count and expiry details directly via HTTP from wham/rate-limit-reset-credits, with one-click consumption in a dialog.
- `/codex reset` and `/codex credits` open the reset details dialog; the bottom-bar Codex area supports hover highlight and clicks.
- Friendly config-error messages at startup: malformed models.json, settings.json, or inline JSON env vars print a concise error with fix hints instead of a full traceback.

### Bug Fixes

- `/compact` now runs on a background worker so model summarization does not block the TUI; cancellation is disabled during the run to prevent corrupted compression state.
- Compatible with SummarizationMiddleware lookup in LangChain 1.3 compiled graph closures.

---

## v0.1.18

### New Features

- Added the LLM Debug Inspector (`F11`) monitoring model communication, tool calls, and token consumption in real time.
- The Inspector supports capture toggling, follow-latest, type filtering (errors/tools/slow calls), turn folding, and call detail views.
- The Inspector overview bar shows the failure rate (based on tool-level error detection); the tools tab lists failed tools and reasons.

### Bug Fixes

- TUI: `F10` restores the delete-session dialog entry; fixed the mouse selection vs. click-copy conflict, auto-copy after drag selection.

### Engineering

- `DebugCaptureRecord` gained tool-level error detection (LangChain `ToolMessage.status` + content patterns).
- `_tool_pairs` returns an `error` field distinguishing "pending" (result null) from "real failure" (error content present).
- The Inspector frontend counts only truly failed tools in the failure rate; "awaiting response" is excluded.

---

## v0.1.17

### New Features

- `find_files` / `search_files` gained `context_lines`, `case_insensitive`, `head_limit`, and `offset` parameters.
- `search_files` supports case-insensitive matching (`case_insensitive`, implemented by the native `synapse-search-core` engine).
- Pagination (`head_limit` + `offset`) supported, so agents can page through results instead of fetching everything at once.

### Engineering

- Tool Pydantic schemas no longer define the `intent` field; it is managed uniformly by the `build_intent_schema_middleware` middleware.
- Added Synapse's own file search tools `find_files` / `search_files`, excluding the deepagents built-in `ls`/`glob`/`grep` tools.
- Fixed the `glob`/`grep` tool names in system prompts to `find_files` / `search_files`.
- Upgraded `synapse-search-core` to 0.1.1 (added the `case_insensitive` parameter).

---

## v0.1.16

### New Features

- Added the required `synapse-search-core` native search core, using Rust ripgrep crates for regex `grep` and `glob`.
- `grep`/`glob` now use the built-in native engine instead of relying on host `rg` or the DeepAgents Python search fallback.
- Native search wheels published to PyPI for Windows x86_64, Linux x86_64/aarch64, and macOS Apple Silicon arm64.

### Engineering

- Kept the Python backend's workspace path authorization, virtual path mapping, and `deny_paths` filtering.
- Added the native search wheel build and PyPI Trusted Publishing workflow, plus backend regression tests and distribution docs.

---

## v0.1.15

### Engineering

- Split the TUI transcript, tool group, todo list, user message, and turn rail widgets, narrowing `tui.py`'s responsibilities.
- Preserved existing component, formatting function, and timeline symbol compatibility exports in `synapse.ui.tui`.
- Kept dynamic themes, streaming display, text selection, copy, and turn rail interactions, with TUI regression tests.

---

## v0.1.14

### Engineering

- PyPI project page gained homepage, source repository, issue tracker, and changelog links.

---

## v0.1.13

### New Features

- Supports publishing `synapse-cli-agent` distributions automatically via PyPI Trusted Publisher.

### Engineering

- Install docs added a PyPI installation path without cloning the repo; `uv` can manage the required Python version automatically.

---

## v0.1.2

### Bug Fixes

- Fixed unresolved `le16toh` / `be16toh` symbols from tree-sitter in the `synapse-tool-compress-core` manylinux2014 wheel so the native extension imports.

### Engineering

- Pinned the native wheel build to the manylinux2014 compatibility target and added an install/import smoke test.
- Synced the Rust crate and Python wheel release versions to `0.1.2`.

---

## v0.1.11

### New Features

- The F5 MCP Tools panel supports toggling the selected MCP server's enabled state with `d` and rebuilds the agent automatically; the state is not written to `mcp.json`.
- Tool output path compression gained clearer statistics and diagnostics, and the compression pipeline was optimized.

### Bug Fixes

- Fixed the responsibility conflict between session-switch and delete shortcuts.

### Engineering

- Expanded `AGENTS.md` with repo structure, architecture constraints, testing, and release collaboration conventions.

---

## v0.1.10

### Engineering

- Reorganized core domain modules (app, commands, runtime, content, sessions) to tighten responsibility and dependency boundaries.
- Split the tool output pipeline into model, repository, detection, and transformation layers, keeping existing public APIs.
- Split slash commands by compression, session, MCP, model, and theme responsibility, keeping a unified dispatch entry.
- Split stream processing into rendering, event normalization, and runtime iteration layers, compatible with existing CLI and TUI call paths.
- Extracted TUI turn rail and user-turn formatting logic to reduce main app module complexity.
- Split model config parsing, Profile, and settings/capability helpers while keeping provider factories and mock contracts stable.

---

## v0.1.9

### New Features

- Compact tool description middleware: replaces verbose upstream tool schema descriptions (~4K chars → ~200 chars) to reduce token overhead.
- Cache-aware compression control plane: tracks provider cache hits/writes in real time, distinguishing cached input from new input.
- Compression diagnostics panel: profile-driven content breakdown and optimization opportunity ranking (TUI `Ctrl+D`).
- Interaction ledger: turn-level and model-call-level association tracking.
- Topbar real-time compression indicator: shows the active zone and compression state.
- `/tool-output` slash command to view tool output transformation statistics.
- GitSummaryTransformer: recognizes `git status`/`git diff --stat` output and summarizes it intelligently.

### Bug Fixes

- Avoided search content detection being misjudged as a critical-line fallback.
- Fixed the Alt+C copy crash.

### Engineering

- Migrated the default path from `.coding-agent` to `.synapse` (checkpoint / sessions / memory).
- Lowered the tool output compression threshold to 512 bytes.
- CI publishes native compression wheels to GitHub Release (instead of PyPI).

---

## v0.1.8

### New Features

- Tool output transformation pipeline: large tool results are archived to a JSONL journal and replaced with a boundary preview + `tool-result://` reference, avoiding LLM context blow-up.
- Rust native compression core (`synapse-tool-compress-core`): smart summarization engine SmartCrusher with code/diff/log/search-specific compressors, BM25 relevance ranking, and an adaptive sizer.
- CLI `--diagnose-tool-output` flag to view per-tool-output transformation statistics.
- F4 multi-select delete sessions + F5 MCP group folding + unified multi-select UI.

### Bug Fixes

- MCP deferred state no longer incorrectly displays as "mcp err".

### Engineering

- Refactored `tool_results.py` into `tool_output.py` + `tool_output_middleware.py` with clearer responsibilities.
- Added the `tool_output_eval.py` evaluation framework.
- CI added the native-compression-wheels workflow for building Rust wheels.

---

## v0.1.7

### New Features

- Slash command TUI output upgraded to Markdown rendering; session/MCP/theme data shown with Rich tables.
- AgentMdMiddleware: statically injects `AGENTS.md` into the system prompt, decoupled from memory.
- MCP per-tool filtering: tools can be toggled in the UI and persisted to config.

### Bug Fixes

- Slash commands typed in a fresh session no longer fail to show in the TUI (the welcome page covered `#log`).
- Shell default is platform-aware: non-Windows uses `bash` instead of `pwsh`.
- `prompt_border` validated against the Textual whitelist; added `panel` style support.

### Engineering

- Added the MkDocs + GitHub Pages docs site.
- Subagents and memory disabled by default; pruned redundant middleware prompt blocks.
- UI: `steer` renamed to `queue`; simplified the bottombar mode label.

---

## v0.1.6

### New Features

- OpenAI Responses API WebSocket transport for lower latency.
- Welcome page animation rework: left-to-right type cursor + sweeping dot-by-dot delete loop.
- Braille logo dot-matrix show/hide animation using only muted/fg theme colors, no intermediate colors.
- Type cursor effect: new characters briefly highlight in accent then fade to fg.
- `prompt_border` field supports theme-defined input border styles (tall/heavy/dashed/dotted/double/round/solid).
- Backend glob/grep tools automatically skip paths matched by `.gitignore`.

### Bug Fixes

- WebSocket: refreshes the async API key before the handshake to avoid gateway 401s.
- WebSocket: disables ping timeout to prevent disconnects during inference.
- WebSocket: filters the Chat Completions-only `thinking` field.
- TUI: theme designer backdrop fully transparent.
- TUI: fixed the missing `_open_theme_designer` callback.
- TUI: removed duplicate `apply_theme` calls in `_save_theme` to avoid UI freezes.
- TUI: git changes popover remounts to avoid DuplicateIds errors.
- SteerQueue: fixed reentrant deadlock and queue loss after graph rebuilds.
- Fixed sub-agent tool call timeline rendering.
- Fixed alt-v multi-line paste being truncated.

### Performance

- Fully async model clients, removing synchronous OpenAI client blocking.
- Faster model switching and shutdown flows.

### Engineering

- Removed cancel-seal diagnostic log output.
- SteerQueue stays visible during active turns.
- Simplified the agent tool surface, reducing unnecessary tool exposure.
- Raised the transient model failure retry cap.

---

## v0.1.5

### New Features

- Added a standalone image recognition service: non-multimodal main models can use a dedicated `vision_model` to turn images into text descriptions before handing them to the main model.
- `vision_model` configurable in `models.json` / `settings.json`; any OpenAI-compatible vision service can be used (Qwen-VL etc.).
- The vision service supports an independent `think` toggle (does not affect the main model's thinking mode), an `allow_remote_urls` security policy, timeout retries, and fallback models.
- Auto-infers whether the main model natively supports image input (by provider/model name), with explicit `image_input` override support.

### Bug Fixes

- Fixed mermaid / git-explore call freezes.
- Fixed garbled git output encoding on Windows.

### Engineering

- Added the `vision_middleware` and `describe_image` modules.
- Added vision service tests and an API check script.

---

## v0.1.4

### New Features

- Added read-only discovery, preview, and import of Codex sessions, with CLI and TUI pickers.
- Import uses terminal-state checkpoint seeding with a ledger, supporting idempotent reuse and crash recovery.

### Bug Fixes

- Fixed missing Codex history caused by state DB expiry, empty threads, Windows extended paths, and long metadata headers.
- Supports `subagent.thread_spawn` sub-agent sessions, generating picker and import titles from the first user message.
- Added backoff retries for recoverable model 5xx failures, with retry state shown in the TUI.

### Engineering

- Expanded Codex discovery, import, TUI, and retry regression coverage.

---

## v0.1.3

### Bug Fixes

- Fixed 49 ruff lint errors (E501 long lines, UP042 StrEnum, F401/F811 unused imports, I001 import sorting).

### Engineering

- CI now triggers only on PRs to avoid duplicate builds with the Release workflow on tag pushes.

---

## v0.1.2

### Bug Fixes

- Fixed the Release workflow CHANGELOG extraction script mistaking a shell variable for a Python variable; now reads via `os.environ`.

---

## v0.1.1

### Engineering

- Added `CHANGELOG.md`; release notes are now extracted automatically from the matching version section.
- Fixed `release.ps1` to push the branch commit when tagging, so the tag never lands without the code.
- Updated the `AGENTS.md` release flow: AI analyzes changes and writes changelog entries.

---

## v0.1.0

Initial release. A local AI coding agent built on LangChain Deep Agents.

### New Features

- Autonomous coding loop: read/edit code, run commands, run tests, Git operations.
- Sub-agent collaboration: built-in researcher / tester / reviewer with automatic task decomposition and parallel execution.
- MCP protocol support, integrating the external tool ecosystem.
- Multi-model switching: OpenAI / Anthropic / DeepSeek / any OpenAI-compatible gateway.
- TUI terminal interface (Textual): slash command completion, real-time streaming, shortcuts.
- CLI commands: `run` / `chat` / `tui` / `sessions` / `models` / `mcp` / `version`.
- Layered config: user-global + project-local, secrets written to models.json.
- Skills system: reusable capability units for Agent Skills.