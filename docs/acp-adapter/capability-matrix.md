# ACP v1 能力矩阵（SDK 0.12.0）

> 基线日期：2026-08-12  
> Python 包：`agent-client-protocol==0.12.0`  
> wire protocol：`1`  
> 依据：本地安装包 `acp.AGENT_METHODS`、`acp.CLIENT_METHODS`、`acp.schema` 及其字段描述。  
> 状态：P0-P7 本地可验证范围已收口，矩阵无未解释项（`planned`/`in_progress` 均已清零）；P8 真实互通与全量门禁进行中。`verified` 仅表示已有对应专项测试证据，`implemented` 不代表真实 MCP/Zed 互通已验收。

## 1. 状态定义

| 状态 | 含义 |
|---|---|
| `planned` | 已纳入完整目标，尚未实现 |
| `in_progress` | 当前阶段正在实现 |
| `implemented` | handler 和必要语义已实现，但尚未完成最终合规门禁 |
| `verified` | 已通过对应协议、错误、取消和资源测试 |
| `not_target` | 明确不属于 ACP v1 stable；仅在升级目标时重新评审 |

原则：未实现能力不得在 `initialize` 中声明。SDK 暴露的方法不等于 Synapse 已实现该能力。

## 2. Agent 方法矩阵

| ACP method | SDK Agent API | 分类 | 目标阶段 | 当前状态 |
|---|---|---|---|---|
| `initialize` | `initialize` | stable required | P1 | verified |
| `session/new` | `new_session` | stable required | P1 | verified |
| `session/prompt` | `prompt` | stable required | P1/P2 | verified |
| `session/cancel` | `cancel` | stable required | P1/P3 | verified |
| `session/load` | `load_session` | stable capability | P4 | verified |
| `session/list` | `list_sessions` | stable capability | P4 | verified |
| `session/delete` | SDK registry `session_delete` | stable capability | P4 | verified |
| `session/close` | `close_session` | stable capability | P4 | verified |
| `session/resume` | `resume_session` | stable capability | P4 | verified |
| `session/set_mode` | `set_session_mode` | compatibility/stable schema | P7 | implemented |
| `session/set_config_option` | `set_config_option` | stable schema | P7 | implemented |
| `authenticate` | `authenticate` | capability-gated | P7 | not_target |
| `logout` | SDK registry `logout` | capability-gated | P7 | not_target |
| `session/fork` | `fork_session` | unstable in SDK 0.12.0 | P4 review | implemented |
| `mcp/message` | `ext_method`/registry | capability/transport-dependent | P5 review | not_target |
| `providers/list` | `list_providers`（server 路由补注册） | unstable | P7 review | implemented |
| `providers/set` | `set_provider`（server 路由补注册） | unstable | P7 review | implemented |
| `providers/disable` | no high-level Agent method | unstable | P7 review | not_target |
| `nes/start` | no high-level Agent method | unstable | P7 review | not_target |
| `nes/suggest` | no high-level Agent method | unstable | P7 review | not_target |
| `nes/accept` | no high-level Agent method | unstable | P7 review | not_target |
| `nes/reject` | no high-level Agent method | unstable | P7 review | not_target |
| `nes/close` | no high-level Agent method | unstable | P7 review | not_target |
| `document/didOpen` | no high-level Agent method | unstable | P7 review | not_target |
| `document/didChange` | no high-level Agent method | unstable | P7 review | not_target |
| `document/didClose` | no high-level Agent method | unstable | P7 review | not_target |
| `document/didSave` | no high-level Agent method | unstable | P7 review | not_target |
| `document/didFocus` | no high-level Agent method | unstable | P7 review | not_target |

说明：SDK 0.12.0 的 `Agent` 类仍提供 `ext_method`/`ext_notification`，但不能把未知或 unstable 方法自动视为已支持。P0 只记录事实，最终是否实现以稳定协议范围和项目能力评审为准。

## 3. Agent capabilities 矩阵

| Capability | schema 字段 | 目标阶段 | 当前状态 |
|---|---|---|---|
| 基础 Agent | `agentCapabilities` | P1 | verified |
| Session load | `loadSession` | P4 | verified |
| Prompt image | `promptCapabilities.image` | P2 | implemented |
| Prompt audio | `promptCapabilities.audio` | P7 review | not_target |
| Embedded context | `promptCapabilities.embeddedContext` | P2 | not_target |
| MCP HTTP | `mcpCapabilities.http` | P5 | implemented |
| MCP SSE | `mcpCapabilities.sse` | P5 review | implemented |
| MCP ACP | `mcpCapabilities.acp` | P5 review | not_target |
| Session list | `sessionCapabilities.list` | P4 | verified |
| Session delete | `sessionCapabilities.delete` | P4 | verified |
| Additional directories | `sessionCapabilities.additionalDirectories` | P4 | implemented |
| Session fork | `sessionCapabilities.fork` | P4 review | implemented |
| Session resume | `sessionCapabilities.resume` | P4 | verified |
| Session close | `sessionCapabilities.close` | P4 | verified |
| Authentication | `auth` / `authMethods` | P7 | not_target |
| Providers | `providers` | P7 review | implemented |
| Config options | session config option fields | P7 | implemented |
| NES | `nes` | P7 review | not_target |
| Position encoding | `positionEncoding` | P7 review | not_target |

## 4. Client methods Agent 需要调用

| ACP method | SDK Client API | 输入 capability | 目标阶段 | 当前状态 |
|---|---|---|---|---|
| `session/update` | `session_update` | 基础 | P2 | verified |
| `session/request_permission` | `request_permission` | 基础 permission | P3 | verified |
| `fs/read_text_file` | `read_text_file` | `clientCapabilities.fs.readTextFile` | P6 | implemented |
| `fs/write_text_file` | `write_text_file` | `clientCapabilities.fs.writeTextFile` | P6 | implemented |
| `terminal/create` | `create_terminal` | `clientCapabilities.terminal` | P6 | implemented |
| `terminal/output` | `terminal_output` | `terminal` | P6 | implemented |
| `terminal/wait_for_exit` | `wait_for_terminal_exit` | `terminal` | P6 | implemented |
| `terminal/kill` | `kill_terminal` | `terminal` | P6 | implemented |
| `terminal/release` | `release_terminal` | `terminal` | P6 | implemented |
| `mcp/connect` | no direct high-level workflow | MCP capability | P5 review | not_target |
| `mcp/message` | no direct high-level workflow | MCP capability | P5 review | not_target |
| `mcp/disconnect` | no direct high-level workflow | MCP capability | P5 review | not_target |
| `elicitation/create` | `create_elicitation` | Client elicitation capability | P7 review | not_target |
| `elicitation/complete` | `complete_elicitation` | Client elicitation capability | P7 review | not_target |

## 5. ContentBlock 矩阵

| 类型 | SDK 类型 | Prompt 输入 | Agent 输出/工具内容 | 目标阶段 | 当前状态 |
|---|---|---:|---:|---|---|
| `text` | `TextContentBlock` | 必选 | 必选 | P1/P2 | verified |
| `image` | `ImageContentBlock` | capability | capability | P2 | not_target |
| `audio` | `AudioContentBlock` | capability | capability | P7 review | not_target |
| `resource_link` | `ResourceContentBlock` | 必选基线 | tool/content | P2/P4 | verified |
| `resource` | `EmbeddedResourceContentBlock` | `embeddedContext` | content | P2/P4 | implemented |
| tool text | `ContentToolCallContent` | - | tool content | P2 | verified |
| file edit | `FileEditToolCallContent` | - | tool content | P2 | verified |
| terminal ref | `TerminalToolCallContent` | - | tool content | P6 | implemented |

## 6. SessionUpdate 矩阵

SDK 0.12.0 的 `SessionNotification.update` 联合类型包括：

| `sessionUpdate` | SDK 类型 | 目标阶段 | 当前状态 |
|---|---|---|---|
| `user_message_chunk` | `UserMessageChunk` | P2/P4 | implemented |
| `agent_message_chunk` | `AgentMessageChunk` | P1/P2 | verified |
| `agent_thought_chunk` | `AgentThoughtChunk` | P2 | verified |
| `tool_call` | `ToolCallStart` | P2 | verified |
| `tool_call_update` | `ToolCallProgress` | P2 | verified |
| `plan` | `AgentPlanUpdate` | P2 | verified |
| `plan_content` | `AgentPlanContentUpdate` | P2 | not_target |
| `plan_removed` | `AgentPlanRemovedUpdate` | P2 | verified |
| `available_commands_update` | `AvailableCommandsUpdate` | P7 | verified |
| `current_mode_update` | `CurrentModeUpdate` | P7 | verified |
| `config_option_update` | `ConfigOptionUpdate` | P7 | verified |
| `session_info_update` | `SessionInfoUpdate` | P4/P7 | verified |
| `usage_update` | `UsageUpdate` | P2/P7 | verified |

## 7. 非 SessionUpdate 输出模型

| 模型 | 用途 | 目标阶段 | 当前状态 |
|---|---|---|---|
| `PromptResponse` | prompt 终态和可选 usage | P1/P2 | verified |
| `Usage` | 累计 token usage | P2/P7 | verified |
| `RequestPermissionResponse` | Client permission 决策 | P3 | verified |
| `NewSessionResponse` | session ID/config/mode | P1/P7 | verified |
| `LoadSessionResponse` | load 配置/mode | P4/P7 | verified |
| `ResumeSessionResponse` | resume 配置/mode | P4/P7 | verified |
| `ForkSessionResponse` | fork session ID | P4 review | implemented |
| `ListSessionsResponse` | session page/cursor | P4 | verified |
| `CloseSessionResponse` | close acknowledgement | P4 | verified |

## 8. P0 结论

- SDK 版本和 wire protocol 已锁定，满足 P0-01/P0-02。
- Agent、Client、capability、ContentBlock、SessionUpdate 和响应类型均已登记，满足 P0-03 的事实提取要求。
- P0-04 的“handler/test 可追溯”在后续实现阶段以本表的目标阶段和测试任务为索引继续维护。
- `not_target` 只表示当前目标不包含 SDK 标记为 unstable、且项目当前没有对应产品语义的能力；若 ACP v1 stable 或项目需求变化，必须通过 ADR 重新评审，不得静默删除。
- 当前声明只包含 initialize 已实现并有行为证据的能力；未声明能力必须返回协议不支持/非法参数错误，不能静默接收。
