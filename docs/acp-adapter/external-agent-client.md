# 外部 ACP Agent 接入开发指南

本文记录 `agy` 的真实协议验证，并定义 Synapse 客户端的实现与验收范围。**独立客户端内核已实现，但 Runtime 和 GUI 尚未开放接入**；现有 `synapse-acp` 是相反方向的服务端入口，保持兼容。开发分支：`feat/acp-external-agents`。

## 已验证的行为

环境：Windows，`agy 1.5.10`，ACP v1 stdio，本机 HTTP 代理 `127.0.0.1:7990`。真实验证在临时工作区使用纯文本提示，不提供 MCP、终端或文件回调，权限请求一律取消。协议测试和产品 GUI 验收是两件事。

| 场景 | 观察结果 |
| --- | --- |
| 子进程连接 | `initialize` 确认协议 v1。未显式覆盖代理时，`session/new` 收到请求但 40 秒无响应；日志出现 Google API `i/o timeout`。仅在子进程环境设置 `HTTP_PROXY` 和 `HTTPS_PROXY` 后，`session/new` 成功（不同轮次约 5–14 秒）。不由此断言原先是直连。 |
| 配置发现 | 新会话返回 `configOptions`，包括 7 个模型、3 个推理档位、3 个模式，同时返回 `models` 和 `modes`。客户端选择标准 `configOptions` 路径。 |
| 会话内切换 | `session/set_config_option` 响应确认 Gemini 4 Argon → Gemini 3.8 Flash，另一次测试确认切回 Argon；推理档位 `medium` → `low`。也观察到 `config_option_update`，但切换成功不依赖另等通知。 |
| 两轮流式对话 | 第一轮设为 Flash，第二轮切回 Argon 并设为 `low`。两轮都返回 `end_turn`，第二轮正确回忆上一轮的测试短码；通过 `agent_message_chunk` 收到回复，第二轮拆成两个块。验证了切换后的会话可继续对话，不是供应商推理路由的独立证明。 |
| 取消 | 在 `session/prompt` 发出后约 250 毫秒发送 `session/cancel` 通知；挂起的 prompt 返回 `stopReason: cancelled`。未覆盖输出后取消或等待审批时取消。 |
| 跨进程加载 | 用同一外部 ID 和工作目录调用 `session/load`，回放用户和助手历史；此前实测推理档位从 `low` 回到 `medium`。新增客户端验证回放未进入实时流，加载后再发一轮，Agent 正确回忆上一轮的测试短码。 |
| 完全权限启动 | 真实 `agy` 接受 `--dangerously-skip-permissions`；在空临时工作区用工具写入指定测试文件，收到 `in_progress` 和 `completed` 工具事件，核对文件内容正确，未收到 ACP 权限请求。 |

真实 Agent 尚未覆盖：工具与审批往返、计划/富媒体、模式切换、异常退出后的恢复，以及其他 Agent 的兼容性。这些不能作为已支持功能对外宣称。

## 实现顺序

### 1. ACP 客户端与进程管理

新增独立客户端模块，复用锁定的 `agent-client-protocol==0.12.0`，参考 `tests/test_acp_p1_transport.py` 的 SDK 子进程测试。先做 `initialize`、`session/new`、`session/prompt`、`session/cancel` 和配置读写。`session/load` 作为可选能力单独实现与验收；不支持或加载失败时保留只读历史，不自动开新会话冒充恢复。

- Agent 定义来自 Runtime 的本机用户配置。可配置可执行文件、参数数组和经允许的子进程环境覆盖项；Web 只接收已配置 Agent ID，项目配置和请求不能指定启动命令。不经 shell，不继承 Synapse 配置中的模型密钥，不在事件、启动参数或日志里公开代理凭据。代理示例只对 `agy` 子进程设置 `HTTP_PROXY`、`HTTPS_PROXY`，不修改系统设置。
- stdout 专用于 ACP JSON-RPC；stderr 单独有界采集、脱敏。区分启动、握手、会话创建、配置调用和回合取消的失败阶段，记录耗时和错误类别。给新建会话单独留出等待窗口，不能套握手超时。子进程退出、超时或版本不兼容时明确报错，不静默回退到原生后端。
- 只启动可信程序。外部进程以 Runtime 的系统账户运行，ACP 能力声明和审批不是系统沙箱。首版不提供客户端文件或终端服务，不传项目 MCP 凭据。取消先走 ACP；超时才回收进程，并清理活跃回合。

#### 完全权限模式

`--dangerously-skip-permissions` 是 `agy` 的启动参数，不是 ACP 通用参数。对可信 Agent，Runtime 用户可在本机 `external-acp-agents.json` 条目中显式设 `"permission_mode": "full_access"`，并在 `args` 中配置 `["acp", "--dangerously-skip-permissions"]`。命令仍须是绝对可执行路径；其他 Agent 使用其各自文档定义的参数。Web 请求不能传启动参数或更改权限模式。

首版 GUI 可显示「完全权限」标识，不需要为这类会话实现审批弹窗。`full_access` 标识本机启动策略，不改变 Runtime ACL，也不赋予系统管理员权限。若 Agent 仍发来 `session/request_permission` 且没有审批处理器，客户端返回 `cancelled`，绝不替它自动批准。默认 `manual`；交互审批留作后续能力。

#### 新建会话的等待窗口

`session/new` 是 Agent 自己的活儿，可能包含它的云端往返（`agy` 会先校验账号与模型目录）。同一台机器实测 `agy 1.5.10`：代理可用时 5–30 秒返回并给出 `model` / `thought_level` / `mode`；云端不可达时 180 秒后才降级返回，且 `configOptions` 只剩 `mode`。客户端默认给这一段 45 秒，因此慢启动的 Agent 会以 `ACP session/new timed out` 失败。

本机 `external-acp-agents.json` 的条目可单独放宽这一段：

```json
{
  "id": "agy",
  "name": "Antigravity CLI",
  "command": "C:\\Users\\me\\AppData\\Local\\agy\\bin\\agy.exe",
  "args": ["acp", "--dangerously-skip-permissions"],
  "permission_mode": "full_access",
  "env": {"HTTPS_PROXY": "http://127.0.0.1:7892"},
  "create_timeout_s": 120
}
```

- `create_timeout_s` 只放宽 `session/new` / `session/load`；握手、配置写、回合和取消仍用各自默认值。
- 取值 5–600 秒，省略即沿用默认 45 秒；非数字、布尔或越界会拒绝整个配置（daemon 启动时记录一条 warning 并关闭该功能，不猜测意图）。
- 文件在 daemon 启动时读取一次，改完需重启 `synapse-runtime`。
- 超时不会被当成"没有配置"静默回退：控制台在等待期间显示「正在连接 <Agent>…」，失败后在输入区保留错误提示，不会改用原生模型/思考选择器冒充正常会话。

### 2. Runtime 会话与配置契约

在 `src/synapse/runtime/service/local.py` 的会话创建、提交和取消接入分流，并通过服务访问层复用现有项目/会话权限检查。Runtime 拥有子进程，不由浏览器或 Tauri 启动。产品首版以一活跃外部会话对应一子进程为基线，同一会话串行处理配置写和回合。

- `session.create` 保持只写元数据；操作者进入外部会话后触发可重入的初始化，确保**第一条消息前**即可看到模型列表。持久化 Synapse 会话 ID、后端类型、Agent ID；支持加载时另存外部 ID 与工作目录。对前端只暴露所需字段和连接状态。
- 配置采用会话作用域，不复用原生全局模型注册表作为数据源。从 `session/new` / `session/load` 和 `config_option_update` 读取完整配置快照。按 `configId` 和候选 `value` 调用 `session/set_config_option`。成功快照整体替换；失败不提交本地选值。由 Runtime 序列化写入并为发布快照分配递增版本，前端忽略旧版本；刷新通过查询获取当前快照。
- 支持 `model`、`thought_level`、`mode`、`model_config` 和通用会话配置；按 Agent 顺序渲染普通/分组选择器与布尔开关，忽略未知类型。没有 `configOptions` 时可兼容旧 `modes` / `session/set_mode`，不自动尝试 `session/set_model`。加载后以 Agent 最新确认值为准，不把上次显示的快照当作当前配置，也不静默重新应用。
- 更新 Runtime 协议注册表，导出生成契约和前端类型，增加权限与旧客户端兼容性用例。原生会话仍是默认，既有调用的形状和行为不变。

### 3. 桌面与浏览器渲染

Tauri 和浏览器共用 React 控制台，无需另写 Rust 聊天界面。

| 入口 | 改造 |
| --- | --- |
| 新建会话 / `ModelControls.tsx` | 选择原生或已配置 Agent，创建后不可切换后端。连接中显示进度；无模型选项显示「由 Agent 管理」。切换等待 Agent 确认并防止发消息抢跑；同时切换两个会话时不串状态。 |
| `Transcript.tsx` 与行渲染器 | 文本块追加到当前回合，思考块折叠，工具按 `toolCallId` 合并到同一卡片，计划按其快照单独展示。保留 `stopReason`，明确区分正常完成、用户取消、额度中止与异常。外部文本/工具结果按不可信输入渲染，不当作已执行的本地变更。 |
| `historyMapper.ts` | 转换后的同一套 Runtime 事件供实时和历史使用。以 Runtime 事件 ID/序号去重；本地已保存历史优先。支持 `session/load` 时显式隔离其回放通知，不将回放当作新回合，不用文本匹配去重。 |

## 开发阶段与交付门槛

| 阶段 | 交付 | 合并前检查 |
| --- | --- | --- |
| P1 客户端内核 | 受控启动、握手、会话、配置、回合、取消和反向回调边界 | 离线 stdio 测试覆盖握手、会话、切换、更新和取消 |
| P2 Runtime | 本机 Agent 配置、后端元数据、会话初始化和回合路由、ACL 与持久化事件 | 原生兼容、拒绝任意命令、退出/超时清理、契约生成通过 |
| P3 GUI | 新建选择器、连接与权限模式标识、模型与推理控件、实时消息 | 组件测试覆盖首条消息前选择、切换失败、双会话隔离和流式渲染 |
| P4 恢复 | 事件重放、重连、可选 `session/load` 和只读降级 | 无重复消息、加载后的配置可信，不支持加载时可回看 |
| P5 集成验收 | 桌面与浏览器、代理排障、跨平台和既有入口回归 | 离线用例过线；用 `agy` 完成桌面手工冒烟并记录未覆盖能力 |

各阶段按顺序合并；P1 不是可用产品入口。未完成的阶段不对用户开放对应功能。

当前进度：

- **P1 完成**。离线子进程验证配置、流式通知、取消、创建超时与加载隔离；真实 `agy` 验证握手、模型与推理档位切换、两轮对话、取消、跨进程加载后续聊和免审批工具写入。
- **P2/P4 完成**。本机 `external-acp-agents.json` 配置、daemon 共享会话池、不可变 Agent 绑定、远端 ID 原子持久化、失效回收、回合事件转换、回合写入既有 `transcript.sqlite`，以及 **daemon 级事件总线回退**：外部会话没有原生 `SessionRuntime`，`runtime.events.read` / `runtime.events.watch` 改由每会话一个 `SessionEventBroker` 提供相同的游标、gap 与重连语义。
- **P3 完成（GUI 已可用）**。新建会话菜单列出「Synapse（内置）」与 daemon 报告的本机 Agent；外部会话的模型、推理档位等控件直接渲染 Agent 的 `configOptions`，切换走 `runtime.external.session.config` 并整体替换快照；`full_access` 只显示为徽标，控制台不能切换它。
- **P5 完成（核心能力与健壮性补齐）**：
  - **并发插话与严格对端队列兜底**：`runtime.turn.steer` 路由至外部 ACP 会话，优先通过并发 `session/prompt` 注入（适配 `agy acp`），若对端返回 `busy` / `RequestError` 则自动降级为客户端 FIFO 队列，并在取消时清空待发队列；分段写入 `transcript.sqlite`。
  - **空闲态后台唤醒续接**：`ExternalTurnBridge` 长期持有 `broker.emit`，捕获对端在 `end_turn` 后因后台任务唤醒主动推送的 `session/update`，自动开启续接 turn 实时推送前端并通过 `TranscriptProjection.append_continuation` 落盘，不伪造空用户气泡。
  - **交互式工具审批往返**：`session/request_permission` 桥接到 `APPROVAL_REQUIRED` / `runtime.turn.pending_approval` / `runtime.turn.resume`，支持在 `manual` 权限模式下由前端弹窗确认并选择 `allow_once` / `reject_once`。
  - **Token 用量与上下文占比**：投影 ACP `usage_update` 与 `PromptResponse.usage` 到 `USAGE_UPDATED`、`TurnTerminalPayload` 及 `TranscriptProjection`。
  - **生命周期与资源治理**：`ExternalSessionPool` 支持默认 30 分钟空闲自动回收（忙碌态自动顺延）与满容量（16）时的 LRU 空闲淘汰；`close_session` 同步释放外部子进程；`fork_session` 对外部会话明确拒绝以防共享 `remote_session_id` 串上下文；`external-acp-agents.json` 支持按文件修改热重载（损坏时保留上一次有效目录）。
  - **诊断与多 Agent 兼容**：子进程 `stderr` 改为有界环形缓冲 + 凭据脱敏采集；兼容仅暴露旧版 `modes` 的对端（`session/set_mode`）；捕获 `available_commands_update` 与 `config_option_update` 并在回合结束时自动同步前端配置快照；支持图片附件与已启用 MCP Server 透传。

### 公开方法（全部走现有 ACL）

| 方法 | 能力 | 作用 |
| --- | --- | --- |
| `runtime.external.agents.list` | `session.read`（项目） | 列出本机配置的 Agent：id、显示名、权限模式；不含命令、参数、代理或凭据 |
| `runtime.session.create`（新增可选 `external_agent_id`） | `session.create`（项目） | 只写元数据并绑定 Agent；daemon 校验该 id 是否在本机目录中 |
| `runtime.external.session.initialize` | `session.open`（会话） | 启动或加载 Agent，返回配置快照 |
| `runtime.external.session.config` | `session.open`（会话） | 写入一个配置项，返回 Agent 确认的完整快照 |
| `runtime.turn.submit` / `runtime.turn.cancel` | 既有 | 外部绑定会话自动路由到 Agent；未绑定会话行为不变 |
| `runtime.events.read` / `runtime.events.watch` | 既有 | 外部会话由 daemon 事件总线回退提供 |

外部会话不能按原生方式打开（否则会静默回退），因此 `runtime.session.open` 对绑定会话报错，控制台改走 initialize。

## 验收顺序

1. **可控 ACP 测试 Agent（Windows/Linux）**：模型和推理配置联动、拒绝切换、无模型、配置更新排序、文本分块、工具更新、审批、取消、子进程崩溃、超时和重连；检查没有进程或待审批泄漏。
2. **Runtime 与 GUI**：新建选择 `agy` → 第一条消息前选模型 → 两轮之间切换模型/推理档位 → 查看流式文本和工具卡片 → 停止一轮 → 刷新历史。若实现加载，再覆盖从已有历史恢复且不产生重复行，并核验实际配置没有被旧值覆盖。
3. **真实 Agent 手工冒烟**：在有网络与已认证的环境下走同一脚本化顺序；提示只要求回复测试短码，不读取项目内容，禁用未验收能力。使用有界超时，记录响应字段、事件类型和耗时，不保存原始认证流量。CI 默认离线，不依赖个人代理、账户和模型配额。
4. **回归**：运行 `tests/test_acp*.py`、Runtime 契约检查与前端针对性测试；原有 `synapse-acp` 仍能被外部客户端连接，未启用外部 Agent 的环境不改变原生会话体验。

参考：[ACP v1 会话配置](https://agentclientprotocol.com/protocol/v1/session-config-options)、[ACP v1 Prompt Turn](https://agentclientprotocol.com/protocol/v1/prompt-turn)、[现有 Synapse ACP 适配方案](index.md)。
