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
