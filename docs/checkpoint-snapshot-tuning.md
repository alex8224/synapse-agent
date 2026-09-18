# 检查点快照频率调优与存量回收评估

> 状态：方案 A 已实施（`snapshot_frequency` 50 → 500）；方案 B 已执行完毕。
> 目标：降低 `.synapse/checkpoints.sqlite` 体积，同时不牺牲会话加载速度。
> 本文记录机制、实测数据、改动点与回滚方式。

## 1. 问题

`.synapse/checkpoints.sqlite` 实测 107,100 行 / 2,046 MB，其中：

| 检查点类型 | 行数 | 占比 | 字节 | 占比 |
| --- | --- | --- | --- | --- |
| 含完整快照 `_DeltaSnapshot` | 753 | 0.70% | 1,443.5 MB | **70.5%** |
| messages 通道缺失（仅增量） | 106,240 | 99.30% | 602.1 MB | 29.4% |

即 **0.7% 的行占用 70.5% 的字节**。这些行是 LangGraph `DeltaChannel` 周期性写入的完整状态快照。

## 2. 机制

### 2.1 增量存储（LangGraph 默认路径）

`DeltaChannel.checkpoint()` 恒返回 `MISSING`（`langgraph/channels/delta.py:193-202`），因此该通道**不出现在** `channel_values` 中。状态由「最近一个带值的祖先快照（seed）+ 其后全部 `writes`」回放重建。

### 2.2 完整快照的触发条件

`langgraph/pregel/_checkpoint.py:65-70`：

```python
updates, supersteps = counters_since_delta_snapshot.get(name, (0, 0))
if updates >= ch.snapshot_frequency or supersteps >= DELTA_MAX_SUPERSTEPS_SINCE_SNAPSHOT:
    result.add(name)
```

| 条件 | 阈值 | 来源 |
| --- | --- | --- |
| `updates >= snapshot_frequency` | **50** | `deepagents/graph.py:68`（LangGraph 默认 1000） |
| `supersteps >= DELTA_MAX_SUPERSTEPS_SINCE_SNAPSHOT` | 5000 | LangGraph，可用环境变量覆盖 |

计数器为 `(updates, supersteps)` 二元组，逐 superstep 递增（`langgraph/pregel/_loop.py:1111-1124`），快照后重置为 `(0, 0)`（`_loop.py:1154-1155`）。

### 2.3 第三个触发源：Overwrite

`langgraph/pregel/_loop.py:1139`：

```python
channels_to_snapshot = (
    delta_channels_to_snapshot(self.channels, new_counters)
    | self._delta_channels_with_overwrite
    if do_checkpoint else set()
)
```

**上下文压缩（`Overwrite`）会立即强制快照，不看计数。** 这带来一个重要性质：压缩之后必然存在一个新快照，因此「最新快照」一定位于最后一次压缩之后。

### 2.4 重建只使用最近的祖先快照

`langgraph/checkpoint/sqlite/_delta.py:68-124` 的 `step_walk_with_row`：沿 `parent_checkpoint_id` 由新到旧翻页，**只检查 `channel_values`**（不查 `channel_versions`），一旦找到该通道的值即停止（`seeded.add(ch)`）。

**推论：旧快照只在读取历史检查点（时间旅行）时才会被使用。** 读取最新状态时它们完全不被访问。

### 2.5 实测频率换算

| 线程 | 快照数 | 用户轮次 | 每快照覆盖轮次 |
| --- | --- | --- | --- |
| `e4fd52b6a50f` | 43 | 45 | 1.05 |
| `a3a18c61d297` | 41 | 43 | 1.05 |
| `227874dc24f9` | 30 | 39 | 1.30 |
| `9d4f32eab064` | 15 | 52 | 3.47 |
| 合计 | 754 | 1,305 | **1.73** |

即 `snapshot_frequency=50` 在实际使用中约等于「**每 1~2 轮对话落一份完整快照**」（单轮约产生 30~50 次 messages 更新）。

## 3. 正确性约束（实测）

在临时副本上做的对照实验（源库只读 ATTACH，不改动真实库）：

| 保留策略 | 普通线程 | 压缩密集线程 |
| --- | --- | --- |
| 删中间快照，保留其余 | 一致 | 一致 |
| **只留最新快照** | 一致 | **一致** |
| 只留最早快照 | 一致 | **不一致** |
| 全删 | 一致 | **不一致** |

- 普通线程（`ecd4fb5b30d8`）：删光快照仍重建一致 —— 快照纯属冗余。
- 压缩密集线程（`90646d4ffaeb`，160 次压缩事件）：删光后 548 条消息**内容不同**。原因是 `writes` 并非完整变更日志，含 `Overwrite` 的线程无法从零还原。

**硬约束：每个线程必须保留最新的一个快照。** 好在这一点没有速度代价（见 §5）。

## 4. 方案 A：调大 `snapshot_frequency`

### 4.1 改动点

`snapshot_frequency=50` 硬编码在依赖内（`deepagents/graph.py:68`），需在 Synapse 侧覆盖 messages 通道定义：

| 项 | 位置 |
| --- | --- |
| 现状传参 | `src/synapse/app/agent.py:687` `state_schema=DeepAgentState` |
| 该 schema 定义 | `deepagents/graph.py:66-68` |
| 复用的 reducer | `deepagents._messages_reducer._messages_delta_reducer`（私有模块） |
| 通道实现 | `langgraph.channels.delta.DeltaChannel` |

方案：在 Synapse 侧定义自己的 state schema，重新声明 `messages` 通道并指定更大的 `snapshot_frequency`，替换 `agent.py:687` 的传参。

注意 `agent.py:684-686` 的既有注释：该处传 `state_schema` 是为了让**子 agent 也继承 delta 存储的 messages 通道**，因此替换时必须保持同样的继承语义（子 agent 也必须用同一 schema）。

### 4.2 取值建议

| 取值 | 效果 | 依据 |
| --- | --- | --- |
| 50（现状） | 每 1~2 轮一个快照 | 当前 |
| **200~500（建议）** | 快照数降 4~10 倍，回放上界可控 | §5 实测增量 +0~55 ms |
| 1000（LangGraph 默认） | 快照数降 ~20 倍 | 回放上界 1000 次更新 |

不建议超过 1000：`supersteps >= 5000` 会成为主导约束，收益递减。

### 4.3 风险

| 风险 | 说明 | 缓解 |
| --- | --- | --- |
| 加载变慢 | 回放上界 = `snapshot_frequency` | 实测增量小（§5） |
| 私有 API 依赖 | `_messages_delta_reducer` 是下划线前缀 | 加契约测试；升级 deepagents 时校验 |
| 通道相等性 | `DeltaChannel.__eq__` 比较 `snapshot_frequency`（`delta.py:96-100`） | 混跑新旧定义前需验证 |
| 存量计数器 | 已存在线程的计数器从 checkpoint metadata 读取，阈值变化自然生效 | 无需迁移 |
| `files` 通道 | `deepagents/middleware/filesystem.py:335` 同样硬编码 50 | 单独评估，优先级低（体积占比小） |

### 4.4 验证步骤

1. 单元：`tests/test_state_schema.py` —— 断言 `messages` 通道的 `snapshot_frequency` 为目标值、reducer 与基类为同一对象、通道集合与基类一致。
2. 契约：与 `tests/test_checkpoint_seed.py` 同类的 round-trip 测试，确认自定义 schema 下同步/异步 SQLite checkpointer 读写正常。
3. 端到端：同一线程跑多轮，统计新产生的快照数量与间隔，确认与目标 F 一致。
4. 回归：`tests/test_session_*`、`tests/test_transcript.py`。

### 4.5 回滚

把 `src/synapse/app/agent.py` 的 `state_schema=SynapseAgentState` 改回 `state_schema=DeepAgentState` 即可。
已写入的快照格式不变（仍是 `_DeltaSnapshot`，`EXT_DELTA_SNAPSHOT=7`，`langgraph/checkpoint/serde/jsonplus.py:302`），因此**回滚无数据风险**，仅恢复为高频快照。

### 4.6 实施记录

实际取值 **`SNAPSHOT_FREQUENCY = 500`**。

| 文件 | 改动 |
| --- | --- |
| `src/synapse/app/state_schema.py`（新增） | 定义 `SynapseAgentState(DeepAgentState)`，重声明 `messages` 通道 |
| `src/synapse/app/agent.py:306` | 惰性 import 改为 `from synapse.app.state_schema import SynapseAgentState` |
| `src/synapse/app/agent.py:687` | `state_schema=SynapseAgentState` |
| `tests/test_agent_factory.py:145` | 断言改为 `is SynapseAgentState` |
| `tests/test_state_schema.py`（新增） | 通道契约测试（4 项） |

两个实现细节：

1. **不导入私有模块**。reducer 从 `DeepAgentState` 的注解中解包取得（`typing.get_type_hints(..., include_extras=True)`），因此与基类是同一个对象，只有 cadence 不同。规避了 `deepagents._messages_reducer` 这一下划线私有路径。
2. **不放进 `agent_assembly.py`**。该模块被 `agent.py:13` 模块级导入，放进去会让 `deepagents` 在 `import synapse.app.agent` 时提前加载；独立模块 + 沿用既有的函数内惰性 import 保持了原有加载时机。

子 agent 覆盖情况：DeepAgents 的 `SubAgent`（字典规格）会继承 `create_deep_agent(state_schema=...)`（`deepagents/middleware/subagents.py:508-509`），Synapse 使用的正是字典规格（`src/synapse/runtime/subagent_specs.py:15`），因此子 agent 同样生效。
唯一例外是 `CompiledSubAgent`（预编译 runnable）不继承 —— Synapse 当前未使用。

### 4.7 运行时验证

**判别原理**：计数器跨进程持久化（`langgraph/pregel/_loop.py:1679` `self.checkpoint_metadata = saved.metadata`），
阈值取自当前进程编译的 schema。因此调参对**已有会话**同样生效，不需要新开会话。
且旧 F=50 下 `updates` 一旦到 50 必然落快照并清零，所以计数器**不可能**稳定停在 50 以上 ——
这是一个不需要等到 500 就能判别的信号。

**实测结果**（重启后）：
| 项 | 值 |
| --- | --- |
| 最新检查点计数器 | `updates=56, supersteps=142` |
| 最后一个快照 | 重启前产生（`ts` 早于进程启动时间） |
| 重启后新增快照 | **0** |

计数器越过 50 而未触发快照，确认新阈值生效。

顺带确认重启确实加载了新代码：源文件修改时间早于进程启动时间，且 `.venv` 为 editable 安装
（`_editable_impl_synapse_cli_agent.pth` 指向 `src`）。

## 5. 方案 B：存量回收（每线程保留最新快照）

### 5.1 收益（实测）

| 项 | 数值 |
| --- | --- |
| 快照总数 | 753 个 / 1,443.5 MB |
| 涉及线程 | 133 个 |
| 保留最新快照 | 133 个 / 225.1 MB |
| **可回收** | **620 个 / 1,218.3 MB** |
| 库体积 | 2,046 MB → 828 MB |

**实际执行结果**（2026-09-17）：`checkpoints.sqlite` 主文件 5,053 MB → **1,315 MB**，
快照 133 个（每线程 1 个），`PRAGMA integrity_check = ok`，空闲页 0。
降幅（约 3.7 GB）大于快照本身的 1.19 GB，因为 VACUUM 同时回收了此前删除 8 月会话留下的空闲页。
抽查 6 个会话（含压缩密集的 `90646d4ffaeb`）的消息数与执行前基线完全一致。

### 5.2 速度影响（实测，5 次取中位）

`load_messages_from_sqlite_file` 耗时：

| 线程 | 消息数 | 全部快照(F=50) | 只留最新 | 隔10留1(≈F=500) | 只留最早 | 全删 |
| --- | --- | --- | --- | --- | --- | --- |
| `a3a18c61d297` | 2244 | 211 ms | **204 ms** | 201 ms | 707 ms | 715 ms |
| `e4fd52b6a50f` | 2259 | 217 ms | **219 ms** | 272 ms | 674 ms | 642 ms |
| `9d4f32eab064` | 916 | 133 ms | **131 ms** | 174 ms | 278 ms | 271 ms |
| `0e3da92ce9e5` | 602 | 65 ms | **60 ms** | 60 ms | 158 ms | 166 ms |

**「只留最新」与「全部保留」耗时基本一致** —— 印证 §2.4 的推论：旧快照不被读取。

口径说明：临时副本、纯 SQLite 读取耗时，不含上层 transcript 投影开销，属乐观下界。

### 5.3 改动点

| 项 | 说明 |
| --- | --- |
| 操作方式 | **改写 blob**：反序列化 checkpoint，移除 `channel_values["messages"]`，重新序列化写回 |
| 禁止做法 | 不能 `DELETE` 整行 —— 会打断 `parent_checkpoint_id` 链 |
| 保留对象 | 每线程 `checkpoint_id` 最大的那个快照 |
| 前置条件 | 独占访问（需先退出 TUI / runtime）；可复用 `scripts/compact_synapse_db.ps1` 的占用检查逻辑 |
| 序列化 | 使用 `langgraph.checkpoint.serde.jsonplus.JsonPlusSerializer`，已验证 round-trip 字节一致 |
| 落地位置 | 已集成为 `scripts/compact_synapse_db.ps1` 的**步骤 2/4**；核心改写逻辑在 `scripts/reclaim_checkpoint_snapshots.py` |
| 编排分工 | ps1 负责占用检查、备份开关、VACUUM 与体积汇报；Python 负责序列化改写与指纹校验 |
| 可复用组件 | `src/synapse/sessions/checkpoint_gc.py`（既有 GC 模式） |

### 5.3.1 使用方式

```powershell
# 1. 退出 Synapse（TUI / runtime / web-console 全部）
# 2. 先看计划（不改任何数据）
pwsh -File scripts/compact_synapse_db.ps1 -DryRun

# 3. 执行（含自动备份 + 指纹校验 + VACUUM）
pwsh -File scripts/compact_synapse_db.ps1 -SnapshotBackup
```

相关开关：

| 开关 | 作用 |
| --- | --- |
| `-SnapshotKeep N` | 每线程每通道保留最新 N 个快照，默认 1；`0` 会被拒绝 |
| `-SkipSnapshotReclaim` | 跳过步骤 2 |
| `-SnapshotBackup` | 运行前把 `checkpoints.sqlite` 备份为 `.bak`（该步骤不可逆） |
| `-DryRun` | 只打印计划 |
| `-NoPurge` | 只做 VACUUM（步骤 1~3 全部跳过） |

### 5.4 风险

| 风险 | 说明 | 缓解 |
| --- | --- | --- |
| **丢失历史回溯** | 旧快照删除后，读取历史检查点（时间旅行/回滚）会退化或失败 | 确认无此需求；或改为「保留最新 N 个」 |
| **不可逆** | blob 改写无法撤销 | 执行前备份 checkpoints.sqlite |
| 重建不一致 | 若误删最新快照，压缩密集线程会静默改变内容（不报错） | 严格按 `checkpoint_id` 最大值保留；执行后校验指纹 |
| 派生数据陈旧 | `transcript.sqlite` / `search-index.sqlite` 持有旧投影 | 失效对应线程的投影，触发重建 |
| 非持续有效 | 未来仍按 F=50 产生新快照 | 配合方案 A；或定期重跑 |

### 5.5 验证步骤

1. 执行前：对每个受影响线程记录 `load_messages_from_sqlite_file` 的消息数 + 内容指纹。
2. 执行后：逐线程比对指纹，必须**完全一致**。任何不一致立即中止并回滚。
3. 派生库：确认 transcript / search-index 能正常重建。
4. 抽样端到端：打开若干会话，比对界面时间线与预期一致。

### 5.6 回滚

blob 改写不可逆，**必须依赖执行前备份**。建议：

```powershell
# 备份（体积约等于当前库大小，需预留磁盘）
Copy-Item .synapse/checkpoints.sqlite .synapse/checkpoints.sqlite.bak
```

回滚 = 停止 Synapse，用备份覆盖，再启动。

## 6. 组合建议

| 动作 | 作用对象 | 速度代价 | 磁盘收益 |
| --- | --- | --- | --- |
| 方案 A：F 调到 200~500 | 未来写入 | +0~55 ms | 抑制 O(M²/F) 增长 |
| 方案 B：只留最新快照 | 存量数据 | **0** | 立即回收 ~1.19 GB |

两者收益不重叠，可叠加。叠加后库体积趋向「每线程一个快照」的地板；F 调大后最新快照本身也会变小，地板随之下降。

**执行顺序建议**：先做方案 B（无速度代价、收益立竿见影），再做方案 A（防止重新长回来）。

## 7. 附：相关证据索引

| 主题 | 位置 |
| --- | --- |
| `DeltaChannel` 定义与 `snapshot_frequency` | `langgraph/channels/delta.py:25-202` |
| 快照触发判定 | `langgraph/pregel/_checkpoint.py:50-71` |
| 快照写入 | `langgraph/pregel/_checkpoint.py:180-201` |
| 计数器递增 | `langgraph/pregel/_loop.py:1111-1162` |
| Overwrite 强制快照 | `langgraph/pregel/_loop.py:1139` |
| 祖先回溯（种子判定） | `langgraph/checkpoint/sqlite/_delta.py:68-124` |
| 快照 blob 序列化 | `langgraph/checkpoint/serde/jsonplus.py:302-307` |
| DeepAgents 的 50 | `deepagents/graph.py:66-68` |
| `files` 通道同问题 | `deepagents/middleware/filesystem.py:335` |
| Synapse 传参点 | `src/synapse/app/agent.py:687` |
| Synapse 重建实现 | `src/synapse/sessions/transcript.py:339-370` |
| 既有子 agent GC | `src/synapse/sessions/checkpoint_gc.py` |
| 既有压缩脚本 | `scripts/compact_synapse_db.ps1` |
| 调优后的 schema | `src/synapse/app/state_schema.py` |
| 快照回收脚本 | `scripts/reclaim_checkpoint_snapshots.py` |
| schema 契约测试 | `tests/test_state_schema.py` |
| 子 agent 继承机制 | `deepagents/middleware/subagents.py:508-509` |
| 计数器持久化 | `langgraph/pregel/_loop.py:1679`、`1937` |
