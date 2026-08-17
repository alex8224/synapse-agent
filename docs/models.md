# 模型配置

Synapse 支持两种模型配置方式：

1. **Legacy 单模型** — 通过环境变量 `MODEL` / `OPENAI_API_KEY` / `OPENAI_BASE_URL`，简单直接
2. **多模型 Profile（推荐）** — 通过 `.coding-agent/models.json`，支持多模型切换

## Legacy 单模型模式

适合快速上手，只需设置环境变量：

```bash
export OPENAI_API_KEY="sk-..."
export MODEL="openai:gpt-4.1"
export OPENAI_BASE_URL="https://api.openai.com/v1"  # 可选
```

或写入 `.env` 文件：

```
OPENAI_API_KEY=sk-...
MODEL=openai:gpt-4.1
```

## 多模型 Profile（推荐）

创建 `.coding-agent/models.json`：

```json
{
  "default": "gpt-4.1",
  "models": {
    "gpt-4.1": {
      "provider": "openai",
      "model": "gpt-4.1",
      "api_key_env": "OPENAI_API_KEY"
    },
    "claude-sonnet": {
      "provider": "anthropic",
      "model": "claude-sonnet-4-20250514",
      "api_key_env": "ANTHROPIC_API_KEY"
    },
    "deepseek": {
      "provider": "openai",
      "model": "deepseek-chat",
      "base_url": "https://api.deepseek.com/v1",
      "api_key_env": "DEEPSEEK_API_KEY"
    }
  }
}
```

### Profile 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `provider` | str | 模型提供商：`openai` / `anthropic` |
| `model` | str | 模型名称 |
| `api_key_env` | str | 读取 API Key 的环境变量名 |
| `base_url` | str | 自定义 API 网关地址 |
| `websocket` | bool | 启用 WebSocket 连接 |
| `context_window` | int | 模型上下文窗口大小（token） |
| `temperature` | float | 采样温度 |
| `max_tokens` | int | 最大输出 token |
| `timeout` | int | 请求超时（秒） |
| `top_p` | float | Top-p 采样 |
| `extra_body` | object | 提供商特定请求体合并 |
| `model_kwargs` | object | ChatModel 构造参数 |

### 全局字段

| 字段 | 说明 |
|---|---|
| `default` | 默认使用的模型 profile 别名 |
| `thinking_levels` | 允许的思考级别列表 |
| `default_thinking` | 默认思考级别 |

## 切换模型

```bash
# CLI 切换
synapse models set deepseek

# 启动时指定
synapse tui -w . -m claude-sonnet

# 通过环境变量
export AGENT_ACTIVE_MODEL=deepseek
```

## 列出可用模型

```bash
synapse models list
```

## 视觉模型

可以为图片理解配置独立的视觉模型：

```bash
export VISION_MODEL='{"model": "qwen-vl-max", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "api_key_env": "VISION_API_KEY"}'
```

## WebSocket 传输

OpenAI Responses API 可在模型 profile 中设置 `"websocket": true`。发生连接关闭、超时或上游流在 `response.completed` 前断开时，Synapse 会按 profile 的 `max_retries` 重建连接并重放当前请求。重试耗尽且尚未输出任何 chunk 时，本轮自动回退到 HTTP/SSE；已经输出内容后不会自动重放，以免重复文本或工具调用。

```json
{
  "models": {
    "gpt-4.1-ws": {
      "provider": "openai",
      "model": "gpt-4.1",
      "api_key_env": "OPENAI_API_KEY",
      "websocket": true,
      "max_retries": 2
    }
  }
}
```

## 自定义请求头

`models.json` 顶层 `headers` 会应用到全部 OpenAI 兼容模型，模型内 `headers` 按名称覆盖全局值。
HTTP 头名称大小写不敏感；分层配置优先级为项目模型 > 用户模型 > 项目全局 > 用户全局。

```json
{
  "headers": {"User-Agent": "synapse-global/1.0", "X-Client": "desktop"},
  "models": {
    "primary": {
      "model": "openai:gpt-4.1",
      "headers": {"User-Agent": "synapse-primary/1.0"}
    }
  }
}
```

请求头值支持 `${ENV_NAME}` 或 `$ENV_NAME` 环境变量展开。不要在项目级配置中存放私密 token。

## OpenAI Codex OAuth

使用 ChatGPT Plus/Pro 的 Codex 配额时，先完成用户级登录：

```bash
synapse auth openai login
# 或复用已经登录的 Codex CLI
synapse auth openai login --import-codex
```

在 `~/.synapse/models.json` 或 `<workspace>/.synapse/models.json` 中定义 profile：

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

`auth: "openai_oauth"` 自动使用 `https://chatgpt.com/backend-api/codex`、OAuth access token
和 `ChatGPT-Account-Id` 请求头。access token 过期时会通过 refresh token 自动更新；凭据位于
`~/.synapse/openai_oauth.json`，不能提交到项目仓库。此认证模式仅适用于 OpenAI Codex backend，
不适用于第三方 OpenAI 兼容网关。浏览器授权回调固定为
`http://localhost:1455/auth/callback`；如端口被占用，关闭占用进程后重试。
Synapse 会自动将 Agent 的 OpenAI `system` 消息转换为 Codex backend 接受的 `developer` 消息，
并移除 DeepSeek 兼容的 `extra_body.thinking` 字段，只发送 Codex 支持的 reasoning 参数；
Responses 请求会强制设置 `store: false`。

### Codex Fast 档（service_tier=priority）

设置 `OPENAI_FAST_MODE=true`（或配置 `openai_fast_mode`）可对 Codex OAuth profile 启用 Fast 档：
每条 Responses 请求注入 `service_tier=priority`（优先处理，费用更高）。运行时可用
`/fast`、`/fast on`、`/fast off`、`/fast status` 切换，无需重建模型；开启后底栏模型
思考级别旁会显示黄色 `FAST` 徽标。Fast 档只对 `auth=openai_oauth` 的模型生效，
第三方 OpenAI 兼容网关不受影响；底栏徽标也仅在 OAuth profile 下显示。

## 自定义 OpenAI 兼容网关

Synapse 支持任何 OpenAI 兼容的 API：

```json
{
  "models": {
    "local-llama": {
      "provider": "openai",
      "model": "llama-3-70b",
      "base_url": "http://localhost:8080/v1",
      "api_key_env": "LOCAL_API_KEY"
    }
  }
}
```

## TUI 模型管理与 Codex 导入

TUI 内可对模型 profile 增删改，并从 Codex CLI 配置导入，无需手写 JSON。

入口（对话内斜杠命令）：

| 命令 | 作用 |
|---|---|
| `/model` | 选择模型与思考级别（对话框内 `m` 打开管理） |
| `/model manage` | 打开模型管理器 |
| `/model import-codex` | 检测并导入 Codex 配置 |
| `/model providers` | 查看支持的 Provider 目录 |

模型管理器中按键：`a` 新增、`e` 编辑、`d` 删除（连按两次确认）、`s` 设为默认、`i` 导入 Codex、`p` Provider 目录。新增/编辑表单字段包括别名、Provider、模型 ID、`api_key_env`、`base_url`、思考级别、上下文窗口、图片输入、自定义 headers / `model_kwargs` / `extra_body`（JSON）。保存或导入后立即生效并重建当前会话的模型。

### Provider 目录

开箱即用的 Provider 只有三种，其余一律走 OpenAI 兼容接口（`openai:` 前缀 + 自定义 `base_url`）：

| Provider | 默认 base_url | 默认 env | 说明 |
|---|---|---|---|
| `openai` | `https://api.openai.com/v1` | `OPENAI_API_KEY` | 官方或任意 OpenAI 兼容网关 |
| `anthropic` | `https://api.anthropic.com` | `ANTHROPIC_API_KEY` | Anthropic 原生 |
| `openai_oauth` | ChatGPT Codex 后端 | —（OAuth） | `auth: openai_oauth`，需 `synapse auth openai login` |

### Codex 配置导入

导入器检测：

- 用户级 `~/.codex/config.toml`
- 项目级 `<workspace>/.codex/config.toml`（覆盖用户级同名键）
- `~/.codex/auth.json` 凭据状态（OAuth / 明文 API key）

映射规则：

| Codex 字段 | Synapse 字段 |
|---|---|
| `model_provider`（内置 openai/anthropic） | 保留原生前缀 |
| `model_provider`（自定义名） | `openai:` + `base_url`（OpenAI 兼容兜底） |
| `base_url` | `base_url`（显式配置即生效，含内置 provider） |
| `env_key`（字符串或数组） | `api_key_env`（取第一个已设置的环境变量） |
| `wire_api = "responses"` | 仅 OAuth 专用；API key 场景降级为 chat 并给出 warning |
| `http_headers` / `env_http_headers` | `headers` |
| `query_params` | `model_kwargs.default_query` |
| `model_reasoning_effort` | `reasoning_effort` |
| `[profiles.<name>]` | 生成同名 profile 别名 |

导入前会逐行预览（别名、`provider:model`、来源、目标文件），冲突别名可切换跳过/覆盖；确认后一次原子写入项目层 `.synapse/models.json`。凭据绝不拷贝进 `models.json`：OAuth 仅提示可用，明文 API key 只通过 `api_key_env` 引用环境变量。
