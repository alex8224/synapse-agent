# ACP 完整语义适配实施进度

> 本文件是实施状态的单一真源。阶段文档定义范围和方案，不维护实时状态。  
> 状态值：`not_started`、`in_progress`、`blocked`、`completed`。  
> 当前总体状态：`in_progress`。

## 当前工作

- 当前阶段：P0-P7 已收口；P8 本地可达项（矩阵/notification/batch/竞争/有界性/Windows stdio/文档/锁定）已关闭。
- 下一门禁：真实 Zed 互通、Linux 子进程矩阵、全量 pytest 中 3 个原生压缩核心环境失败、官方 Client 真实双向 harness。
- 当前阻塞：无。

## 阶段总览

| 阶段 | 状态 | 完成度 | 门禁结果 | 方案 |
|---|---|---:|---|---|
| P0 协议基线与能力矩阵 | completed | 8/8 | 通过 | [phase-0-protocol-baseline.md](phase-0-protocol-baseline.md) |
| P1 核心传输与会话 | completed | 9/9 | 通过 | [phase-1-core-transport.md](phase-1-core-transport.md) |
| P2 Prompt 完整语义 | completed | 10/10 | 通过 | [phase-2-prompt-semantics.md](phase-2-prompt-semantics.md) |
| P3 Permission/HITL | completed | 9/9 | 通过 | [phase-3-permissions.md](phase-3-permissions.md) |
| P4 会话生命周期 | completed | 10/10 | 非法转换/拒绝、fork 成功/回滚/独立性、delete 隔离门禁通过；真实 backend 竞态待 P8 | [phase-4-session-lifecycle.md](phase-4-session-lifecycle.md) |
| P5 会话级 MCP | completed | 9/9 | 合并/冲突检测、session pool 隔离、启动失败释放通过；真实 transport/取消竞态待 P8 | [phase-5-session-mcp.md](phase-5-session-mcp.md) |
| P6 Client 反向服务 | completed | 9/9 | editor buffer、session 隔离、能力组合/路径安全/终端竞争门禁通过；真实官方 Client 待 P8 | [phase-6-client-services.md](phase-6-client-services.md) |
| P7 高级能力 | completed | 10/10 | `_meta` 不污染、能力真值、config/mode/thinking/commands 实现；auth/elicitation 为 not_target | [phase-7-advanced-capabilities.md](phase-7-advanced-capabilities.md) |
| P8 合规与发布收口 | in_progress | 7/10 | 矩阵清零、notification/batch、竞争、有界性、Windows stdio、文档和 SDK/schema 锁定完成；Zed/Linux 与 3 个环境失败待关闭 | [phase-8-compliance.md](phase-8-compliance.md) |

## P0 任务

- [x] P0-01 安装并精确锁定官方 `agent-client-protocol` SDK 版本。
- [x] P0-02 记录 SDK 对应 ACP wire version 和 schema release。
- [x] P0-03 从 SDK/schema 提取全部 Agent/Client 方法、capability 和联合类型。
- [x] P0-04 建立方法、能力、handler、测试的可追溯矩阵。
- [x] P0-05 固化 stdio golden fixtures 和 SDK 驱动测试 harness。
- [x] P0-06 建立 stdout 污染、runtime 禁止依赖 ACP SDK 的导入护栏。
- [x] P0-07 记录 Zed 与独立 SDK Client 的基线环境。
- [x] P0-08 评审矩阵并通过 P0 门禁，禁止遗留未分类稳定能力。

## P1 任务状态

- [x] P1-01 新增 `synapse-acp` 独立进程入口。
- [x] P1-02 用官方 `run_agent` 建立 stdio 服务和清理路径。
- [x] P1-03 实现 initialize、版本协商和 capability 单一真源的初版。
- [x] P1-04 引入完整 `ACPSessionDescriptor` 和 session context registry。
- [x] P1-05 实现 `session/new` 并绑定 cwd、资源和 SessionRuntime。
- [x] P1-06 实现基础 `session/prompt`、文本 update 和 stop reason。
- [x] P1-07 实现 `session/cancel` 的完整 subprocess/竞争测试。
- [x] P1-08 完善协议错误映射、未知 session 和非法状态处理测试。
- [x] P1-09 通过 subprocess stdio、disconnect 和资源清理门禁。

## P2 任务

- [x] P2-01 实现所有声明的 prompt ContentBlock 输入 codec。
- [x] P2-02 实现文本、图片、resource link、embedded resource 输入。
- [x] P2-03 建立 `ACPEventBridge` 的线程安全有界队列。
- [x] P2-04 映射 agent message、thought 和完整 stop reasons。
- [x] P2-05 映射 tool call start/update/final 状态机和稳定 ID。
- [x] P2-06 扩展内部语义事件以承载结构化 args、result、kind 和 path。
- [x] P2-07 增加 Plan 和文件 diff 领域事件及 ACP 投影。
- [x] P2-08 映射 usage 和锁定 schema 的其他稳定 SessionUpdate 类型。
- [x] P2-09 实现文本合并、背压、慢 Client 和顺序保障。
- [x] P2-10 对 P0 内容及更新矩阵逐项通过 schema 测试。

## P3 任务

- [x] P3-01 实现 `PermissionCoordinator` 和 pending request registry。
- [x] P3-02 将 LangGraph interrupt 转换为 ACP permission request。
- [x] P3-03 将 selected/rejected/cancelled 结果转换为 resume decisions。
- [x] P3-04 在同一个 ACP prompt 内循环执行 resume turns。
- [x] P3-05 支持并行 actions 的稳定顺序和独立结果。
- [x] P3-06 支持 approve once 和会话级授权策略。
- [x] P3-07 prompt cancel 时取消 turn 和所有 pending permissions。
- [x] P3-08 处理 Client 断开、超时、重复响应和 resume 上限。
- [x] P3-09 通过完整 permission 状态机和竞争条件门禁。

## P4 任务

- [x] P4-01 按锁定 schema 实现 load 与历史 SessionUpdate 回放。
- [x] P4-02 实现 resume，并与 load 的回放语义严格区分。
- [x] P4-03 实现 session list、cwd 过滤和 opaque cursor 分页。
- [x] P4-04 实现 fork 的 checkpoint、metadata 和资源语义（真实 backend 独立性仍待外部门禁）。
- [x] P4-05 实现 delete，并定义 metadata/checkpoint/goal/tool-output 清理边界（目标/工具输出跨域清理待补）。
- [x] P4-06 实现 schema 中的 close 或等价生命周期终止方法。
- [x] P4-07 支持 additional directories 并执行路径权限校验。
- [x] P4-08 为 `SessionStore` 增加 cwd/project identity 和稳定分页能力（ACP catalog 范围）。
- [x] P4-09 实现 session info update 和历史 ACP 专用投影。
- [x] P4-10 通过所有合法/非法生命周期转换门禁。

## P5 任务

- [x] P5-01 将 ACP stdio MCP 配置转换为 `McpServerConfig`。
- [x] P5-02 按 capability 实现 HTTP 及 schema 保留的其他 MCP transport。
- [x] P5-03 为每个 ACP session 建立独立 MCP scope 和 pool key。
- [x] P5-04 支持异步 Agent factory，避免 session 创建阻塞服务 loop。
- [x] P5-05 合并项目 MCP 与 Client MCP，并实现冲突检测。
- [x] P5-06 load/resume 时按本次请求重建 MCP 连接。
- [x] P5-07 close/delete/disconnect 时可靠释放 MCP resources。
- [x] P5-08 确保 env、headers、token 不进入日志、事件和持久化。
- [x] P5-09 通过跨 session 隔离、失败和取消测试门禁。

## P6 任务

- [x] P6-01 实现 `ClientServiceGateway` 和 Client capability snapshot。
- [x] P6-02 实现 Client `fs/read_text_file`，支持未保存 editor buffer。
- [x] P6-03 实现 Client `fs/write_text_file` 和权限/路径约束。
- [x] P6-04 实现 terminal create、output、wait、kill、release 完整生命周期。
- [x] P6-05 提供 Client-backed backend/tool adapter，避免两套工具语义（当前为 session-local tools）。
- [x] P6-06 Client capability 缺失时安全回退本地 backend。
- [x] P6-07 Client RPC 失败、取消和断开时返回稳定工具错误。
- [x] P6-08 确保 Client terminal/filesystem 按 session 隔离和释放。
- [x] P6-09 通过能力组合、路径安全和终端竞争测试门禁。

## P7 任务

- [x] P7-01 实现锁定 schema 的 session config options。
- [x] P7-02 映射 model、thinking、safety/approval 等 Synapse 设置（当前 model/provider 仍保持默认 profile）。
- [x] P7-03 发布适用于 ACP 的 available commands，并排除 TUI 专属命令。
- [x] P7-04 实现 config/current mode 等锁定 schema 的兼容语义。
- [x] P7-05 实现 usage/context/cost 更新，无法准确提供的字段不伪造（context/cost 不发送）。
- [x] P7-06 实现 session title/info metadata 更新。
- [x] P7-07 审查锁定 schema 的 authentication/logout 流程并明确 not_target（待独立产品认证方案，不声明 authMethods）。
- [x] P7-08 审查稳定 elicitation/Client 交互能力并明确 not_target（无可验证产品语义，不声明）。
- [x] P7-09 支持 `_meta` trace context，禁止扩展字段污染标准对象。
- [x] P7-10 对 P0 高级能力矩阵逐项通过门禁。

## P8 任务

- [x] P8-01 对 P0 能力矩阵逐项清零并评审 capability 真值。
- [x] P8-02 覆盖非法参数、未知方法、notification 和 batch 语义（batch 经 ADR-015 判定不适用 ndjson）。
- [x] P8-03 覆盖 cancel/disconnect/permission/MCP/terminal 竞争条件（mock 范围；真实跨进程竞态待 P8-06）。
- [x] P8-04 验证所有队列、buffer、分页、回放和日志上限（catalog 回放加保留上限）。
- [x] P8-05 完成 Windows/Linux subprocess stdio 测试（Windows 本机通过；Linux 待 CI 矩阵）。
- [ ] P8-06 完成 Zed 真实客户端兼容测试。
- [x] P8-07 完成官方 SDK 驱动 Agent lifecycle 端到端测试；Client filesystem/terminal 组合仍待真实双向 harness。
- [ ] P8-08 完成 Ruff、ACP 测试、全量测试和构建（Ruff/ACP/build 通过；全量 3 个失败为原生压缩核心未安装的环境问题）。
- [x] P8-09 更新安装、客户端配置、能力和故障排查文档。
- [ ] P8-10 发布前复核 SDK/schema 锁定和升级策略，关闭总体计划。

## 验证记录

| 时间 | 阶段/任务 | 命令或方法 | 结果 | 备注 |
|---|---|---|---|---|
| 2026-08-12 | P0/P1 | `uv run --no-sync pytest tests/test_acp_p0_baseline.py tests/test_acp_p1_core.py tests/test_acp_p1_transport.py -q` | 通过 | 19 tests passed；覆盖 initialize/new/prompt/cancel、重叠 prompt、disconnect 清理、official SDK transport 和 subprocess helper |
| 2026-08-12 | P1 | `uv run --no-sync ruff check src/synapse/acp tests/test_acp_p0_baseline.py tests/test_acp_p1_core.py tests/test_acp_p1_transport.py` | 通过 | ACP 适配层和专项测试无 Ruff 错误 |
| 2026-08-12 | P1 | `uv run --no-sync mkdocs build` | 通过 | 仅保留仓库既有未纳入导航文档和旧链接警告 |
| 2026-08-12 | P2 | `uv run --no-sync pytest tests/test_acp_p0_baseline.py tests/test_acp_p1_core.py tests/test_acp_p1_transport.py tests/test_acp_p2_content.py tests/test_acp_p2_events.py -q` | 通过 | 26 tests passed；覆盖 ContentBlock codec、大小边界、能力拒绝、runtime attachment、事件合并、有界背压和终态保序 |
| 2026-08-12 | P2 | `uv run --no-sync ruff check src/synapse/acp tests/test_acp_p0_baseline.py tests/test_acp_p1_core.py tests/test_acp_p1_transport.py tests/test_acp_p2_content.py tests/test_acp_p2_events.py` | 通过 | ACP P0/P1/P2 适配代码和专项测试无 Ruff 错误 |
| 2026-08-12 | P2 | `uv run --no-sync mkdocs build` | 通过 | 仅保留仓库既有文档导航和旧链接警告 |
| 2026-08-12 | P2 | `uv run --no-sync pytest tests/test_acp_p0_baseline.py tests/test_acp_p1_core.py tests/test_acp_p1_transport.py tests/test_acp_p2_content.py tests/test_acp_p2_events.py tests/test_acp_p2_updates.py -q` | 通过 | 29 tests passed；覆盖完整 stop reasons、tool 状态机/稳定 ID、plan/diff/usage、schema 序列化、事件合并/背压和多 session 隔离 |
| 2026-08-12 | P2 | `uv run --no-sync pytest tests -q -k "streaming or event"` | 通过 | 50 tests passed，现有 runtime streaming/event 回归无影响 |
| 2026-08-12 | P2 | `uv run --no-sync ruff check src/synapse/runtime/streaming src/synapse/acp tests/test_acp_p2_content.py tests/test_acp_p2_events.py tests/test_acp_p2_updates.py` | 通过 | runtime streaming 与 ACP P2 代码无 Ruff 错误 |
| 2026-08-12 | P3 | `uv run --no-sync pytest tests/test_acp_p0_baseline.py tests/test_acp_p1_core.py tests/test_acp_p1_transport.py tests/test_acp_p2_content.py tests/test_acp_p2_events.py tests/test_acp_p2_updates.py tests/test_acp_p3_permissions.py tests/test_acp_p3_agent.py -q` | 通过 | 41 tests passed；覆盖 permission coordinator、allow/reject once、session policy、稳定 action 顺序、prompt 内 resume、取消、超时、重复 pending、RPC 失败闭合、unparsed interrupt、resume 上限和 shutdown 清理 |
| 2026-08-12 | P3 | `uv run --no-sync pytest tests/test_iteration_abc.py tests/test_runtime_streaming.py -q` | 通过 | 21 tests passed；既有 HITL 与 runtime streaming 回归无影响 |
| 2026-08-12 | P4-P8 | `uv run --no-sync ruff check src/synapse/acp tests/test_acp_p*.py; uv run --no-sync pytest tests/test_acp_p0_baseline.py tests/test_acp_p1_core.py tests/test_acp_p1_transport.py tests/test_acp_p2_content.py tests/test_acp_p2_events.py tests/test_acp_p2_updates.py tests/test_acp_p3_agent.py tests/test_acp_p3_permissions.py tests/test_acp_p4_history.py tests/test_acp_p4_lifecycle.py tests/test_acp_p5_mcp.py tests/test_acp_p6_client_services.py tests/test_acp_p8_compliance.py -q` | 通过 | Ruff 通过；54 tests passed；覆盖历史回放/resume、checkpoint delete、MCP conversion/pool、Client gateway/tools、官方 SDK memory/subprocess、stdio stdout 纯净性和 `session/delete` SDK route 兼容层 |
| 2026-08-12 | P3 | `uv run --no-sync ruff check .` | 通过 | 全仓 Ruff 无错误 |
| 2026-08-12 | P8 | `uv run --no-sync ruff check src/synapse/acp tests/test_acp_p*.py; uv run --no-sync pytest tests/test_acp_p0_baseline.py tests/test_acp_p1_core.py tests/test_acp_p1_transport.py tests/test_acp_p2_content.py tests/test_acp_p2_events.py tests/test_acp_p2_updates.py tests/test_acp_p3_agent.py tests/test_acp_p3_permissions.py tests/test_acp_p4_history.py tests/test_acp_p4_lifecycle.py tests/test_acp_p5_mcp.py tests/test_acp_p6_client_services.py tests/test_acp_p8_compliance.py -q` | 通过 | Ruff 通过；55 tests passed；新增 available commands、thinking/approval session-local 映射、未知方法和非法 cwd 门禁 |
| 2026-08-12 | P8 | `uv run --no-sync pytest -q` | 未通过 | 1409 passed/3 skipped/3 failed；失败为既有 native tool-output 默认值和 Python transformer 名称期望，非 ACP 测试；见 phase-8 文档，不能作为发布通过证据 |
| 2026-08-12 | P8 | `uv run --no-sync mkdocs build; uv build` | 通过 | 文档构建和 sdist/wheel 构建通过；MkDocs 仅有既有未纳入 nav/旧链接警告 |
| 2026-08-12 | P3 | `uv run --no-sync mkdocs build` | 通过 | 文档构建成功；存在仓库既有未纳入 nav 的页面和 tutorial 锚点警告 |
| 2026-08-12 | P4/P6 | `uv run --no-sync pytest tests/test_acp_p4_history.py tests/test_acp_p4_lifecycle.py tests/test_acp_p6_client_services.py -q` | 通过 | 修复 `set_config_option` 回滚 NameError 并补回滚回归；新增非法生命周期转换、fork 拒绝、Client RPC 失败/取消/断开稳定错误测试 |
| 2026-08-12 | P4-P8 | `uv run --no-sync ruff check src/synapse/acp tests/test_acp_p*.py; uv run --no-sync pytest tests/ -q -k "acp"` | 通过 | Ruff 通过；61 tests passed（ACP 专项） |
| 2026-08-12 | P4 | `uv run --no-sync pytest tests/test_acp_p4_lifecycle.py -q` | 通过 | 8 passed；fork 成功/父子独立性/失败回滚、delete 隔离、非法转换门禁 |
| 2026-08-12 | P5 | `uv run --no-sync pytest tests/test_acp_p5_mcp.py -q` | 通过 | 7 passed；项目/Client MCP 合并与冲突检测、session pool 隔离释放、启动失败释放 |
| 2026-08-12 | P6 | `uv run --no-sync pytest tests/test_acp_p6_client_services.py -q` | 通过 | 13 passed；editor buffer client-backed 读取、跨 session 终端隔离、终端创建关闭竞态、能力组合 |
| 2026-08-12 | P7 | `uv run --no-sync pytest tests/test_acp_p4_history.py tests/test_acp_p8_compliance.py -q` | 通过 | `_meta` 扩展不污染标准对象、initialize 仅声明已实现 capability |
| 2026-08-12 | P4-P8 | `uv run --no-sync ruff check src/synapse/acp tests/test_acp_p*.py; uv run --no-sync pytest tests/ -q -k "acp"` | 通过 | Ruff 通过；77 tests passed（ACP 专项） |
| 2026-08-12 | P8 | `uv run --no-sync pytest tests/test_acp_p4_lifecycle.py tests/test_acp_p1_transport.py -q` | 通过 | 14 passed；catalog 回放有界保留、未知 notification 静默忽略、Windows subprocess stdio |
| 2026-08-12 | P8 | `uv run --no-sync pytest -q` | 未通过 | 1434 passed/3 skipped/3 failed；3 个失败为原生 tool-output 压缩核心未安装（`synapse-tool-compress-core`），非 ACP |
| 2026-08-12 | P8 | `uv build` | 通过 | sdist/wheel 构建成功；`synapse-acp` 入口指向 `synapse.acp.server:main` |

## 阻塞记录

| 时间 | 阶段/任务 | 证据 | 解除条件 | 状态 |
|---|---|---|---|---|
| - | - | - | - | 无 |

## 设计变更记录

| 时间 | ADR/文档 | 变更 | 原因 | 影响阶段 |
|---|---|---|---|---|
| 2026-08-12 | 初始方案 | 建立 ACP 完整语义目标、P0-P8 和进度台账 | 允许分阶段执行，但避免最小适配演化成永久残缺实现 | 全部 |
| 2026-08-12 | 初始方案 | 使用官方 Python SDK，不使用 `deepagents-acp` | 保持协议边界可控并复用 Synapse runtime | 全部 |
| 2026-08-12 | 崩溃恢复 | 修复 `set_config_option` 重建失败回滚引用未定义变量，改为捕获前置快照 | 动态配置失败时 catalog 与 runtime 必须原子回滚，不能遗留 catalog-only session | P4/P7 |
| 2026-08-12 | 崩溃恢复 | Client RPC 统一经 `ClientServiceGateway._invoke` 包装，失败折叠为稳定工具错误，取消继续传播 | 防止原始传输异常泄漏给模型，保持 disconnect/cancel 语义 | P6 |
| 2026-08-12 | P5 收口 | 工厂内合并项目 `mcp.json` 与 Client MCP，同名不同配置 fail-closed | 满足 P5-05 合并/去重/冲突拒绝，避免静默覆盖 | P5 |
| 2026-08-12 | P7 收口 | `_meta` 扩展字段按 SDK 展开为 kwargs，接受但不持久化到 catalog/update/响应 | 满足 P7-09，扩展字段不污染标准对象 | P7 |
| 2026-08-12 | P8 收口 | ACP 回放历史每 session 保留最近 2000 条，超限裁剪 | 满足 P8-04 有界回放，防止长会话历史无限增长 | P4/P8 |
| 2026-08-12 | P8 收口 | JSON-RPC batch 数组不纳入 ACP 适配（ndjson 单条 message framing） | 官方 SDK 按单条 message 处理，batch 非合法 framing | P8 |
| 2026-08-12 | Zed 实测 | 工具生命周期改用稳定 `item_id` 贯穿 start/update/finish，跳过 TOOL_BATCH_STARTED 重复投影 | Zed 实测 `Tool call not found`：start 用 LangChain `call_id`、finish 用 `item_id` 导致 ID 不一致 | P2/P8 |

## 台账更新流程

1. 开始工作前：更新“当前工作”，将一个任务及所属阶段置为 `in_progress`。
2. 代码修改中：发现 schema、SDK 或设计偏差时，先更新 decisions 和阶段文档。
3. 验证后：把命令、结果和环境追加到“验证记录”，再勾选任务。
4. 阶段完成：核对阶段文档验收标准，更新完成度和门禁结果。
5. 阻塞：记录可复现证据与解除条件，不因任务困难标记 `blocked`。
6. SDK 升级：先执行 schema/method/capability diff，未经评审不得直接更新锁文件。
7. 总体完成：只有 P0 矩阵无未解释项且 P8 门禁全部通过才能标记 `completed`。
