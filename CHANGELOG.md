# Changelog

All notable changes to this project are documented in this file.

Each release section starts with `## v{version}` and ends before the next `## ` heading.
The release workflow automatically extracts the matching section as release notes.

---

## v0.1.6

### 新功能

- 支持 OpenAI Responses API WebSocket 传输，降低延迟
- Welcome 页面动画重构：左到右打字光标出现 + 斜扫逐点删除循环
- Braille Logo 点阵逐点显隐动画，仅用 muted/fg 两种主题色无中间杂色
- 打字光标效果：新字符短暂高亮 accent 后降为 fg
- `prompt_border` 字段支持主题自定义输入框边框样式（tall/heavy/dashed/dotted/double/round/solid）
- 后端 glob/grep 工具自动跳过 `.gitignore` 匹配路径

### 修复

- WebSocket：握手前刷新异步 API key，避免网关 401
- WebSocket：关闭 ping timeout，防止推理期间误断连
- WebSocket：过滤 Chat Completions 专用 `thinking` 字段
- TUI：主题设计器 backdrop 完全透明
- TUI：修复 `_open_theme_designer` 缺失回调
- TUI：移除 `_save_theme` 重复 `apply_theme` 调用，避免 UI 卡死
- TUI：git changes popover 重新挂载避免 DuplicateIds 异常
- SteerQueue：修复可重入死锁和 graph 重建后队列丢失
- 修复子 agent 工具调用的时间线渲染
- 修复 alt-v 多行粘贴被截断

### 性能

- 纯异步 model clients，消除同步 OpenAI 客户端的阻塞
- 加速模型切换和 shutdown 流程

### 工程

- 移除 cancel-seal 诊断日志输出
- SteerQueue 在活跃 turn 期间保持可见
- 简化 agent 工具表面，减少不必要的工具暴露
- 上调模型瞬时故障重试上限

---

## v0.1.5

### 新功能

- 新增独立识图服务：非多模态主模型可通过 `vision_model` 配置独立图片识别服务，自动将图片转为文字描述后交给主模型处理
- 支持 `models.json` / `settings.json` 中配置 `vision_model`，可自由更换任意 OpenAI-compatible 识图服务（Qwen-VL 等）
- 识图服务支持独立 `think` 开关（不影响主模型思考模式）、`allow_remote_urls` 安全策略、超时重试和 fallback 模型
- 自动推断主模型是否原生支持图片输入（按 provider/model 名匹配），支持 `image_input` 显式覆盖

### 修复

- 修复 mermaid / git-explore 调用卡死问题
- 修复 Windows 下 git 输出编码导致的乱码问题

### 工程

- 新增 `vision_middleware`、`describe_image` 模块
- 新增识图服务测试和 API 检查脚本

---

## v0.1.4

### 新功能

- 新增 Codex session 的只读发现、预览和导入，支持 CLI 与 TUI picker
- 导入使用终态 checkpoint seeding 与 ledger，支持幂等重用和崩溃恢复

### 修复

- 修复 state DB 过期、空 thread、Windows 扩展路径、长 metadata header 导致 Codex 历史缺失的问题
- 支持 `subagent.thread_spawn` 子代理会话，并按首条用户消息生成 picker 与导入标题
- 对可恢复的模型服务 5xx 故障增加退避重试，并向 TUI 显示重试状态

### 工程

- 扩充 Codex discovery、import、TUI 和 retry 回归覆盖

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
