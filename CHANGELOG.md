# Changelog

All notable changes to this project are documented in this file.

Each release section starts with `## v{version}` and ends before the next `## ` heading.
The release workflow automatically extracts the matching section as release notes.

---

## v0.1.23

### 新增功能

- Rich Markdown 渲染接入 Synapse 主题系统：内置主题提供完整 Markdown 样式映射，支持标题、段落、强调、行内代码、引用、链接、列表和表格等元素跟随主题配色。
- `themes.json` 支持通过独立的 `markdown` 字段覆盖 Markdown 元素的 Rich style，并支持主题继承与分层配置。
- 运行时切换主题会重绘已显示的答案、思考内容和独立 Markdown 块；主题设计器保存主题时保留 Markdown 样式配置。

### 修复

- 修复 Rich Markdown 使用内置默认颜色、导致 Markdown 内容无法跟随 Synapse 主题切换的问题。
- 修复主题设计器保存已有主题时可能丢失手写 Markdown 样式覆盖的问题。

### 工程改进

- 增加 Markdown 主题解析、Rich 样式注入、主题继承、运行时重绘和内置主题完整性测试。
- 补充 Markdown 主题配置文档，并保留代码块使用 `code_theme` 控制语法高亮的边界说明。

---

## v0.1.22

### 新增功能

- TUI 恢复会话时改为分页加载：启动只渲染最近 `AGENT_HISTORY_TAIL_TURNS` 轮（默认 20），滚动到 transcript 顶部时异步加载更早历史并保持滚动位置，避免超长会话导致启动卡顿。
- 新增 `AGENT_HISTORY_TAIL_TURNS` 设置，控制 TUI 启动时初始渲染的最近可见会话轮数（支持环境变量与分层 `settings.json`）。

### 修复

- 修复历史分页异步加载的请求代际竞争：切换/重新加载会话后，旧分页 worker 的回调不再清除当前请求的加载状态或插入过期数据。

### 工程改进

- 直接依赖版本固定到 `uv.lock` 对应版本。

---

## v0.1.21

### 新增功能

- 会话工具 `list_sessions` 重构为 `search_session`：新增本地增量全文索引（`SessionSearchIndex`），除标题/摘要/模型外支持会话消息全文双路命中；`query` 为空时列出最近会话，支持 `limit`/`offset` 分页与摘要。
- `read_session` 支持 `include_tools`（默认去掉工具消息，输出体积最多缩小 13 倍）与 `offset`/`limit` 轮次分页。
- 新增原生 `patch` 工具，`read_file`/`edit_file`/`patch` 全部路由到 `synapse-core-tool` 编码保持的原生实现。
- 新增 `AGENT_EXPAND_THINKING` 设置，控制 reasoning 块的展开/收起。

### 修复

- 修复 `load_messages_from_checkpointer` 只读最新 checkpoint 导致新版 SqliteSaver（delta 存储）下读不到消息：改为 delta 重建优先、旧逻辑兜底。
- 修复 `search_session` 索引库并发写锁竞争（`database is locked`）：WAL + busy_timeout + 同步降级容错。
- 修复 JSON 类型检测晚于 LOG、无法识别括号风格日志的问题。
- 修复长对话框行标签被裁剪的问题。

### 工程改进

- 文件系统核心由 `synapse-search-core` 迁移并更名为 `synapse-core-tool`（新增原生 read/edit/patch），CI workflow 同步迁移为 `native-core-tool-wheels.yml`。
- 隐藏 DeepAgents 内置 `ls`/`glob`/`grep`，新增 prompt 中间件注入权威文件工具指引。
- 主 TUI 欢迎 logo 动画时机调整。
- 清理 `list_sessions` 旧名残留（docstring、错误提示、prompts、文档）。

---

## v0.1.20

### 新增功能

- LLM Debug Inspector 支持采集并展示原始 HTTP 请求与响应 payload，便于排查模型传输问题。
- 主 TUI 内联展示子 Agent 运行状态，并根据任务复杂度动态选择 DAG 并行执行路径。
- 系统提示固定注入当前 shell 语法规则，减少 PowerShell、Bash 与 cmd 命令混用。

### 修复

- 完善 `search_files` 的 ripgrep-compatible pattern 示例、参数边界和 `glob` include-only 语义；当 native include-glob 异常返回空结果时，使用同一 Rust core 枚举候选并降级搜索，避免合法 `*.py` / `**/*.py` 过滤导致漏报。
- 修复 Responses API、Anthropic thinking block 等多种 reasoning 内容提取与流式显示问题，并避免 reasoning 后重复输出答案。
- 为 HTTP 客户端启用 SOCKS 代理支持，修复相关代理配置不可用问题。
- 未配置 Codex OAuth profile 时隐藏用量标签，并修正重置弹窗样式。

### 工程改进

- 增加 `search_files` StructuredTool 到 Rust native core 的完整 glob 回归测试，并校验对外正则示例可由实际 matcher 编译。
- 更新 Agent 装配、流式 UI、HTTP transport、提示注入和子 Agent 状态相关测试。

---

## v0.1.19

### 新增功能

- Codex OAuth 用量底部栏组件：展示 5h/1d 用量窗口、重置剩余时间和账号到期时间；低于 50% 时红色显示。
- Codex 速率限制重置能力：直接通过 HTTP 请求 wham/rate-limit-reset-credits 读取可用重置次数与到期详情，支持在弹窗中一键消费重置。
- /codex reset、/codex credits 命令打开重置详情弹窗；底部栏 Codex 区域支持 hover 高亮与点击。
- 启动时配置错误友好提示：models.json、settings.json 或内联 JSON 环境变量格式错误时输出简洁错误与修复提示，不再抛出完整 traceback。

### 修复

- /compact 改为后台 worker 执行，避免模型摘要阻塞 TUI；执行期间禁止取消，防止压缩状态损坏。
- 兼容 LangChain 1.3 编译图闭包中的 SummarizationMiddleware 定位。

---

## v0.1.18

### 新功能

- 新增 LLM Debug Inspector（`F11`），实时监控模型通信、工具调用和 token 消耗。
- Inspector 支持采集开关、跟随最新、按类型筛选（异常/工具/慢调用）、回合折叠和调用详情查看。
- Inspector 概览栏显示失败率（基于工具级错误检测），工具标签页展示失败工具及原因。

### 修复

- TUI：`F10` 恢复删除 session 弹框入口；修复鼠标选中与点击复制的冲突，拖选后自动复制。

### 工程改进

- `DebugCaptureRecord` 增加工具级错误检测（LangChain `ToolMessage.status` + 内容模式）。
- `_tool_pairs` 返回 `error` 字段，区分 "等待中"（result null）与 "真失败"（有错误内容）。
- Inspector 前端：失败率仅统计真正失败的工具，"待响应"不计入。

---

## v0.1.17

### 新功能

- `find_files` / `search_files` 工具新增 `context_lines`、`case_insensitive`、`head_limit`、`offset` 参数。
- `search_files` 支持忽略大小写（`case_insensitive`，由 `synapse-search-core` 原生引擎实现）。
- 支持分页查询（`head_limit` + `offset`），Agent 可按需翻页而非一次性获取全部结果。

### 工程改进

- 工具的 Pydantic schema 不再定义 `intent` 字段，改由 `build_intent_schema_middleware` 中间件统一管理。
- 新增 Synapse 自有文件搜索工具 `find_files` / `search_files`，排除 deepagents 内置 `ls`/`glob`/`grep` 工具。
- 系统提示词中的 `glob`/`grep` 工具名修正为 `find_files` / `search_files`。
- `synapse-search-core` 升级至 0.1.1（新增 `case_insensitive` 参数）。

---

## v0.1.16

### 新功能

- 新增必需的 `synapse-search-core` 原生搜索核心，使用 Rust ripgrep crates 提供正则 `grep` 和 `glob`。
- `grep`/`glob` 改为使用内置原生引擎，不再依赖宿主机 `rg` 或 DeepAgents Python 搜索回退。
- 原生搜索 wheel 发布到 PyPI，支持 Windows x86_64、Linux x86_64/aarch64 和 macOS Apple Silicon arm64。

### 工程改进

- 保留 Python 后端的工作区路径授权、虚拟路径映射和 `deny_paths` 过滤。
- 增加原生搜索 wheel 构建与 PyPI Trusted Publishing 工作流，以及对应的后端回归测试和分发文档。

---

## v0.1.15

### 工程改进

- 拆分 TUI transcript、工具组、待办清单、用户消息和 turn rail widget，缩小 `tui.py` 的职责范围。
- 保留 `synapse.ui.tui` 的既有组件、格式化函数和 timeline 符号兼容导出。
- 保持动态主题、流式展示、文本选择、复制与 turn rail 交互行为，并覆盖相关 TUI 回归测试。

---

## v0.1.14

### 工程改进

- PyPI 项目页增加主页、源代码仓库、问题追踪和变更日志链接。

---

## v0.1.13

### 新功能

- 支持通过 PyPI Trusted Publisher 自动发布 `synapse-cli-agent` 分发包。

### 工程改进

- 安装文档增加无需克隆仓库的 PyPI 安装方式；`uv` 可自动管理所需 Python 版本。

---

## v0.1.2

### 修复

- 修复 `synapse-tool-compress-core` manylinux2014 wheel 中 tree-sitter 的 `le16toh` / `be16toh` 未解析符号，确保原生扩展可导入。

### 工程改进

- 原生 wheel 构建固定 manylinux2014 兼容目标，并增加安装导入冒烟测试。
- 同步 Rust crate 与 Python wheel 的发布版本为 `0.1.2`。

---

## v0.1.11

### 新功能

- F5 MCP Tools 面板支持按 `d` 临时切换当前选中 MCP server 的启用状态，并自动重建 agent；该状态不写入 `mcp.json`
- 工具输出路径压缩增加更清晰的统计与诊断展示，优化压缩处理路径

### 修复

- 修复会话切换与删除快捷键的职责冲突

### 工程改进

- 扩充 `AGENTS.md` 的仓库结构、架构约束、测试和发布协作规范

---

## v0.1.10

### 工程改进

- 重组应用、命令、运行时、内容与会话等核心领域模块，收紧模块职责与依赖边界
- 拆分工具输出管道为模型、仓储、检测和变换层，保留既有公共 API
- 将 slash 命令按压缩、会话、MCP、模型和主题职责拆分，保留统一分发入口
- 拆分流处理为渲染、事件归一化与运行时迭代层，兼容现有 CLI 和 TUI 调用路径
- 提取 TUI turn rail 和用户 turn 格式化逻辑，降低主应用模块复杂度
- 拆分模型配置解析、Profile 与 settings/能力辅助逻辑，保持 provider 工厂和 mock 契约稳定

---

## v0.1.9

### 新功能

- 紧凑工具描述中间件：替换上游冗长的工具 schema 描述（~4K chars → ~200 chars），减少 token 开销
- Cache-aware 压缩控制面：实时追踪 provider 缓存命中/写入，区分缓存输入与新输入
- 压缩诊断面板：profile-driven 内容分解与优化机会排序（TUI `Ctrl+D`）
- 请求账本（interaction ledger）：turn 级与 model-call 级关联追踪
- Topbar 实时压缩指示器：显示活跃 zone 与压缩状态
- `/tool-output` slash 命令：查看工具输出变换统计
- GitSummaryTransformer：识别 `git status`/`git diff --stat` 输出并智能摘要

### 修复

- 避免 search 内容检测误判为 critical-line 回退
- 修复 Alt+C 复制崩溃

### 工程改进

- 默认路径从 `.coding-agent` 迁移至 `.synapse`（checkpoint / sessions / memory）
- 工具输出压缩阈值降至 512 bytes
- CI native compression wheel 发布到 GitHub Release（替代 PyPI）

---

## v0.1.8

### 新功能

- 工具输出变换管道：大型工具结果自动归档到 JSONL journal，替换为边界预览 + `tool-result://` 引用，避免撑爆 LLM 上下文
- Rust 原生压缩核心 (`synapse-tool-compress-core`)：智能摘要引擎 SmartCrusher，支持 code/diff/log/search 专用压缩器，BM25 相关性排序，adaptive sizer
- CLI `--diagnose-tool-output` 诊断标志：查看每次工具输出的变换统计
- F4 多选删除 session + F5 MCP 分组折叠 + 多选 UI 统一

### 修复

- MCP deferred 状态不再错误显示为 "mcp err"

### 工程改进

- `tool_results.py` 重构为 `tool_output.py` + `tool_output_middleware.py`，职责更清晰
- 新增 `tool_output_eval.py` 评估框架
- CI 新增 native-compression-wheels workflow 用于构建 Rust wheel

---

## v0.1.7

### 新功能

- 斜杠命令 TUI 输出升级为 Markdown 渲染，会话/MCP/主题等数据使用 Rich 表格展示
- AgentMdMiddleware：将 `AGENTS.md` 静态注入 system prompt，与 memory 解耦
- MCP per-tool 过滤：支持 UI 勾选工具并持久化到配置

### 修复

- 全新 session 输入斜杠命令不会在 TUI 显示内容（welcome 页面遮挡 `#log`）
- shell 默认值平台感知：非 Windows 用 `bash` 替代 `pwsh`
- prompt_border 校验 Textual 白名单，新增 `panel` 样式支持

### 工程改进

- 添加 MkDocs + GitHub Pages 文档站点
- subagents 和 memory 默认关闭，精简冗余 middleware prompt 块
- UI：steer 更名为 queue，简化 bottombar mode 标签

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
