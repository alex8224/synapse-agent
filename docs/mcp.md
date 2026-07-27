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

按 **F5** 或输入 `/mcp` 打开 MCP Tools 面板，可以：
- 查看每个 Server 提供的所有工具
- `Enter` 勾选/取消勾选单个工具
- `a` 全选当前 Server 的所有工具
- `d` 取消全选当前 Server 的所有工具
- `s` **保存到配置文件** + 重新加载
- `r` 重新连接 MCP Server 并刷新工具列表

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
