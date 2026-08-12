<p align="center">
  <img src="assets/synapse-logo.svg" alt="Synapse" width="96">
</p>

<h1 align="center">Synapse</h1>

<p align="center">
  The open-source coding agent that lives in your terminal — built on <strong>LangChain Deep Agents</strong>. Ask it to fix a test, refactor a module, or carry a goal across turns until it is done.
</p>

<p align="center">
  <a href="https://pypi.org/project/synapse-cli-agent/"><img src="https://img.shields.io/pypi/v/synapse-cli-agent?style=for-the-badge&logo=pypi&logoColor=white&label=PyPI" alt="PyPI version"></a>
  <a href="https://github.com/alex8224/synapse-agent"><img src="https://img.shields.io/badge/GitHub-synapse--agent-181717?style=for-the-badge&logo=github&logoColor=white" alt="GitHub"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-3da639?style=for-the-badge&logo=apache&logoColor=white" alt="Apache 2.0"></a>
</p>

<p align="center">
  <a href="README.md">English</a> ·
  <a href="README.zh-CN.md">简体中文</a> ·
  <a href="docs/">Docs</a> ·
  <a href="CHANGELOG.md">Changelog</a>
</p>

<p align="center">
  <a href="https://synapse-agent.best/demo.mp4">
    <img src="assets/demo.gif" alt="Synapse TUI demo — click to watch the full video" width="720">
  </a>
</p>

Synapse is a terminal-first coding-agent runtime. Unlike a one-shot "run this and reply" prompt, it is designed for sessions that last more than a few turns: a responsive TUI that keeps the timeline, tool calls, and context usage visible; token-aware handling of tool output; and persistent goals that keep the agent working until the work is actually done.

## Install

One command, no clone required:

```bash
uv tool install synapse-cli-agent
```

Then start the TUI from anywhere:

```bash
synapse tui -w .
```

Open a session in any registered project from the global catalog:

```bash
synapse --session <project_id>:<thread_id>   # global session reference
synapse --project <ref>                      # project by id prefix, name, or path
```

Inside the TUI, click the topbar `≡` (or the workspace label) to open the
floating project drawer: it lists every registered project, groups their
sessions, marks live runtime status, and lets you switch sessions in place or
jump to another project (the TUI restarts into that project).

Press `Ctrl+Tab` for a lighter active-session switcher: it lists only sessions
that are doing work right now (across projects), lets you `Tab` / `Shift+Tab`
through them and `Enter` to switch — the old session keeps running in the
background and is never cancelled.  Some terminals never forward `Ctrl+Tab` to
the app; `Ctrl+O` is a drop-in alternative.

<details>
<summary><b>Or run from source</b></summary>

```bash
git clone https://github.com/alex8224/synapse-agent.git
cd synapse-agent
uv sync

# Windows venv entry
.\.venv\Scripts\synapse.exe tui -w .

# Or module entry
uv run python -m synapse tui -w .
```

</details>

## Use it

Type it like you would ask a colleague:

- "Fix the failing test in `tests/test_backends.py`"
- "Refactor this module and add type annotations"
- "Review the latest commit and suggest improvements"
- "Keep working on this goal until it is done" (via `/goal`)

Three ways to talk to it:

| Mode | What it does |
| --- | --- |
| **TUI** | Full terminal UI: timeline, turn rail, tool groups, context usage, themes |
| **Chat** | Plain interactive REPL — `synapse chat -w .` |
| **Run** | One-shot task that prints the answer — `synapse run "summarize this repo" -w .` |

## Why it is built for long sessions

- **Long-running goals** — `/goal <objective>` survives turn boundaries, tracks tokens and elapsed time, and steers the next turn automatically until the goal is completed, paused, blocked, or budget-limited.
- **Token-aware tool output** — search results, logs, diffs, JSON, and code are classified and compressed before they re-enter the model context; large originals stay recoverable through references.
- **Managed long context** — automatic summarization and `/compact` keep sessions inside the model window, with occupancy and savings visible in the TUI.
- **Direct Codex OAuth** — sign in with the Codex-compatible browser flow or import an existing Codex grant. No API key required; tokens refresh automatically.
- **Your model, your choice** — OpenAI-compatible providers via `models.json` profiles (OpenAI, DeepSeek, local gateways, and more), including a persistent WebSocket mode.
- **MCP built in** — attach MCP servers and their tools appear in the agent automatically.
- **Sessions that resume** — SQLite checkpoints, a global project catalog across all your projects, and a lightweight paged transcript so even huge sessions reopen fast.
- **Memory and skills** — `AGENTS.md` memory plus Agent Skills (`skills/**`) that load only when relevant.
- **Sub-agents** — built-in `researcher`, `tester`, and `reviewer` roles for parallel delegation.
- **Approvals when you want them** — optional human-in-the-loop (`--require-approval`) and safety profiles.

## Slash commands

Type `/help` in the TUI for the full reference. The essentials:

| Command | What it does |
| --- | --- |
| `/goal <objective>` | Set a long-running goal that auto-continues across turns |
| `/goal pause` · `/goal resume` · `/goal clear` | Manage the active goal |
| `/new` · `/switch <id>` · `/sessions` | Create, switch, and list sessions |
| `/export [md\|json]` | Export the transcript to a file |
| `/model <provider:model>` | Switch models at runtime |
| `/fast [on\|off\|status]` | Toggle the Codex Fast tier (OAuth profiles) |
| `/mcp list` · `/mcp reload` | Manage MCP servers |
| `/theme <name>` | Switch UI themes |
| `/compact` | Force context compaction |
| `/context` | Show context usage stats |
| `/safety <profile>` | Switch safety profiles |
| `/approve` · `/reject` | Human-in-the-loop decisions |

## Pick a model

Synapse works with any OpenAI-compatible endpoint. Configure profiles in `~/.synapse/models.json` (or `<workspace>/.synapse/models.json`):

```json
{
  "default": "deepseek",
  "models": {
    "deepseek": {
      "model": "openai:deepseek-v4-pro",
      "api_key": "sk-...",
      "base_url": "http://127.0.0.1:3000/v1",
      "thinking": "high"
    }
  }
}
```

For a zero-config Codex experience, use the OAuth profile — see [Models](docs/models.md).

## Documentation

| | |
| --- | --- |
| [Quickstart](docs/quickstart.md) | First run, CLI reference, common workflows |
| [Configuration](docs/config.md) | Layered settings, environment variables, paths |
| [Models](docs/models.md) | Provider profiles, OAuth, Fast tier, WebSocket mode |
| [MCP](docs/mcp.md) | Attaching and managing MCP servers |
| [Sessions](docs/sessions.md) | Checkpoints, resume, transcript paging |
| [Skills](docs/skills.md) | Bundled skills and the Agent Skills format |
| [Permissions](docs/permissions.md) | Read-only mode and approval flows |
| [Install](docs/install.md) | All installation methods |

## Repository layout

```
src/synapse/    Agent assembly, commands, runtime, sessions, TUI, integrations
rust/           Optional native compression cores (PyO3)
docs/           User documentation
tests/          Python test suite
scripts/        Install / release helpers
```

## License

Apache License 2.0 — see [LICENSE](LICENSE). Third-party dependencies and Rust subcomponents may carry their own licenses; retain the corresponding license and NOTICE files when distributing.