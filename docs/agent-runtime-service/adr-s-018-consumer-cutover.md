# ADR-S-018：消费者切换统一使用 application DTO ports

- 状态：Accepted
- 日期：2026-08-08

## Context

S10 将 CLI、TUI 与 ACP 接入统一 Agent Runtime Service。消费者需要共享 submit、resume、cancel、steer、approval、session 查询和事件 watch 的语义，同时保持进程内 facade 与未来远程 client 的边界一致。执行所有权仍属于既有 runtime 栈，而不是某个 UI 或协议适配器。

## Decision

1. CLI、TUI、ACP 的默认路径统一依赖纯 application DTO ports。in-process facade 与 remote client 实现同一 contract；DTO 不携带 runtime、manager、handle、task、future 或 widget。
2. 唯一执行链保持为：

   ```text
   AgentRuntimeService -> RuntimeManager.submit_ref/resume_ref -> SessionRuntime -> AgentTurnRuntime
   ```

3. `LocalProjectRuntimeConsumer`、`TUIRuntimeSessionFacade` 与 ACP adapter 是 composition owner/consumer adapter，不是第二个业务 runtime。UI/ACP 不访问 `owner.manager`；agent metadata 留在 composition 层，不通过 wire 传输。
4. TUI 的 UI-only queue 只用于 UI 唤醒/渲染调度，不拥有 execution runtime。`RuntimeEvent` renderer 负责展示策略；submit、cancel、steer、approval、watch、status、session switch 与 dialogs/chrome 均通过 service DTO。
5. watch detach 或 connection close 只关闭观察连接/subscription，不 cancel turn；事件客户端使用 session sequence。CLI 在提交前建立 watch；consumer 关闭由同 loop owner 负责，并使用 cancel fence。
6. ACP approval 使用 `PendingApprovalQuery` 与 `ResumeTurnCommand`，approval wire/client 遵循同一 DTO contract。checkpoint copy/delete 是纯 callback。
7. legacy `ui.stream.stream_agent` 可继续作为兼容 utility，但 CLI/TUI/ACP 默认路径不得调用 `stream_agent` 或 `agent.ainvoke`。

## Consequences

- 三类消费者拥有一致的 application contract，可在进程内 facade 与远程 client 之间替换实现。
- runtime 生命周期、turn 结果和事件顺序不再依赖 TUI/ACP 的订阅或本地 buffer。
- agent metadata 不进入 wire，减少跨边界耦合；需要 metadata 的 composition 负责组装。
- UI-only queue 与 execution queue 的职责清晰，但仍需保留兼容导出，直至外部调用方完成迁移。
- 最终状态已通过全仓安全门禁、MkDocs strict、`uv build` 与 review；S10 总体状态为 `completed`。

## Rejected Alternatives

- **让每个消费者直接持有或调用 `RuntimeManager`**：会绕过 service contract，重新引入多套业务入口和生命周期所有权。
- **在 TUI queue 中运行 turn**：会使 UI 订阅状态影响 execution，违背无订阅者运行与 detach 不 cancel 语义。
- **把 agent metadata 编入 approval/event wire**：会扩大协议耦合，并混淆 composition metadata 与 application DTO。
- **立即删除 `ui.stream.stream_agent`**：会破坏公共兼容导出；先保留 utility，默认路径不再依赖它。

## Compatibility

- 保留既有 public compatibility aliases/re-exports，包括 legacy stream utility；不改变其可用性，但不允许它成为 CLI/TUI/ACP 默认执行路径。
- 进程内 service、facade 与未来 remote client 均以 frozen/纯 application DTO ports 为契约，不暴露执行对象。
- watch 的 session sequence、detach/close 不 cancel turn、approval resume 语义保持一致。

## Verification

- CLI：`LocalProjectRuntimeConsumer`、watch-before-submit、DTO result、同 loop owner close、cancel fence；默认路径无 `stream_agent`/`agent.ainvoke`。
- ACP：service-only `ACPManagedSession`、approval query/command、approval wire/client、checkpoint callbacks；安全进程内 ACP 135 passed，B2 50 passed。
- TUI：`TUIRuntimeSessionFacade`、`RuntimeEvent` renderer、service DTO consumers；最终 C1/C2 明确清单 443 passed。
- ACL：13 ports；S6 更新 78 passed；approval service/transport 37 passed。
- 最终安全全仓 2348 passed、2 skipped；明确排除 `test_acp_p0_baseline.py`、`test_acp_p1_transport.py`、`test_agent_turn_runtime.py`、`test_backends.py`、`test_git_chrome.py`、`test_herdr_integration.py`、`test_runtime_service_routing_s5.py`、`test_runtime_transport_s7.py`、`test_startup_trace.py`、`test_transcript_migration.py`、`test_runtime_daemon_s8.py`、`test_runtime_transport_client_methods_s9.py`、`test_runtime_transport_websocket_s7.py`。原因分为 process API 安全约束及 socket 环境不稳定；不声称排除项通过。lifecycle/consumer 核心 194 passed；permit2/crossloop/generation 分别为 5/5/4；真实 approval 9；CLI registry 3；project exact/generation tests 通过。Ruff、`git diff --check`、MkDocs strict、`uv build` 通过，包版本 `0.1.43`。三轮 review 最终无阻塞发现，workspace freeze Medium 已修复。未跟踪 `.sessions.sqlite` 与 `transcript.sqlite` 未删除、不可提交；详见 residual/local artifacts note。
