# Codex 会话导入：阶段 2

> 状态：已实现 CLI 导入、终态 checkpoint seeding 与 import ledger。
> 范围：只导入 `codex_visible_text_v1` 的人机可见文本快照；不恢复 Codex runtime、工具调用、审批、shell 进程、world state 或 pending work。

## 命令

```text
synapse sessions codex-import NATIVE_ID [--workspace PATH] [--codex-home PATH]
```

`codex-import` 会先复用阶段 1 的只读 scanner 定位 Codex rollout，再用 `CodexHistoryProjector` 生成安全文本快照。投影失败时拒绝导入，只输出脱敏原因。

导入成功后命令输出 Synapse `thread_id`。相同 Codex `native_id` 和相同快照再次导入时返回已有 `thread_id`，不会重复追加消息。

## CheckpointSeeder

`src/synapse/checkpoint_seed.py` 是 checkpoint 写入的唯一入口。

约束：

- 仅支持当前验证过的 `langgraph==1.2.9` 与 `deepagents==0.6.12`。
- 只接受冻结投影契约 `codex_visible_text_v1` / `parser_version=1`。
- 只写入 `HumanMessage` 和 `AIMessage`，且必须有稳定唯一 id、非空纯文本内容。
- 拒绝 `ToolMessage`、assistant tool calls、invalid tool calls、usage metadata、response metadata 与 additional kwargs。
- 目标 thread 必须为空；拒绝覆盖已有 checkpoint。

写入流程：

1. 使用公开 LangGraph API `update_state(config, {"messages": ...}, as_node="model")` 写入安全消息。
2. 立即使用 `update_state(config, None, as_node=END)` 消费 pending graph tasks，生成终态 checkpoint。
3. 重新读取并验证：`next == ()`、无 interrupts、无 pending writes、消息 id/type/content 精确回读。
4. 任一步失败时删除本次新建的 checkpoint thread；清理失败时 fail closed。

该实现不手写 LangGraph checkpoint 内部结构，也不依赖 DeepAgents `DeltaChannel` 的私有序列化格式。

## Import Ledger

`src/synapse/codex_import.py` 维护项目本地 `codex-imports.sqlite`，默认与 `sessions.sqlite` 位于同一目录。

ledger 以 `codex:{native_id}` 为来源键，以安全文本快照 digest 作为不可变内容身份。digest 来自投影后的 `projection_kind`、`parser_version` 和消息列表，不使用原始 rollout bytes，也不使用 scanner 的弱 locator fingerprint。

状态机：

- `pending`：已领取导入租约，正在写 checkpoint / session metadata。
- `completed`：checkpoint 与 Synapse session metadata 都已验证完成。

行为：

- 同一来源、同一 digest、`completed`：验证 checkpoint 与 metadata 后重用原 `thread_id`。
- 同一来源、不同 digest：拒绝，避免 Codex 源变化后覆盖既有导入。
- 未过期 `pending`：拒绝并发导入。
- 过期 `pending`：验证已有 checkpoint；若缺失则重建；随后补齐 session metadata 并标记 completed。
- 新导入在 metadata 或 checkpoint 后续步骤失败时，会删除本次 checkpoint、session metadata 和 pending ledger row。

## TUI

TUI 提供两个入口：

```text
F7
/codex import
/codex import NATIVE_ID
```

前两者打开只读 Codex session picker，只列出当前 Synapse workspace 中可读取、且含至少一条已完成用户或助手文本的 Codex 会话。指定 `NATIVE_ID` 时直接执行导入。

导入发现会以 state DB 为主，并在导入场景下用有界 rollout header 扫描补充 state DB 未列出的会话。发现器兼容 Codex 的 `subagent.thread_spawn` 子代理会话，并读取有界的 `~/.codex/session_index.jsonl` 覆盖 Codex UI 设置的 thread title。当前 Codex 可能创建只有 `session_meta` 的空 thread；这些没有可见文本的条目会从 picker 过滤，直接导入时返回 `no_visible_messages`，不会创建空 Synapse session。

导入使用当前 TUI agent 的 checkpointer，在后台 worker 中完成。worker 与正常 turn 互斥，避免并发修改同一个 checkpoint。完成后 TUI 通过现有 `/switch <thread_id>` 路径切换会话、恢复 timeline 和刷新标题/状态栏；不会直接修改 `thread_id` 或手写 transcript。

## 仍未实现

- 批量导入。
- 将 Codex 源元数据展示在会话详情中。
- 对未来 LangGraph / DeepAgents 版本的 checkpoint seeding 兼容适配。

## 验证覆盖

- `tests/test_checkpoint_seed.py`：终态 seeding、真实 DeepAgents `DeltaChannel`、同步/异步 SQLite checkpoint round-trip、版本漂移与工具状态拒绝、失败补偿。
- `tests/test_codex_import.py`：ledger 幂等、来源内容变化拒绝、metadata 失败补偿、过期 pending 恢复与重建。
- `tests/test_cli_codex_import.py`：CLI helper 导入、重复导入重用、不安全 rollout 拒绝。
- `tests/test_tui_codex_import.py`：TUI picker 降级、后台导入路由和完成后经既有 session switch 切换。
