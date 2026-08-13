# ACP 完整语义适配总体方案

> 文档状态：Active  
> 实施状态：P0-P7 completed（本地可验证范围）；P8 release gates in progress（真实 MCP/Zed/Linux 互通与全量门禁）  
> 目标协议：ACP v1 stable（具体 schema 在 P0 锁定）  
> 实现约束：使用官方 Python SDK `agent-client-protocol`，不使用 `deepagents-acp`  
> 权威进度台账：[progress.md](progress.md)  
> 架构决策记录：[decisions.md](decisions.md)

## 1. 结论

Synapse 将新增独立的 ACP Agent 适配层，对外完整实现锁定版本 ACP v1 的 Agent 侧语义，对内复用现有 Agent assembly、`RuntimeManager`、`SessionRuntime`、语义流事件、会话存储和 MCP 基础设施。

实施可以分阶段交付，但最终目标不缩减：

- 覆盖锁定 schema 中全部必选 Agent 方法和语义。
- 对锁定 schema 中每项稳定可选能力作出实现，并在能力矩阵中逐项验收。
- capability 声明与运行行为来自同一真源。
- 内容、流事件、权限、取消、错误、并发和资源释放都属于协议实现范围。
- stdio JSON-RPC 由官方 SDK 承担，Synapse 不重复实现协议编解码。
- ACP 适配层不直接调用 Deep Agent 原始 `astream()`，必须经过现有 headless runtime。

## 2. 范围定义

### 2.1 包含

- ACP v1 stable Agent 角色。
- stdio transport。
- 初始化、能力协商和协议错误。
- 完整 prompt turn、内容块和 session update 语义。
- Permission/HITL 闭环。
- 锁定 schema 中稳定的会话生命周期方法。
- 会话级 MCP servers。
- Agent 对 ACP Client filesystem、terminal、permission 等反向 RPC 的使用。
- session config、commands、usage、metadata、认证等稳定能力。
- Windows/Linux 兼容和真实 ACP Client 验收。

### 2.2 不包含

- ACP v2 draft；待其稳定后另立升级阶段。
- 自研 JSON-RPC transport 或复制官方 SDK schema。
- 实现一个通用 ACP Client 产品。
- 使用 `deepagents-acp` 作为运行时依赖。
- 未经 capability 协商调用 Client 可选服务。
- 将 ACP Client 传入的 MCP 凭据持久化到 Synapse 全局配置。

## 3. 完整实现定义

ACP 是 capability-driven 协议。这里的“完整语义”按以下标准验收：

1. 锁定 schema 中所有必选 Agent 方法均实现。
2. 锁定 schema 中每个稳定可选 Agent 方法和 capability 均进入能力矩阵，不允许遗漏。
3. 最终声明的每项 capability 都有成功、错误、取消和资源释放测试。
4. 未声明能力仍须按协议返回正确的不支持行为，不能静默接收或丢弃。
5. 所有稳定 ContentBlock、SessionUpdate 和 stop reason 联合类型均有显式转换策略。
6. capability、handler 和测试矩阵可互相追溯。
7. SDK 负责 wire schema，不代表 Synapse 自动获得业务语义；业务状态机必须独立验收。

P0 会基于实际 SDK 源码和 schema 生成最终矩阵。P0 前不写死 SDK 版本和方法签名。

## 4. 当前基础

可直接复用：

| 能力 | 当前实现 |
|---|---|
| Agent 组装 | `src/synapse/app/agent.py` |
| 无 UI turn 执行 | `src/synapse/runtime/agent_loop/turn.py` |
| 多会话管理 | `src/synapse/runtime/sessions/manager.py` |
| 单会话状态、取消、订阅 | `src/synapse/runtime/sessions/runtime.py` |
| UI 无关语义事件 | `src/synapse/runtime/streaming/events.py` |
| 有界事件回放 | `src/synapse/runtime/sessions/events.py` |
| HITL 解析与 resume payload | `src/synapse/runtime/hitl.py` |
| 会话元数据 | `src/synapse/sessions/store.py` |
| checkpoint 历史读取 | `src/synapse/sessions/transcript.py` |
| MCP 配置和连接池 | `src/synapse/integrations/mcp_client.py` |
| 图片附件 | `src/synapse/content/multimodal.py` |

当前缺口：

- ACP 独立进程入口和 stdout 隔离。
- ACP session descriptor 与 session-scoped resources。
- 完整 ContentBlock codec。
- `TurnEvent` 到 ACP SessionUpdate 的稳定状态机。
- Permission request 在同一 ACP prompt 内 resume。
- load/resume/fork/list/delete 等生命周期的精确语义。
- Client-provided MCP 的会话隔离。
- Client filesystem/terminal backend。
- Plan、diff、结构化 tool 内容和完整 usage 事件。
- capability 单一真源和协议合规测试。

## 5. 目标架构

```text
ACP Client
  -> official ACP SDK / stdio JSON-RPC
      -> SynapseACPAgent
          ├─ CapabilityRegistry
          ├─ ACPSessionManager
          ├─ ContentCodec
          ├─ ACPEventBridge
          ├─ PermissionCoordinator
          ├─ ClientServiceGateway
          └─ ACPMcpAdapter
              -> RuntimeManager / SessionRuntime
                  -> AgentTurnRuntime
                      -> Synapse Deep Agent
```

建议目录：

```text
src/synapse/acp/
├── __init__.py
├── server.py
├── agent.py
├── capabilities.py
├── models.py
├── sessions.py
├── content.py
├── events.py
├── permissions.py
├── client_services.py
├── mcp.py
└── errors.py
```

SDK schema 类型只允许出现在 `synapse.acp` 边界。runtime、sessions、integrations 不反向依赖 `acp`。

## 6. 核心领域模型

完整实现前先引入会话描述，避免后续为 cwd、MCP 和 Client capability 推翻 factory：

```python
@dataclass(frozen=True, slots=True)
class ACPSessionDescriptor:
    session_id: str
    cwd: Path
    additional_directories: tuple[Path, ...]
    mcp_servers: tuple[object, ...]
    client_capabilities: object
    config_options: Mapping[str, object]
```

每个 ACP session 至少拥有：

- 独立 `SessionRuntime`。
- 固定 cwd 和额外根目录。
- 独立 MCP scope。
- Client capability snapshot。
- 活跃 prompt、pending permissions 和 event bridge。
- 可取消的有界输出队列。

## 7. 关键语义

### 7.1 Prompt 生命周期

一个 ACP `session/prompt` 可以包含多个 Synapse turn：普通 turn 及若干 HITL resume turn。对 ACP Client 而言，它们仍是同一个 prompt request。

```text
prompt
  -> submit turn
  -> stream updates
  -> waiting approval?
      -> request_permission
      -> submit resume turn
      -> stream updates
  -> final stop reason
```

### 7.2 事件桥

`SessionEventBroker` 回调可能发生在 Agent runtime 线程；ACP SDK 运行于服务 asyncio loop。桥接必须使用 `loop.call_soon_threadsafe` 和有界队列，不能在 broker callback 中直接 await。

- Tool、permission 和终态事件不得丢失。
- 文本 delta 可合并但不得乱序。
- 慢 Client 必须触发有界背压策略，不能无限积压。

### 7.3 会话级 MCP

ACP `session/new/load/resume` 传入的 MCP servers 只属于当前 session：

- 转换为 Synapse `McpServerConfig`。
- 使用 session scope key 建立连接池。
- 构建该 session 的 Agent tools。
- session 关闭时释放。
- 不写入全局 MCP 配置，不泄漏到其他 session。

### 7.4 Client 反向服务

当 Client 声明 filesystem/terminal capability 时，Synapse 可以通过 Client RPC 获取未保存 buffer 或 IDE terminal。未声明时回退本地 backend；所有路径仍受 workspace 和安全策略约束。

## 8. 阶段划分

| 阶段 | 目标 | 方案 |
|---|---|---|
| P0 | 锁定 SDK/schema、能力矩阵和测试护栏 | [phase-0-protocol-baseline.md](phase-0-protocol-baseline.md) |
| P1 | stdio 入口、初始化和核心会话 | [phase-1-core-transport.md](phase-1-core-transport.md) |
| P2 | 完整 prompt/content/update 语义 | [phase-2-prompt-semantics.md](phase-2-prompt-semantics.md) |
| P3 | Permission/HITL 闭环 | [phase-3-permissions.md](phase-3-permissions.md) |
| P4 | 完整会话生命周期和历史 | [phase-4-session-lifecycle.md](phase-4-session-lifecycle.md) |
| P5 | 会话级 MCP servers | [phase-5-session-mcp.md](phase-5-session-mcp.md) |
| P6 | Client filesystem/terminal 反向服务 | [phase-6-client-services.md](phase-6-client-services.md) |
| P7 | Config、commands、usage、metadata、auth | [phase-7-advanced-capabilities.md](phase-7-advanced-capabilities.md) |
| P8 | 合规、跨平台、真实客户端和发布收口 | [phase-8-compliance.md](phase-8-compliance.md) |

阶段允许增量发布，但每个阶段只声明已经过门禁的 capability。最终 P8 对 P0 矩阵逐项清零。

## 9. 跨阶段约束

- `agent-client-protocol` 必须精确锁版本，升级必须新增决策记录和 schema diff。
- ACP stdout 只允许协议消息，日志只写 stderr。
- 所有输入大小、队列、历史回放、分页和日志必须有界。
- Client 环境变量、MCP env、headers、token 不得写日志或落盘。
- 同一 session 禁止重叠 prompt；不同 session 按配置并发。
- capability 不能先于实现和测试开启。
- 任何协议对象都必须使用 SDK model/helper 构造，禁止手写不受校验的 wire dict，除非 SDK 明确要求。
- Windows 和 Linux 都是发布门禁。

## 10. 总体验收

- P0 能力矩阵全部关闭，无未解释空项。
- 所有必选和声明能力有协议级测试。
- 所有稳定联合类型有转换或明确的不声明策略。
- Permission、cancel、disconnect、并发竞争均保持状态一致。
- 会话 MCP、Client terminal 和 filesystem 无跨 session 泄漏。
- Zed 和一个官方 SDK 驱动 Client 完成端到端测试。
- `uv run --no-sync ruff check .` 通过。
- ACP 相关测试及全量测试通过。
- 文档明确 SDK 版本、schema 版本、能力列表和客户端配置。

实时状态和任务勾选只维护在 [progress.md](progress.md)。
