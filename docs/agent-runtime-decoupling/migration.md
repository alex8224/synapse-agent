# 迁移说明与余留风险清单

> 状态：Completed
> 用途：P8-09 收口文档。记录从“单 TUI、单 Agent、单回合”到“headless runtime + 多项目/多会话”的迁移要点、兼容边界与已知风险。

## 1. 目标架构（最终形态）

```text
CLI / TUI / ACP consumer
  -> AgentRuntimeService application DTO ports
      -> RuntimeManager.submit_ref/resume_ref
          -> SessionRuntime (thread_id 域：task/cancel/steer/usage/goal/broker)
              -> AgentTurnRuntime (headless，无 Textual 依赖)
                  -> runtime streaming core -> AgentEventSink
TUI facade -> RuntimeEvent renderer -> TranscriptController / DOM
RuntimeManager (project_id 域，可 SessionRef 路由)
ProjectRuntime (project 域：Settings/checkpointer/GoalService/MCP scope)
ProjectRegistry / ProjectCatalog (全局发现与投影)
```

- 单进程、双 loop（Textual 主线程 + Agent `AsyncRuntime` loop）。
- TUI 是可随时 attach/detach 的观察者；Agent turn 在无订阅者时仍完整运行、取消、持久化并产出标准事件。
- 项目是资源隔离域，会话是执行/取消域。

## 2. 迁移要点

### 2.1 新代码应导入的路径

| 概念 | 推荐导入 | 兼容旧路径 |
|---|---|---|
| Turn runtime | `synapse.runtime.agent_loop` | `synapse.ui.stream`（保留 re-export） |
| 事件契约 | `synapse.runtime.streaming` | `synapse.ui.stream_events` |
| 会话运行时 | `synapse.runtime.sessions` | - |
| 项目运行时 | `synapse.runtime.projects` | - |
| Settings | `synapse.settings` | `synapse.config`（兼容导出） |

### 2.2 行为变化

- 会话切换不再取消旧会话的 Agent task；旧会话在后台继续运行并持久化事件。
- 切换会话时 TUI 只改变 attached thread；Esc 只取消当前 attached session。
- 项目 `.env` 不再通过 `load_dotenv(override=True)` 修改进程环境；并发项目使用私有 env mapping。
- GoalService 与 MCP pool 从进程单例改为项目实例注入（未注入时回退进程单例，行为不变）。
- `catalog.sync_project()` 现在会清理源库中已消失的 session 投影。

### 2.3 需要显式迁移的调用方

- 依赖 `get_goal_service()` / `_ACTIVE_MCP_POOL` 单例语义的代码：在新路径中通过 `ProjectRuntime.goal_service` / MCP registry 注入。
- 依赖 `bootstrap_project_env()` 修改 `os.environ` 的扩展：改为 `load_project_settings()` / `project_env_mapping()`。
- TUI 之外调用 `run_turn` 的 harness：改走 `SessionRuntime.submit_threadsafe` / `RuntimeManager.submit`。

## 3. 余留风险清单

S10 CLI、TUI、ACP consumer implementation 与 final gates 均已完成；总体 S0-S10 状态为 `completed`。

已核实且值得修复的运行时、线程归属和架构依赖问题，统一跟踪于
[审查问题修复计划](post-review-fix-plan.md)。

| ID | 风险 | 影响 | 缓解/后续 |
|---|---|---|---|
| R1 | 每会话独立 Agent graph 的内存增量 | 多会话并发时峰值内存上升 | P8 已实现 idle SessionRuntime/ProjectRuntime LRU 回收（`collect_idle`）；真实 provider 基线仍待集成性能阶段 |
| R2 | MCP 项目级 registry 的 config digest 语义 | reload 竞争时旧连接可能短暂存活 | `McpPoolRegistry.release/close_all` 显式释放；atexit 关闭全部 pool |
| R3 | legacy `ui.stream.stream_agent` 仍作为兼容 utility 存在 | 兼容实现与新 consumer 路径并存，可能造成误用 | CLI/TUI/ACP 默认路径均不调用；保留 re-export 以维持兼容 |
| R4 | 进程退出时的超时关闭路径只记录、不等待 | 极端情况下个别 SessionRef 未在超时内关闭 | 已记录未关闭 SessionRef；不会无限挂起 |
| R5 | `load_global_settings()` 不创建 cwd `.synapse` | 全局 landing 依赖 catalog 已存在 | catalog 路径在用户层，首次使用前需注册项目 |
| R6 | Python allocator 高水位不下降 | LRU 回收后 RSS 可能不回落 | 已区分“仍被引用泄漏”与“allocator 高水位”；后续可加 `gc.collect` + 内存基线测试 |
| R7 | catalog 投影与项目本地库可能短暂不一致 | 全局列表 freshness 有滞后 | 选择会话时回源验证（`resolve_session_ref(verify=True)`） |

## 4. 平台与兼容性

- CI 覆盖 Windows/Linux、Python 3.12/3.13。
- 原生压缩核心（`synapse-tool-compress-core`）是可选依赖，Python 主程序保留 `ImportError`/`OSError` fallback。
- S8 已引入 foreground daemon；本计划记录的消费者迁移不改变 daemon 的进程边界、认证或生命周期语义。

## 5. 完成定义（对照）

- [x] Agent turn 在无 TUI 时完整运行、取消、持久化并产出标准事件。
- [x] TUI 切换会话不销毁后台任务；多会话可并行运行。
- [x] 多项目资源（Settings/.env/Goal/MCP/数据库）隔离。
- [x] 任意目录启动进入全局控制面，按 `SessionRef` 操作跨项目会话。
- [x] idle runtime LRU 回收、有序关闭、故障恢复测试、全量 lint/test/build 通过。
- [x] CLI/TUI/ACP consumer 通过 application DTO ports 接入，且 watch detach/connection close 不取消 turn。
- [x] S10 最终全仓安全门禁、MkDocs strict、`uv build` 与 review 完成。

## 6. 最终验证与本地残留说明

- TUI C1/C2 最终明确清单：443 passed。
- 安全全仓最终结果：2348 passed、2 skipped。明确排除 13 个文件：`test_acp_p0_baseline.py`、`test_acp_p1_transport.py`、`test_agent_turn_runtime.py`、`test_backends.py`、`test_git_chrome.py`、`test_herdr_integration.py`、`test_runtime_service_routing_s5.py`、`test_runtime_transport_s7.py`、`test_startup_trace.py`、`test_transcript_migration.py`、`test_runtime_daemon_s8.py`、`test_runtime_transport_client_methods_s9.py`、`test_runtime_transport_websocket_s7.py`。原因分为 process API 安全约束及 socket 环境不稳定；不声称排除项通过。
- lifecycle/consumer 核心 194 passed；permit2 5、crossloop 5、generation 4；真实 approval 9；CLI registry 3；project exact/generation tests 通过。Ruff、`git diff --check`、MkDocs strict 与 `uv build` 通过，包版本为 `0.1.43`。
- 三轮 review 最终无阻塞发现，workspace freeze Medium 已修复。唯一执行链为 `AgentRuntimeService -> RuntimeManager.submit_ref/resume_ref -> SessionRuntime -> AgentTurnRuntime`；consumer 不变量为 DTO ports 不暴露执行对象、detach/close 不 cancel turn、同 loop owner close + cancel fence，以及 UI-only queue 不拥有 execution runtime。默认 CLI/TUI/ACP 不使用 `stream_agent` 或 `agent.ainvoke`。
- residual/local artifacts note：未跟踪 `.sessions.sqlite` 和 `transcript.sqlite` 未删除，属于本地残留，不可提交。
