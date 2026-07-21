# Coding Agent 技术方案（基于 LangChain 生态）

> 状态：已落地实现（默认关闭审批 / 无 sandbox）  
> 依赖管理：`uv`  
> 目标：在本空项目中落地一个可本地运行的 Coding Agent（读写代码、执行命令、自测修复）

---

## 1. 结论（先看这个）

| 项 | 建议 |
|---|---|
| 核心框架 | **LangChain + LangGraph + Deep Agents** |
| Agent 入口 | `deepagents.create_deep_agent`（不是从零拼 tool loop） |
| 运行时 | LangGraph（流式、checkpoint、HITL） |
| 编码工作区 | `LocalShellBackend(root_dir=workspace)`（开发阶段） |
| 依赖管理 | `uv` + `pyproject.toml` |
| CLI | 自研轻量 CLI（`typer`/`rich`），不直接依赖官方 `dcode` 二进制 |
| 模型 | 通过 `provider:model` 可切换（OpenAI / Anthropic / 兼容 OpenAI 网关） |
| 第一期目标 | 本地仓库内：读文件、改文件、跑测试、根据错误修复 |

**一句话**：不要从 2023 年的 `AgentExecutor` 起步；直接用 2025+ 的 **Deep Agents harness**，它已经把 coding agent 的核心能力打包好了。

---

## 2. 调研：LangChain 现在怎么做 Agent

### 2.1 生态分层（必须分清）

```text
┌────────────────────────────────────────────┐
│  Deep Agents Code (dcode)                  │  官方终端 coding agent 产品
│  预置交互/审批/技能/沙箱能力                  │
├────────────────────────────────────────────┤
│  deepagents.create_deep_agent              │  batteries-included harness
│  规划 / 文件系统 / 子代理 / 上下文压缩 / HITL │
├────────────────────────────────────────────┤
│  langchain.agents.create_agent             │  最小 harness：model + tools + middleware
├────────────────────────────────────────────┤
│  LangGraph                                 │  图运行时：状态、持久化、流式、中断
├────────────────────────────────────────────┤
│  LangSmith                                 │  观测、评估、部署（可选）
└────────────────────────────────────────────┘
```

| 层级 | 包/入口 | 适合什么 |
|---|---|---|
| 最小 Agent | `langchain.agents.create_agent` | 自定义强，从零组装 middleware |
| 完整 Harness | `deepagents.create_deep_agent` | 复杂多步任务，**最适合 coding agent** |
| 低层编排 | `langgraph` | 强控制确定性流程 + 智能体混合 |
| 官方成品 | Deep Agents Code (`dcode`) | 直接当产品用；二次定制成本高 |

官方关系说明（摘要）：

- **LangGraph** = 图运行时
- **LangChain `create_agent`** = 最小 agent harness
- **Deep Agents** = 在 `create_agent` 之上的意见型 harness（规划、文件、子代理、上下文管理）

### 2.2 Deep Agents 默认内置（对 coding 极关键）

`create_deep_agent(...)` 默认可用：

| 能力 | 工具/机制 | Coding Agent 用途 |
|---|---|---|
| 任务规划 | `write_todos` | 拆任务、跟踪进度 |
| 文件读写 | `ls/read_file/write_file/edit_file/glob/grep` | 浏览与修改代码 |
| 命令执行 | `execute`（需 sandbox 或 LocalShell） | `uv run pytest`、`ruff`、`git` |
| 子代理 | `task` | 并行调研/修复/写测试 |
| 上下文管理 | summarization + offload | 长会话不爆 context |
| 记忆/技能 | `memory=` / `skills=` | 项目约定、编码规范 |
| 人工审批 | `interrupt_on=` | 危险命令先确认 |
| 权限 | `permissions=` | 限制可读写路径 |

### 2.3 Backend 选择（coding agent 的核心设计点）

| Backend | 文件 | Shell | 隔离 | 推荐场景 |
|---|---|---|---|---|
| `StateBackend`（默认） | 状态内虚拟 FS | 否 | 高 | 纯对话/草稿，不落地真文件 |
| `FilesystemBackend` | 本地真实文件 | 否 | 中 | 只改文件不跑命令 |
| `LocalShellBackend` | 本地真实文件 | 是（主机） | **无** | 本地开发 CLI（本项目 Phase 1） |
| Sandbox（E2B/Modal/LangSmith 等） | 沙箱 FS | 是 | 高 | 多租户/不可信输入/生产 |
| `CompositeBackend` | 路由到多后端 | 视路由 | 可混合 | 工作区真文件 + `/memories` 持久化 |

**本项目 Phase 1 选择：`LocalShellBackend`**

- 原因：coding agent 必须“改代码 → 跑测试 → 看失败 → 再改”，本地闭环最快
- 风险：无隔离，可执行任意 shell；必须配合 HITL + 工作区限制 + 明确仅开发机使用

### 2.4 两种实现路径对比

| 路径 | 做法 | 优点 | 缺点 | 结论 |
|---|---|---|---|---|
| A. `create_deep_agent` | 直接用 deepagents harness | 启动快，能力齐 | 黑盒相对多 | **推荐主路径** |
| B. `create_agent` 自组装 | 自己挂 Filesystem/Summarization/Skills middleware | 可控、可教学 | 工程量大 | 作为进阶/定制兜底 |
| C. 直接用 `dcode` | 安装官方 coding agent | 几乎零开发 | 难做产品差异化 | 可参考，不作为本仓库实现主体 |

推荐：**路径 A 为主**；若后续需要极细控制，再拆到路径 B。

---

## 3. Coding Agent 目标定义

### 3.1 产品目标

做一个**本地仓库 Coding Agent**：

1. 理解用户任务（修 bug / 加功能 / 重构 / 写测试）
2. 探索代码库（glob/grep/read）
3. 制定计划（todos）
4. 修改代码（write/edit）
5. 执行验证（pytest / ruff / typecheck）
6. 根据报错继续修复
7. 输出变更说明

### 3.2 非目标（Phase 1 不做）

- 多租户 SaaS / Web IDE
- 完全自主无审批的危险命令
- 自动 push / 自动创建远程 PR（可后续加）
- 替代完整 IDE 的 UI

### 3.3 成功标准

| 指标 | 标准 |
|---|---|
| 基本任务 | 能在示例仓库里修一个失败测试并跑通 |
| 工具闭环 | 至少完成 read → edit → execute → retest |
| 可配置 | 模型、workspace、API Key 可通过 env/配置切换 |
| 可观测 | 终端可见工具调用过程；可选 LangSmith trace |
| 安全底线 | 危险操作可审批；工作区默认限制在指定目录 |

---

## 4. 总体架构

```text
                    ┌──────────────────────┐
  用户输入 ────────▶│  CLI (typer + rich)  │
                    └──────────┬───────────┘
                               │ invoke / stream
                    ┌──────────▼───────────┐
                    │  CodingAgent Facade  │
                    │  - 加载配置           │
                    │  - 组装 create_deep  │
                    │  - session/checkpoint│
                    └──────────┬───────────┘
                               │
         ┌─────────────────────┼─────────────────────┐
         ▼                     ▼                     ▼
   System Prompt         Tools / Backend        Middleware
   + AGENTS.md           LocalShellBackend      HITL / Summarize
   + skills/*            (+ 自定义 git 工具)     Memory / Skills
         │                     │                     │
         └─────────────────────┼─────────────────────┘
                               ▼
                      LangGraph Runtime
                      (stream + checkpoint)
                               │
                               ▼
                      Workspace 文件系统
                      (read/edit/test/run)
```

### 4.1 运行闭环（Agent Loop）

```text
用户任务
  → 规划 write_todos
  → 检索代码 glob/grep/read
  → 修改 write/edit
  → 执行 execute（pytest/ruff）
  → 失败则分析错误并回到修改
  → 成功则总结 diff 与验证结果
```

这就是 Claude Code / Cursor Agent 类产品的核心反馈环：**Write → Run → Observe → Fix**。

---

## 5. 技术选型

### 5.1 依赖（uv 管理）

核心：

| 包 | 作用 |
|---|---|
| `deepagents` | coding harness（`create_deep_agent` + backend） |
| `langchain` | 模型初始化、tool 协议、agent 基础 |
| `langgraph` | 运行时/checkpoint（deepagents 依赖） |
| `langchain-openai` / `langchain-anthropic` | 模型 provider（按需） |
| `typer` | CLI |
| `rich` | 终端流式展示 |
| `pydantic` / `pydantic-settings` | 配置 |
| `python-dotenv` | 本地 `.env` |
| `httpx` | 兼容网关调用（可选） |

开发依赖：

| 包 | 作用 |
|---|---|
| `pytest` | 测试 |
| `ruff` | lint/format |
| `mypy` 或 `pyright` | 类型检查（可选） |

### 5.2 Python / 工具版本

- Python：`>=3.11,<3.14`（建议 3.12）
- 包管理：`uv`
- 初始化命令预览：

```bash
uv init --package
uv add deepagents langchain langgraph langchain-openai typer rich pydantic-settings python-dotenv
uv add --dev pytest ruff
```

### 5.3 模型接入策略

统一用 provider 字符串：

```python
model = "openai:gpt-4.1"              # 或兼容网关
# model = "anthropic:claude-sonnet-4-6"
# model = "openai:xxx" + base_url 自定义
```

配置项：

| 环境变量 | 含义 |
|---|---|
| `MODEL` | 默认模型 ID |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | 密钥 |
| `OPENAI_BASE_URL` | 兼容 OpenAI 的中转 |
| `WORKSPACE` | 默认操作目录 |
| `LANGSMITH_API_KEY` / `LANGSMITH_TRACING` | 可选观测 |
| `AGENT_REQUIRE_APPROVAL` | 是否强制危险命令审批 |

---

## 6. 项目目录规划（空仓起步）

```text
py-agent/
├── pyproject.toml
├── README.md
├── .env.example
├── .gitignore
├── docs/
│   └── design.md                 # 本方案
├── AGENTS.md                     # 项目级记忆/约定（给 agent 读）
├── skills/                       # 可选 skills（渐进披露）
│   └── python-testing/
│       └── SKILL.md
├── src/
│   └── coding_agent/
│       ├── __init__.py
│       ├── __main__.py           # python -m coding_agent
│       ├── cli.py                # typer CLI
│       ├── config.py             # settings
│       ├── agent.py              # create_deep_agent 装配
│       ├── prompts.py            # system prompt
│       ├── backends.py           # LocalShell / Composite 封装
│       ├── tools/                # 自定义工具（可选）
│       │   ├── git.py
│       │   └── project.py
│       ├── safety.py             # 审批策略、命令黑名单
│       └── ui/
│           └── stream.py         # 流式渲染
└── tests/
    ├── test_config.py
    ├── test_safety.py
    └── fixtures/
        └── sample_repo/          # 给 agent 自测用的小仓库
```

---

## 7. 核心模块设计

### 7.1 `agent.py`：装配 Deep Agent

伪代码：

```python
from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend  # 或 CompositeBackend

def build_coding_agent(settings):
    backend = LocalShellBackend(
        root_dir=settings.workspace,
        # 开发机使用；生产再换 sandbox
        env={"PATH": settings.path},
        inherit_env=False,
        timeout=120,
    )

    agent = create_deep_agent(
        model=settings.model,
        system_prompt=CODING_SYSTEM_PROMPT,
        backend=backend,
        memory=["./AGENTS.md"],
        skills=["./skills/"],
        tools=[git_status, git_diff],  # 可选增强
        interrupt_on={
            "execute": True,           # shell 先审批（可配置）
            # 或按更细策略
        },
        # checkpointer=...           # 多轮会话
    )
    return agent
```

### 7.2 System Prompt 设计原则

System Prompt 应明确：

1. 角色：资深软件工程师 / coding agent
2. 工作区边界：只在 `WORKSPACE` 内操作
3. 默认流程：先探索再改；小步提交式修改；改完必须验证
4. 工具策略：优先 `grep/glob`，避免整仓盲读；大文件按行读取
5. 安全：不泄露密钥；不随意删数据；危险命令先说明
6. 输出：变更摘要 + 验证命令 + 结果

### 7.3 安全策略（Phase 1 必须有）

| 层级 | 措施 |
|---|---|
| 工作区 | `root_dir=workspace`，文档约束不越界 |
| 命令审批 | `interrupt_on` 对 `execute` / 危险路径开启 |
| 命令策略 | 黑名单：`rm -rf /`、格式化磁盘、改 SSH key 等 |
| 密钥 | 不把 `.env` 内容回显到日志；工具输出截断 |
| 运行环境 | Phase 1 仅本地受控开发机 |
| 后续 | 换 SandboxBackend，禁止主机 shell |

> Deep Agents 文档明确：`LocalShellBackend` **无隔离**，`virtual_mode` 不能限制 shell。安全边界必须在工具/审批层做，不能指望模型自觉。

### 7.4 CLI 交互

```bash
# 交互模式
uv run coding-agent chat --workspace .

# 单次任务
uv run coding-agent run "修复 tests/test_foo.py 中的失败用例" --workspace ./demo

# 指定模型
uv run coding-agent run "..." --model openai:gpt-4.1
```

CLI 能力：

- 流式打印 assistant token / tool call / tool result
- 审批提示：`[approve/edit/reject]`
- 显示当前 todos
- 退出时打印 session id（便于恢复）

### 7.5 会话与持久化

Phase 1：

- 内存 checkpointer 或本地 sqlite checkpointer
- `thread_id` 支持多轮对话

Phase 2：

- 跨会话 memory（`/memories` + StoreBackend）
- 项目级 `AGENTS.md` 自动沉淀约定

---

## 8. 自定义工具规划

Deep Agents 已有文件系统与 execute；本项目只补 coding 场景高频缺口。

| 工具 | 优先级 | 说明 |
|---|---|---|
| `git_status` | P1 | 看工作区状态 |
| `git_diff` | P1 | 看变更 |
| `run_tests` | P2 | 封装 `uv run pytest`（可只是 prompt 约定） |
| `apply_patch` | P2 | 若 edit_file 不够稳再补 |
| MCP tools | P3 | 接浏览器/issue 系统等 |

原则：**能用内置工具解决的，不重复造轮子**。

---

## 9. 分阶段实施计划

### Phase 0：工程骨架（0.5 天）

- [ ] `uv` 初始化项目与包结构
- [ ] `pyproject.toml` / README / `.env.example` / `.gitignore`
- [ ] 可安装入口：`coding-agent` / `python -m coding_agent`

### Phase 1：最小可用 Coding Agent（1–2 天）

- [ ] 配置加载（model/workspace/keys）
- [ ] `create_deep_agent` + `LocalShellBackend`
- [ ] system prompt + `AGENTS.md`
- [ ] CLI：`run` / `chat`
- [ ] 流式输出工具调用
- [ ] 在 `tests/fixtures/sample_repo` 上跑通“修测试”

### Phase 2：安全与可用性（1 天）

- [ ] execute 审批开关
- [ ] 危险命令策略
- [ ] 输出截断与错误友好提示
- [ ] checkpoint 多轮会话

### Phase 3：增强（按需）

- [ ] skills（测试规范、提交规范）
- [ ] git 工具增强 / 自动 commit（需审批）
- [ ] LangSmith tracing
- [ ] Sandbox backend 切换
- [ ] 子代理：`researcher` / `tester` / `reviewer`

---

## 10. 关键实现草图（评审通过后落地）

### 10.1 `pyproject.toml` 方向

```toml
[project]
name = "coding-agent"
version = "0.1.0"
description = "A local coding agent built on LangChain Deep Agents"
requires-python = ">=3.11"
dependencies = [
  "deepagents",
  "langchain",
  "langgraph",
  "langchain-openai",
  "typer",
  "rich",
  "pydantic-settings",
  "python-dotenv",
]

[project.scripts]
coding-agent = "coding_agent.cli:app"

[dependency-groups]
dev = ["pytest", "ruff"]
```

### 10.2 最小调用形态

```python
agent = build_coding_agent(settings)
result = agent.invoke({
    "messages": [
        {"role": "user", "content": "修复 sample_repo 里失败的测试"}
    ]
})
```

流式：

```python
for event in agent.stream({"messages": [...]}, stream_mode="updates"):
    render(event)
```

---

## 11. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| LocalShell 无隔离 | 误删/危险命令 | HITL + 黑名单 + 仅开发机 |
| 模型乱改文件 | 代码质量差 | 小步修改、强制测试、diff 展示 |
| Context 膨胀 | 成本高/失败 | 依赖 deepagents 压缩；大结果落盘 |
| Provider 差异 | tool calling 不稳 | 优先选强 tool-calling 模型；统一 init_chat_model |
| deepagents API 演进快 | 升级成本 | 封装在 `agent.py`，少在业务层直接依赖细节 |
| 与官方 dcode 能力重叠 | 重复造轮子 | 本项目聚焦“可定制 SDK/CLI”，不追求完全对标 |

---

## 12. 为什么不直接用官方 Deep Agents Code

官方 `dcode` 已是成熟终端 coding agent。本项目仍建议自建的原因：

1. 可完全控制 prompt、审批、工具与产品形态
2. 便于嵌入现有平台/工作流（后续可变成服务）
3. 学习与沉淀本团队的 agent 工程能力
4. 依赖面更小，便于二次开发

参考价值：把 `dcode` 当作产品对标与 UX 参考，而不是依赖它的二进制。

---

## 13. 推荐默认决策（请确认）

| 决策点 | 默认选择 | 备选 |
|---|---|---|
| Harness | `create_deep_agent` | 自组装 `create_agent` |
| Backend | `LocalShellBackend` | 后期 Sandbox |
| 包管理 | `uv` | — |
| CLI | typer + rich | textual TUI |
| 会话 | 本地 checkpointer | 仅单次 invoke |
| 审批 | execute **默认关闭**（auto-pass） | `--require-approval` 可开启 |
| 测试策略 | fixture 小仓库 E2E | 纯单测 |

---

## 14. 下一步

评审通过后立即执行：

1. 用 `uv` 初始化包与依赖
2. 落地 `config/agent/cli` 最小闭环
3. 用 `sample_repo` 验证“读-改-测-修”
4. 补安全审批与 README 使用说明

---

## 15. 参考资料

- Deep Agents overview: https://docs.langchain.com/oss/python/deepagents/overview
- create_deep_agent API: https://reference.langchain.com/python/deepagents/graph/create_deep_agent
- Backends: https://docs.langchain.com/oss/python/deepagents/backends
- LocalShellBackend 安全说明: https://reference.langchain.com/python/deepagents/backends/local_shell/LocalShellBackend
- LangChain agents: https://docs.langchain.com/oss/python/langchain/agents
- deepagents GitHub: https://github.com/langchain-ai/deepagents
- Deep Agents Code: https://docs.langchain.com/oss/python/deepagents/code/overview
- Deep agent from scratch: https://docs.langchain.com/oss/python/langchain/deep-agent-from-scratch
