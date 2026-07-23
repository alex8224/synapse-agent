# Changelog

All notable changes to this project are documented in this file.

Each release section starts with `## v{version}` and ends before the next `## ` heading.
The release workflow automatically extracts the matching section as release notes.

---

## v0.1.3

### 修复

- 修复 49 个 ruff lint 错误（E501 超长行、UP042 StrEnum、F401/F811 未使用导入、I001 导入排序）

### 工程

- CI 仅对 PR 触发，避免 push tag 时与 Release workflow 重复构建

---

## v0.1.2

### 修复

- Release workflow 中 CHANGELOG 提取脚本误将 shell 变量当 Python 变量，改用 `os.environ` 读取

---

## v0.1.1

### 工程

- 新增 `CHANGELOG.md`，发布说明从此文件对应版本段落自动提取
- 修复 `release.ps1`：打 tag 时同步推送分支提交，避免 tag 到了代码没跟上
- 更新 `AGENTS.md` 发布流程：AI 自动分析变更、写入 changelog 条目

---

## v0.1.0

初版发布。基于 LangChain Deep Agents 的本地 AI 编码 Agent。

### 新功能

- 自主编码闭环：读改代码、执行命令、运行测试、Git 操作
- 子代理协作：内置 researcher / tester / reviewer，任务自动拆解并行执行
- MCP 协议支持，接入外部工具生态
- 多模型切换：OpenAI / Anthropic / DeepSeek / 任意 OpenAI-compatible 网关
- TUI 终端界面（Textual）：斜杠命令补全、实时流式输出、快捷键
- CLI 命令行：`run` / `chat` / `tui` / `sessions` / `models` / `mcp` / `version`
- 分层配置：用户全局 + 项目本地，密钥写入 models.json
- Skills 系统：Agent Skills 可复用能力单元
- 会话管理：SQLite checkpointer，支持导出
- TUI 文本选择与复制、mermaid 渲染、Git Explore
- 自适应顶栏与底栏，模型/MCP 状态显示

### 修复

- Windows subprocess timeout 管道卡死问题
- TeXicode 解析错误污染最终回答
- Textual DiffView 卸载后样式缓存泄漏
- stream_chunk_timeout 默认关闭，避免长思考被掐断

### 工程

- uv 依赖管理，Python 3.12+
- GitHub Actions：CI（lint + test）和 Release（自动构建 wheel）
- 一键发布脚本 `scripts/release.ps1`
