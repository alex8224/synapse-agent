# Agent Runtime 解耦实施进度

> 本文件是实施状态的单一真源。阶段文档定义范围和方案，不维护实时状态。  
> 状态值：`not_started`、`in_progress`、`blocked`、`completed`。  
> 当前总体状态：`completed`。

## 当前工作

- 当前阶段：已完成（P0-P8）及 P7 UI 收尾（项目侧栏 / 全局启动参数）。
- 当前任务：无。
- 下一门禁：无；后续改动按增量变更维护。
- 当前阻塞：无。

## 阶段总览

| 阶段 | 状态 | 完成度 | 门禁结果 | 方案 |
|---|---|---:|---|---|
| P0 基线与护栏 | completed | 7/7 | 通过 | [phase-0-baseline.md](phase-0-baseline.md) |
| P1 Streaming core 与事件契约 | completed | 9/9 | 通过 | [phase-1-streaming-core.md](phase-1-streaming-core.md) |
| P2 AgentTurnRuntime | completed | 8/8 | 通过 | [phase-2-turn-runtime.md](phase-2-turn-runtime.md) |
| P3 TUI 事件适配与切换 | completed | 9/9 | 通过 | [phase-3-tui-cutover.md](phase-3-tui-cutover.md) |
| P4 SessionRuntime 与事件 Broker | completed | 10/10 | 通过 | [phase-4-session-runtime.md](phase-4-session-runtime.md) |
| P5 同项目多会话 | completed | 9/9 | 通过 | [phase-5-multi-session.md](phase-5-multi-session.md) |
| P6 ProjectRuntime | completed | 11/11 | 通过 | [phase-6-project-runtime.md](phase-6-project-runtime.md) |
| P7 全局控制面 | completed | 10/10 | 通过 | [phase-7-global-control-plane.md](phase-7-global-control-plane.md) |
| P8 稳定性与性能收口 | completed | 9/9 | 通过 | [phase-8-hardening.md](phase-8-hardening.md) |

## P0 任务

- [x] P0-01 建立当前 turn/stream/TUI 行为矩阵。
- [x] P0-02 增加代表性 fake-agent 原始流 fixtures。
- [x] P0-03 建立 answer/reasoning/tool/usage 的有序 trace 回归测试。
- [x] P0-04 补齐 cancel/retry/HITL/compact/subagent 边界测试。
- [x] P0-05 建立 runtime 禁止依赖 Textual 的导入护栏测试。
- [x] P0-06 记录可重复的导入、首事件、长输出、取消和内存基线；真实 provider/TUI mounted 指标延后到集成性能阶段。
- [x] P0-07 执行当前 P0 门禁并固化基线报告；完整 fixtures 和性能项仍单独开放。

## P1 任务

- [x] P1-01 定义 `TurnEvent`、payload 和版本化契约。
- [x] P1-02 将 ToolItem 等运行领域数据移出 UI 命名空间并保留 re-export。
- [x] P1-03 实现 runtime-owned `TurnAccumulator`。
- [x] P1-04 实现 `AgentEventSink`、collecting/null/composite sink。
- [x] P1-05 实现 legacy `StreamSink` 与新事件之间的兼容 adapter。
- [x] P1-06 将流归一化和 runner 核心迁入 `synapse.runtime`。
- [x] P1-07 将 retry notifier 改为并发安全的上下文作用域。
- [x] P1-08 保留旧导入路径并迁移内部调用方。
- [x] P1-09 通过事件契约、兼容和流回归门禁。

## P2 任务

- [x] P2-01 定义 `TurnContext`、`TurnStatus`、`TurnResult` 和 `TurnHandle`。
- [x] P2-02 实现无 Textual 依赖的 `AgentTurnRuntime.arun()`。
- [x] P2-03 提供受约束的同步兼容入口并处理 sync checkpointer fallback。
- [x] P2-04 将请求构造从 `ui/turn/request.py` 迁入 runtime。
- [x] P2-05 将取消、checkpoint repair、错误和 HITL 终态纳入状态机。
- [x] P2-06 确保所有终态只发一次且结果不依赖订阅者。
- [x] P2-07 添加完整 headless runtime 测试。
- [x] P2-08 通过 P2 API、并发安全和资源清理门禁。

## P3 任务

- [x] P3-01 定义 `TextualTurnEventRenderer` 的 host 协议。
- [x] P3-02 将标准事件映射到现有 transcript 控制器。
- [x] P3-03 实现 delta 合并、UI 唤醒合并和有界刷新频率。
- [x] P3-04 将 generation 过滤放到 renderer/subscription 边界。
- [x] P3-05 让 `TurnController` 仅调度 runtime 并消费结果。
- [x] P3-06 保持当前 goal/summary/catalog 收尾行为不变。
- [x] P3-07 切换 TUI 调用路径并保留兼容回滚路径。
- [x] P3-08 完成 Textual pilot、顺序和性能回归测试。
- [x] P3-09 通过 Agent loop/TUI 解耦里程碑验收。

## P4 任务

- [x] P4-01 定义 `SessionRuntime` 状态机和命令接口。
- [x] P4-02 实现有界 `SessionEventBroker` 与 session-local sequence。
- [x] P4-03 实现 snapshot + subscribe(after_sequence) 无缝接续。
- [x] P4-04 将 Agent turn busy/task/cancel/steer 真状态移入 SessionRuntime；UI `_busy` 保留兼容投影。
- [x] P4-05 将 usage 和 turn accumulator 变为会话状态。
- [x] P4-06 提供冻结上下文驱动的持久化接口，projection failure 不改变turn终态。
- [x] P4-07 将 goal 结算和自动续跑移入 SessionRuntime。
- [x] P4-08 实现无订阅者运行及重新 attach。
- [x] P4-09 实现 event buffer 背压、丢弃和终态保留策略。
- [x] P4-10 通过 detach/reattach、取消和持久化门禁。

## P5 任务

- [x] P5-01 实现当前项目的 `RuntimeManager[thread_id, SessionRuntime]`。
- [x] P5-02 为每个运行会话创建独立 Agent graph/SteerQueue。
- [x] P5-03 显式共享 model client、checkpointer 和项目级昂贵资源。
- [x] P5-04 移除 Textual `exclusive=True` 对 Agent task 的所有权。
- [x] P5-05 实现 per-session run lock 和全局并发限制。
- [x] P5-06 实现会话 attach/detach、后台状态和按会话 cancel/steer。
- [x] P5-07 让会话列表展示 running/waiting/failed/idle 状态。
- [x] P5-08 增加并发串线、切换和 goal 自动续跑测试。
- [x] P5-09 通过同项目多会话门禁。

## P6 任务

- [x] P6-01 定义 `SessionRef(project_id, thread_id)` 和严格 resolver。
- [x] P6-02 定义惰性 `ProjectRuntime` 与项目资源生命周期。
- [x] P6-03 修正稳定 project identity 和目录移动语义。
- [x] P6-04 将项目 `.env` 改为私有 mapping，停止并发路径修改全局环境。
- [x] P6-05 将 GoalService 从进程单例改为项目实例注入。
- [x] P6-06 将 MCP active pool 改为项目/config digest registry。
- [x] P6-07 隔离 backend、checkpointer、session tools、tool output 和 transcript。
- [x] P6-08 让 RuntimeManager 按 `SessionRef` 路由命令和事件。
- [x] P6-09 实现 idle ProjectRuntime 的安全资源回收。
- [x] P6-10 增加双 workspace 配置、工具和数据库隔离测试。
- [x] P6-11 通过跨项目资源隔离门禁。

## P7 任务

- [x] P7-01 增加只加载用户层配置的 global bootstrap。
- [x] P7-02 任意目录启动时避免自动注册和污染 cwd。
- [x] P7-03 实现全局 landing page 和项目列表。
- [x] P7-04 实现按项目分组的全局会话列表与搜索。
- [x] P7-05 选择会话时回源验证 workspace、session 和索引新鲜度。
- [x] P7-06 实现跨项目 open/submit/steer/cancel/rename/delete 等操作边界。
- [x] P7-07 显示 missing/inaccessible/stale/running 等状态。
- [x] P7-08 完善 catalog 对账、歧义解析和投影清理。
- [x] P7-09 更新 CLI、TUI、README 和配置文档。
- [x] P7-10 通过任意位置启动和端到端全局操作门禁。

## P8 任务

- [x] P8-01 建立多会话/多项目长时运行和压力测试。
- [x] P8-02 根据 P0 基线确定并验证内存、事件积压和 UI 延迟预算。
- [x] P8-03 实现 idle SessionRuntime/ProjectRuntime 的 LRU 回收。
- [x] P8-04 验证 running runtime 永不被错误回收。
- [x] P8-05 完成程序退出时 task、MCP、HTTP、SQLite 和 loop 的有序关闭。
- [x] P8-06 增加异常、取消竞争、数据库锁和 provider failure 恢复测试。
- [x] P8-07 完成可观测性、诊断视图和泄漏检查。
- [x] P8-08 运行 Ruff、全量 pytest、MkDocs build 和平台分支检查。
- [x] P8-09 完成最终架构文档、迁移说明和余留风险清单。

## 阻塞记录

| ID | 阶段/任务 | 证据 | 影响 | 解除条件 | 状态 |
|---|---|---|---|---|---|
| - | - | - | - | - | - |

## 验证记录

| 时间 | 阶段/任务 | 命令 | 结果 | 备注 |
|---|---|---|---|---|
| 2026-08-07 | P0/P1 针对性回归 | `pytest test_runtime_streaming + stream/cancel/tool/textual/turn` | 62 passed | 2.80s |
| 2026-08-07 | P0/P1 最终针对性门禁 | `pytest runtime/fixtures/stream/cancel/tool/textual/turn/retry` | 91 passed | 2.49s |
| 2026-08-07 | P0/P1 最终全量门禁 | `pytest -q` | 977 passed | 113.15s |
| 2026-08-07 | P2 针对性门禁 | `ruff check .` + P2/P1/P0 相关 pytest | 104 passed | 3.16s |
| 2026-08-07 | P2 全量门禁 | `pytest -q` | 987 passed | 99.82s |
| 2026-08-07 | P2 文档门禁 | `mkdocs build` | passed | 8.91s；仅仓库既有警告 |
| 2026-08-07 | P3 解耦针对性门禁 | `ruff check .` + renderer/bridge/TUI/stream pytest | 94 passed | 2.33s |
| 2026-08-07 | P3 全量门禁 | `pytest -q` | 993 passed | 103.11s |
| 2026-08-07 | P3 文档门禁 | `mkdocs build` | passed | 9.14s；仅仓库既有警告 |
| 2026-08-07 | P4 detach/状态所有权门禁 | `ruff check .` + session/runtime/TUI pytest | 102 passed | 2.31s |
| 2026-08-07 | P4 全量门禁 | `pytest -q` | 1001 passed | 102.86s |
| 2026-08-07 | retry 并发兼容 | `pytest tests/test_model_retry.py -q` | 18 passed | 0.90s |
| 2026-08-07 | 全仓 lint | `ruff check .` | passed | 无违规 |
| 2026-08-07 | 全量测试 | `pytest -q` | 966 passed | 103.71s |
| 2026-08-07 | runtime 事件性能 | 10k/50k answer delta 累积 | 0.0175s / 0.0955s | 无 UI、无网络 |
| 2026-08-07 | 文档构建 | `mkdocs build` | passed | 9.08s；仅仓库既有警告 |
| 2026-08-07 | P5 针对性门禁 | `pytest turn_controller/runtime_manager/dialogs/session_runtime/goals` | 140 passed | 37.62s |
| 2026-08-07 | P5 全量门禁 | `pytest -q` | 1016 passed | 104.36s |
| 2026-08-07 | P5 新增能力 | agent factory 资源共享、切换保持后台运行、会话列表 runtime 状态 | 3 个新测试 | 全量 1016 passed |
| 2026-08-07 | P6 针对性门禁 | `pytest project_runtime/config/layered_config/goals/runtime_manager/session_runtime/turn_controller` | 102 passed | 5.83s |
| 2026-08-07 | P6 全量门禁 | `pytest -q` | 1035 passed | 102.78s |
| 2026-08-07 | P6 新增能力 | SessionRef/resolver、ProjectRuntime 惰性生命周期、project.json 身份、私有 env mapping、goal/MCP 注入 | 21 个新测试 | 全量 1035 passed |
| 2026-08-07 | P7 针对性门禁 | `pytest project_runtime/cli` | 36 passed | 3.28s |
| 2026-08-07 | P7 全量门禁 | `pytest -q` | 1041 passed | 106.69s |
| 2026-08-07 | P7 新增能力 | load_global_settings、启动解析修正、catalog 对账/resolve/remove、SessionRef 全局路由 | 4 个新测试 | 全量 1041 passed |
| 2026-08-07 | P8 针对性门禁 | `pytest test_runtime_hardening.py` | 9 passed | 0.51s |
| 2026-08-07 | P8 全量门禁 | `pytest -q` | 通过 | 见下方最终验证 |
| 2026-08-07 | 最终全量验证 | `ruff check .` + `pytest -q` + `mkdocs build` | 全部通过 | 最终收口 |
| 2026-08-07 | P7 UI 收尾 | 项目侧栏（topbar ≡ 打开）、`--session/--project` 启动参数、跨项目切换重启 | 新增 drawer + 解析测试 | 全量 1056 passed |
| 2026-08-07 | P7 UI 修复 | drawer 渲染方法名冲突（`_render`→`_paint`）、CLI workspace 缺失/判定修复 | 渲染冒烟测试 + 回归测试 | 全量 1058 passed |
| 2026-08-07 | 多会话并发修复 | SessionRuntime 状态回调、ProjectDrawer 实时展示运行 session、session 独立 graph/steer queue、worker 冻结 session 身份、transcript reset 后再 attach renderer、后台结果冻结持久化、退出关闭全部 runtime | 相关 session/TUI/stream/dialog/goal/transcript 测试 283 passed；mounted Textual 生命周期、Ruff 与 MkDocs 通过 | 全量 1114 passed、2 skipped；仅 2 个既有可选原生 tool-output transformer 断言失败 |
| 2026-08-08 | P7 跨项目进程内切换 | drawer 跨项目切换不再退出重启：新增 `CodingAgentApp._switch_project`（catalog 查 workspace → `load_project_settings` 快照 → 进程内替换 settings/thread_id/project_root/stores/transcript projection → transcript reset 后 attach）；per-project settings/store/transcript projection/goal service 隔离（`TurnController.settings_for/projection_for/store_for/goal_service_for`，goal 复用全局单例仅当路径匹配）；SessionRuntime 增加 `project_id`；`_persist_runtime_result` 改为冻结项目资源闭包；修复 `_current_project_id` 的 catalog/project.json id 分叉误判（同项目 session 误走重启）；切换窗口期 busy 回退 `_sessions` 查询；`waiting_approval` 纳入后台计数/drawer/前台状态；切回有活跃 runtime 的项目复用冻结 agent | 新增 `test_project_switch.py`（进程内切换、原项目 runtime 保留、缺失项目容错）；更新 2 个旧 exit 断言测试 | 全量 1116 passed、2 skipped；仅 2 个既有可选原生 tool-output transformer 断言失败 |

## 设计变更记录

| 时间 | ADR/文档 | 变更 | 原因 | 影响阶段 |
|---|---|---|---|---|
| 2026-08-07 | 初始方案 | 建立总体方案、P0-P8 和进度台账 | 大型重构需可持续跟踪 | 全部 |
| 2026-08-07 | P1 实现 | 采用 runtime accumulator + legacy renderer adapter 渐进迁移 | 保留成熟 stream 状态机行为并先切断结果对 UI buffer 的依赖 | P1-P3 |
| 2026-08-07 | P1 实现 | stream runner/normalizer 迁入 runtime，UI 路径保留 wrapper | 建立单向 runtime -> UI 依赖边界 | P1-P2 |

## 台账更新流程

1. 开始工作前：更新“当前工作”，将一个任务置为 `in_progress`；同一执行者不要并行占用多个依赖任务。
2. 代码修改中：发现设计偏差时先更新 ADR 和阶段文档，再继续实现。
3. 验证后：将命令和结果追加到“验证记录”，然后勾选任务。
4. 阶段完成：核对阶段文档全部验收项，更新总览完成度和门禁结果。
5. 阻塞：填写证据和解除条件，将任务/阶段标为 `blocked`；禁止仅因任务困难而标阻塞。
6. 恢复：保留原阻塞记录，追加解除说明，不删除历史。
