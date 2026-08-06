

# Synapse

A local Synapse based on **LangChain Deep Agents**.

This project is released under the [Apache License 2.0](LICENSE). Third-party dependencies and Rust subcomponents may use their own licenses; please retain the corresponding license and NOTICE files when distributing.

- Harness: `deepagents.create_deep_agent`
- Backend: `LocalShellBackend` (**no sandbox**)
- Default Approval: **Disabled / Auto-Approved**
- Dependency Management: `uv`

## Features

| Capability | Description |
|---|---|
| Read/Edit Code | `read/write/edit/glob`; directory and search invoke project commands via `execute` |
| Execute Commands | `execute` local shell |
| Planning | `write_todos` |
| Sub-agents | Default `researcher` / `tester` / `reviewer` (`task` delegation) |
| Custom Tools | Session lookup tools; git/test/search etc. invoke project commands via `execute` |
| Long-term Goals | `/goal <objective>` sets cross-turn persistent goals; Agent automatically continues until completion/blocked/budget exhausted; `get_goal`/`create_goal`/`update_goal` tools + token/time usage accounting |
| Memory | `AGENTS.md` |
| Skills | `skills/**` (Agent Skills frontmatter) |
| Sessions | sqlite checkpointer + session metadata management |
| Global Project Catalog | User-level `~/.synapse/catalog.sqlite` projects session metadata and run records across all projects, supports cross-project viewing/searching (`synapse projects ...`) |
| Multi-model | `ModelRegistry` (single-model compatible / JSON multi-profile) |
| MCP | Injected as tools after configuring MCP Server |
| Permissions/Read-Only | `FilesystemPermission` + `HarnessProfile.excluded_tools` |
| CLI | `run` / `chat` / `tui` / `sessions` / `projects` / `models` / `mcp` / `version` |
| Optional HITL | `--require-approval` (disabled by default) |

## Installation

### Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/) (automatically installs and manages required Python >= 3.12)

### Method 1: Install as a System CLI Tool from PyPI (Recommended)

No need to clone the repository or pre-install Python:

```powershell
uv tool install synapse-cli-agent
```

For developers to install editably from local source:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install.ps1 -Force
# Or: uv tool install --editable --force .
```

Use directly from any directory after installation:

```bash
synapse tui -w .
synapse run "View the current repository structure and summarize it" -w .
```

The repository root also provides a thin launcher `synapse.cmd` (prioritizes PATH entry, then `.venv\Scripts`, finally falls back to `uv run`).

### Method 2: Local venv Development

```bash
# Sync dependencies
uv sync

# Use venv entry (Windows)
.\.venv\Scripts\synapse.exe tui -w .

# Or module entry
uv run python -m synapse tui -w .

# Legacy compatibility
uv run synapse chat -w .
```

### Uninstall

```powershell
uv tool uninstall synapse-cli-agent
# Or
powershell -ExecutionPolicy Bypass -File scripts/install.ps1 -Uninstall
```

## Quick Start

After installation and configuration, you can run:

```bash
# TUI interactive interface (Recommended)
synapse tui -w .

# One-shot execution
synapse run "Summarize the current project structure" -w .

# CLI chat
synapse chat -w .
```

### Session Management

```bash
synapse sessions list
synapse sessions export <thread_id> -f md
# Default writes to .coding-agent/exports/<thread_id>.md; add --stdout to print to terminal
```

### Model & MCP Management

```bash
synapse models list
synapse mcp list
```

## Configuration

Synapse adopts a **layered configuration** strategy: user global configuration (`~/.synapse/`) and project local configuration (`<workspace>/.synapse/`) are merged, with the project layer overriding the user layer.

```
~/.synapse/              # User global layer (lower priority)
  models.json            # Model profiles + api_key (recommended)
  mcp.json               # MCP Server definitions
  settings.json          # Non-sensitive Settings overrides
  themes.json            # Custom UI themes

<workspace>/.synapse/    # Project layer (higher priority, overrides user layer)
  models.json
  mcp.json
  settings.json
  themes.json
  system_prompt.md       # Custom system prompt
  sessions.sqlite
  checkpoints.sqlite
```

### Method 1: Environment Variables (.env Quick Start)

Create from the template and fill in your keys:

```bash
cp .env.example .env
```

Core Variables:

| Variable | Required | Description |
|---|---|---|
| `MODEL` | Yes | Model identifier, e.g., `openai:gpt-4.1`, `openai:deepseek-chat` |
| `OPENAI_API_KEY` | Yes* | OpenAI-compatible API key |
| `OPENAI_BASE_URL` | No | Custom API endpoint (required for relay/local services) |
| `OPENAI_WEBSOCKET` | No | Use WebSocket for standard Responses API; defaults to `false` (HTTP/SSE). Transient disconnects reconnect according to model `max_retries`, and fall back to HTTP/SSE if retries are exhausted before content is output |
| `ANTHROPIC_API_KEY` | No | Anthropic native API key |
| `WORKSPACE` | No | Workspace path, defaults to `.` |
| `SHELL_EXECUTABLE` | No | Shell type, defaults to `pwsh` (options: `cmd`/`bash`) |
| `SHELL_TIMEOUT` | No | Command timeout in seconds, defaults to 120 |
| `TOKEN_STREAM` | No | Token-level streaming output, defaults to `true` |
| `PARALLEL_TOOL_CALLS` | No | Concurrent tool calls, defaults to `true` |

See `.env.example` for the complete variable list.

### OpenAI Codex OAuth (ChatGPT Plus/Pro)

In addition to standard API Keys, Synapse can use ChatGPT OAuth login for Codex. Credentials are only saved to the user directory `~/.synapse/openai_oauth.json` and will not be written to project configurations or displayed in the terminal.

```bash
# Browser login; add --no-browser to manually open the terminal-printed link
synapse auth openai login

# If already logged in via Codex CLI on this machine, securely import its existing login state
synapse auth openai login --import-codex

synapse auth openai status
synapse auth openai logout
```

Then configure the model in `~/.synapse/models.json` or `<workspace>/.synapse/models.json`:

```json
{
  "default": "codex",
  "models": {
    "codex": {
      "model": "openai:gpt-5",
      "auth": "openai_oauth"
    }
  }
}
```

The OAuth profile forces the use of the Codex backend and cannot be overridden via `base_url`. This login method cannot be used with third-party OpenAI-compatible gateways and does not require setting `OPENAI_API_KEY`. The browser authorization callback is fixed to `http://localhost:1455/auth/callback`; if the port is occupied, close the occupying process and retry. Synapse will automatically convert the Agent's OpenAI `system` messages to `developer` messages accepted by the Codex backend, remove the DeepSeek-compatible `extra_body.thinking` field, only send Codex-supported reasoning parameters, and force set `store: false`.

#### Codex Fast Tier (service_tier=priority)

Setting `OPENAI_FAST_MODE=true` or running `/fast on` enables the Fast tier for the Codex OAuth profile:
Injects `service_tier=priority` into every Responses request (priority processing, higher cost). Toggle at runtime with `/fast`, `/fast on`, `/fast off`, `/fast status` without rebuilding the model; when enabled, a yellow `FAST` badge appears next to the model thinking level in the bottom bar. Only takes effect for models with `auth=openai_oauth`; third-party gateways are unaffected.

### Method 2: models.json (Multi-model profiles, Recommended)

Create `models.json` under `~/.synapse/` or `<workspace>/.synapse/`. Supports multiple profiles and custom model parameters (temperature, max_tokens, thinking, etc.).

Reference example: `examples/models.example.json`

```json
{
  "default": "primary",
  "models": {
    "primary": {
      "model": "openai:gpt-4.1",
      "api_key": "sk-REPLACE_ME",
      "websocket": false,
      "context_window": 128000,
      "temperature": 0.2,
      "max_tokens": 8192
    },
    "deepseek": {
      "model": "openai:deepseek-v4-pro",
      "api_key": "sk-REPLACE_ME",
      "base_url": "http://127.0.0.1:3000/v1",
      "context_window": 128000,
      "thinking": "high",
      "temperature": 0.2
    }
  }
}
```

The OpenAI Responses API supports both HTTP/SSE and standard LLM WebSocket. When `"websocket": true` is set in a profile, Synapse uses a persistent `/v1/responses` WebSocket; omit or set to `false` to maintain existing HTTP/SSE. Custom OpenAI-compatible gateways must implement this WebSocket endpoint, otherwise keep it disabled. WebSocket mode automatically enables `use_responses_api`.

Switch profiles via the `AGENT_ACTIVE_MODEL` environment variable or CLI arguments.

#### Image Recognition for Non-Multimodal Models

The main model and vision model are configured independently. When the main model profile omits `image_input`, Synapse infers based on common model names; unknown OpenAI-compatible models default to text-only. To override inference, set `"image_input": true/false` in the profile.

Configure the vision model in the `vision_model` top-level field of the same `models.json` (also supported in `settings.json`), allowing you to freely swap any OpenAI Chat Completions-compatible image input service:

```json
{
  "vision_model": {
    "model": "qwen-vl-max",
    "base_url": "https://your-vision-gateway.example/v1",
    "api_key_env": "VISION_API_KEY",
    "timeout_secs": 45,
    "max_input_bytes": 10485760,
    "max_retries": 2,
    "fallback_model": "qwen-vl-plus",
    "allow_remote_urls": false,
    "think": true,
    "prompt": "Describe the image for a text-only coding assistant. Extract visible text and errors."
  }
}
```

`vision_model.model`, `base_url`, and `api_key_env` can be changed; `api_key` is also supported but environment variables are recommended. `think` is an independent thinking toggle for the vision model; when enabled, it sends `thinking: {"type": "enabled"}`; it does not affect the main model. By default, only local/in-memory images are processed; set `allow_remote_urls` to `true` to forward HTTP(S) image URLs from messages to the vision service. This service is solely for converting images to text; once configured, image content is sent to the specified service. When the main model profile explicitly sets `image_input=false`, Synapse will also automatically inject the `describe_image` tool, allowing the Agent to proactively read and recognize images within the workspace; the tool explicitly returns unavailable when `vision_model` is not configured. Multimodal main models skip the middleware and will not inject this tool.

### MCP Server Configuration

Create `mcp.json` under `~/.synapse/` or `<workspace>/.synapse/`.

Reference example: `examples/mcp.example.json`

```json
{
  "servers": [
    {
      "name": "filesystem",
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
      "enabled": false
    },
    {
      "name": "anysearch",
      "transport": "streamable_http",
      "url": "https://api.anysearch.com/mcp",
      "headers": {
        "Authorization": "Bearer ${ANYSEARCH_API_KEY}"
      },
      "enabled": false,
      "tool_prefix": "anysearch__"
    }
  ]
}
```

Check status via `synapse mcp list` after configuration, and dynamically enable/disable in the TUI.

### Settings Override

Write non-sensitive configurations in `~/.synapse/settings.json` or `<workspace>/.synapse/settings.json`. Acts the same as `.env` environment variables but with higher priority (Project Layer > User Layer > Environment Variables).

## Usage Tips

### TUI Shortcuts

| Shortcut | Function |
|--------|------|
| `Ctrl+T` | Collapse/Expand recent tool group |
| `Ctrl+E` | Expand/Collapse recent Thought summary |
| `Ctrl+L` | Clear transcript |
| `Alt+C` | Copy current selection (copies latest answer if no selection) |
| `Ctrl+Shift+Y` | Copy latest assistant answer |
| `Ctrl+C` / `Ctrl+Q` | Exit |

Text Selection: Answers/Thoughts/Tool Groups/User lines in the transcript support mouse drag selection (with highlight). Use `Alt+C` to copy after selection.

### Slash Commands (chat / TUI)

- `/help` `/thread` `/new` `/sessions` `/session ...` `/switch <id>`
- `/rename` `/export [md\|json] [path]`
- `/model` `/model <alias>`
- `/mcp list|tools|test|reload|enable|disable|config`
- `/clear` `/exit`

Autocomplete: After typing `/` in TUI, ghost suggestions appear (`Tab`/`→` to accept, `Shift+Tab` for previous, `Ctrl+Space` to list candidates); `Tab` autocomplete in chat mode.

### MCP transports

| transport | Usage | Key Fields |
|-----------|------|----------|
| `stdio` | Local MCP process | `command` / `args` / `env` |
| `sse` | Remote SSE | `url` / `headers` |
| `streamable_http` / `http` | Remote Streamable HTTP | `url` / `headers` |

Connections are kept alive and reused in the background event loop; `/mcp reload` rebuilds the agent and connection pool.

### models.json Field Description

| Field | Meaning |
|---|---|
| `api_key` | **Recommended**: Key written directly in models.json |
| `api_key_env` | Legacy: Read key from environment variable |
| `auth` | Auth method; use `openai_oauth` to use the Codex OAuth login state saved by `synapse auth openai login` |
| `headers` | Model-level HTTP request header object; same-named headers override top-level `headers`, useful for custom `User-Agent` etc. request fingerprints |
| `thinking` / `thinking_level` / `reasoning_effort` | Thinking level: `off\|minimal\|low\|medium\|high\|max` |
| `enable_thinking` | Legacy compatible bool field |
| `temperature` / `max_tokens` / `timeout` etc. | Passed directly to ChatModel |
| `stream_chunk_timeout` | Streaming adjacent chunk silent timeout (seconds); disabled by default to prevent long thinking from being cut off by langchain-openai 120s limit |
| `model_kwargs` | Request body kwargs |
| `extra_body` | Vendor extension body (merged with thinking) |

### Custom Headers & Request Fingerprints

Following the configuration style of `cmd-agent`, top-level `headers` in `models.json` apply to all OpenAI-compatible models; model-level `headers` override global values by HTTP header name (case-insensitive). Priority order: Project Model > User Model > Project Global > User Global. Useful for setting request fingerprints like `User-Agent` or client identifiers.

```json
{
  "headers": {
    "User-Agent": "synapse-global/1.0",
    "X-Client-Channel": "desktop"
  },
  "models": {
    "codex": {
      "model": "openai:gpt-5",
      "auth": "openai_oauth",
      "headers": {
        "User-Agent": "synapse-codex/1.0"
      }
    }
  }
}
```

In OAuth mode, protocol authentication headers like `ChatGPT-Account-Id` and `originator` are forcefully set at runtime and cannot be overridden by configuration.
Header values support `${ENV_NAME}` / `$ENV_NAME` environment variable expansion; do not write private tokens to project-level configurations.

`.env` is still readable for migration/CI compatibility, but is **not recommended** as a standard distribution method.

## Example: Fix sample_repo

The `sub()` function in `tests/fixtures/sample_repo` is intentionally buggy and can be used to verify the agent's closed-loop behavior:

```bash
# First confirm tests fail
uv run pytest tests/fixtures/sample_repo -q

# Let the agent fix it (requires valid model key)
uv run synapse run "Fix the bug in calculator.sub so that all tests pass" -w tests/fixtures/sample_repo
```

## Environment Variable Reference

Key Agent Behavior Variables:

| Variable | Default | Meaning |
|---|---|---|
| `MODEL` | `openai:gpt-4.1` | `provider:model` or profile name |
| `AGENT_MODELS_CONFIG` | - | Multi-model JSON path |
| `AGENT_ACTIVE_MODEL` | - | Current profile alias |
| `WORKSPACE` | `.` | Workspace |
| `AGENT_REQUIRE_APPROVAL` | `false` | Enable HITL |
| `AGENT_ENABLE_SUBAGENTS` | `true` | Default sub-agents |
| `AGENT_READONLY` | `false` | Exclude write/execute tools |
| `AGENT_ENABLE_FS_PERMISSIONS` | `false` | Filesystem permission rules |
| `AGENT_ENABLE_MCP` | `true` | Enable MCP injection |
| `AGENT_MCP_EAGER` | `false` | Connect MCP immediately at startup (defaults to lazy connect in TUI background phase 2) |
| `AGENT_TUI_DEFER_AGENT` | `true` | TUI starts UI first, builds agent in background |
| `AGENT_MCP_CONFIG` | - | MCP servers JSON |
| `CHECKPOINT_BACKEND` | `sqlite` | `sqlite` or `memory` |
| `AGENT_SESSION_PREWARM_ENABLED` | `false` | Preheat provider cache in background after restoring large-context sessions (first request still billed by input tokens; see `docs/config.md`) |
| `AGENT_ENABLE_TOOL_RESPONSE_TRUNCATE` | `false` | Enable tool response folding (compress large tool outputs outside keep window into summary+reference, saves 20-30% for large-context sessions; see `docs/config.md`) |
| `INHERIT_ENV` | `true` | Shell inherits host environment |
| `VIRTUAL_MODE` | `true` | Virtual root for file paths |
| `AGENT_SHOW_REASONING_PLACEHOLDERS` | `true` | Show placeholder thinking nodes when gateway does not expose reasoning text; can be set to `false` to hide Codex encrypted reasoning |

## Security Notes

- **No sandbox**: Commands execute on the host machine.
- Default is **no approval**; only recommended for trusted development machines.
- Use `--require-approval` to temporarily enable HITL.
- `safety.py` provides a blacklist for dangerous commands (for warning/detection only); it does not intercept the agent's built-in `execute` by default.
- `--readonly` / `AGENT_READONLY=true` excludes `execute/write_file/edit_file` via the harness.

## Development

```bash
uv run pytest
uv run ruff check src tests
```

## Project Structure

```text
src/synapse/
  agent.py           # create_deep_agent assembly
  backends.py        # LocalShellBackend
  cli.py             # typer CLI
  config.py          # pydantic-settings
  models_registry.py # Multi-model catalog
  mcp_client.py      # MCP → tools
  sessions.py        # Session metadata
  subagents.py       # Default sub-agents
  harness.py         # excluded_tools
  fs_permissions.py  # FilesystemPermission
  prompts.py
  safety.py
  tools/             # session tools
  ui/                # stream + TUI
docs/design.md
skills/              # agent skills
examples/            # models/mcp config examples
AGENTS.md
```

## Design Document

For a more complete architecture and phased breakdown, see [`docs/design.md`](docs/design.md).
