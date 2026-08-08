# Agent Runtime 解耦审查问题修复计划

> 状态：Implemented（F1–F6 全部落地，含针对性回归测试；2026-08-08）
> 范围：修复解耦重构审查中已核实、具有实际正确性或可诊断性价值的问题。  
> 原则：先修高风险生命周期问题，再收敛线程归属，最后完成架构依赖迁移。

## 1. 目标

本计划处理以下六项已确认问题：

| ID | 优先级 | 问题 | 主要影响 |
|---|---|---|---|
| F1 | P0 | 正常完成路径以 1.5 秒等待 `bound_future`，超时会被当作 turn failure | 正常请求可能被误报失败 |
| F2 | P0 | `TextualTurnEventRenderer` 渲染异常后静默关闭 | 后续工具和回答消失，且缺乏诊断 |
| F3 | P1 | `RuntimeManager.submit` 在 acquire 后、`try` 前存在 lock 泄漏窗口 | 单 session 后续提交可能永久阻塞 |
| F4 | P1 | tool-output refresh 的 pending/dirty 状态跨线程读写 | 刷新合并存在竞争和状态不一致风险 |
| F5 | P1 | MCP 后台 worker 直接写 `_mcp_reloading` | UI 状态线程归属不一致 |
| F6 | P2 | `AgentTurnRuntime` 默认动态导入 `synapse.ui.stream` | runtime/UI 解耦不彻底，导入护栏存在盲区 |

## 2. 非目标

- 不修改模型是否并行发出 tool calls 的策略。
- 不把跨模型响应的连续工具调用强行合并为同一 UI 工具组。
- 不重写 `afe9ac2` 等已存在提交历史。
- 不在本计划内重命名或拆分 `TurnPersistenceController`；该项是低风险职责清理，可单独重构。
- 不引入 daemon、多进程或新的 RPC 边界。

## 3. 实施阶段

### 阶段 A：turn 正确性与可诊断性

#### F1：修复正常完成超时误判

涉及文件：

- `src/synapse/runtime/streaming/runtime.py`
- `tests/test_runtime_streaming.py`
- 必要时增加专用 async cleanup fixture

实施要求：

1. 区分“事件生产完成”和“graph/checkpointer 清理完成”。
2. 正常路径不得因固定 1.5 秒清理等待而产生 `TimeoutError` turn failure。
3. 取消路径仍必须等待 graph 退出，不能提前报告 cancelled 后留下后台写入。
4. 不允许无界卡死；如必须设置上限，应形成独立 cleanup timeout 状态或诊断，而不是伪装成 provider failure。
5. worker thread 与 bound loop 两条路径保持一致语义。

测试：

- graph 已发完事件但清理超过 1.5 秒，turn 仍正常完成。
- graph 主体真实抛错时仍传播原始异常。
- cancel 时等待 cleanup 完成后才结束。
- cleanup 不留下活跃 task/thread。

#### F2：Renderer 异常诊断

涉及文件：

- `src/synapse/ui/turn/event_renderer.py`
- `tests/test_turn_event_renderer.py`
- 复用项目现有 logging/observability 设施

实施要求：

1. 捕获渲染异常时记录 bounded warning。
2. 日志至少包含 `thread_id`、`turn_id`、event kind、sequence 和异常类型。
3. 不记录完整 payload、用户正文、工具输出或凭据。
4. renderer 仍可关闭，且异常不得反向导致 Agent turn 失败。
5. 如项目已有 debug capture，按既有模式接入，不新增重复日志系统。

测试：

- host 在工具事件渲染时抛错，renderer 关闭但 Agent/event producer 不受影响。
- warning 包含定位字段，不包含 payload 正文。
- stale generation 和重复 sequence 仍保持静默忽略，不误报异常。

阶段 A 门禁：

```powershell
uv run --no-sync pytest tests/test_runtime_streaming.py tests/test_turn_event_renderer.py tests/test_agent_turn_runtime.py -q
uv run --no-sync ruff check src/synapse/runtime/streaming src/synapse/ui/turn tests/test_runtime_streaming.py tests/test_turn_event_renderer.py tests/test_agent_turn_runtime.py
```

### 阶段 B：并发状态生命周期

#### F3：闭合 `RuntimeManager.submit` 资源生命周期

涉及文件：

- `src/synapse/runtime/sessions/manager.py`
- `tests/test_runtime_manager.py`

实施要求：

1. `submit_lock.acquire()` 成功后立即进入受保护的 `try/finally`。
2. `_get_semaphore()`、`mark_queued()`、`semaphore.acquire()`、`mark_starting()`、`session.submit()` 任意失败均正确回滚。
3. submit lock、semaphore permit、queued/starting 状态各自最多释放或清理一次。
4. 不改变同一 session 单 turn、跨 session 有界并发的现有语义。

测试：

- `mark_queued()` 抛错后可再次提交。
- semaphore 获取期间取消不会泄漏 lock/permit。
- `mark_starting()` 和 `session.submit()` 抛错后状态恢复。
- 正常 settlement 后资源只释放一次。

#### F4：统一 tool-output refresh 的线程归属

涉及文件：

- `src/synapse/ui/chrome/controller.py`
- `src/synapse/ui/tui.py`（仅必要状态初始化）
- 对应 chrome/tool-output 测试

首选方案：

- pending/dirty 的读写全部封送到 Textual UI thread；worker 只计算 SQLite snapshot，通过 `call_from_thread` 发布结果。

备选方案：

- 若 callback 无法可靠封送，则使用小范围 lock 保护 pending/dirty，并避免持锁调用 UI 或 SQLite。

实施要求：

1. 消除跨线程 check-then-set 竞争。
2. 切 session 时旧 thread 结果不得覆盖新 thread chrome。
3. dirty 期间至少触发一次后续刷新，不要求一事件一刷新。
4. 保持 debounce 和 bounded refresh 行为。

测试：

- refresh pending 时多个 metrics changed 合并为一次后续刷新。
- 切 session 后旧结果被丢弃并刷新当前 session。
- worker 完成和新 dirty 信号交错时不丢最终刷新。

#### F5：MCP reload 状态回到 UI thread

涉及文件：

- `src/synapse/ui/dialogs/controller.py`
- `tests/test_dialogs.py` 或对应 MCP dialog/controller 测试

实施要求：

1. `_mcp_reloading` 的 UI 可见状态只在 UI thread 写入。
2. success、slash error、exception 三条路径统一通过一个 finish helper 收尾。
3. finish helper 同时恢复 activity，并确保只执行一次。
4. 不扩大锁范围，不在 UI thread 执行 MCP reload 或配置 I/O。

测试：

- toggle/save/reload 的成功与失败均清理 reloading 状态。
- worker 不直接修改 UI 状态。
- 重复点击在 reload 期间仍被抑制。

阶段 B 门禁：

```powershell
uv run --no-sync pytest tests/test_runtime_manager.py tests/test_dialogs.py -q
uv run --no-sync ruff check src/synapse/runtime/sessions/manager.py src/synapse/ui/chrome/controller.py src/synapse/ui/dialogs/controller.py tests/test_runtime_manager.py tests/test_dialogs.py
```

### 阶段 C：完成 runtime/UI parser 依赖迁移

#### F6：移除 runtime 到 `synapse.ui.stream` 的默认依赖

涉及文件预期：

- 将 `stream_agent` 的语义解析主循环迁至 `src/synapse/runtime/streaming/` 下的明确模块
- `src/synapse/ui/stream.py` 保留兼容 wrapper/re-export
- `src/synapse/runtime/agent_loop/turn.py`
- streaming、CLI、TUI 和 import-boundary 测试

实施步骤：

1. 先移动实现，不重写解析算法，保持 diff 可审查。
2. runtime 模块只依赖 runtime/domain 类型，不导入 Textual 或 `synapse.ui`。
3. Rich/Textual sink 默认选择留在 CLI/TUI 装配层；headless runtime 使用 no-op renderer + semantic event sink。
4. `AgentTurnRuntime` 从 runtime 路径直接导入 parser，或在构造时强制注入；删除动态字符串导入 UI 的 fallback。
5. `synapse.ui.stream.stream_agent` 保留兼容导出，避免破坏现有扩展和测试。
6. 增加整个 `src/synapse/runtime/` 的 AST/import 护栏，而非只检查 `runtime/streaming/`。

测试：

- runtime 包导入时 `sys.modules` 不出现 `textual` 和 `synapse.ui`。
- headless turn 的 answer/reasoning/tool/usage/cancel/HITL 语义 trace 不变。
- CLI Rich sink 与 TUI event renderer 行为不变。
- 旧导入路径仍可用。

阶段 C 门禁：

```powershell
uv run --no-sync pytest tests/test_runtime_streaming.py tests/test_stream_semantic_fixtures.py tests/test_agent_turn_runtime.py tests/test_stream_tool_items.py tests/test_turn_event_renderer.py tests/test_stream_cancel.py -q
uv run --no-sync ruff check .
```

## 4. 实施顺序与提交建议

建议拆为三个独立提交：

1. `fix(runtime): harden stream completion and renderer diagnostics`
2. `fix(runtime): close session and UI worker state races`
3. `refactor(runtime): move stream parser out of UI package`

阶段 A、B 可独立合入；阶段 C 风险较高，必须在 A、B 稳定后实施。每个提交只包含对应代码和测试，避免再次形成难以回滚的大提交。

## 5. 最终验证

完成所有阶段后执行：

```powershell
uv run --no-sync ruff check .
uv run --no-sync pytest -q
uv run --no-sync mkdocs build
```

还需进行一次人工 TUI 冒烟：

1. 单工具调用正确显示。
2. 同一模型响应中的多个工具调用归为一个工具组。
3. 思考 → 工具 → 思考 → 工具保持两个真实批次，不错误合并。
4. 工具失败后后续回答仍可渲染。
5. 会话 detach/attach 后工具 item 生命周期可 replay。
6. MCP reload 成功和失败后 UI 均恢复可操作。
7. 使用 async checkpointer 的 turn 在慢 cleanup 下不误报失败。

## 6. 完成定义

- F1-F6 均有针对性回归测试。
- 正常 turn 不因固定 cleanup timeout 被误判失败。
- renderer failure 有安全、可定位且不泄露 payload 的日志。
- session submit 不泄漏 lock、semaphore 或 queued 状态。
- tool-output 与 MCP UI 状态遵守明确线程归属。
- `AgentTurnRuntime` 默认路径不再导入 `synapse.ui`。
- 兼容导入、CLI 和 TUI 行为不回退。
- Ruff、全量 pytest、MkDocs build 和人工 TUI 冒烟通过。
