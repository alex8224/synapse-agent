# 编码 Agent 项目记忆

## 约定
- Python 依赖使用 `uv` 管理。
- 优先小步、可测试的改动。
- CLI 输出简洁，不使用 emoji。
- 默认关闭人工审批（自动放行）；除非用户要求，不要自行加审批门槛。
- Backend 仅使用 `LocalShellBackend`，无远程 sandbox。
- 对用户回复与模型思考过程使用中文；代码标识符/路径/命令可保留原文。

## 常用命令
- 安装依赖：`uv sync`
- 运行 CLI：`uv run synapse ...`
- 测试：`uv run pytest`
- 检查：`uv run ruff check .`

## 安全注意
- 除非用户明确要求，不要 force-push 或 hard-reset。
- 不要打印 `.env` 中的密钥。
