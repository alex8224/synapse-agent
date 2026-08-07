# Agent Runtime 与 TUI 解耦及全局项目管理总体方案

> 文档状态：Proposed  
> 实施状态：尚未开始  
> 首要里程碑：先完成 Agent loop 与 TUI 的生命周期解耦，再扩展后台会话和跨项目能力。  
> 权威进度台账：[progress.md](progress.md)  
> 架构决策记录：[decisions.md](decisions.md)

## 1. 结论

本次演进采用以下总体架构：

- 保持单进程，不为每个项目创建常驻进程。
- 保持 Textual UI loop 与现有进程级 Agent `AsyncRuntime` loop。
- Agent turn、session 和 project runtime 不由 TUI widget 或 Textual worker 拥有。
- Agent runtime 只产生带明确归属和顺序的语义事件。
- TUI 通过进程内事件桥订阅事件并在主线程渲染。
- 项目是资源隔离域，会话是执行和取消域，TUI 是可随时 attach/detach 的客户端。
- `catalog.sqlite` 只负责全局发现和定位；项目本地数据库仍是会话数据真源。

第一阶段目标不是立即实现多项目，而是证明：

> 即使没有 `CodingAgentApp`、Textual widget 或实时订阅者，一次 Agent turn 仍可完整运行、取消、持久化并产出标准事件。

## 2. 背景与问题

当前代码已经有部分可复用基础：

- `src/synapse/runtime/async_runtime.py` 提供进程生命周期的后台 asyncio loop。
- `src/synapse/ui/stream.py` 使用 `StreamSink` 抽象 CLI/TUI 输出。
- `src/synapse/ui/turn/controller.py` 会冻结当前回合的 Agent 和 `thread_id`。
- transcript generation 可以丢弃切换后的迟到 UI 回调。
- `~/.synapse/catalog.sqlite` 已有项目和会话元数据投影。

但是 Agent turn 的生命周期仍由 `CodingAgentApp` 和 Textual worker 控制：

- `_busy`、`_cancel_event`、`_active_turn_agent`、`_active_turn_thread_id` 是 App 级状态。
- `run_turn` 使用 Textual `@work(thread=True, exclusive=True)`。
- `TextualStreamSink` 在运行过程中直接修改当前 transcript DOM。
- 回合收尾、usage、goal、summary、catalog 投影依赖当前 App 状态。
- `stream_agent()` 的结果还依赖 sink 的可变缓冲区。
- retry notifier、GoalService、MCP pool 等存在进程级单例或单槽状态。

因此，当前“切换 thread”不等于切换完整执行上下文，也无法让旧会话在 UI 切走后独立继续运行。

## 3. 目标

### 3.1 功能目标

1. Agent turn 可在无 TUI 环境中独立执行。
2. TUI 切换或暂时不订阅时不影响 Agent 执行。
3. 同一项目的多个会话可独立运行、取消、steer 和恢复观察。
4. Synapse 可在任意目录启动并浏览全部已注册项目及会话。
5. 选择跨项目会话后，Agent 使用该会话所属 workspace、配置和数据库工作。
6. 其他已启动会话继续运行，直到完成、单独取消或程序退出。

### 3.2 架构目标

- 运行层不导入 Textual，也不调用 `CodingAgentApp`。
- UI 层不拥有 Agent task，只发送命令和消费事件。
- 所有运行事件都有稳定归属、顺序和终态。
- 持久化只依赖冻结的运行上下文和领域事件，不依赖 widget 状态。
- 项目配置、Goal、MCP、backend 和数据库资源可按项目隔离。
- 所有队列、缓存、搜索和历史读取均有界。

## 4. 非目标

本计划当前不包含：

- 每项目一个进程或每会话一个进程。
- TUI 退出后 Agent 仍跨进程继续运行的 daemon。
- 网络 RPC 或 IPC 协议。
- 将全部项目会话正文集中迁移到全局数据库。
- 第一阶段直接开放无限并行。
- 在运行时动态修改进程 `cwd`。

未来如需要关闭 TUI 后仍继续执行，可在稳定的 RuntimeManager API 之外增加 daemon；不能让该需求反向污染当前解耦步骤。

## 5. 当前与目标依赖方向

### 5.1 当前方向

```text
CodingAgentApp
  -> TurnController
      -> stream_agent
          -> TextualStreamSink
              -> TranscriptController / DOM
      -> goal / steer / persistence / catalog
```

### 5.2 解耦里程碑后的方向

```text
CodingAgentApp
  -> TUI command adapter
      -> AgentTurnRuntime
          -> runtime streaming core
          -> AgentEventSink

AgentEvent
  -> TextualEventRenderer
      -> TranscriptController / DOM
```

### 5.3 最终方向

```text
Synapse 单进程
├─ Textual UI loop（主线程）
│  ├─ Global project/session views
│  ├─ active SessionRef
│  └─ TextualEventRenderer
│
└─ Agent AsyncRuntime loop（后台线程）
   └─ RuntimeManager
      ├─ ProjectRuntime A
      │  ├─ SessionRuntime A/1
      │  └─ SessionRuntime A/2
      └─ ProjectRuntime B
         └─ SessionRuntime B/1
```

## 6. 核心领域边界

### 6.1 Turn

一次用户提交或 HITL resume 对应一个不可变 `TurnContext`：

```python
@dataclass(frozen=True)
class TurnContext:
    thread_id: str
    turn_id: str
    agent: Any
    settings: Settings
    request: TurnRequest
```

运行开始后，TUI 的当前页面、活动 thread 或 model picker 都不能改变该上下文。

### 6.2 Session

会话是实际执行和取消域：

```python
class SessionRuntime:
    ref: SessionRef
    agent: Any
    status: SessionStatus
    active_turn: TurnHandle | None
    cancel_token: CancelToken
    steer_queue: SteerQueue
    usage: SessionUsage
```

同一会话同时最多一个 graph run；不同会话可在受控并发限制内运行。

### 6.3 Project

项目是资源和配置隔离域：

```python
class ProjectRuntime:
    project_id: str
    workspace: Path
    settings: Settings
    session_store: SessionStore
    checkpointer: Any
    goal_service: GoalService
    mcp_scope: McpScope
```

项目不是独立进程，也不是独立 event loop。

### 6.4 UI

TUI 只负责：

- 查询项目和会话。
- 向 RuntimeManager 发送 open/submit/steer/cancel 命令。
- attach/detach 某个 `SessionRef`。
- 从 snapshot 和有序事件恢复并实时渲染。
- 展示状态，不决定运行是否继续。

## 7. 事件模型原则

Turn 层首先产出 turn-local 事件；SessionEventBroker 后续为其包装 session-local 序号。

```python
@dataclass(frozen=True)
class TurnEvent:
    thread_id: str
    turn_id: str
    sequence: int
    kind: TurnEventKind
    payload: object
```

最少事件集合：

- `turn_started`
- `activity_changed`
- `reasoning_delta` / `reasoning_completed`
- `answer_delta` / `answer_completed`
- `tool_batch_started`
- `tool_started` / `tool_updated` / `tool_finished`
- `tool_batch_finished`
- `usage_updated`
- `context_compacted`
- `info`
- `turn_cancelled`
- `turn_failed`
- `turn_completed`

约束：

- payload 必须是 runtime 领域数据，不能是 Textual widget。
- 运行结果不能依赖消费者是否存在或是否渲染了事件。
- delta 可以合并显示，但 completed/failed/cancelled 终态不能丢。
- UI 策略如 DAG task group 隐藏、Git chrome 刷新、颜色和折叠均留在 renderer。

## 8. 实施阶段

| 阶段 | 名称 | 核心产物 | 完成后的能力 |
|---|---|---|---|
| P0 | 基线与护栏 | 行为矩阵、回归 trace、依赖约束 | 可安全开始重构 |
| P1 | Streaming core 与事件契约 | UI-independent 事件、accumulator、兼容 adapter | 流解析不再以 UI sink 状态为真源 |
| P2 | AgentTurnRuntime | 无 TUI 的独立 turn 状态机与 API | headless turn 可完整运行 |
| P3 | TUI 事件适配与切换 | Textual renderer、现有 TUI 接入 | Agent loop 与 TUI 解耦里程碑完成 |
| P4 | SessionRuntime 与事件 Broker | task/cancel/goal/persistence 会话化 | 无订阅者时会话仍继续 |
| P5 | 同项目多会话 | RuntimeManager、会话并发与 attach/detach | 切换后旧会话继续运行 |
| P6 | ProjectRuntime | 项目配置和资源隔离 | 单进程内安全打开多个项目 |
| P7 | 全局控制面 | 任意位置启动、项目分组会话与操作 | 达成全局项目/会话管理目标 |
| P8 | 稳定性与性能收口 | 负载、内存、关闭、故障恢复验证 | 达到可长期运行标准 |

详细方案：

- [P0：基线与护栏](phase-0-baseline.md)
- [P1：Streaming core 与事件契约](phase-1-streaming-core.md)
- [P2：AgentTurnRuntime](phase-2-turn-runtime.md)
- [P3：TUI 事件适配与切换](phase-3-tui-cutover.md)
- [P4：SessionRuntime 与事件 Broker](phase-4-session-runtime.md)
- [P5：同项目多会话](phase-5-multi-session.md)
- [P6：ProjectRuntime](phase-6-project-runtime.md)
- [P7：全局控制面](phase-7-global-control-plane.md)
- [P8：稳定性与性能收口](phase-8-hardening.md)

## 9. 阶段门禁

每个阶段必须满足：

1. 上一阶段验收项全部通过。
2. `progress.md` 中不存在未处理的阻塞项。
3. 新公共边界有类型标注和最小文档。
4. 先运行最窄测试，再运行相关领域测试。
5. 涉及导入路径移动时保留必要 re-export。
6. 不允许通过关闭测试、放宽断言或吞异常绕过门禁。
7. 行为或配置变化同步更新用户文档。

P3 是首个强制停顿点：只有证明 Agent turn 在 TUI 无订阅时仍正确运行，才能开始 P4。

P5 是第二个强制停顿点：只有同项目多会话不串线，才能开始跨项目资源隔离。

## 10. 测试策略

### 10.1 分层测试

- 纯事件测试：fake LangGraph events -> ordered `TurnEvent`。
- Turn runtime 测试：headless 运行、取消、错误、HITL、retry。
- Adapter 合约测试：同一事件 trace 在 Rich/Textual 中语义一致。
- Session 测试：detach、reattach、后台完成、按会话取消。
- Project 测试：两个临时 workspace 的配置、文件和数据库不串线。
- TUI pilot 测试：实时 token、工具组、状态栏和历史恢复。
- 长时测试：并行任务、内存回收、程序关闭和异常恢复。

### 10.2 常用验证命令

```powershell
uv run --no-sync pytest tests/test_stream_cancel.py -q
uv run --no-sync pytest tests/test_stream_tool_items.py -q
uv run --no-sync pytest tests/test_textual_stream_sink.py -q
uv run --no-sync pytest tests/test_turn_controller.py -q
uv run --no-sync pytest tests/test_project_catalog.py -q
uv run --no-sync ruff check .
uv run --no-sync pytest -q
```

具体阶段的最窄测试见对应阶段文档。

## 11. 兼容与回滚策略

- 旧导入路径如 `synapse.ui.stream.stream_agent` 在迁移期继续 re-export。
- 每一阶段保持一个可工作的兼容 adapter，不允许同时重写 runtime 和所有 renderer。
- 先建立新路径和契约测试，再切换调用方，最后删除旧路径。
- 数据库 schema 变化必须向前迁移，不能要求用户删除本地状态。
- P0-P3 不改变现有用户命令和会话数据格式。
- 每阶段独立提交，出现回归时可按阶段回退。

## 12. 进度与方案更新规则

`progress.md` 是实施状态的单一真源：

- 开始任务前，将对应任务标为 `in_progress` 并更新“当前工作”。
- 完成任务后记录验证命令和结果，再标为 `completed`。
- 阻塞时记录证据、影响和解除条件，不能仅写“有问题”。
- 设计变化先更新 `decisions.md` 和对应阶段文档，再修改代码。
- 阶段完成时更新阶段验收、风险余项和变更日志。
- 阶段文档中的任务表定义范围，不重复维护实时状态。

## 13. 整体完成定义

只有同时满足以下条件，本计划才算完成：

- Agent runtime 包不依赖 Textual。
- TUI 切换订阅不会取消后台会话。
- 不同会话的事件、usage、goal、取消和持久化不串线。
- 不同项目的 workspace、配置、MCP、Goal 和数据库不串线。
- 任意目录启动不污染该目录，仍能浏览和打开已注册项目。
- 全局会话操作使用 `(project_id, thread_id)` 唯一定位。
- 运行中的会话只在完成、单独取消或程序退出时终止。
- 全量 Ruff、pytest 和文档构建通过。
- 长时运行的内存和任务数量符合 P8 确定的预算。
