# S2 生命周期专项验证记录

本轮新增 `tests/test_runtime_service_lifecycle.py`，共 25 个确定性用例，使用 `asyncio.run`、controlled runtime、Event 与 `asyncio.wait_for` 覆盖：

- DTO frozen/slots/pure-data、command id、Protocol/export/error code 与 service import guard；
- open 幂等、同 ref 并发创建、close/reopen generation、manager 缺失/项目不匹配/closed/binding；
- cancel/steer fencing、取消原因、幂等、队列深度、cancelling 冲突、旧 turn id 隔离；
- listener 重入、settlement 清理、close join/重复 close、reservation/queued/running/cancelling/settling 冲突；
- active close 等待 future/persistence/goal settlement、queued owner、外部取消、shutdown 资源清理、直接 `SessionRuntime.close` 兼容性与普通 `RuntimeError` 原样传播。

首轮失败均为测试 fake/测试同步问题：custom `session_factory` 未接受 manager 注入的旧 kwargs、queued session 观察时机、close claim 与 controlled future 的 barrier，以及错误的测试导入；已在测试侧修正。没有新增产品代码修复。

真实结果：

- `uv run --no-sync pytest tests/test_runtime_service_lifecycle.py -q`：25 passed；
- 核心四文件回归：143 passed；
- runtime hardening/agent turn/streaming/project runtime：67 passed；
- 变更 Python Ruff：通过；
- `git diff --check`：通过。

最终门禁：

- `uv run --no-sync pytest -q`：1935 passed, 1 skipped；
- `uv run --no-sync mkdocs build --strict`：exit 0；仅报告仓库既有未纳入 nav 的页面与 tutorial 锚点 INFO，另有 Material 版本提示，未阻断构建。

主流程独立复核（同样结果）：

- S2 专项：25 passed；核心回归：143 passed；
- 全量：`1935 passed, 1 skipped in 181.37s`；
- Ruff 与 `git diff --check`：通过；
- `uv run --no-sync mkdocs build --strict`：exit 0。

因此 S2 已按门禁改为 `completed`。未修改产品代码；未实现 S3+，未迁移 CLI/TUI/ACP。
