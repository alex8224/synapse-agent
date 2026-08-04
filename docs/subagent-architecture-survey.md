# Synapse 子代理（Subagent）实现调查与重构建议

> 调研日期：2026-06-07。本文依据当前工作区代码、测试和官方公开文档；外部产品能力可能随版本变化。
>
> 术语说明：仓库代码使用 `subagent`；用户问题中的 “sbuagent” 按子代理理解。

## 1. 结论

Synapse 不是直接使用 DeepAgents 内建的 `SubAgentMiddleware`，而是在 `deepagents==0.6.12`、`langgraph==1.2.9`、`langchain==1.3.14` 之上，实现了 `DAGSubAgentMiddleware`。它将同一轮模型产生的多个 `task` 工具调用解析为 DAG，以波次（wave）执行：同一波内通过 `asyncio.gather()` 并发，下游任务将上游最终文本拼入自身提示词。

现有实现的核心价值是：

- 基于专长的静态角色：`researcher`、`tester`、`reviewer`；
- 以中间件兼容 DeepAgents，并在保留 ToolNode 协议正确性的前提下预先执行子任务；
- 把异步执行、依赖排序、结果回填、LangSmith tracing 和 Textual TUI 可视化串成完整链路；
- 子代理运行期间提供单独的 `SubagentMonitor`，规避 DAG 执行在父 `task` ToolMessage 绘制前发生、常规子图流不可见的问题。

主要问题不是“没有并行”，而是“调度契约和隔离边界仍不够严格”：`task_id` 不做唯一性校验、依赖的失败语义是继续执行且注入错误文本、全局并发控制与每任务预算缺失、代码存在未接入的启发式路由模块、配置文档遗漏实际生效的并行开关、监控注册表生命周期不完整。优先建议先收紧 DAG 规范和资源治理，再逐步演化为可持久化的计划/执行模型。

## 2. 调研范围与证据

### 2.1 工作区内关键文件

| 领域 | 文件 | 证据与职责 |
|---|---|---|
| 装配 | `src/synapse/app/agent.py` | `build_coding_agent()` 在 272-311 行决定 `parallel_subagents`，构造并检查 `DAGSubAgentMiddleware`；500-501 行加入 middleware；539-555 行用 `subagents=None` 调用 `create_deep_agent()`，确保不是框架内建子代理接管。 |
| 角色规格 | `src/synapse/runtime/subagents.py` | `build_default_subagents()` 定义 researcher/tester/reviewer 的描述、prompt、模型覆盖、工具排除和输出变换。 |
| DAG 调度 | `src/synapse/parallel_subagents.py` | `DAGTaskArgs`、拓扑波次规划、预编译、`DAGSubAgentMiddleware`、执行、缓存、追踪配置均在此。 |
| UI 中立监控 | `src/synapse/subagent_monitor.py` | 线程安全 `SubagentMonitor`，运行状态、事件、callback 和 LangChain event 解析。 |
| TUI | `src/synapse/ui/tui.py`、`src/synapse/ui/dialogs/subagent_monitor.py`、`src/synapse/ui/stream.py` | F9 和自动弹窗、状态栏、详情弹窗、嵌套工具事件归属。 |
| 用户入口 | `src/synapse/commands/slash_cmds.py` | `/subagents` 展示当前角色规格和模式，不负责开关或启动任务。 |
| 配置 | `src/synapse/settings/schema.py` | `parallel_subagents=False`、`max_parallel_subagents=6` 等字段。 |
| 测试 | `tests/test_dag_parallel.py`、`tests/test_subagent_monitor_dialog.py`、`tests/test_subagent_status.py`、`tests/test_slash_and_mcp.py` | 覆盖 DAG、监控、TUI 行选择、slash 展示等关键单元。 |

### 2.2 依赖和版本

`pyproject.toml` 当前固定：`deepagents==0.6.12`、`langchain==1.3.14`、`langgraph==1.2.9`、`textual==8.2.8`。因此本文的 DeepAgents 对比以这套“中间件 + `task` 工具 + LangGraph”模型为基准。

### 2.3 结论边界

- 未使用有效模型凭据执行真实的多代理端到端任务；本文对运行行为的判断来自代码路径和现有测试。
- 工作树已有与本报告无关的未提交改动；本文没有修改它们。

## 3. 框架与组件结构

### 3.1 总体结构

```text
用户请求
  │
  ▼
Textual TUI / CLI
  │  config.configurable: thread_id + subagent_monitor_id
  ▼
LangGraph / create_deep_agent
  │
  ├─ Synapse 主 middleware（安全、工具输出、路径、intent、steer 等）
  ├─ DAGSubAgentMiddleware
  │    ├─ 暴露 task(subagent_type, description, task_id, depends_on)
  │    ├─ 预编译 researcher / tester / reviewer 为 Runnable
  │    └─ 处理 task 的模型调用与工具调用
  └─ ToolNode（普通工具 + task 缓存结果）
       │
       ▼
子代理独立 LangGraph Runnable
  │  Filesystem / Todo / Summarization / Patch + 专有 middleware
  ▼
最终 AI 文本 → 父任务 ToolMessage → 主 Agent 汇总 → 用户
```

### 3.2 装配选择

`build_coding_agent()` 计算：

```python
effective_parallel_subagents = (
    bool(settings.parallel_subagents)
    if force_parallel_subagents is None
    else bool(force_parallel_subagents)
)
```

只有 `effective_parallel_subagents` 为真并且角色规格生成成功，才创建 `DAGSubAgentMiddleware`。编译后的 runnable 为空会回退到禁用模式。生成的 agent 元数据中记录：

- `_coding_subagents`：规格列表或 `None`；
- `_coding_parallel_subagents`：是否实际启用；
- `_coding_subagent_mode`：`parallel` 或 `disabled`。

注意：`Settings.enable_subagents` 已定义，但当前装配判断只读取 `parallel_subagents`；也就是说前者没有控制实际 DAG 启用。这是命名/语义漂移，应修正。

### 3.3 角色与最小权限

`build_default_subagents()` 返回三个 declarative spec：

| 角色 | 目标 | 工具策略 | 模型 |
|---|---|---|---|
| `researcher` | 定位符号、调用链、只读总结 | 在隔离模式排除 `write_file/edit_file/patch/execute` 和 todo 工具 | 继承主模型，预留 `researcher_model` 参数但 Settings 未暴露该字段 |
| `tester` | 运行窄测试、定位回归 | 保留 `execute`，排除 todo；明确 `tools=[]` 以采用内建工具 | 可由 `AGENT_SUBAGENT_TESTER_MODEL` 覆盖 |
| `reviewer` | 正确性、安全、风格审查 | 排除写工具，允许只读 shell / 测试 | 可由 `AGENT_SUBAGENT_REVIEWER_MODEL` 覆盖 |

所有角色附加：

1. intent schema middleware；
2. 工具排除 middleware；
3. 与父一致的工具输出可逆变换和错误恢复；
4. 父使用 OpenAI OAuth 且子角色未显式模型时，追加兼容 middleware；
5. 可读取 `tool-output://` 的结果读取工具。

隔离是“隐藏工具 + prompt 约束”，不是 OS 或文件系统隔离。代码注释已说明 DeepAgents `FilesystemPermission` 与可执行 shell backend 不兼容。对本地 coding agent 而言，这是务实的兼容方案，但不能把 researcher/reviewer 当成强安全边界。

## 4. 关键流程

### 4.1 从模型 tool call 到 DAG

主模型得到追加的 `DAG_USAGE_INSTRUCTIONS`，可以在同一个 assistant 回合发出多个：

```python
task(
    subagent_type="researcher",
    description="定位认证调用链",
    task_id="search",
)
task(
    subagent_type="tester",
    description="基于调研结果运行目标测试",
    task_id="test",
    depends_on=["search"],
)
```

流程：

1. `DAGSubAgentMiddleware.awrap_model_call()` 调用模型；
2. 从最后一个 `AIMessage.tool_calls` 挑出名称为 `task` 的调用；
3. `_execute_dag()` 转换参数为任务，调用 `_plan_dag_batches()`；
4. 规划器以 Kahn 风格的“就绪集合”生成波次，并以 `max_parallel` 对 ready 任务切片；无 ready 任务时抛 `ValueError("DAG 死锁...")`；
5. 每个波次先把所有任务置为 monitor 的 `pending`，待该波运行时改 `running`；
6. `asyncio.gather(*wave_coros)` 同时执行同一波；
7. 一个波次结果统一写入 `results` 后，才启动下一波；
8. 父 Agent 获得每个 task 的 ToolMessage，继续决定是否需要合并、追问或调用其他工具。

复杂度近似为 O(V + E) 的多轮扫描加任务执行成本；但当前用 list 扫描和 `ready[:limit]` 分批，独立任务超出上限时会形成额外“波次”。这符合并发限流语义，却让“波次”同时承担拓扑层级和容量批次两种含义，监控/报告应区分为 `dependency_level` 与 `batch_index`。

### 4.2 子任务执行、依赖传递与结果提取

子任务通过：

```python
await runnable.ainvoke(
    {"messages": [HumanMessage(content=description)]},
    subagent_config,
)
```

执行。`_enrich_description()` 将各依赖结果完整拼到后续 prompt 前部，再追加“你的任务”。这使 DAG 的数据流简单可见，但没有大小限制和结构化协议；大输出、错误文本和 prompt injection 都会沿依赖边放大。

结束时 `_extract_final_response()`：

1. 优先倒序取得最后一个无 tool call 的 `AIMessage`；
2. 兼容字符串和 content blocks；
3. 若没有最终回答则回退到最近 AI 文本，并清理 XML/DSML 工具 DSL 片段；
4. 若只看到工具错误，形成可读错误摘要；
5. 否则返回“子 Agent 未返回文本回复”。

这对不同 provider 输出格式有较强兼容性；代价是父 Agent 只接收文本，没有强制 JSON schema、证据字段或状态字段。

### 4.3 为什么有“预执行 + 缓存”两阶段

`task` 被作为普通 ToolNode 工具暴露，LangChain/模型协议要求 tool result 对应先前的 tool call ID。DAG middleware 不修改原 AIMessage：

1. `awrap_model_call()` 预先执行全部 DAG，将结果写入 `_dag_cache[tool_call_id]`；
2. ToolNode 之后触发 `awrap_tool_call()`；
3. 该 hook 取缓存生成 `ToolMessage`，不再运行子代理。

好处：保留所有模型 tool calls，避免 tool call/result 对不上；普通工具仍走正常 ToolNode。风险：缓存是 middleware 实例的可变字典，缺少每父运行的显式命名空间或锁；并行父请求、重试、取消后的清理与超时场景仍需补强。

### 4.4 追踪、命名空间与取消

- `_capture_parent_run_config()` 优先读取 `var_child_runnable_config`，其次 `langgraph.config.get_config()`，最后兼容 `runtime.config`；这是为了保留 callback、tag 和可配置项。
- `_build_subagent_run_config()` 生成 `checkpoint_ns`：`{parent}|task_call:{tool_call_id}|dag_{type}_{task_id}`，并设 `ls_agent_type=subagent`、metadata、tags、run_name。
- `_execute_dag()` 可用时创建名为 `dag_subagents` 的 LangSmith chain span；每子任务还使用 DeepAgents 私有 `_subagent_tracing_context`。
- TUI 取消会取消外层 `astream` producer 并修复会话；但是 `asyncio.gather()` 未显式处理 cancellation、timeout 或部分结果策略，需建立明确语义。

## 5. TUI 展示与可观测性

### 5.1 为什么另建 monitor

DAG 子任务在父 middleware 的 `awrap_model_call()` 内执行，而不是 ToolNode 后才启动；这时父 `task` ToolMessage 尚未显示。仅依赖 LangGraph `subgraphs=True` 不能稳定展示“已规划、执行中”的实时状态。因此使用进程内、UI 无关的 `SubagentMonitor`。

TUI 每次请求向 config 注入：

```python
"configurable": {
    "thread_id": turn_thread_id,
    MONITOR_CONFIG_KEY: self._subagent_monitor.monitor_id,
}
```

调度器经 `monitor_from_config()` 拿到同一个 monitor。

### 5.2 运行状态模型

`SubagentRun` 字段：

- `call_id`、`task_id`、`subagent_type`、`description`；
- `status`: `pending` / `running` / `ok` / `error`；
- `wave`、`depends_on`、开始结束时间；
- 一组 `SubagentEvent`：模型想法、工具执行、最终回答等。

`SubagentMonitor` 用 `threading.RLock` 保护 registry 和运行数据，`revision` 递增以使 UI 增量刷新。它支持两条事件采集路径：

1. runnable 有 `astream_events` 时，`SubagentStreamEventRecorder` 即时记录 `on_chat_model_end`、`on_tool_start/end/error`、`on_chain_end`；
2. 不支持流时安装 `SubagentMonitorCallback`，并在子图最终消息历史中回填事件。

事件显示优先 `intent`，不暴露完整工具参数/路径；这是隐私和信息密度上更适合 TUI 的选择。

### 5.3 Textual 交互

- `CodingAgentApp` 初始化一个 `SubagentMonitor`；每个新 turn reset。
- `F9` 绑定 `dialog_subagents`；任务开始后也会自动打开。
- 内联 `#subagent-status` 在 0.35 秒状态 tick 中显示 pending/running/done/error 计数；可点击打开详情。
- `SubagentMonitorDialog` 是近全屏 modal：左列包含 task ID、角色、波次、依赖、状态；右列显示任务描述、工具 intent、最终回答和耗时；`j/k` 或上下键选择，`r` 刷新，`Esc/q` 关闭。
- 父级纯 `task` 工具组会被 `TextualStreamSink` 抑制，避免“父 task 工具组”和监控对话框重复展示。对子图 tool event，`stream_agent()` 用 `checkpoint_ns` 内的 `task_call:{id}` 将嵌套工具精确归属到父 task；当缺少 namespace 且同批有多个父 task 时拒绝猜测，避免串任务。

### 5.4 TUI 现状问题

1. 弹窗自动打开策略分散：`sync_subagent_monitor_block()`、`_maybe_auto_open_subagent_monitor()` 都可开启，依赖布尔标志抑制重复；应收敛为一个 presenter 状态机。
2. monitor registry 用强引用全局 dict，`unregister_subagent_monitor()` 有定义但 TUI 生命周期未见调用；长时间运行或反复创建 App 可能泄漏。
3. status 和 dialog 不提供取消单个任务、重试失败任务、限制展示输出或下载 trace 的操作。
4. `SubagentRunRow` 定义中重复写了两次 `ALLOW_SELECT = False`，无功能影响但反映组件代码可清理。
5. 展示用“wave”未区分依赖层与受 `max_parallel` 拆分后的批次，容易误解调度因果。

## 6. 测试现状

已有较好的低层覆盖：

- `tests/test_dag_parallel.py`：独立、链、菱形、混合依赖；依赖输出注入；同波并发、未知角色、子代理异常、死锁；真实 specs 预编译；backend 注入 filesystem 工具；namespace/tracing config；XML 清理与结果提取；监控在没有 tool call ID、无 callback、使用 event stream 等情形下的回填。
- `tests/test_subagent_monitor_dialog.py`：依赖标签、关闭文本选择、刷新中点击行。
- `tests/test_subagent_status.py`：嵌套工具的 intent 标签。
- `tests/test_iteration_abc.py`：静态工具排除与规格摘要。
- `tests/test_slash_and_mcp.py`：`/subagents` 的 disabled / parallel 输出。

仍缺：

- 重复/空/非法 `task_id`；
- 未知 `depends_on` 的错误分类；
- `max_parallel` 分批时的实际最大并发与波次语义；
- 依赖任务失败后的阻断或继续策略；
- 外层取消时子任务是否都取消、cache 是否清理；
- 超时、`asyncio.gather` 意外抛出、monitor 完结状态；
- 两个父 run 并发时 `_dag_cache` 不串结果；
- 真正 `create_deep_agent → model task calls → ToolNode → ToolMessage` 协议回归测试；
- settings/文档契约、TUI 自动弹窗与 registry 注销测试。

## 7. 与主流方案对比

### 7.1 共性

主流实现普遍采用：独立上下文、专长/工具/模型配置、父代理汇总、并行适合读多写少的独立工作。这样能减少主线程的 context pollution/context rot，但一定增加 token、协调和可观测性成本。并行写同一工作区通常应避免或隔离。

### 7.2 对比表

| 方案 | 调度与状态模型 | 并发/依赖 | 安全/隔离 | 可观测与 UI | 与 Synapse 的关系 |
|---|---|---|---|---|---|
| DeepAgents | `SubAgentMiddleware` 将命名子代理作为 `task` 工具；子代理独立上下文，返回最终摘要 | 官方同步子代理支持同轮多 `task` 并行；另有 async/dynamic subagents 路径 | spec 可配 tools、middleware、permissions、interrupt | LangGraph/LangSmith 基础观测 | Synapse 的直接底座；自研 DAG 补了显式 `depends_on` 和 Textual 监控，但需跟随上游 API 演变。 |
| Claude Code / Agent SDK | 自定义 agent 定义（文件或代码），独立上下文、prompt、工具与权限；可恢复的自定义 agent | 可并行；SDK 支持 direct invocation/resume；Agent Teams 是跨 session 的另一层 | 明确 tool allowlist/denylist 与权限；子代理可限制 Read/Grep | CLI/产品可查看子代理；Agent Teams 还有消息与共享任务 | Synapse 的角色/工具隔离相近；缺少可恢复子会话、细粒度授权和 team 级协作。 |
| OpenAI Codex | 内建 default/worker/explorer 和项目级 custom agents；主线程收集汇总 | 专长子代理并行；主线程等待、可查看/切换线程；建议以读任务为主 | 默认继承父 sandbox/审批，可单 agent 只读覆盖 | App/CLI/IDE 展示子线程；CLI `/agent` 可切换 | Synapse 已有独立 monitor 和 DAG；缺少 thread 级持久审计、单子代理 stop/steer、工作树隔离策略。 |
| Cursor | 自定义 subagents 具备 prompt、工具、模型、只读配置；产品有默认角色 | 并行，偏后台/IDE 工作流 | 可用只读；并行写通常通过 worktree 降低冲突 | IDE sidebar/子代理活动与差异视图 | Synapse 更强调依赖 DAG；Cursor 对写入隔离和用户审阅的产品化更强。 |
| Google ADK | hierarchy + LLM agent；还提供 Sequential/Parallel/Loop 等 workflow agent 与 graph workflow | 既可 LLM 动态委派，又可确定性工作流显式并行 | framework 可集成企业治理，具体由工具/部署实现 | ADK 开发/运行观测工具 | Synapse 当前是“LLM 生成 DAG + 固定调度器”的折中；可借鉴明确的 workflow primitive 与 state key。 |
| LangGraph | 低层 `StateGraph`/`Send`/`@task`，可表达 routing、fan-out/fan-in、评估循环 | 图的边表达确定依赖；`Send` 动态 worker；checkpoint/state 可持久化 | 应用自己定义权限与工具边界 | graph 可视化、stream/checkpoint、LangSmith | Synapse 底层已经是 LangGraph；自研 scheduler 轻量，但不具备显式持久 DAG 状态和可视图化拓扑。 |
| Pydantic AI | delegation 作为 tool；也支持代码 hand-off、graph control flow 和 `SubAgents` | 可并发由应用编排；强调 usage/deps 传递、限制和 typed output | 依赖类型/输出类型在 API 层约束 | Logfire/OTel 有按 agent token、时延、工具 trace | Synapse 的文本结果与无单任务预算较弱；可借鉴类型化结果、usage limits 与并发背压。 |
| CrewAI | Crews 负责角色协作，Flows 负责事件驱动状态、分支、恢复；有 sequential/hierarchical/hybrid process | `kickoff_async()` 可并行，Flow 负责组合 | guardrails/HITL/企业控制面（具体取决部署） | Flow state、控制面 tracing | Synapse 比 Crew 更贴近 coding agent，较少业务流程原语；可借鉴“自主角色”和“确定性 Flow”分层。 |
| AutoGen / Microsoft Agent Framework | AutoGen 是多 agent 会话/运行时；官方已说明 AutoGen maintenance，MAF 为后继，增加显式 workflow/state | round-robin/group chat 或 workflow graph；可实现 join/聚合 | 由 runtime/tool host 控制 | 企业 telemetry、跨 runtime/A2A/MCP 方向 | 不建议将自由群聊引入 Synapse 默认路径；其显式 workflow、typed messaging、持久化值得参考。 |

### 7.3 差异评价

Synapse 的定位最接近“DeepAgents coding harness + 轻量显式 DAG 调度 + 本地 Textual 可观测性”。相比主流产品，优势是：

- 可在一个模型回合中把依赖声明和并行执行结合；
- 角色限制与现有本地工具、工具输出存储、AGENTS.md、会话命名空间自然结合；
- 对实时 UI 做了代码级的事件补偿，不只是等待最终摘要。

不足是：

- DAG 只存在于一次模型调用期间，不是可恢复、可版本化的工作流实体；
- 父/子共享同一 backend，写权限靠 middleware，不是隔离的 workspace 或 worktree；
- 没有 per-task budget/timeout/retry/failure policy；
- 子代理通信只有“上游完整文本注入”，没有结构化 artifact/state；
- 模式选择不透明：存在 `runtime/subagent_routing.py`，但当前全仓搜索只找到定义未找到调用，实际上开关由 `parallel_subagents` 决定。

## 8. 重构与优化路线

### P0：正确性、安全与资源治理

1. **建立 `TaskPlan` 和静态验证**
   - 新增 dataclass/Pydantic model：`id`、`role`、`description`、`depends_on`、`on_dependency_error`、`timeout_s`、`max_output_chars`、`result_schema_version`。
   - 在执行前验证：非空/唯一 ID、角色存在、无自依赖、依赖全集、无环、数量上限、描述上限。
   - 不要让重复 ID 静默覆盖 `results`、`task_wave` 和 cache。

2. **明确失败和取消语义**
   - 默认 `on_dependency_error="skip"`，下游显示“blocked by dependency”；只有显式 `continue_with_error` 才注入失败摘要。
   - `asyncio.gather(..., return_exceptions=True)`，把每任务异常映射为结构化状态；外层取消时取消所有未完成 child task，最终 `await gather(..., return_exceptions=True)`。
   - 使用 `asyncio.timeout()` 或可配置 timeout；明确 timeout 是 retry、fail 还是 skip。

3. **资源限额和背压**
   - `max_parallel_subagents` 应为 `Field(ge=1, le=<合理上限>)`；现在没有 Pydantic 下界，0/负值虽在 planner 中被兜底却不透明。
   - 增加全局 semaphore，覆盖同一进程多个父 turn；当前限制只在单次 DAG 内生效。
   - 为角色/任务增加请求数、工具调用、总输出字符/令牌预算；将消费显示在 monitor。

4. **强化隔离声明**
   - 将 `isolate_tools=True` 更名/文档化为 `tool_restriction`，避免误称 sandbox。
   - 默认禁止并行写任务；若将来允许 writer，要求 task 声明 write scope，并使用 git worktree 或 backend namespace。
   - 对 reviewer/tester 的 `execute` 增加命令政策或批准继承；当前“可执行 shell”不等于只读。

### P1：可维护性和产品契约

5. **收敛配置和模式选择**
   - 决定 `enable_subagents` 与 `parallel_subagents` 的关系：建议 `enable_subagents` 为总开关，`parallel_subagents` 为调度策略；总开关 false 必须禁用 task 工具。
   - 要么接入并测试 `decide_subagent_routing()`，要么删除它；当前未使用会误导维护者。
   - 在 `Settings` 补 `subagent_researcher_model`，或删除 `build_default_subagents()` 中不可由配置到达的参数。
   - 在 `README.md` 与 `docs/config.md` 补 `AGENT_PARALLEL_SUBAGENTS`、`AGENT_MAX_PARALLEL_SUBAGENTS`、生效条件、风险和 F9 / `/subagents` 使用说明。

6. **抽取调度层，降低 `parallel_subagents.py` 耦合**
   - `TaskPlanner`：解析、校验、拓扑批次；纯函数易测。
   - `TaskExecutor`：并发、timeout、cancel、retry、结果状态。
   - `SubagentRunner`：runnable 调用、prompt/context 组装、最终回答提取。
   - `TaskResultStore`：按 parent run ID 隔离的 cache；保证 finally 清理。
   - `MonitorPublisher`：monitor/LangSmith callback 的适配层。

7. **以结构化 artifact 替换纯文本依赖拼接**
   - 规范子代理返回 JSON/Pydantic：`summary`、`findings`、`evidence`、`changed_files`、`commands`、`status`、`risks`、`truncated`。
   - 给每个依赖只传受限 artifact（例如 `summary` + evidence references），而不是全部文本；保留 `tool-output://` 供按需追溯。
   - 对 description 和上游输入做长度/来源标记，降低上下文膨胀与提示注入传播。

### P2：可观测与长程能力

8. **把 DAG 计划做成可观察对象**
   - 增加 `dag_run_id`，任务 span/monitor/cache 都以此键隔离；显示 dependency level 与 capacity batch。
   - 向 TUI 提供计划视图（节点、边、状态、预算、失败原因）而非只显示列表；可以先用 Mermaid 文本，再考虑图组件。
   - 在 monitor dialog 支持单任务 cancel、重试、复制 artifact、打开 LangSmith trace；未配置 LangSmith 时保持本地可用。
   - `SubagentMonitor.close()` 中注销 registry；TUI shutdown 时调用，采用 `weakref` 或 session scoped registry。

9. **持久化与恢复（只在 P0/P1 稳定后）**
   - 将 plan、状态转换、artifact reference 写入 checkpoint/session store。
   - 恢复时只重跑 pending/running 中断任务；完成 artifact 不重复消耗 token。
   - 这会让 Synapse 接近 LangGraph workflow / CrewAI Flow 的可恢复性，但不要把“单轮 task tool”直接扩展成无限后台作业。

## 9. 建议的实施顺序与验收

| 阶段 | 变更 | 最小验收 |
|---|---|---|
| 1 | `TaskPlan` 校验、唯一 ID、依赖失败/取消语义 | 增加重复 ID、未知依赖、失败阻断、取消清理、最大并发测试；`uv run --no-sync pytest tests/test_dag_parallel.py -q`。 |
| 2 | timeout、全局 semaphore、每任务输出上限 | 模拟慢 runnable，证明并发上限、timeout 和最终 monitor 状态；测试无悬挂 task。 |
| 3 | 配置/路由收敛、文档 | settings + slash + docs 测试；`uv run --no-sync pytest tests/test_config.py tests/test_layered_config.py tests/test_slash_and_mcp.py -q`，再 `mkdocs build`。 |
| 4 | scheduler 拆分和结构化 artifact | planner/executor/runner 单测独立；端到端 fake model 验证 ToolMessage ID 对齐。 |
| 5 | monitor 生命周期、DAG 视图、单任务控制 | Textual 测试覆盖 auto-open、registry cleanup、任务选择和状态转移。 |
| 6 | 持久化恢复 | checkpoint 中断恢复集成测试，确保已经完成的任务不重复执行。 |

质量门槛：每阶段先跑最窄测试，后跑 `uv run --no-sync ruff check .`；涉及文档时跑 `uv run --no-sync mkdocs build`。涉及异步取消与图协议的变更建议在 CI 的 Windows/Linux 环境都执行相关测试。

## 10. 外部资料

以下均为官方文档或官方仓库，访问日期 2026-06-07：

1. LangChain Deep Agents, [Subagents](https://docs.langchain.com/oss/python/deepagents/subagents)；[SubAgentMiddleware API](https://reference.langchain.com/python/deepagents/middleware/subagents/SubAgentMiddleware)；[Dynamic subagents](https://docs.langchain.com/oss/python/deepagents/dynamic-subagents)。
2. LangChain, [Subagent architecture](https://docs.langchain.com/oss/python/langchain/multi-agent/subagents)；LangGraph, [Workflows and agents](https://docs.langchain.com/oss/python/langgraph/workflows-agents)。
3. Anthropic, [Claude Code custom subagents](https://code.claude.com/docs/en/sub-agents)；[Claude Agent SDK subagents](https://platform.claude.com/docs/en/agent-sdk/subagents)。
4. OpenAI, [Codex Subagents](https://developers.openai.com/codex/subagents)；[Responses API multi-agent](https://developers.openai.com/api/docs/guides/responses-multi-agent)。
5. Cursor, [Subagents](https://cursor.com/docs/subagents)；[Cursor 2.4 changelog](https://cursor.com/changelog/2-4)。
6. Google ADK, [Multi-agent systems](https://adk.dev/agents/multi-agents/)；[Workflows](https://adk.dev/workflows/)；[multi-agent patterns](https://developers.googleblog.com/developers-guide-to-multi-agent-patterns-in-adk/)。
7. Pydantic AI, [Multi-agent patterns](https://pydantic.dev/docs/ai/guides/multi-agent-applications)；[SubAgents](https://pydantic.dev/docs/ai/harness/subagents)。
8. CrewAI, [官方文档](https://docs.crewai.com/)；[官方仓库](https://github.com/crewAIInc/crewAI)。
9. Microsoft, [AutoGen repository and migration notice](https://github.com/microsoft/autogen)；[Microsoft Agent Framework overview](https://learn.microsoft.com/en-us/agent-framework/overview/)；[AutoGen migration guide](https://learn.microsoft.com/en-us/agent-framework/migration-guide/from-autogen/)。
