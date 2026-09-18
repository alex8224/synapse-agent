# MCP Server

Synapse 支持 [Model Context Protocol (MCP)](https://modelcontextprotocol.io/)，可以将 MCP Server 的工具自动注入为 Agent tools。

## 配置

创建 `.coding-agent/mcp_servers.json`：

```json
{
  "mcpServers": {
    "filesystem": {
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/allowed/files"],
      "enabled": true
    },
    "fetch": {
      "transport": "stdio",
      "command": "uvx",
      "args": ["mcp-server-fetch"],
      "enabled": true
    }
  }
}
```

## Server 配置字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `transport` | str | 传输方式：`stdio` / `sse` / `streamable_http` |
| `command` | str | 可执行文件路径（stdio 模式） |
| `args` | list[str] | 命令行参数 |
| `env` | dict | 额外环境变量 |
| `url` | str | 服务端 URL（sse / streamable_http 模式） |
| `headers` | dict | HTTP 请求头 |
| `enabled` | bool | 是否启用（默认 true） |
| `tool_prefix` | str | 工具名前缀，避免冲突 |
| `include_tools` | list[str] | 白名单：只加载列表中的工具。不设置则加载全部 |
| `exclude_tools` | list[str] | 黑名单：排除列表中的工具（在 include 之后生效） |

## Per-Tool 过滤

通过 `include_tools` / `exclude_tools` 可以精确控制每个 MCP Server 加载哪些工具：

```json
{
  "servers": [
    {
      "name": "anysearch",
      "transport": "streamable_http",
      "url": "https://api.anysearch.com/mcp",
      "headers": {
        "Authorization": "Bearer ${ANYSEARCH_API_KEY}"
      },
      "enabled": true,
      "tool_prefix": "anysearch__",
      "include_tools": ["search", "batch_search"]
    }
  ]
}
```

上例中 anysearch 的 `extract` 工具不会被加载。

**过滤规则**：
- `include_tools` 不设置 → 加载所有工具
- `include_tools: ["a", "b"]` → 只加载 `a` 和 `b`
- `exclude_tools: ["c"]` → 排除 `c`，其余加载
- 两者同时设置 → 先 include 再 exclude

## UI 工具选择面板

在 TUI 中按 **F5** 或输入 `/mcp` 打开 MCP Tools 面板，可以：
- 查看每个 Server 提供的所有工具
- `Enter` 勾选/取消勾选单个工具
- `a` 全选当前 Server 的所有工具
- `d` 取消全选当前 Server 的所有工具
- `s` **保存到配置文件** + 重新加载
- `r` 重新连接 MCP Server 并刷新工具列表

## Web 控制台 MCP 面板

Web 控制台底栏的 MCP 面板（F5，`web/src/components/McpPanel.tsx`）与 TUI 面板
读取同一份运行态：它显示的是**真实运行态**，而不是配置里的 `enabled` 开关。

- **每个服务器一行**：状态圆点 + 名称 + `transport` + 状态文案 + `ON`/`OFF` 徽标。
  状态文案为 **已停用**（配置关闭）/ **启动中…**（附着请求在途）/ **已连接**
  （工具已进入本会话工具列表）/ **未连接**（已启用但未加载工具）/
  **已启用**（已启用、运行态尚未上报时的保守显示）。
- **工具白名单**：已连接时显示「已连接 N/M 工具」，可展开该服务器提供的工具列表
  逐个勾选，点「保存工具选择」写入该服务器的 `include_tools` 白名单；保存会重连
  该会话的 MCP 并刷新状态。**全部勾选等于清空白名单（加载全部工具）**，与 TUI
  `s` 键的语义一致。
- **重新连接**：面板顶部按钮重新附着/重载所有已启用服务器并刷新工具列表，
  连接过程中按钮显示「连接中…」。
- **开关语义**：点击服务器行仍是切换该服务器的 `enabled` 开关；全局 MCP 开关
  仍是只读。
- **键盘可达**：F5 打开后焦点落在第一个服务器行；`↑`/`↓` 在面板控件之间按阅读
  顺序移动（服务器行 → 展开/收起 → 工具勾选框 → 保存工具选择），`Enter`/`Space`
  触发聚焦项。服务器行是真正的 `<button>`（不再只能点击），`Esc` 关闭并把焦点
  还给 F5 触发器。
- **warnings 可见**：面板底部展示 daemon 上报的 warnings（例如连接失败原因、
  `mcp deferred at startup (…)`），不再静默丢弃。
- **底栏标签**：按真实运行态显示 `mcp: 启动中` / `mcp: N on · 未连接` /
  `mcp: N on · M 已连接` / `mcp: off`。
- **attach 时自动附着**：打开会话时控制台会像 TUI 一样在后台自动附着已启用的
  MCP 服务器，结果到达前显示「启动中」；切换（启用/停用/工具白名单）从**下一轮
  对话**开始生效，面板在连接期间会提示这一点。
- **设置对话框**：SettingsDialog 的 MCP 段落新增「会话连接 M / N 已连接」以及
  每个服务器的「已连接 X 工具 / 未连接」。

面板动作对应的协议参数与结果见下文「MCP 面板协议」。

## 环境变量展开

配置中的字符串支持 `${VAR}` 和 `$VAR` 展开：

```json
{
  "mcpServers": {
    "github": {
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"
      },
      "enabled": true
    }
  }
}
```

## MCP 管理命令

```bash
# 列出所有配置的 MCP Server
synapse mcp list

# 查看 MCP 提供的工具列表
synapse mcp tools

# 检查某个 Server 的详细配置
synapse mcp inspect <name>
```

## MCP 连接模式

| 变量 | 说明 |
|---|---|
| `AGENT_ENABLE_MCP` (默认 `true`) | 控制是否启用 MCP |
| `AGENT_MCP_EAGER` (默认 `false`) | 设为 `true` 时，Agent 构建时即时连接 MCP；`false` 时 TUI 后台连接 |

MCP 连接失败是 **非致命的**：缺失的 Server 只会降级为空工具列表并发出警告，不会阻止 Agent 启动。

## 使用 MCP 工具

配置完成后，MCP Server 提供的工具会自动出现在 Agent 的工具列表中。在对话中直接用自然语言引用即可：

- "用 fetch 工具获取 https://example.com/api 的返回内容"
- "查看 /data 目录下有哪些文件"

工具名会以 `mcp__<server>__<tool>` 或 `tool_prefix` 指定的前缀命名。

## MCP 面板协议

TUI 与 Web 控制台的面板动作都落在同一个 runtime 方法
`runtime.session.mcp.reload` 上，除 `session` 外的参数均为可选：

| 参数 | 类型 | 说明 |
|---|---|---|
| `session` | object | 必填，目标会话（`project_id` / `thread_id`） |
| `server` | str | 可选；省略表示**附着所有已启用服务器**（等价 TUI 的 `/mcp reload`），不写配置 |
| `enabled` | bool | 可选；写该服务器的启用开关 |
| `include_tools` | list[str] | 可选；写该服务器的工具白名单，空数组表示清空白名单（加载全部工具） |
| `command_id` | str | 可选 |

`enabled` / `include_tools` 只描述 `server` 指定的单个服务器：不带 `server`
的请求不允许携带这两个参数（否则返回 `invalid_params`）。

结果字段：

| 字段 | 说明 |
|---|---|
| `tool_names` | 本会话实际加载的工具名（带前缀，如 `anysearch__search`） |
| `servers` | 每个服务器：`name` / `enabled` / `attached` / `include_tools` / `discovered` / `loaded`。`discovered` 是服务器上报的全部工具，`loaded` 是过滤后真正进入工具列表的工具 |
| `attached` | 本会话是否已附着（未指定 `server` 时按会话整体报告） |
| `active_servers` | 当前活跃的服务器名 |
| `tool_count` | 加载的工具数量 |
| `warnings` | 连接失败等原因 |
| `server` / `enabled` | 可为 `null`（未指定单个服务器时） |
