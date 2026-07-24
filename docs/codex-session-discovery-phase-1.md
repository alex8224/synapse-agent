# Codex 会话发现：阶段 1

> 状态：已实现，只读发现与检查。
> 范围：本阶段不导入历史、不创建 Synapse `thread_id`、不写 LangGraph checkpoint、不启动或恢复 Codex runtime。

## 目标与边界

本阶段提供三个 CLI 命令：

```text
synapse sessions codex-list [--workspace PATH] [--codex-home PATH]
synapse sessions codex-inspect NATIVE_ID [--workspace PATH] [--codex-home PATH]
synapse sessions codex-preview NATIVE_ID [--workspace PATH] [--codex-home PATH] [--limit N]
```

`codex-list` 不传 `--workspace` 时列出所有可读取会话，并显示其所属项目目录；传入该参数时才进行精确工作区过滤。`codex-preview` 只显示 `codex_visible_text_v1` 已确认安全的、已结束 turn 中的用户/助手文本；它不显示工具调用、工具输出、推理、环境内容或原始 JSON。可用 `--offset` 和 `--limit` 翻页；单条消息最多显示 12,000 字符。投影失败时不输出正文，只输出脱敏原因。

实现位于 [src/synapse/codex_sessions.py](/src/synapse/codex_sessions.py)。模块输出的 `CodexSession` 是外部会话的元数据引用，不是 Synapse session。`fingerprint` 是基于 locator、size 和 mtime 的弱定位值，只适合显示和重新扫描比对；后续 import ledger 不得用它作为内容幂等依据。

扫描顺序：

1. 读取 `CODEX_HOME`，或默认 `~/.codex`。
2. 选择最高版本的 `state_N.sqlite`。
3. 使用 SQLite `mode=ro` 和 `PRAGMA query_only=ON` 查询 `threads` 表。
4. state DB 缺失或 schema 不支持时，回退为 `sessions/**/*.jsonl` 的有限头部检查。

扫描可按可选 workspace 精确过滤；未提供 workspace 时列出所有可读取会话。筛选模式只接受与目标 workspace 完全相同的 canonical `cwd`，不做父目录匹配。

## 当前安全约束

- state DB 只读打开，不执行 schema 写入、WAL checkpoint 或修复操作。
- rollout 路径 resolve 后必须位于 `CODEX_HOME/sessions` 内，并且文件名 UUID 必须与 `native_id` 一致。
- 回退扫描限制为 500 个文件、每文件 32 MiB、前 50 条记录、128 KiB 头部和 16 KiB 单行；state DB 最多读取 5,000 行，并在截断时 warning。
- `CODEX_HOME/sessions` 若是 symlink/junction 则拒绝扫描，避免逻辑根目录逃逸。
- 标题会折叠空白并截断到 120 字符；检测到 `<environment_context>`、`<user_instructions>` 等内部注入时不展示该文本。
- 只识别 `cli`、`vscode`、`atlas`、`chatgpt` 来源；不展示不受支持来源和子代理记录。
- `.jsonl` 与 `.jsonl.zst` 都可读取。压缩文件使用 `zstandard` 流式解压：发现阶段最多读取前 50 条、128 KiB 解压头部和 16 KiB 单行；完整预览最多读取 32 MiB 解压文本和 256 KiB 单行。格式损坏或超过限制时拒绝预览并给出脱敏原因。
- CLI 仅输出 metadata、locator fingerprint 和 warnings，不打印对话正文、工具输出或内部 instructions。

## 已验证的格式假设

参考 Codex state DB 的 `threads` 表，当前 scanner 兼容以下字段：

```text
id, rollout_path, updated_at_ms|updated_at, source, cwd, archived
title?, first_user_message?
```

`state_N.sqlite` 是优化路径，不是持久格式承诺。因此缺字段或不可读时必须安全地回退，而不是猜测列含义。

rollout 回退只使用 `session_meta` 的 `cwd/source/title` 和第一个普通 `event_msg.user_message` 作为 title fallback。它不尝试把 rollout 转换为 transcript。

## 不可跨越的后续条件

导入不能直接调用：

```python
agent.update_state(..., as_node="model")
```

实际 DeepAgents graph 上该写法会留下 pending `next`，并且 `messages` reducer 会让重试重复追加。导入阶段必须先实现并验证 `CheckpointSeeder`：

- 写入后 `next == ()`，没有 pending task。
- 同一输入重试不增加消息。
- 写入失败后能删除 checkpoint。
- 当前 DeepAgents/LangGraph 版本变化时 fail closed。

Codex rollout 也不能线性抽取 user/assistant。其恢复语义包含 `ThreadRolledBack`、`Compacted.replacement_history` 和 legacy compaction。后续 `CodexHistoryProjector` 必须在接口中声明 `projection_kind` 与 `parser_version`，并选择唯一语义：

```text
codex_visible_text_v1
```

该 MVP 语义为“当前有效的人机可见文本快照”，不迁移工具调用、工具结果、审批、world state、shell 进程或 Codex runtime。

## 后续实施顺序

1. 收集脱敏 rollout fixtures：普通对话、rollback、replacement history、legacy compaction、重复 user event、未完成 turn。
2. 在 fixtures 上实现并冻结 `CodexHistoryProjector` 的文本快照语义。
3. 实验并实现 `CheckpointSeeder`，确认终态 checkpoint 写入方式。
4. 新增 import ledger，处理 metadata SQLite 与 checkpoint SQLite 的补偿、并发唯一约束和崩溃恢复。
5. 仅在 CLI 导入稳定后再接入 TUI picker 和异步 worker。

可选 app-server 后端应优先使用稳定的 `thread/read(includeTurns=true)` 作为只读投影。`thread/resume.history`、`thread/resume.path`、`thread/turns/list` 和 `thread/items/list` 不应成为 MVP 的硬依赖，其中后两者仍属于 experimental API。

## 阶段 0：冻结的文本投影契约

`src/synapse/codex_history.py` 提供纯只读的 `CodexHistoryProjector`。它不依赖 scanner、不写 SQLite、不创建 Synapse thread，也不执行 Codex runtime。输出是不可变 `CodexTextSnapshot`：

```text
projection_kind = codex_visible_text_v1
parser_version = 1
```

该版本只表达“当前有效且已结束 turn 的人机可见文本”，不声称还原 Codex 的完整模型上下文。每条 `CodexVisibleMessage` 有确定性的 `source_id`、`turn_id`、`role` 与 `text`；`source_id` 只用于未来 ledger 的来源追踪，不能替代 checkpoint reducer 的幂等策略。

固定规则：

- 正常文本只取 `event_msg.user_message` 和 `event_msg.agent_message`。`response_item` 一律忽略，避免与 EventMsg 重复，并防止工具、内部 prompt 或模型上下文进入快照。
- `task_started`/`turn_started` 与 `task_complete`/`turn_complete` 划分可消费 turn。中止和文件结束时未完成的 turn 都不输出，并给出脱敏 warning。
- `thread_rolled_back.num_turns` 删除最近已完成 turn；若数值非法或超过已完成 turn 数，投影拒绝。
- `compacted.replacement_history` 是唯一允许读取 `ResponseItem` 的位置。它必须完全由 `user.input_text` 和 `assistant.output_text` 的 `message` 项组成；解析成功后替换此前全部文本基线，再回放后缀。
- legacy compaction（没有 `replacement_history`）、不支持的 replacement 项、JSON 损坏、非 UTF-8 或读取失败均 fail closed：`importable=False` 且 `messages=()`，绝不返回可能已经过时的前缀。
- warning 只包含稳定的 code 和行号，不携带原文、JSON 片段、命令、工具输出或内部 instructions。未知普通 record/event 仅产生 warning 并忽略；未来版本若要消费它们必须提升 `parser_version` 并增加 fixture。

回归 fixtures 位于 `tests/fixtures/codex_rollouts/`，覆盖正常已完成对话、rollback、replacement history、legacy compaction、重复 response item、中止/未完成 turn 与不支持 replacement。该模块只是 import 前置条件，尚未实现 `CheckpointSeeder`、import ledger、导入 CLI 或 TUI 集成。

## 验证覆盖

`tests/test_codex_sessions.py` 覆盖：

- 最高 state DB 的只读扫描和 workspace 精确过滤。
- state DB 不存在时的 JSONL 头部回退。
- `.jsonl.zst` 的有界发现、预览和损坏文件降级。
- state DB 指向 `sessions` 目录以外文件时的拒绝。
- 内部上下文标记不会作为展示标题。
