# 迁移说明与余留风险清单

> 状态：Accepted  
> 用途：P8-09 收口文档。记录从“单 TUI、单 Agent、单回合”到“headless runtime + 多项目/多会话”的迁移要点、兼容边界与已知风险。

## 1. 目标架构（最终形态）

```text
CodingAgentApp (TUI)
  -> TurnController
      -> SessionRuntime (thread_id 域：task/cancel/steer/usage/goal/broker)
          -> AgentTurnRuntime (headless，无 Textual 依赖)
              -> runtime streaming core -> AgentEventSink
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

| ID | 风险 | 影响 | 缓解/后续 |
|---|---|---|---|
| R1 | 每会话独立 Agent graph 的内存增量 | 多会话并发时峰值内存上升 | P8 已实现 idle SessionRuntime/ProjectRuntime LRU 回收（`collect_idle`）；真实 provider 基线仍待集成性能阶段 |
| R2 | MCP 项目级 registry 的 config digest 语义 | reload 竞争时旧连接可能短暂存活 | `McpPoolRegistry.release/close_all` 显式释放；atexit 关闭全部 pool |
| R3 | `stream_agent()` 成熟语义解析主循环仍在 `synapse.ui.stream` | 与 runtime 存在双实现窗口 | 已通过 runtime adapter + 兼容路径；后续渐进迁移，避免一次性重写 |
| R4 | 进程退出时的超时关闭路径只记录、不等待 | 极端情况下个别 SessionRef 未在超时内关闭 | 已记录未关闭 SessionRef；不会无限挂起 |
| R5 | `load_global_settings()` 不创建 cwd `.synapse` | 全局 landing 依赖 catalog 已存在 | catalog 路径在用户层，首次使用前需注册项目 |
| R6 | Python allocator 高水位不下降 | LRU 回收后 RSS 可能不回落 | 已区分“仍被引用泄漏”与“allocator 高水位”；后续可加 `gc.collect` + 内存基线测试 |
| R7 | catalog 投影与项目本地库可能短暂不一致 | 全局列表 freshness 有滞后 | 选择会话时回源验证（`resolve_session_ref(verify=True)`） |

## 4. 平台与兼容性

- CI 覆盖 Windows/Linux、Python 3.12/3.13。
- 原生压缩核心（`synapse-tool-compress-core`）是可选依赖，Python 主程序保留 `ImportError`/`OSError` fallback。
- 未引入 daemon：跨进程退出持续运行不在本计划范围内（ADR-010）。

## 5. 完成定义（对照）

- [x] Agent turn 在无 TUI 时完整运行、取消、持久化并产出标准事件。
- [x] TUI 切换会话不销毁后台任务；多会话可并行运行。
- [x] 多项目资源（Settings/.env/Goal/MCP/数据库）隔离。
- [x] 任意目录启动进入全局控制面，按 `SessionRef` 操作跨项目会话。
- [x] idle runtime LRU 回收、有序关闭、故障恢复测试、全量 lint/test/build 通过。
