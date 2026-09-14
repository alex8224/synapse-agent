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

Windows users can also install via Scoop (single-file executable, auto-updated
through `checkver`): add the bucket, then install:

```powershell
scoop bucket add synapse https://github.com/alex8224/synapse-agent
scoop install synapse
```

Or install directly from the manifest URL:

```powershell
scoop install https://raw.githubusercontent.com/alex8224/synapse-agent/main/bucket/synapse.json
```

Then start the TUI from anywhere:

```bash
synapse tui -w .
```

To run the standalone foreground runtime daemon (S8), use the installed
`synapse-runtime` command or the module form. It prints one ready JSON metadata
line to stdout after binding; the token is generated in the private state
directory and is never printed.

```bash
synapse-runtime --state-dir ~/.synapse/runtime --host 127.0.0.1 --port 0
# or: python -m synapse.runtime.daemon --state-dir ~/.synapse/runtime --port 0
```

Use `--token-file PATH` to select a deployment-specific token file. The daemon
uses a held lock in the state directory, runs in the foreground, and stops on
SIGINT/SIGTERM. It does not provide install/start/stop/status controls and does
not migrate the CLI, TUI, or ACP consumers.

## Web 控制台（React，正式宿主）

`web/` 是 React + TypeScript 单页控制台。生产路径使用独立正式宿主
`synapse-web-console`（薄 aiohttp 进程：静态产物 + 配对 API + WebSocket 中继），
不依赖 Vite 开发中间件承担业务；Vite 只在开发时做静态热更新与受控代理。旧的
`synapse-web`（textual-serve 把 TUI 放进浏览器）入口保持不变、仍可用，与正式宿主
语义不同。

先同步依赖与前端产物再启动。**一条命令即可**：宿主默认会在该 `--state-dir` 下没有
**运行中** daemon 时自己拉起一个（`--port 0` 由内核分配），并在自己退出时把它停掉；
已经有一个在跑则直接复用、绝不动它。想要「daemon 必须由我亲自启动」就加
`--no-start-runtime`；给了 `--runtime-port` 也视为「用那个 daemon」，不会另起。

```bash
uv sync                                   # 源码检出必做：console script 由安装步骤生成
cd web && npm ci && npm run build && cd ..
# 一条命令：宿主按需拉起/复用 daemon（产物目录需显式指定；wheel 不内置 web/dist）
synapse-web-console --workspace . --static-dir web/dist \
  --state-dir ~/.synapse/runtime --port 8080
```

仍然可以把 daemon 当作独立服务来跑（服务化部署、或想让它跨控制台存活）：先在一个
终端启动它，再启动宿主——宿主会发现它并复用。

```bash
synapse-runtime --state-dir ~/.synapse/runtime --host 127.0.0.1 --port 0
```

`synapse-runtime` 与 `synapse-web-console` 两个 console script 由安装步骤（`uv sync`）
生成：未同步的源码检出里不存在，上面的 console script 形式会直接失败。此时改用等价的模块形式，
不依赖 console script（测试亦用该形式，见 `tests/test_web_console_security.py`）：

```bash
uv run --no-sync python -m synapse.web_console.entry --workspace . --static-dir web/dist \
  --state-dir ~/.synapse/runtime --port 8080
```

宿主的自动拉起只覆盖它自己启动的 daemon：正常退出（Ctrl+C / SIGTERM）会连带停掉；
被强杀（任务管理器、`taskkill /F`）则 daemon 会留下来继续跑（它是服务，下次启动会被
复用而不是重复拉起）。两条命令行形式都是前台常驻进程，前台启动会占住调用它的终端：
交互使用各开一个终端；脚本/自动化
（含 agent）必须非阻塞启动——让子进程脱离调用方进程树，并把 stdout/stderr 重定向到
文件。PowerShell 下从 shell 直接 `Start-Process -RedirectStandardOutput ...` 会因
子进程继承 stdout 管道而卡住调用方（直到进程退出才返回），可行做法是 `cmd.exe /c`
包一层带重定向的脚本，再经 `Win32_Process.Create` 脱离启动：

```powershell
$log = "$env:TEMP\synapse-webui"; New-Item -ItemType Directory -Force $log | Out-Null
# $log\start-console.cmd 内一行（@echo off 之后）：
#   "<repo>\.venv\Scripts\python.exe" -m synapse.web_console.entry --workspace "<repo>" ^
#     --static-dir "<repo>\web\dist" --state-dir "%USERPROFILE%\.synapse\runtime" --port 8080 ^
#     > "%TEMP%\synapse-webui\console.out" 2> "%TEMP%\synapse-webui\console.err"
Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
  CommandLine = "cmd.exe /c `"$log\start-console.cmd`""; CurrentDirectory = "$PWD" }
```

配对码随后从 `$log\console.err` 读取（前台运行时直接出现在宿主 stderr）。

宿主启动时 stdout 恰好一行 JSON 元数据，配对码只出现在 stderr：

```
synapse-web-console: pairing code XXXXXXXX (expires in 300s; open http://127.0.0.1:8080/ and enter it)
```

打开 <http://127.0.0.1:8080/> 输入该 8 位配对码完成配对（单次使用，默认 300s
过期）。会话 cookie 只在 `POST /api/pair` 成功时签发；`GET /api/bootstrap` 已删除，
恒返回 405 且不签发 cookie。

宿主默认且仅支持 loopback（127.0.0.1/localhost/::1，配置层强制），是 loopback
单用户工具，不是公网多租户安全产品。已建立的边界：浏览器始终拿不到 daemon
bearer token/env/model secret——宿主服务端持有 token 并以 `Authorization: Bearer`
连接 daemon，再中继 JSON-RPC 帧（唯一例外：指向非宿主自身项目的请求由宿主以 typed
`not_found` 拒绝、**不进入 daemon**，边界与残余风险见
`docs/web-console/formal-host.md` §4.1）；URL 不再携带 token，前端也不再硬编码
token/project/workspace。配对码只存在于宿主进程内存与 stderr，不落盘、不进 stdout
JSON。`/runtime-ws` 中继在升级前要求 Host 与 Origin 精确匹配（含端口）+ 有效会话
cookie；浏览器断线不会取消 daemon 中仍在运行的 turn。静态服务带路径遍历/符号
链接逃逸防护、帧大小上界、SPA 回退与 no-store/asset immutable 缓存规则；中继出站
缓冲另有显式上界（64 帧 / 8 MiB，取先到者），超限即断开并计数。已配对浏览器可只读
`GET /api/runtime-status` 获取 daemon 端点、state dir 与 `start synapse-runtime` 提示
（响应不含 token；`POST` → 405）。

**明确不承诺**（详见 `docs/web-console/formal-host.md` §4、§9）：`Host`/`Origin`/
`Sec-Fetch-Site` 都是客户端可伪造的头，只算纵深防御，真正的门是配对码；本切片
不支持 TLS/反向代理（外部端口 ≠ 绑定端口）；会话不持久化，宿主重启后需重新配对；
会话 TTL 到期不主动断开已建立的中继；中继缓冲上界是**进程内**上界（不含
aiohttp/内核 socket 缓冲，**不等于进程 RSS 上界**）；跨项目拒绝对不可读/非文本帧
fail-open，且白名单与 `protocol.decode_params` 无源码级绑定，安全性依赖 daemon 的
严格校验；历史投影与实时 broker 之间不承诺跨存储原子一致（见 `docs/sessions.md`）。

开发模式（仅热更新与受控代理，不经中间件读 token）：

```bash
synapse-web-console --workspace . --static-dir web/dist --port 8080 &
cd web && npm run dev     # 打开 http://127.0.0.1:5173
```

Vite 只把 `/api` 与 `/runtime-ws` 代理到宿主，并把转发请求的 `Origin` 重写为宿主
origin；它不直连 daemon、不注入任何凭据、不读 token 文件，`server.host` 保持
loopback 默认值，因此不构成认证旁路，宿主也不因 dev 放宽任何校验。dev 下同样需要
配对码（从宿主 stderr 读取）。完整命令与参数见 `docs/web-console/formal-host.md`。

Open a session in any registered project from the global catalog:

```bash
synapse --session <project_id>:<thread_id>   # global session reference
synapse --project <ref>                      # project by id prefix, name, or path
```

Inside the TUI, click the topbar `≡` (or the workspace label) to open the
floating project drawer: it lists every registered project, groups their
sessions, marks live runtime status, and lets you switch sessions in place or
jump to another project (the TUI restarts into that project).

Press `Ctrl+Tab` for a global recent-sessions switcher: it lists the most
recently changed sessions across every registered project (from the user-layer
catalog), marks each one's live runtime status — `running` / `queued` /
`idle` / `cold`, etc. — lets you `Tab` / `Shift+Tab` through them and `Enter`
to switch.  The old session keeps running in the background and is never
cancelled.  Some terminals never forward `Ctrl+Tab` to the app; `Ctrl+O` is a
drop-in alternative.

`/new` and switches to cold sessions prepare an independent agent in a background
worker, leaving the current session visible until preparation succeeds. While
preparing, submitted prompt text stays in the input instead of being sent to the
old session; submit it again after the switch. A failed preparation leaves the
current session unchanged. Mid-turn steer notifications do not wait for UI repaint,
and streaming updates are coalesced into bounded UI batches.

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
- **Sub-agents** — built-in `researcher`, `tester`, and `reviewer` roles for parallel delegation, plus user-defined subagents from `.synapse/agents/*.md`.
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
| `/model manage` · `/model import-codex` · `/model providers` | Manage profiles, import Codex config, list providers |
| `/fast [on\|off\|status]` | Toggle the Codex Fast tier (OAuth profiles) |
| `/mcp list` · `/mcp reload` | Manage MCP servers |
| `/theme <name>` | Switch UI themes |
| `/compact` | Force context compaction |
| `/context` | Show context usage stats |
| `/safety <profile>` | Switch safety profiles |
| `/approve` · `/reject` | Human-in-the-loop decisions |

### TUI error diagnostics

Turn and approval-resume errors show a nonempty summary (including the exception
type when its message is empty) and an `Error log:` path. Failure diagnostics are
enabled by default and written lazily to the turn's own workspace:
`.synapse/logs/errors-<pid>.log`. Each process rotates its file at 1 MiB with three
backups. If the file cannot be written, the TUI explicitly reports that instead
of hiding the original error.

The JSON-lines log keeps timestamps, operation/session/turn identifiers,
exception types, numeric error codes and stack locations. It deliberately omits
raw exception messages, source lines, locals, prompts, tool output and settings;
the on-screen summary redacts common credential formats. Runtime failures are
recorded before conversion to result data, when the traceback is still available.
See [TUI error details and logs](docs/cli.md#tui-错误详情与日志) for the PowerShell
tail command and retention details. Restart the TUI after updating; earlier
unlogged failures cannot be recovered from an `ERROR:` screenshot.

## ACP v1 adapter

The package also installs the standalone `synapse-acp` stdio entry point. It
uses the locked `agent-client-protocol==0.12.0` dependency and keeps stdout
reserved for ACP JSON-RPC; diagnostics go to stderr.

```bash
# Run from an installed package
synapse-acp

# Run from a source checkout
uv run synapse-acp
```

For a client such as Zed, configure the ACP agent as a subprocess whose command
is `synapse-acp` (or the absolute path to that executable). The adapter accepts
absolute `cwd` values and session-scoped MCP servers. MCP credentials are used
only for the live session and are not written to the ACP catalog or transcript.

The published capability set currently covers text prompts, image prompts,
permission/HITL, session load/list/close/delete/resume, session-local
mode/config, HTTP/SSE MCP configuration, model selection via
`providers/list`/`providers/set`, and capability-gated Client filesystem/
terminal tools. Thinking level maps directly to Synapse levels (`off`,
`minimal`, `low`, `medium`, `high`, `max`). Audio, embedded-context,
authentication/logout, elicitation, NES, and document-sync capabilities are
not advertised until their runtime semantics and interoperability tests exist.

Troubleshooting:

- stdout is reserved for ACP JSON-RPC; any log line on stdout breaks the
  protocol. Logs and startup traces go to stderr.
- `cwd` and `additionalDirectories` must be absolute paths; relative paths are
  rejected with an ACP error.
- Session metadata lives in `~/.synapse/acp-sessions.sqlite`; MCP credentials
  and headers are never persisted there.
- If a client reports "Method not found", verify it negotiates the same ACP
  protocol version and does not rely on `not_target` methods (auth, providers,
  elicitation, NES, document sync).

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
| [ACP adapter](docs/acp-adapter/index.md) | ACP v1 setup, capability matrix, limitations, and verification status |
| [Runtime contract](docs/agent-runtime-service/adr-s-019-contract-freeze.md) | v1 wire/DTO contract: generated manifest and TS types, compatibility rules, gates |

The runtime contract (wire method table, event kinds, DTO schemas) is generated
from `src/synapse/runtime/service/contract_registry.py` into
`src/synapse/runtime/service/contract_manifest.json` and
`web/src/runtime-client/contract.generated.ts`. Do not hand-edit either artifact:

```bash
uv run --no-sync python scripts/export_contract_manifest.py         # regenerate
uv run --no-sync python scripts/export_contract_manifest.py --check # verify committed output
```

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