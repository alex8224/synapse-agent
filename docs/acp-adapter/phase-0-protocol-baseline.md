# P0：协议基线与能力矩阵

> 状态：Completed（2026-08-12）  
> 前置条件：总体方案已确认。  
> 后续阶段：P1 核心传输与会话。

## 1. 目标

在不修改生产行为的前提下，锁定官方 SDK 和 ACP v1 schema，建立完整能力矩阵、协议测试 harness 和实现护栏。

## 2. 非目标

- 不新增 `synapse-acp` 入口。
- 不实现任何 ACP handler。
- 不修改 runtime 行为。

## 3. 工作范围

- 验证 PyPI 包、官方仓库、许可证和 Python 版本兼容性。
- 精确锁定 SDK，并记录 wire/schema 版本。
- 从生成 schema 和 `acp.Agent`/`acp.Client` 接口提取方法与类型。
- 建立 methods、capabilities、ContentBlock、SessionUpdate、stop reason、错误和 Client service 矩阵。
- 建立 SDK 驱动的内存/stdio 测试 harness 和 golden fixtures。
- 固化 stdout 污染、包依赖方向和敏感字段护栏。

## 4. 产物

- 依赖与 lockfile 变更。
- `docs/acp-adapter/capability-matrix.md`。
- `tests/acp/` 基础 harness、fixtures 和协议基线测试。
- SDK/schema 版本记录及升级步骤。

## 5. 门禁

- 矩阵覆盖锁定 schema 的所有稳定方法、capability 和联合类型。
- 每个条目都有目标阶段和验收方式，不存在“待以后调查”的空项。
- 测试不依赖网络或真实模型。
- 故意向 stdout 输出非协议文本时测试失败。
- 故意从 runtime 导入 `acp` 时依赖护栏失败。

## 6. 风险与回滚

- 风险：SDK 文档与发布包不一致。缓解：以安装包源码和生成 schema 为准。
- 风险：过早锁定过时版本。缓解：记录选择依据，升级必须做矩阵 diff。
- 回滚：P0 只增加依赖、测试和文档，可独立回退。

## 7. 实际结果

- `agent-client-protocol==0.12.0` 已写入 `pyproject.toml` 和 `uv.lock`。
- 本地确认 `acp.PROTOCOL_VERSION == 1`，生成 schema 为 `schema-v1.19.0`（见 `acp.meta`）。
- 已建立 `docs/acp-adapter/capability-matrix.md`。
- 已建立 `tests/fixtures/acp/initialize.jsonl` 和 `tests/test_acp_p0_baseline.py`。
- P0 测试结果：7 passed。
- P0 门禁通过，后续 handler 实现进入 P1。
