# Synapse Agent 完全教学指南

> 目标读者：新手小白，从零掌握本项目全部流程与细节，能独立开发新 Agent 应用。
> 前置知识：会 Python 基础语法、用过命令行即可。
> 预计学习时间：逐节阅读 + 动手实践，约 4-6 小时。

---

## 目录

1. [先导：这个项目是什么](#1-先导这个项目是什么)
2. [技术栈逐层解析](#2-技术栈逐层解析)
3. [项目文件结构全览](#3-项目文件结构全览)
4. [第一条执行链路：`synapse run` 全流程](#4-第一条执行链路synapse-run-全流程)
5. [配置系统详解](#5-配置系统详解)
6. [Agent 创建：`build_coding_agent` 内部解密](#6-agent-创建build_coding_agent-内部解密)
7. [工具系统：Agent 的手和脚](#7-工具系统agent-的手和脚)
8. [子 Agent 系统：分工协作的团队](#8-子-agent-系统分工协作的团队)
9. [会话与记忆：让 Agent 拥有"记忆"](#9-会话与记忆让-agent-拥有记忆)
10. [Skills 技能系统：给 Agent 装"插件"](#10-skills-技能系统给-agent-装插件)
11. [TUI 界面：终端里的可视化控制台](#11-tui-界面终端里的可视化控制台)
12. [中间件体系：Agent 的"管道"](#12-中间件体系agent-的管道)
13. [MCP 集成：连接外部工具](#13-mcp-集成连接外部工具)
14. [安全与权限：保护你的电脑](#14-安全与权限保护你的电脑)
15. [实践：如何从零开发一个新 Agent 应用](#15-实践如何从零开发一个新-agent-应用)
16. [附录：项目约定与命令速查](#16-附录项目约定与命令速查)

---

## 1. 先导：这个项目是什么

### 1.1 一句话定义

Synapse 是一个**本地编码 Agent**——它就像一个住在你终端里的 AI 程序员。你告诉它要做什么（比如"帮我修复这个 bug"），它会自己去读代码、改文件、跑测试，然后把结果告诉你。

### 1.2 它和 ChatGPT / Claude 网页版有什么区别？

| 对比维度 | ChatGPT 网页版 | Synapse |
|----------|---------------|---------|
| 运行位置 | 云端 | 你的电脑 |
| 操作权限 | 只能聊天 | 可以读文件、改文件、执行命令 |
| 上下文 | 手动粘贴代码 | 自动读取整个项目 |
| 工作流 | 你告诉它，它回答 | 你告诉它，它自己做、自己验证 |

### 1.3 三种使用模式

```bash
# 模式1：TUI 交互界面（最推荐新手使用）
synapse tui -w /path/to/your/project

# 模式2：单次任务执行
synapse run "总结当前项目结构并生成 README" -w /path/to/project

# 模式3：命令行对话
synapse chat -w /path/to/project
```

---

## 2. 技术栈逐层解析

### 2.1 全景图

用盖房子的比喻来理解这个技术栈：

```
第5层: Synapse（我们的项目）          ← 你装修好的房子
第4层: deepagents.create_deep_agent   ← 精装房（水电/墙面/厨卫已做好）
第3层: langchain.agents.create_agent  ← 毛坯房（只有框架）
第2层: LangGraph                       ← 钢筋混凝土框架
第1层: LangChain                        ← 砖块/水泥等原材料
```

### 2.2 每一层做什么

**第1层：LangChain** —— 原材料的仓库

```python
# LangChain 提供了"模型统一接口"，不管用 OpenAI 还是 Anthropic，
# 调用方式都一样
from langchain.chat_models import init_chat_model

model = init_chat_model("openai:gpt-4.1")  # 一行代码拿到模型
response = model.invoke("Hello")            # 同样方式调用
```

**第2层：LangGraph** —— 让 Agent 有"记忆"和"流程"

```python
# LangGraph 做三件事：
# 1. 状态管理：Agent 每一步看到什么、生成了什么，都存下来
# 2. 流式输出：一边生成一边显示，不用等全部完成
# 3. 检查点：中断/恢复对话，就像游戏存档
```

**第3层：`create_agent`** —— 最小 Agent 框架

```python
from langchain.agents import create_agent

agent = create_agent(
    model=model,
    tools=[read_file, write_file, execute],  # 给它工具
)
# 这就是最小的 Agent：模型 + 工具 = Agent
```

**第4层：`create_deep_agent`** —— 适合 Coding 的完整 Agent

```python
from deepagents import create_deep_agent

# Deep Agents 在 create_agent 基础上预置了：
# - write_todos：任务规划
# - read_file / write_file / edit_file / glob：文件操作
# - execute：命令执行
# - task：子 Agent 委派
# - 自动上下文压缩（对话太长时自动总结）
```

**第5层：Synapse** —— 我们的定制层

在 Deep Agents 之上，我们添加了：
- 自定义系统提示词（中文、规范、工作流）
- 三种子 Agent（researcher / tester / reviewer）
- 会话查阅工具（跨会话引用）
- MCP 工具集成
- Textual TUI 界面
- 分层配置系统
- 安全检查
- 模型注册表（多模型切换）

### 2.3 为什么选这个技术栈？

一句话：**不要重新发明轮子**。

- LangChain 的 Deep Agents 已经把 coding agent 的核心能力（文件读写、命令执行、子 Agent、上下文压缩）打包好了
- 我们只需要在上面做定制：换提示词、加工具、做界面
- 如果从零写，这些能力至少需要几千行代码和几个月调试

### 2.4 系统全景架构图

了解 Synapse 内部模块如何协作，对后续学习至关重要。这是一张"总览图"：

```
                           用户 (终端)
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         CLI / TUI 入口层                             │
│                                                                     │
│  synapse (Typer)                                                    │
│  ├─ tui         → CodingAgentApp (Textual 全屏 TUI)                 │
│  ├─ run         → 单次任务执行                                       │
│  ├─ chat        → 命令行对话                                         │
│  ├─ sessions    → 会话管理（list/delete/rename/export/import）       │
│  ├─ models      → 模型管理（list）                                   │
│  └─ mcp         → MCP 管理（list/test）                              │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        配置层 (config.py)                            │
│                                                                     │
│  加载顺序: 代码默认 → ~/.synapse/ → .synapse/ → CLI 参数             │
│  ├─ models.json (模型 API key + profile)                            │
│  ├─ mcp.json (外部工具服务器)                                        │
│  ├─ settings.json (通用配置)                                         │
│  ├─ themes.json (UI 主题)                                           │
│  └─ system_prompt.md (系统提示词)                                    │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Agent 工厂 (agent.py)                             │
│                                                                     │
│  build_coding_agent()  ── 装配流水线 ──→  create_deep_agent()       │
│                                                                     │
│  装配步骤:                                                           │
│  ① 安全配置 → ② 后端构建 → ③ 模型构建 → ④ 工具排除 →                │
│  ⑤ 审批中断 → ⑥ 检查点 → ⑦ 子 Agent → ⑧ 权限 →                     │
│  ⑨ 自定义工具 → ⑩ MCP 工具 → ⑪ 中间件链 → ⑫ 创建图                │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
            ┌──────────────────┼──────────────────┐
            ▼                  ▼                  ▼
┌───────────────────┐ ┌───────────────┐ ┌───────────────────┐
│   中间件链 (onion) │ │  后端 (手脚)  │ │  模型 (大脑)      │
│                   │ │               │ │                   │
│ ModelRetry        │ │ LocalShell    │ │ ModelRegistry     │
│  → IntentSchema   │ │  Backend      │ │  ├─ OpenAI        │
│  → MemoryInjection│ │  ├─ execute   │ │  ├─ Anthropic     │
│  → PlanTracking   │ │  ├─ read_file │ │  └─ OpenAI兼容    │
│  → LLM API        │ │  ├─ write_file│ │ (DeepSeek/网关)   │
│  → PathNormalize  │ │  ├─ edit_file │ │ LRU 缓存 (≤8)    │
│  → ToolError      │ │  ├─ glob      │ │ 独立连接池        │
│  → TaskNamespace  │ │  └─ grep      │ │ 视觉模型独立路由  │
└───────────────────┘ └───────────────┘ └───────────────────┘
            │                  │                  │
            └──────────────────┼──────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       LangGraph 引擎                                │
│                                                                     │
│  状态图 (StateGraph) → 自动 checkpoint → SQLite 持久化              │
│  流式输出 (astream_events) → UI 实时渲染                             │
│  中断/恢复 (interrupt) → HITL 人工审批                               │
└─────────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      扩展能力层                                      │
│                                                                     │
│  ├─ Skills (SKILL.md)    → 过程性知识手册                            │
│  ├─ RAG 知识库           → 项目文档语义搜索                          │
│  ├─ 长期记忆 (LTM)       → SQLite + 向量嵌入，自动提取经验           │
│  ├─ 子 Agent (researcher/tester/reviewer) → deepagents 原生委派      │
│  ├─ MCP 工具             → 外部工具服务器注入                        │
│  ├─ 会话管理             → sessions.sqlite + checkpoints.sqlite      │
│  └─ Steer 引导           → 运行时用户中途注入指令                    │
└─────────────────────────────────────────────────────────────────────┘
```

**关键数据流**：用户输入 → CLI/TUI → 配置加载 → Agent 装配 → 模型思考 → 工具执行 → 结果流式返回。

---

## 3. 项目文件结构全览

```
synapse-agent/                       # 项目根目录
│
├── pyproject.toml                   # 【核心】项目配置：依赖、入口点、工具配置
├── uv.lock                          # 依赖锁文件（类似 package-lock.json）
├── .python-version                  # Python 版本要求：3.12
├── .env                             # 环境变量（API Key 等，不提交 git）
├── AGENTS.md                        # 【核心】Agent 记忆文件：用户信息、项目约定
├── README.md                        # 项目说明
├── CHANGELOG.md                     # 版本更新记录
├── synapse.cmd                      # Windows 启动脚本（薄封装）
│
├── src/synapse/                     # 【核心】所有源代码
│   ├── __init__.py                  # 惰性加载入口
│   ├── __main__.py                  # python -m synapse 入口
│   ├── cli.py                       # 【核心】CLI 命令定义（Typer）
│   ├── agent.py                     # 【核心】Agent 组装工厂
│   ├── harness.py                   # Harness 配置（工具排除）
│   ├── config.py                    # 【核心】Settings 配置模型
│   ├── config_paths.py              # 分层配置路径解析
│   ├── backends.py                  # LocalShellBackend 实现
│   ├── prompts.py                   # 系统提示词
│   ├── middleware.py                # 中间件集合
│   ├── subagents.py                 # 【核心】子 Agent 定义
│   ├── models_registry.py           # 多模型注册表
│   ├── llm_openai_compat.py         # OpenAI 兼容层扩展
│   ├── llm_openai_websocket.py      # WebSocket 连接支持
│   ├── mcp_client.py                # MCP 客户端
│   ├── sessions.py                  # 会话元数据管理
│   ├── safety.py                    # 安全配置
│   ├── fs_permissions.py            # 文件系统权限
│   ├── hitl.py                      # 人工审批（Human-In-The-Loop）
│   ├── context_compact.py           # 上下文压缩
│   ├── skills_catalog.py            # 技能目录
│   ├── tools/                       # 自定义工具
│   │   ├── __init__.py
│   │   └── session_tools.py         # 跨会话查阅工具
│   ├── memory/                      # 记忆系统
│   │   ├── __init__.py
│   │   ├── auto_recorder.py         # 自动记录
│   │   ├── embedder.py              # 向量嵌入
│   │   └── long_term.py             # 长期记忆
│   ├── planner/                     # 规划器
│   │   ├── __init__.py
│   │   └── task_planner.py          # 任务规划
│   ├── rag/                         # RAG 检索增强
│   │   ├── __init__.py
│   │   └── knowledge_base.py        # 知识库
│   └── ui/                          # 【核心】TUI 界面
│       ├── tui.py                   # TUI 主应用（继承 Textual App）
│       ├── stream.py                # 流式输出渲染
│       ├── timeline.py              # 时间线视图（工具调用/思考/回答）
│       ├── sink.py                  # 输出接收器抽象
│       ├── welcome.py               # 启动动画（SYNAPSE 大字）
│       ├── theme.py                 # 主题系统
│       ├── selectable_text.py       # 可选择文本
│       ├── steer_widget.py          # 引导控件
│       ├── topbar/                  # 顶栏
│       │   ├── core.py              # 顶栏核心
│       │   ├── context.py           # 上下文状态
│       │   ├── git_chrome.py        # Git 分支装饰
│       │   ├── git_changes_popover.py # Git 变更弹窗
│       │   └── components/          # 顶栏组件
│       │       ├── branch.py        # 分支显示
│       │       ├── title.py         # 标题显示
│       │       ├── usage.py         # Token 使用量
│       │       └── workspace.py     # 工作区路径
│       ├── bottombar/               # 底栏
│       │   ├── core.py              # 底栏核心
│       │   ├── context.py           # 上下文状态
│       │   └── components/          # 底栏组件
│       │       ├── key_hints.py     # 快捷键提示
│       │       ├── mcp.py           # MCP 状态
│       │       ├── mode.py          # 模式显示
│       │       ├── model.py         # 当前模型
│       │       └── thread.py        # 会话 ID
│       ├── dialogs/                 # 弹窗
│       │   ├── base.py              # 弹窗基类
│       │   ├── session_list.py      # 会话列表
│       │   ├── codex_session_list.py # Codex 会话列表
│       │   ├── model_picker.py      # 模型选择器
│       │   ├── mcp_panel.py         # MCP 面板
│       │   ├── safety_panel.py      # 安全面板
│       │   ├── theme_picker.py      # 主题选择器
│       │   ├── theme_designer.py    # 主题设计器
│       │   └── git_explore.py       # Git 浏览
│       └── git_explore/             # Git 浏览引擎
│           ├── engine.py            # 引擎
│           ├── provider.py          # 数据提供者
│           └── unified.py           # 统一视图
│
├── tests/                           # 测试代码
│   ├── test_cli.py                  # CLI 测试
│   ├── test_config.py               # 配置测试
│   ├── test_agent_factory.py        # Agent 工厂测试
│   ├── test_subagent_status.py      # 子 Agent 状态测试
│   ├── test_stream_ui.py            # 流式 UI 测试
│   ├── ...                          # 还有 40+ 测试文件
│   └── fixtures/                    # 测试 fixtures
│
├── skills/                          # Agent Skills
│   ├── session-crash-repair/        # 会话崩溃修复技能
│   │   └── SKILL.md                 # 技能定义文件
│   └── session-cache-analysis/      # 会话缓存命中率分析技能
│       └── SKILL.md
│
├── docs/                            # 文档
│   ├── design.md                    # 技术方案设计
│   ├── agent-development-guide.md   # Agent 开发指南
│   └── tutorial.md                  # 你现在看的这个文档
│
├── examples/                        # 示例配置
│   ├── models.example.json          # 多模型配置示例
│   ├── mcp.example.json             # MCP 配置示例
│   └── settings.example.json        # 设置示例
│
├── scripts/                         # 工具脚本
│   ├── install.ps1                  # Windows 安装脚本
│   └── release.ps1                  # 发布脚本
│
└── .github/workflows/               # CI/CD
    ├── ci.yml                        # 持续集成（测试 + lint）
    └── release.yml                   # 自动发布
```

### 3.1 新手应该先看哪些文件？

按优先级：

1. `pyproject.toml` —— 了解项目依赖和配置（3 分钟）
2. `AGENTS.md` —— 了解项目约定（2 分钟）
3. `src/synapse/cli.py` —— 理解入口和所有命令（10 分钟）
4. `src/synapse/agent.py` —— 理解 Agent 如何被创建（15 分钟）
5. `src/synapse/config.py` —— 理解所有配置项（10 分钟）
6. `src/synapse/subagents.py` —— 理解子 Agent（10 分钟）

---

## 4. 第一条执行链路：`synapse run` 全流程

这是最重要的部分。当你执行 `synapse run "查看当前仓库结构" -w .` 时，发生了什么？

### 第1步：入口——找到 `main()` 函数

```
你在终端输入:  synapse run "查看当前仓库结构" -w .
                    │
                    ▼
pyproject.toml:  [project.scripts]
                 synapse = "synapse.cli:main"
                    │
                    ▼
synapse/__init__.py:  def main():
                          from synapse.cli import main as cli_main
                          cli_main()
```

**关键知识点**：`pyproject.toml` 中的 `[project.scripts]` 告诉了 pip/uv 安装时要创建什么命令、指向什么函数。

### 第2步：CLI 解析 —— Typer 把命令行变成函数调用

```python
# src/synapse/cli.py
app = typer.Typer(name="synapse")

@app.command("run")
def run_cmd(
    task: str = typer.Argument(...),             # "查看当前仓库结构"
    workspace: Path | None = typer.Option(None, "--workspace", "-w"),  # "."
    model: str | None = typer.Option(None, "--model", "-m"),
    require_approval: bool = typer.Option(False, "--require-approval"),
    ...
) -> None:
```

Typer 自动把 `"查看当前仓库结构"` 映射到 `task` 参数，`"."` 映射到 `workspace` 参数。

### 第3步：配置加载 —— 合并多层配置

```python
def run_cmd(...):
    env_path = _bootstrap_env()        # ① 加载 .env 文件（API Key）
    settings = _resolve_settings(      # ② 合并配置
        workspace=workspace,
        model=model,
        ...
    )
```

`_bootstrap_env()` 做了两件事：
1. 确保系统提示词文件存在（`~/.synapse/system_prompt.md`）
2. 从 `.env` 文件加载 API Key

`_resolve_settings()` 合并了三层配置：
```
命令行参数（最高优先级）
    ↓ 覆盖
项目 .synapse/settings.json
    ↓ 覆盖
用户 ~/.synapse/settings.json
    ↓ 覆盖
代码默认值（最低优先级）
```

### 第4步：创建 Agent —— `build_coding_agent()`

```python
agent = build_coding_agent(settings, project_root=...)
result = agent.invoke({"messages": [{"role": "user", "content": task}]})
```

这是整个流程的核心。`build_coding_agent()` 做了什么？下一节详细解释。

### 第5步：流式输出 —— 一边生成一边显示

Agent 不是一次性生成所有输出，而是**流式输出**：

```python
# 对于 run 命令，使用 stream_agent 流式打印
asyncio.run(stream_agent(agent, task, settings, ...))
```

每生成一个新 token（几个字），就立刻打印到屏幕上。这让你感觉 Agent 在"思考"。

### 第6步：Agent 自主循环 —— 模型思考 → 调用工具 → 观察结果 → 再思考

```
用户: "查看当前仓库结构"
  │
  ▼
Agent 思考: "用户想看项目结构，我应该用 ls 或 glob 工具来列出文件"
  │
  ▼
Agent 调用: glob(pattern="**/*.py")
  │
  ▼
工具返回: ["/src/synapse/cli.py", "/src/synapse/agent.py", ...]
  │
  ▼
Agent 思考: "拿到了文件列表，让我总结一下项目结构..."
  │
  ▼
Agent 回复: "当前项目是 Synapse，包含以下模块：cli.py（CLI入口）..."
```

这个循环一直持续，直到 Agent 认为任务完成（不需要再调用工具），然后给出最终回答。

---

## 5. 配置系统详解

### 5.1 配置从哪里来？（三层优先级）

```
高优先级 ─ 命令行参数（-m, -w, --require-approval 等）
  │
  ├─ 项目 .synapse/settings.json（项目专属配置）
  │
  ├─ 用户 ~/.synapse/settings.json（全局个人配置）
  │
低优先级 ─ 代码默认值（在 Settings 类中定义）
```

### 5.2 配置目录结构

```
~/.synapse/                         # 用户级配置（影响所有项目）
├── settings.json                   # 全局设置
├── models.json                     # 模型配置
├── mcp.json                        # MCP 服务器配置
├── themes.json                     # 自定义主题
├── system_prompt.md                # 全局系统提示词
├── sessions.sqlite                 # 会话元数据
└── checkpoints.sqlite              # 对话存档

<项目>/.synapse/                    # 项目级配置（只影响当前项目）
├── settings.json                   # 项目专属设置（覆盖用户级）
├── models.json                     # 项目模型配置
├── mcp.json                        # 项目 MCP 配置
├── themes.json                     # 项目主题
├── system_prompt.md                # 项目专属提示词（覆盖用户级）
├── sessions.sqlite                 # 项目会话
└── checkpoints.sqlite              # 项目存档
```

### 5.3 Settings 配置项一览

```python
# src/synapse/config.py ~ line 71

class Settings(BaseSettings):
    # ===== 模型 =====
    model: str = "openai:gpt-4.1"          # 默认模型
    openai_api_key: str | None              # OpenAI API Key（从 .env 读）
    openai_base_url: str | None             # 自定义网关地址
    anthropic_api_key: str | None           # Anthropic API Key

    # ===== 多模型 =====
    models_config_path: Path | None         # models.json 路径
    active_model: str | None                # 当前激活的模型 profile

    # ===== 工作区 =====
    workspace: Path = Path.cwd()            # 工作目录（Agent 能看到的范围）
    shell_timeout: int = 120                # shell 命令超时（秒）
    shell_executable: str = "pwsh"          # 默认 shell（PowerShell 7+）

    # ===== 安全 =====
    require_approval: bool = False          # 人工审批（默认关闭）
    auto_approve: bool = True               # 自动放行
    safety_profile: str = "dev-autopass"    # 安全配置名

    # ===== 会话 =====
    checkpoint_backend: str = "sqlite"      # 存档后端（sqlite 或 memory）
    checkpoint_path: Path                   # 存档文件路径

    # ===== 记忆 =====
    memory_paths: list[str] = ["AGENTS.md", "MEMORY.md", ...]
    skills_paths: list[str] = ["skills"]

    # ===== 子 Agent =====
    enable_subagents: bool = True
```

### 5.4 多模型配置示例

```json
// .synapse/models.json
{
  "default": "primary",
  "thinking_levels": ["off", "low", "medium", "high", "max"],
  "models": {
    "primary": {
      "model": "openai:gpt-4.1",
      "api_key": "sk-...",
      "context_window": 128000
    },
    "deepseek": {
      "model": "openai:deepseek-v4-pro",
      "base_url": "http://127.0.0.1:3000/v1",
      "thinking": "high"
    }
  }
}
```

切换模型：`synapse tui -m deepseek` 或在 TUI 中按 `F2` 弹出模型选择器。

### 5.5 模型客户端缓存与 HTTP 连接管理

Synapse 的模型客户端不是每次对话都重新创建，而是使用 **LRU 缓存**（最多 8 个）：

```python
# src/synapse/models_registry.py
model_cache = {}  # 按 (model_id, thinking_level) 缓存

def build_model_from_settings(settings):
    cache_key = (settings.active_model, settings.thinking_level)
    if cache_key in model_cache:
        return model_cache[cache_key]  # 命中缓存
    model = build_chat_model(...)       # 创建新 client
    model_cache[cache_key] = model     # 存入缓存
    # 超过 8 个时驱逐最久未使用的
```

**为什么要缓存？** 模型的 HTTP 客户端（`httpx.AsyncClient`）维护着连接池，频繁创建/销毁会浪费资源。缓存让同一模型的多次调用复用一个长连接。

**HTTP 连接管理**（`http_clients.py`）：
- **每模型独立连接池**：避免跨模型共享导致的资源泄漏
- **长 keep-alive**（300 秒空闲超时）：减少 TCP 握手
- **大连接池**（100 个 keep-alive 连接，总计 1000）：支持高并发子 Agent
- **Provider 分离**：OpenAI 和 Anthropic 各有独立的 patch 函数

**思考模式的多层优先级：**

```
优先级（从高到低）：
  1. 显式传入的 enable_thinking / reasoning_effort（运行时 /model thinking high）
  2. 模型配置文件中的默认值（models.json → thinking / enable_thinking）
  3. 全局 default_thinking 回退值
  4. 代码中的 fallback 参数

normalize_thinking_level():
  "off" / false / disabled  → "off"
  "minimal"                  → "minimal"
  "low"                      → "low"
  "medium" / "med"           → "medium"
  "high" / "on"              → "high"
  "max" / "xhigh" / "ultra"  → "max"
```

这意味着用户可以在运行时通过 `/model thinking high` 动态调整思考深度，无需重启。

---

## 6. Agent 创建：`build_coding_agent` 内部解密

这是整个项目最核心的函数。它像一个"汽车工厂流水线"，每一步组装一个部件。

```python
# src/synapse/agent.py

def build_coding_agent(settings, *, project_root=None, ...):
    # 步骤1: 初始化可观测性（LangSmith 跟踪）
    _apply_observability(settings)

    # 步骤2: 构建 Backend（文件 + Shell 能力）
    backend = build_backend(settings)

    # 步骤3: 构建模型实例
    registry, model = build_model_from_settings(settings)

    # 步骤4: 注册 Harness 配置（排除 ls/grep 等内置工具）
    apply_harness_exclusions(model_spec, readonly=..., excluded_tools=...)

    # 步骤5: 创建中断条件（人工审批）
    interrupt_on = build_interrupt_on(require_approval=...)

    # 步骤6: 创建检查点（对话存档）
    saver = _build_checkpointer(settings)  # SQLite 或 Memory

    # 步骤7: 构建子 Agent（researcher / tester / reviewer）
    subagents = build_default_subagents(enabled=..., ...)

    # 步骤8: 文件系统权限
    permissions = build_filesystem_permissions(...)

    # 步骤10: 收集自定义工具
    tools = []
    tools.extend(build_session_tools(...))   # 会话查阅工具

    # 步骤11: 加载 MCP 工具（可选）
    if should_load_mcp:
        mcp_result = load_mcp_tools(servers)
        tools.extend(mcp_result.tools)

    # 步骤12: 组装中间件栈（顺序很重要）
    middleware = [
        build_path_normalize_middleware(),      # ① 路径规范化
        build_intent_schema_middleware(),       # ② 注入 intent 参数
        build_memory_injection_middleware(),    # ③ 注入记忆文件
        build_task_namespace_middleware(),      # ④ 任务命名空间
        build_plan_tracking_middleware(),       # ⑤ 跟踪 todo 进度
        build_tool_error_recovery_middleware(), # ⑥ 工具错误恢复
        build_steer_middleware(),               # ⑦ 用户中途引导
    ]
    if settings.enable_compact_tool:
        middleware.append(build_compact_tool_middleware(model, backend))
    middleware.append(build_model_retry_middleware())  # ⑧ 模型重试

    # 步骤13: 调用 deepagents 创建最终 Agent
    agent = create_deep_agent(
        model=model,
        backend=backend,
        subagents=subagents,
        tools=tools,
        middleware=middleware,
        system_prompt=build_system_prompt(...),
        permissions=permissions,
        interrupt_on=interrupt_on,
        checkpointer=saver,
        ...
    )

    return agent
```

### 6.1 Backend 是什么？

Backend 决定了 Agent 的"身体"——它能碰什么：

```python
# src/synapse/backends.py

class CodingLocalShellBackend(LocalShellBackend):
    """本项目的 Backend：直接操作本地文件 + 执行 Shell 命令"""

    def __init__(self, root_dir, shell_executable="pwsh", ...):
        super().__init__(root_dir=root_dir)
        # 配置 shell：Windows 默认 pwsh（PowerShell 7+）
        self._shell_executable = shell_executable

    def execute(self, command: str) -> ExecuteResponse:
        # 实际执行命令：subprocess.Popen + 编码处理 + 超时控制
        ...
```

关键：本项目用的是 `LocalShellBackend`，**没有沙箱隔离**，Agent 可以直接操作你的文件。生产环境应该用沙箱化 Backend。

### 6.2 检查点（Checkpoint）是什么？

检查点就是"对话存档"。每次 Agent 和用户交互一轮，状态就被保存到 SQLite 数据库。

```python
def _build_checkpointer(settings):
    if settings.checkpoint_backend == "memory":
        return MemorySaver()           # 仅在内存中，重启消失
    # 默认：SQLite 持久化，重启后可以恢复对话
    return AsyncSqliteSaver(conn)      # 存在 .coding-agent/checkpoints.sqlite
```

这就是为什么你可以用 `synapse tui --thread-id <id>` 恢复之前的对话。

---

## 7. 工具系统：Agent 的手和脚

Agent 本身只是一个语言模型，只能"说"不能"做"。**工具**赋予了它行动能力。

### 7.1 当前 Agent 工具

| 工具名 | 功能 | 对应的人类操作 |
|--------|------|---------------|
| `read_file` | 读取文件内容 | 用编辑器打开文件 |
| `write_file` | 创建新文件 | 新建文件并写入 |
| `edit_file` | 精确替换文件片段 | 修改一个唯一的小片段 |
| `patch` | 应用 unified diff | 修改已有文件中的多行内容 |
| `find_files` | 按 glob 模式查找路径 | 在资源管理器里搜索 `*.py` |
| `search_files` | 使用正则搜索文件内容 | 在代码中查找符号或文本 |
| `execute` | 执行 Shell 命令 | 在终端里输入命令 |
| `write_todos` | 创建/更新任务列表 | 写待办事项清单 |
| `task` | 委派子 Agent | 把任务分给同事 |
| `compact_conversation` | 压缩对话上下文 | 清空不需要的聊天记录 |

其中 `read_file`、`edit_file`、`find_files`、`search_files` 和 `patch` 的文件处理底层由
`synapse-core-tool` 提供。Deep Agents 原有的 `ls`、`glob`、`grep` 不会暴露给主 Agent，
以避免与 Synapse 文件搜索工具产生冲突。

### 7.2 自定义工具：会话查阅工具

除了内置工具，Synapse 还注册了两个自定义工具：

```python
# src/synapse/tools/session_tools.py

@tool
def search_session(query="", limit=20):
    """搜索本地会话，支持按标题/摘要/消息全文匹配；query 为空时列出最近会话。"""
    # 默认禁止调用，只有用户明确要求才用
    ...

@tool
def read_session(thread_id, max_turns=0):
    """读取指定会话的对话历史内容。"""
    # 默认禁止调用
    ...
```

这两个工具让 Agent 可以在不同会话之间"记忆"——当你提到"上次我们讨论的那个 bug"时，Agent 可以搜索之前的会话来获取上下文。

### 7.3 工具调用协议

每个工具调用都必须带一个 `intent` 参数，用简短的英文描述调用目的：

```python
# Agent 内部的工具调用格式
{
    "name": "read_file",
    "args": {
        "intent": "locate login handler",        # ← 必须！描述目的
        "file_path": "/src/auth/login.py"
    }
}
```

这是通过 `build_intent_schema_middleware()` 注入到每个工具的 Schema 中的，用于：
- 让 Agent 更清楚自己在做什么
- 在 UI 时间线中显示工具调用的目的
- 方便排查问题

### 7.4 工具排除机制

有些 Deep Agents 自带的工具不适合本项目，通过 `HarnessProfile` 排除：

```python
# src/synapse/harness.py

_DEFAULT_EXCLUDES = frozenset({"ls", "grep"})
# 为什么要排除 ls 和 grep？
# 因为 execute 可以运行项目原生的目录/搜索命令
# 让 Agent 用两个不同的工具做同样的事会造成混淆

_DEFAULT_READONLY_EXCLUDES = frozenset({"execute", "write_file", "edit_file"})
# 只读模式下，排除所有写入和执行工具
```

### 7.5 虚拟路径系统：Agent 看到的"镜像世界"

Synapse 运行在一个**虚拟文件系统**概念下。工具调用中看到的路径是 POSIX 风格的虚拟路径（如 `/src/main.py`），但实际操作需要映射到宿主机的真实路径（如 `F:/project/repo/src/main.py`）。

这是通过 `pathing.py` 实现的：

| 函数 | 功能 | 示例 |
|------|------|------|
| `is_virtual_path(path)` | 判断是否是 POSIX 虚拟路径 | `"/src/a.py"` → True |
| `is_windows_absolute(path)` | 判断是否是 Windows 绝对路径 | `"C:\\Users\\..."` → True |
| `to_virtual_path(path, workspace)` | 宿主路径 → 虚拟路径 | `"F:/repo/src/a.py"` → `"/src/a.py"` |
| `rewrite_tool_args_paths(args, workspace)` | 批量改写工具调用参数中的路径 | `{"file_path": "src/a.py"}` → `{"file_path": "/src/a.py"}` |

**路径改写机制**：`rewrite_tool_args_paths()` 预定义了 15 个路径相关参数名（`path`, `file_path`, `filename`, `source`, `src`, `dst`, `glob` 等），自动将它们从宿主路径转换为虚拟路径。这确保了 LLM 看到的始终是简洁的虚拟路径，不会接触到宿主机的文件系统细节。

> **为什么需要这个？** 因为 LLM 在云端运行，它不知道你的文件系统结构。虚拟路径是一个"翻译层"——让 LLM 以为所有文件都在 `/` 下，而 Synapse 负责把它翻译成真实的文件路径。

### 7.6 启动追踪：Agent 启动性能计时器

`startup_trace.py` 是一个轻量级性能计时工具。通过环境变量 `AGENT_STARTUP_TRACE=1` 开启：

```python
@dataclass
class StartupTrace:
    enabled: bool           # 由 AGENT_STARTUP_TRACE 环境变量控制
    t0: float               # 启动时刻 (perf_counter)
    marks: list[tuple[str, float, float]]  # (阶段名, 累计ms, 步进ms)
    _last: float            # 上次 mark 的时刻
```

提供三种使用方式：

```python
# 方式1: 单点标记
startup_trace.mark("skills loaded")

# 方式2: 上下文管理器（自动计时代码块）
with startup_trace.span("embedder init"):
    embedder = build_embedder()

# 方式3: 最终 dump（列出所有阶段耗时和 Top-N 最慢阶段）
startup_trace.dump()
# 输出示例:
# [startup-trace] total=1250.3ms stages=12
#   +   150.2ms  @   150.2ms  skills loaded
#   +   320.1ms  @   470.3ms  embedder init
#   ...
# [startup-trace] top stages:
#      320.1ms  embedder init
#      150.2ms  skills loaded
```

线程安全：主线程使用全局单例 `TRACE`，其他线程通过 `threading.local()` 获取独立实例。

---

## 8. 子 Agent 系统：分工协作的团队

Synapse 内置了三个子 Agent，通过 `task` 工具委派。就像团队里有三个不同角色的同事。

### 8.1 三个子 Agent

```python
# src/synapse/subagents.py

def build_default_subagents():
    return [
        {
            "name": "researcher",
            "description": "Explore the codebase to answer questions...",
            "system_prompt": "You are a codebase researcher...",
            # 工具限制：不能写文件、不能执行命令（只读）
        },
        {
            "name": "tester",
            "description": "Run focused tests, diagnose failures...",
            "system_prompt": "You are a testing specialist...",
            # 工具限制：不能写文件（可以执行命令跑测试）
        },
        {
            "name": "reviewer",
            "description": "Review code changes for correctness...",
            "system_prompt": "You are a code reviewer...",
            # 工具限制：不能写文件（可以执行 git diff 等只读命令）
        },
    ]
```

### 8.2 子 Agent 的安全隔离

每个子 Agent 只能使用被允许的工具：

| 子 Agent | read_file | glob | write_file | edit_file | execute |
|----------|-----------|------|------------|-----------|---------|
| researcher | ✅ | ✅ | ❌ | ❌ | ❌ |
| tester | ✅ | ✅ | ❌ | ❌ | ✅ |
| reviewer | ✅ | ✅ | ❌ | ❌ | ✅ |

此外，**所有子 Agent 都不能使用 `write_todos`**（只有主 Agent 能规划任务）。

### 8.3 默认调度方式（deepagents 原生 SubAgentMiddleware）

默认使用 deepagents 内建的 `SubAgentMiddleware`：`build_coding_agent()` 把 `subagents` 直接传给
`create_deep_agent(subagents=...)`，由框架在 ToolNode 阶段串行执行 `task` 工具调用。每个子 Agent
在独立子图（独立上下文）中运行，完成后只把最终文本返回主 Agent，实现上下文隔离。

> 说明：仓库曾引入过一版自研 `DAGSubAgentMiddleware`（拓扑排序 + `asyncio.gather` 波次并行），
> 但已回退。如需并行/依赖编排，应在该原生委派基础上以独立 workflow 层实现。

### 8.4 子 Agent 创建时的关键设计决策

每个子 Agent 也是一个完整的 `CompiledStateGraph`（LangGraph 图），有自己的模型、系统提示词和工具集。创建时遵循以下原则：

- **共享 backend**：researcher/tester/reviewer 共用同一个 `CodingLocalShellBackend`，否则子 Agent 的文件读取、搜索和 `execute` 会变成"空壳"。
- **通过工具排除实现隔离**（而非 `FilesystemPermission`）：因为 `LocalShellBackend` 不支持权限模式，改用 `ToolExclusionMiddleware` 从模型可见工具列表中直接移除被禁工具（如 researcher 的 `write_file`、`edit_file`、`patch`、`execute`）。
- **禁用 write_todos**：所有子 Agent 额外排除 `write_todos` 类任务规划工具，防止子 Agent 内部再拆解出"子子 Agent"，造成递归复杂度爆炸。
- **每个子 Agent 可指定不同模型**：通过 `tester_model` / `reviewer_model` / `researcher_model` 设置，比如 tester 用便宜的模型（如 DeepSeek），researcher 用强推理模型（如 Claude）。

---

## 9. 会话与记忆：让 Agent 拥有"记忆"

### 9.1 两种"记忆"

```
┌──────────────────────────────────────────────────────┐
│                    会话记忆                           │
│  ┌──────────────────────────────────────────────┐    │
│  │  短期：LangGraph checkpointer（对话存档）     │    │
│  │  - 存在 .coding-agent/checkpoints.sqlite    │    │
│  │  - 自动保存每轮对话                          │    │
│  │  - 支持通过 thread_id 恢复                   │    │
│  └──────────────────────────────────────────────┘    │
│                                                       │
│  ┌──────────────────────────────────────────────┐    │
│  │  长期：AGENTS.md 文件（项目记忆）             │    │
│  │  - 存在项目根目录                             │    │
│  │  - 每次对话开始时自动注入到系统提示词          │    │
│  │  - 包含：用户信息、项目约定、测试步骤等        │    │
│  └──────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────┘
```

### 9.2 会话元数据

```python
# src/synapse/sessions.py

@dataclass
class SessionInfo:
    thread_id: str         # 唯一会话 ID
    title: str             # 标题（自动取第一条用户消息的前80字）
    model: str | None      # 使用的模型
    created_at: str        # 创建时间
    updated_at: str        # 最后活动时间
    tags: list[str]        # 标签
    summary: str | None    # 自动生成的摘要

    def binding(self) -> ModelBinding:
        # 模型绑定信息（模型名 + thinking 等级）
        ...
```

### 9.3 会话恢复

```python
# 用 thread_id 恢复之前的对话
synapse tui --thread-id 550e8400-e29b-41d4-a716-446655440000
```

恢复时，Agent 使用同一个 `thread_id` 读取 LangGraph checkpoint；TUI 同时从
transcript projection 加载最近的可见回合，因此模型上下文与界面历史都能延续。
更早的界面历史会在向上滚动时分页加载。

### 9.4 上下文自动压缩

当对话太长（接近模型上下文窗口限制），deepagents 内置的 `SummarizationMiddleware` 会自动：

1. 把早期对话总结为一小段摘要
2. 将完整对话写入"offload 文件"（存到 `/large_tool_results/` 目录）
3. 替换新摘要到上下文中

用户也可以通过 `/compact` 命令或 Agent 调用 `compact_conversation` 工具来手动触发。

### 9.5 长期记忆：从对话中自动提取知识

除了 AGENTS.md 这种**静态**的项目约定，Synapse 还有一个**动态**的长期记忆系统，会自动从每轮对话中提取有价值的信息。

```
用户消息
   │
   ▼
Agent 执行任务
   │
   ▼
AutoRecorder.record_if_valuable()    ← 每轮结束后自动触发
   │
   ├─ 第一层：快速剔除
   │   - 问候语（"你好"、"谢谢"、"bye"）
   │   - 太短的任务（< 4 个词）且回答 < 200 字符
   │   - 回答极短（< 120 字符）
   │
   ├─ 第二层：知识信号检测
   │   - 检查是否包含关键词：fix, bug, error, config, pattern,
   │     refactor, 最佳实践, 经验, 架构, 设计, def, class, import...
   │   - 如果没有信号且回答不够长 → 跳过
   │
   ├─ 第三层：经验提取
   │   - 优选：如果有 LLM，调用它分析对话，提取最多 3 条经验
   │   - 降级：纯关键词权重打分（"关键/核心" 权重 0.8、"bug/修复" 0.7...）
   │
   └─ 第四层：持久化
       └─ LongTermMemory.remember(经验, 向量嵌入, 来源元数据)
```

存储结构：`long_term_memory.sqlite`，一条 `memories` 表：
- `id`: 16 位 hex UUID
- `text`: 记忆内容（如"认证模块用 JWT，15 分钟过期"）
- `embedding`: float32 BLOB（384 维向量）
- `metadata`: JSON（来源、thread_id、任务片段）
- `created_at`: Unix 时间戳

**检索（recall）：** 查询文本 → 嵌入器生成向量 → 遍历所有记忆计算余弦相似度 → top-k 排序。

为什么不用近似最近邻（ANN）索引（如 FAISS/Chroma）？因为项目级记忆通常只有几十到几百条，全表扫描完全够用，零额外依赖。

**嵌入器自动降级策略：**
- 优先用 `sentence-transformers` 的 `all-MiniLM-L6-v2`（384 维，真正的语义理解）
- 不行就用纯 Python TF-IDF（256 维，词频 + 逆文档频率，零依赖）
- 工厂函数 `_build_default_embedder()` 自动选择

### 9.6 取消修复：ESC 中断后的"手术缝合"

当用户按 ESC 中断 Agent 时，LangGraph 的 checkpoint 会处于不一致状态：AI 消息里有 `tool_calls`，但对应的 `ToolMessage` 还没写入。下一轮对话会因此出错。

`cancel_repair.py` 中的 `repair_thread_after_cancel()` 自动做"手术缝合"：

1. 找到所有"发出了但未收到回复"的工具调用
2. 为每个未完成的工具调用插入 `ToolMessage(content="[cancelled by user]", status="error")`
3. 追加一个 `AIMessage("[本轮已由用户终止，上下文已保留]")` 作为占位符

这样被取消的轮次被安全"封口"，下一轮可以正常继续。

### 9.7 Checkpoint 种子：导入外部对话

`checkpoint_seed.py` 提供了把外部 Codex 对话注入为 LangGraph checkpoint 的能力。流程：

1. 解析外部对话中的 `HumanMessage` / `AIMessage`
2. 通过 `agent.update_state(config, {"messages": messages})` 写入
3. 写入后立即验证（精确回读消息 + 确认无 pending graph 任务）
4. 失败自动补偿删除该 thread（`_compensate` 方法）

---

## 10. Skills 技能系统：给 Agent 装"插件"

Skills 是给 Agent 的可加载"专业说明手册"。

### 10.1 Skill 文件格式

```markdown
<!-- skills/session-cache-analysis/SKILL.md -->

---
name: session-cache-analysis
description: Analyze the prompt cache hit rate of a Synapse session (thread).
license: Apache-2.0
allowed-tools: execute read_file write_file search_files find_files
---

# Session cache hit-rate analysis

## Data source
Prefer:
`load_messages_from_sqlite_file(checkpoints.sqlite, thread_id)`

...
```

前沿（`---` 之间的 YAML）定义了 Skill 的元数据：
- `name`: 技能名称
- `description`: 简短描述（Agent 据此判断是否需要加载）
- `allowed-tools`: 加载此 Skill 时允许使用的工具
- `license` / `compatibility`: 许可和兼容性信息

### 10.2 技能如何被加载

```python
# src/synapse/skills_catalog.py

def discover_skills(skills_paths: list[str]) -> list[SkillInfo]:
    """扫描 skills 目录，找到所有 SKILL.md 文件"""
    for root in skills_paths:
        for skill_md in root.rglob("SKILL.md"):
            meta = _parse_frontmatter(text)  # 解析 --- 块
            # 收集 name、description 等信息
```

Agent 启动时会扫描 `skills/` 目录，将所有 Skill 的描述注入系统提示词。当 Agent 判断需要某个 Skill 的详细内容时，会通过 `read_file` 读取完整的 SKILL.md。

### 10.3 现有 Skills

- **session-crash-repair**: 告诉 Agent 如何检测并修复异常退出后 checkpoint 不一致的会话
- **session-cache-analysis**: 告诉 Agent 如何分析会话的 prompt 缓存命中率并定位逐出根因

### 10.4 如何创建新 Skill

1. 在 `skills/<skill-name>/` 下创建目录
2. 创建 `SKILL.md`，必须包含 YAML 前沿
3. 编写具体的操作指南
4. 重启 Agent，它会自动发现新 Skill

---

## 10-A. RAG 知识库：让 Agent 理解你的项目

RAG（Retrieval-Augmented Generation）是 Synapse 的**项目文档索引系统**。它会扫描整个项目，把所有文档切成小片段，用向量嵌入存储，供 Agent 语义搜索。

### 10-A.1 工作流程

```
项目文件 (.md / .py / .rst / .txt / .toml / .yaml / .yml)
       │
       ▼
_discover_docs()     ← 扫描，排除 .git / node_modules / __pycache__ / .venv
       │               最多索引 300 个文件
       ▼
_chunk()             ← 智能切分：每个片段 ≤ 1500 字符
       │               优先在段落边界断开，其次句子边界
       ▼
EmbeddingProvider    ← 向量化：384 维 float32 向量
  .embed()
       │
       ▼
SQLite (knowledge 表) ← 存储：source, text, chunk_index, embedding BLOB
       │
       ▼
search()             ← 查询：query 向量化 → 余弦相似度排序 → top-k 结果
```

### 10-A.2 智能切分策略

`_chunk()` 不是粗暴按字符数切割，而是：
1. 先按段落（`\n\n`）分
2. 每个段落组不超过 1500 字符
3. 如果某组仍然超标，再按句子（`. `）细分
4. 保证 chunk 在语义上尽量完整

### 10-A.3 如何使用

```python
from synapse.rag import ProjectKnowledgeBase

kb = ProjectKnowledgeBase(project_root=".")

# 索引项目（首次全量，后续增量）
await kb.index()  # 返回 chunk 总数

# 语义搜索
results = await kb.search("认证是怎么实现的？", top_k=3)
# 返回: [{source, text, chunk_index, chunk_total, similarity}, ...]

# 统计
stats = await kb.stats()
# {total_chunks: 156, total_sources: 42}
```

数据库存储在 `.synapse/knowledge.sqlite`，使用 WAL 模式提升并发。

### 10-A.4 Skills vs RAG：分工明确

| 维度 | Skills | RAG 知识库 |
|------|--------|-----------|
| 内容来源 | 开发者手动编写 | 项目文件自动扫描 |
| 更新方式 | 静态，手动维护 | 动态，可增量索引 |
| 内容性质 | "怎么做"（过程性知识） | "是什么"（事实性知识） |
| 典型场景 | 教你如何在这个项目中跑测试 | 回答"认证模块在哪个文件？" |
| 存储格式 | SKILL.md (Markdown+frontmatter) | SQLite + 向量嵌入 |

---

## 11. TUI 界面：终端里的可视化控制台

### 11.1 什么是 TUI？

TUI（Textual User Interface）是在终端里运行的"图形界面"。它看起来像 GUI 但完全由文字构成。

Synapse 的 TUI 使用 [Textual](https://textual.textualize.io/) 框架构建，类似 Cursor/Warp 的终端体验。

### 11.2 布局结构

```
┌─────────────────────────────────────────────────────┐
│  ≡ /path/to/project · ⎇ main · Title  │ in/out/ctx  │ ← TopBar
├─────────────────────────────────────────────────────┤
│                                                      │
│  ● User: "帮我修复登录 bug"                 10:30   │ ← 用户消息
│                                                      │
│  ◆ Thought for 3s                         展开 ▼    │ ← 思考过程
│  ▾ Tools (3 items)                        收起 ▲    │ ← 工具调用组
│    ◆ read_file: locate login handler              │
│    ◆ glob: find auth files                        │
│    ◆ execute: pytest tests/test_auth.py -q        │
│                                                      │
│  AI: 已定位到问题在 auth.py 第42行...              │ ← AI 回答
│                                                      │
├─────────────────────────────────────────────────────┤
│  Worked for 12s.  (content copied)                  │ ← 状态栏
├─────────────────────────────────────────────────────┤
│  › 能再帮我加个单元测试吗？                           │ ← 输入框
├─────────────────────────────────────────────────────┤
│  gpt-4.1 · thinking:high │ MCP: 2 │ ↑↓历史 │ ...  │ ← BottomBar
└─────────────────────────────────────────────────────┘
```

### 11.3 核心组件

| 组件 | 文件 | 职责 |
|------|------|------|
| `SynapseApp` | `ui/tui.py` | 主应用，继承 `textual.App` |
| `TopBar` | `ui/topbar/` | 顶部状态栏（路径、分支、用量） |
| `BottomBar` | `ui/bottombar/` | 底部状态栏（模型、MCP、快捷键） |
| `Timeline` | `ui/timeline.py` | 对话/工具时间线渲染 |
| `StreamSink` | `ui/sink.py` | 流式输出接收器抽象 |
| `WelcomeView` | `ui/welcome.py` | 启动动画（Braille 点阵 "SYNAPSE"） |
| Dialogs | `ui/dialogs/` | 弹窗：模型选择、主题、会话列表等 |

### 11.4 数据流

```
用户输入（Input widget）
    │
    ▼
TUI.run_agent()  # 在工作线程中调用 Agent
    │
    ▼
Agent astream_events()  # 流式获取 Agent 事件
    │
    ▼
TextualStreamSink  # 把事件翻译成 UI 更新
    │
    ├─→ on_thinking()  → Timeline 显示思考过程
    ├─→ on_tool_call() → Timeline 显示工具调用
    ├─→ on_text()      → Timeline 追加 AI 回复文本
    └─→ on_done()      → 状态栏更新完成时间
```

### 11.5 快捷键

| 快捷键 | 功能 |
|--------|------|
| `F2` | 模型选择框：`↑/↓` 移动，`Space` 分别标记模型与推理级别（可各选一个），`Enter` 一起保存，`Esc` 取消 |
| `Ctrl+T` | 选择主题 |
| `Ctrl+S` | 会话列表 |
| `Ctrl+O` | 查看 MCP 工具 |
| `Ctrl+E` | 展开/收起思考过程 |
| `Ctrl+C` | 取消当前 Agent 运行 |
| `Ctrl+L` | 清屏 |
| `↑/↓` | 浏览输入历史 |
| `Alt+V` | 粘贴剪贴板内容（文本或图片） |

> 图片支持：按 `Alt+V` 粘贴图片后，输入框上方会显示图片预览（`[image#N]` 占位符对应一张图），删除占位符即移除该图；发送后图片会出现在对话时间线中，点击用户消息可展开/收起图片。
>
> 渲染协议：启动时自动探测终端能力（Sixel / Kitty TGP / 半块字符 / Unicode 降级），TUI 会为图片预留正确的布局高度，像素协议（Sixel）在支持的终端（如 Windows Terminal）可直接显示。TUI 内输入 `/image` 可查看当前生效的渲染器与终端探测结果；`/image halfcell`、`/image sixel` 等可手动切换，或启动前设置环境变量 `SYNAPSE_IMAGE_RENDERER`。

---

## 12. 中间件体系：Agent 的"管道"

### 12.1 什么是中间件？

中间件（Middleware）是 Agent 处理流程中的"管道"。每当 Agent 做某件事——调用模型前、调用工具前、收到结果后——中间件都可以插手修改。

```
用户消息 → [中间件1] → [中间件2] → ... → 模型调用 → ... → [中间件N] → 回复
```

### 12.2 Synapse 的中间件栈

```python
# src/synapse/agent.py build_coding_agent()

middleware = [
    # ① 路径规范化：把 Windows 路径转成虚拟 / 路径
    build_path_normalize_middleware(),

    # ② Intent Schema 注入：给每个工具的参数加 intent 字段
    build_intent_schema_middleware(),

    # ③ 记忆注入：把 AGENTS.md 内容注入到消息中
    build_memory_injection_middleware(memory_paths),

    # ④ 任务命名空间：给并行任务分配独立上下文
    build_task_namespace_middleware(),

    # ⑤ 规划跟踪：追踪 write_todos 的状态变化
    build_plan_tracking_middleware(),

    # ⑥ 工具错误恢复：工具调用失败时自动重试
    build_tool_error_recovery_middleware(),

    # ⑦ 用户引导：支持用户中途给 Agent 发提示
    build_steer_middleware(steer_queue),

    # ⑧ 上下文压缩：提供 compact_conversation 工具
    build_compact_tool_middleware(model, backend),

    # ⑨ 模型重试：模型返回临时错误时自动重试
    build_model_retry_middleware(),

    # ⑩ 视觉中间件：处理图片输入
    build_describe_image_middleware(...),
]
```

### 12.3 模型重试中间件

```python
# src/synapse/middleware.py

# 当模型返回以下错误时，自动重试（最多3次）：
_TRANSIENT_MODEL_ERROR_MARKERS = (
    "overloaded",           # 服务过载
    "rate limit",           # 频率限制
    "service unavailable",  # 服务不可用
    "upstream timeout",     # 上游超时
    ...
)
```

每次重试前会等待递增的时间（1秒 → 2秒 → 4秒），并在 UI 中显示"Retrying... (attempt 2/3)"。

### 12.4 洋葱模型：中间件的执行顺序

中间件的"洋葱模型"是最重要的概念。模型调用和工具调用被多层中间件包裹，每层做一件事并透传给下一层：

```
                            请求进入
                               │
    ┌──────────────────────────┼──────────────────────────┐
    │  第1层: ModelRetry        │  捕获瞬时错误，指数退避重试  │
    │  ┌───────────────────────┼───────────────────────┐  │
    │  │ 第2层: IntentSchema   │  注入/剥离 intent 字段    │  │
    │  │ ┌────────────────────┼────────────────────┐  │  │
    │  │ │ 第3层: Memory      │  注入 AGENTS.md 记忆   │  │  │
    │  │ │ ┌─────────────────┼─────────────────┐  │  │  │
    │  │ │ │ 第4层: Plan      │  注入步骤提示       │  │  │  │
    │  │ │ │                 │                   │  │  │  │
    │  │ │ │   实际模型调用 (LLM API)             │  │  │  │
    │  │ │ │   → 返回文本 + tool_calls            │  │  │  │
    │  │ │ │                                     │  │  │  │
    │  │ │ └─────────────────────────────────────┘  │  │  │
    │  │ └─────────────────────────────────────────┘  │  │
    │  └─────────────────────────────────────────────┘  │
    └──────────────────────────────────────────────────┘
                               │
                    ┌──────────┴──────────┐
                    │   工具调用阶段       │
                    └─────────────────────┘
                               │
    ┌──────────────────────────┼──────────────────────────┐
    │  第1层: PathNormalize     │  宿主路径 → 虚拟 / 路径    │
    │  ┌───────────────────────┼───────────────────────┐  │
    │  │ 第2层: IntentSchema   │  移除 intent 参数        │  │
    │  │ ┌────────────────────┼────────────────────┐  │  │
    │  │ │ 第3层: ToolError   │  异常 → 结构化消息    │  │  │
    │  │ │ ┌─────────────────┼─────────────────┐  │  │  │
    │  │ │ │ 第4层: TaskNS   │  子任务隔离命名空间  │  │  │  │
    │  │ │ │                 │                   │  │  │  │
    │  │ │ │   工具执行 (backend + tools)        │  │  │  │
    │  │ │ │   → ToolMessage 结果                │  │  │  │
    │  │ │ └─────────────────────────────────────┘  │  │  │
    │  │ └─────────────────────────────────────────┘  │  │
    │  └─────────────────────────────────────────────┘  │
    └──────────────────────────────────────────────────┘
```

**关键点：中间件的注册顺序决定执行顺序。**

- **模型调用阶段**：外层的 ModelRetry 最先拦截（可以在模型调用失败时重试），内层的 Plan/Memory 最后注入内容
- **工具调用阶段**：外层的 PathNormalize 最先处理（先转换路径），然后逐层向内剥除 Intent、恢复错误、隔离命名空间

> **类比**：就像快递包装——最外面是 ModelRetry（如果运输损坏，再发一次），然后一层层打开，直到最里面拿到工具执行结果。

---

## 13. MCP 集成：连接外部工具

### 13.1 什么是 MCP？

MCP（Model Context Protocol）是一个标准协议，让 AI 应用可以连接外部工具服务器。就像 USB 协议——只要支持 USB 的设备都能插上电脑。

### 13.2 配置 MCP 服务器

```json
// .synapse/mcp.json
{
  "servers": [
    {
      "name": "filesystem",
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
      "enabled": true
    },
    {
      "name": "web-search",
      "transport": "streamable_http",
      "url": "https://api.search.com/mcp",
      "headers": {
        "Authorization": "Bearer ${SEARCH_API_KEY}"
      },
      "enabled": true
    }
  ]
}
```

### 13.3 三种传输方式

| 传输方式 | 说明 | 使用场景 |
|----------|------|---------|
| `stdio` | 启动本地进程，通过标准输入/输出通信 | 本地 MCP 服务器 |
| `sse` | HTTP Server-Sent Events，长连接 | 远程 HTTP 服务 |
| `streamable_http` | HTTP 流式响应 | 现代 HTTP API |

### 13.4 MCP 连接池架构详解

MCP 的连接管理是 Synapse 最精巧的部分之一，核心是 `McpSessionPool`（进程级连接池）。

**架构概览：**

```
mcp.json（多层合并: 用户 ~/.synapse/ + 项目 .synapse/ + 环境变量）
       │
       ▼
McpServerConfig（解析后的每个 server）
       │
       ▼
McpSessionPool（进程级单例，独立事件循环线程）
  │
  ├─ _LoopThread：独立 asyncio 事件循环（daemon 线程）
  │    专为 MCP 长连接服务，与主 UI 线程分离
  │
  └─ _LiveServer 字典（按 server name 索引）
       ├─ stdio: stdio_client + subprocess（如 npx + MCP server）
       ├─ sse: sse_client + HTTP 长连接
       └─ streamable_http: streamablehttp_client
```

**关键设计决策：**

- **独立事件循环线程**：MCP 连接（stdio subprocess / SSE / HTTP）在专用线程上长存，不阻塞 UI 线程
- **连接复用**：`_open_stdio` / `_open_http` 只在启动时调用一次，后续 `call_tool` 复用已有连接
- **故障隔离**：单次 `call_tool` 失败只会自动移除该 server，不会导致级联崩溃
- **全局池管理**：`_ACTIVE_POOL` 是进程级单例，重新加载时关闭旧池并创建新池（线程安全）

**工具注入三步骤：**

1. **Schema 转换**：`_json_schema_to_args()` 把 MCP 的 `inputSchema`（JSON Schema）正规化为 `{"type": "object", "properties": {...}}`
2. **Pydantic 模型动态生成**：`json_schema_to_pydantic_model()` 根据 MCP 工具的 schema **动态**创建一个 Pydantic 参数模型（`string→str, integer→int, number→float, boolean→bool, array→list`）
3. **StructuredTool 包装**：`_make_tool()` 创建 LangChain 标准 `StructuredTool`，函数体是一个闭包 `_invoke(**kwargs)`，过滤掉 `None` 值后调 `McpSessionPool.call_tool()`

工具名称格式为 `{prefix}{tool_name}`（默认 prefix = `{server_name}__`，如 `filesystem__read_text`）。

**Eager vs Deferred 加载：**
- **Eager**：`mcp_eager=True` 时，Agent 构建阶段同步连接所有 MCP 服务器（可能拖慢启动）
- **Deferred**（默认）：Agent 先启动，TUI 显示 MCP 服务器名称但未连接。用户通过 `/mcp connect` 或 UI 面板手动触发 `attach_mcp_to_agent()` 热挂载（不阻塞 UI 启动）

---

### 13.5 MCP 在 Synapse 中的实现

```python
# src/synapse/mcp_client.py

def load_mcp_tools(servers, enabled=True):
    """连接所有启用的 MCP 服务器，发现并返回工具列表"""
    pool = McpToolPool()
    for server in servers:
        if server.enabled:
            pool.connect(server)
    tools = pool.list_tools()  # 获取所有远程工具
    return McpLoadResult(tools=tools, ...)
```

MCP 连接在独立线程中保持活跃，避免每次调用工具时重新连接。

---

## 14. 安全与权限：保护你的电脑

### 14.1 安全态势

Synapse 默认使用 `dev-autopass` 配置，适合**本地开发**：

```python
# src/synapse/safety.py

SAFETY_PROFILES = {
    "dev-autopass": {
        "require_approval": False,     # 不需要人工审批
        "readonly": False,             # 可以写文件
        "auto_approve": True,          # 所有操作自动放行
        "enable_command_blacklist": True,  # 但危险命令会被标记
    },
    "dev-approve": {
        "require_approval": True,      # 需要人工审批
        ...
    },
    "readonly": {
        "readonly": True,              # 只读模式
        ...
    },
}
```

### 14.2 危险命令黑名单

即使默认自动放行，以下模式的命令仍会被标记为危险：

```python
DANGEROUS_PATTERNS = [
    r"\brm\s+-rf\s+/",               # rm -rf /
    r"\bgit\s+push\s+.*--force\b",   # git push --force
    r"\bgit\s+reset\s+--hard\b",     # git reset --hard
    r"\bshutdown\b",                  # shutdown
    r"\breboot\b",                    # reboot
    r"\bformat\s+[a-z]:",            # format C:
    ...
]
```

### 14.3 只读模式

```bash
synapse tui --readonly
```

只读模式下，Agent 不能：
- 写文件（`write_file`、`edit_file`、`patch` 被排除）
- 执行命令（`execute` 被排除）

只能读取和搜索代码。

### 14.4 文件系统权限

通过 `FilesystemPermission` 可以限制 Agent 能访问哪些路径：

```python
# 默认情况下，Agent 只能操作工作区内的文件
# 可以通过 deny_paths 显式禁止某些路径
settings.deny_fs_paths = ["/etc", "~/.ssh"]
```

### 14.5 HITL 中断恢复流程详解

HITL（Human-In-The-Loop）处理 LangGraph 的**审批暂停 / 恢复**流程。当 Agent 想执行敏感操作时，图执行暂停，等待人类决策。

**中断检测流程（`hitl.py`）：**
1. 调用 `agent.get_state(config)` 获取 LangGraph 状态
2. 从 `state.interrupts` 提取等待审批的操作列表
3. 支持两种中断格式：新版 `HITLRequest` 格式和旧版单项格式
4. 回退到 `state.tasks[*].interrupts` 查找
5. 如果所有路径都找不到，检查 `state.next` 是否为空来判断是否真的在等待

**审批与恢复：**

```python
# 构建审批决策
decisions = build_decisions(pending, action="approve")

# 构建恢复载荷（LangGraph Command）
resume_payload = build_resume_payload(decisions)
# 实际是: Command(resume={"decisions": [{"type": "approve"}, ...]})

# 拒绝时带理由
decisions = build_decisions(pending, action="reject", message="请先检查依赖")
```

用户通过 CLI 命令或 TUI 按钮交互：`/approve`（批准所有等待中的操作）、`/reject [reason]`（拒绝）。

### 14.6 Steer 引导机制：运行中的"副驾驶"

Steer 与 HITL 的不同在于时机：

| 维度 | HITL | Steer |
|------|------|-------|
| 触发时机 | Agent 执行敏感操作前（事先阻断） | Agent 运行中任意时刻（异步注入） |
| 交互模式 | 审批 / 拒绝（二元决策） | 自由文本引导（"先别动配置，检查依赖"） |
| 技术实现 | LangGraph interrupt + Command(resume) | 线程安全队列 + HumanMessage 注入 |
| 比喻 | 红绿灯（停车检查） | 副驾驶（随时指路） |

SteerQueue 运作方式：
- 用户在 Agent 运行时输入文本 → `push()` 入队
- 下一轮 LLM 调用前 → `drain()` 一次性取出所有引导
- 注入时包装为 `[Mid-run user guidance]` 前缀的 HumanMessage
- 用户可以 `remove_at(index)` 在消息被消费前删除

### 14.7 视觉模型独立路由

Synapse 支持**视觉模型与主模型分离**。当用户粘贴图片时，DescribeImageMiddleware 自动判断：

- **主模型支持图片**（`image_input=true`，如 Claude/GPT-4o）→ 直接透传图片
- **主模型不支持图片**（如 DeepSeek V3）→ 将图片发送到专用视觉模型获取文字描述，用 `[image]...[/image]` 标记包裹后传给主模型

视觉模型有独立的重试策略、fallback 模型链和 SHA256 缓存（避免重复描述同一张图片）。

---

## 15. 实践：如何从零开发一个新 Agent 应用

### 15.1 最简 Agent（30 行代码）

```python
# my_first_agent.py
from langchain.chat_models import init_chat_model
from langchain.agents import create_agent

# 1. 创建模型
model = init_chat_model("openai:gpt-4.1")

# 2. 定义工具
from langchain_core.tools import tool

@tool
def get_weather(city: str) -> str:
    """获取城市天气（模拟）"""
    return f"{city}: 晴天，25°C"

# 3. 创建 Agent
agent = create_agent(
    model=model,
    tools=[get_weather],
    system_prompt="你是一个天气助手，用中文回答。",
)

# 4. 运行
result = agent.invoke({
    "messages": [{"role": "user", "content": "北京今天天气怎么样？"}]
})

# 5. 打印结果
for msg in result["messages"]:
    if hasattr(msg, "content"):
        print(msg.content)
```

### 15.2 升级到 Deep Agent（加上文件操作能力）

```python
# my_coding_agent.py
from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend
from langchain.chat_models import init_chat_model

model = init_chat_model("openai:gpt-4.1")
backend = LocalShellBackend(root_dir="/path/to/project")

agent = create_deep_agent(
    model=model,
    backend=backend,
    system_prompt="你是一个 Python 编程助手。",
)

# 它现在有了 read_file、write_file、edit_file、glob、execute 等工具！
result = agent.invoke({
    "messages": [{"role": "user", "content": "在 src/ 下创建一个 hello.py，输出 Hello World"}]
})
```

### 15.3 参考 Synapse 的模式添加自定义功能

```python
# my_advanced_agent.py
from deepagents import create_deep_agent

# 1. 自定义工具（参考 synapse/tools/session_tools.py）
from langchain_core.tools import tool

@tool
def my_custom_tool(query: str) -> str:
    """你的自定义工具描述"""
    return f"处理了: {query}"

# 2. 自定义中间件（参考 synapse/middleware.py）
from langchain.agents.middleware import AgentMiddleware

class MyMiddleware(AgentMiddleware):
    """你的自定义中间件"""
    ...

# 3. 子 Agent（参考 synapse/subagents.py）
my_subagents = [
    {
        "name": "helper",
        "description": "一个帮助子 Agent",
        "system_prompt": "你是一个辅助 Agent...",
    }
]

# 4. 组装
agent = create_deep_agent(
    model=model,
    backend=backend,
    tools=[my_custom_tool],
    middleware=[MyMiddleware()],
    subagents=my_subagents,
    system_prompt="你是一个强大的自定义 Agent。",
)
```

### 15.4 学习路径建议

```
第1周：理解概念
  - 阅读本文档第1-4节
  - 读懂 pyproject.toml、cli.py、agent.py
  - 手动运行 synapse run 和 synapse tui

第2周：深入核心
  - 阅读 agent.py 的 build_coding_agent 函数
  - 理解中间件栈的顺序和作用
  - 理解子 Agent 的定义和隔离

第3周：动手实践
  - 写一个最简单的 Agent（15.1 节代码）
  - 添加一个自定义工具
  - 写一个自定义中间件

第4周：读懂全部
  - 通读 src/synapse/ 下所有 .py 文件
  - 理解 TUI 的数据流
  - 尝试修改一个小功能并运行
```

---

## 16. 附录：项目约定与命令速查

### 16.1 项目约定（来自 AGENTS.md）

- Python 依赖使用 `uv` 管理
- 优先小步、可测试的改动
- CLI 输出简洁，不使用 emoji
- 默认关闭人工审批（自动放行）
- Backend 仅使用 `LocalShellBackend`
- 对用户回复使用中文；代码标识符/路径/命令保留原文

### 16.2 常用命令

```bash
# 安装
powershell -ExecutionPolicy Bypass -File scripts/install.ps1

# 运行
synapse tui -w .                        # 启动 TUI
synapse run "任务描述" -w .              # 单次执行
synapse chat -w .                        # CLI 对话

# 会话管理
synapse sessions list                    # 列出会话
synapse sessions export <id> -f md       # 导出会话为 Markdown

# 模型管理
synapse models list                      # 列出可用模型
synapse models current                   # 查看当前模型

# 开发
uv sync                                  # 安装依赖
uv run --no-sync pytest tests/ -q        # 运行测试
uv run --no-sync pytest tests/test_cli.py -q  # 针对性测试
uv run --no-sync ruff check .            # 代码检查
```

### 16.3 测试步骤（来自 AGENTS.md）

1. 先运行针对性测试：`uv run --no-sync pytest tests/test_xxx.py -q`
2. 针对性测试通过后，再运行完整测试：`uv run --no-sync pytest -q`
3. 代码检查：`uv run --no-sync ruff check .`

### 16.4 环境变量速查

| 变量名 | 用途 | 配置位置 |
|--------|------|---------|
| `OPENAI_API_KEY` | OpenAI API 密钥 | `.env` |
| `OPENAI_BASE_URL` | 自定义 API 网关 | `.env` |
| `ANTHROPIC_API_KEY` | Anthropic API 密钥 | `.env` |
| `VISION_API_KEY` | 视觉模型 API 密钥 | `.env`（用于图片识别） |
| `AGENT_MODELS_CONFIG` | 多模型配置文件路径 | `.env` |
| `AGENT_ACTIVE_MODEL` | 默认激活的模型 profile | `.env` |

---

## 总结

你现在应该已经理解了：

1. **Synapse 是什么**：基于 LangChain Deep Agents 的本地编码 Agent
2. **技术栈**：LangChain → LangGraph → create_agent → create_deep_agent → Synapse
3. **核心流程**：CLI → 配置加载 → Agent 创建 → 模型思考 → 工具调用 → 结果返回
4. **关键模块**：agent.py（组装）、config.py（配置）、subagents.py（子 Agent）、middleware.py（中间件）
5. **如何扩展**：添加自定义工具、注册中间件、定义子 Agent、加载 Skills

最重要的是：**动手实践**。把 15.1 节的代码复制到本地，运行它，修改它，打破它，修复它。这是最快的学会方式。