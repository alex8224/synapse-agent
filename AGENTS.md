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

## 发布流程

推送前必须询问用户：**"本次是否需要打 tag 发 Release？"**

如果用户确认需要发布，按以下步骤一条龙完成：

1. 确认版本号（读取 `pyproject.toml` 中的 `version`，用户可覆盖）
2. 更新 `pyproject.toml` 中的版本号（如需要）
3. 提交：`git add pyproject.toml && git commit -m "release: bump to v{version}"`
4. 运行 `powershell -ExecutionPolicy Bypass -File scripts/release.ps1` 打 tag 并推送
5. GitHub Actions 自动构建 wheel、生成发布说明、创建 Release

如果用户说不需要发布，直接 `git push` 即可。
