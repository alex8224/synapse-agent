# 子代理（Subagents）

Synapse 的内置子代理（`researcher` / `tester` / `reviewer`）通过 deepagents 的 `task`
工具暴露，采用 **manager 模式（agent-as-tool）**：主 Agent 保留最终回答所有权，子 Agent
做有界任务、返回 summary。

本页描述子代理系统的**三层架构**、**自定义扩展方式**，以及为未来 **handoff** 与
**workflow 编排**预留的演进基础。

## 三层架构

子代理系统把「定义」「注册」「编译」三层解耦，使未来的编排拓扑（handoff、显式图编排）
可以复用同一套定义与注册，而不需要改动解析与加载逻辑。

```mermaid
flowchart TB
    subgraph SRC["来源层"]
        B["内置定义<br/>researcher / tester / reviewer"]
        U["~/.synapse/agents/*.md"]
        P["workspace/.synapse/agents/*.md"]
        E["custom_agents_dirs"]
    end

    subgraph DEF["描述层（与拓扑无关）"]
        D["SubAgentDefinition<br/>name · description · system_prompt · model<br/>tools · disallowed_tools<br/>ownership · output_schema · enabled"]
    end

    subgraph REG["注册层（唯一事实来源）"]
        R["SubagentRegistry<br/>分层合并 · 同名覆盖 · 禁用 · name 唯一"]
    end

    subgraph CMP["编译层（可插拔，按拓扑模式）"]
        C1["compile_task_specs<br/>现状：agent-as-tool"]
        C2["compile_handoffs<br/>未来：所有权转移"]
        C3["compile_workflow<br/>未来：图节点 / 边"]
    end

    subgraph RT["运行时"]
        T["deepagents task 工具"]
        H["handoff 原语"]
        G["LangGraph StateGraph"]
    end

    B --> D
    U --> D
    P --> D
    E --> D
    D --> R
    R --> C1
    R --> C2
    R --> C3
    C1 --> T
    C2 --> H
    C3 --> G
```

- **描述层**：`SubAgentDefinition` 是拓扑无关的声明式描述，只回答「这个子代理是谁、能干什么」。
- **注册层**：`SubagentRegistry` 是「有哪些子代理」的唯一事实来源。
- **编译层**：每个拓扑模式一个编译器；当前只有 `compile_task_specs`，未来新增
  `compile_handoffs` / `compile_workflow` 时定义层与注册层零改动。

## 自定义子代理

在用户层 `~/.synapse/agents/*.md` 或项目层 `<workspace>/.synapse/agents/*.md` 放置
Markdown 文件即可新增子代理。YAML frontmatter 提供元数据，文件正文是子代理的
system prompt。

首次启动时（`AGENT_ENABLE_CUSTOM_SUBAGENTS=true`），Synapse 会在
`~/.synapse/agents/` 自动生成 `researcher.md` / `tester.md` / `reviewer.md` 三个
内置定义的种子文件。编辑这些文件即可覆盖对应内置角色；已存在的文件不会被覆盖或
重复生成（除非删除后重启）。同名文件存在但未显式写 `model` 时，
`AGENT_SUBAGENT_*_MODEL` 仍会生效；显式写 `model`（或 `model: inherit`）后由文件接管。

```markdown
---
name: security-reviewer
description: Use after security-sensitive changes. Reviews for injection and secret leaks.
model: inherit            # 或 "provider:model-name"
tools: [read_file, search_files, find_files, execute]   # 可选 allowlist
disallowed_tools: [write_file, edit_file]               # 可选 denylist
ownership: task           # 预留字段，当前仅支持 task
---

You are a security reviewer. Inspect diffs for...
```

### frontmatter 字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `name` | 是 | 唯一标识；与内置同名时覆盖内置定义 |
| `description` | 是 | 主 Agent 据此决定何时委托（路由规则，写具体触发条件） |
| （正文） | 是 | 子代理 system prompt |
| `model` | 否 | `inherit` 或 `provider:model-name`；缺省继承主 Agent 模型 |
| `reasoning_effort` | 否 | `off`/`minimal`/`low`/`medium`/`high`/`max`；缺省继承主 Agent 当前推理级别 |
| `tools` | 否 | allowlist：`null` 继承 `find_files`/`search_files`；`[]` 仅用 deepagents 内置工具；`[names]` 按名过滤主 Agent 工具 |
| `disallowed_tools` | 否 | denylist，作用于继承/allowlist 之后的工具集 |
| `ownership` | 否 | `task` 或 `handoff`（预留）；当前仅 `task` 参与编译 |
| `output_schema` | 否 | 预留：未来 workflow 节点间结构化契约 |
| `enabled` | 否 | `false` 时跳过编译 |

## 合并与加载流程

扫描顺序：用户层 → 可执行目录层 → 项目层 → `custom_agents_dirs`。后出现的定义覆盖
同名的先出现定义（项目覆盖用户）。解析失败的文件被跳过并记录 warning，不会导致启动失败。

```mermaid
flowchart LR
    A["layered_agents_dirs<br/>user → exe → project"] --> S["扫描 *.md"]
    S --> F{"parse 成功?"}
    F -- "否" --> W["记录 warning 并跳过<br/>（degradation，不崩溃）"]
    F -- "是" --> N{"name 已存在?"}
    N -- "是" --> O["后者覆盖前者<br/>project 覆盖 user"]
    N -- "否" --> I["加入 registry"]
    O --> I
    I --> B{"与内置同名?"}
    B -- "是" --> C["覆盖内置定义"]
    B -- "否" --> K["作为自定义追加"]
    C --> DD{"name 在<br/>disable_builtin_subagents?"}
    K --> DD
    DD -- "是" --> X["移除"]
    DD -- "否" --> OUT["输出最终定义集"]
```

## 编译流程

`compile_task_specs` 把 `SubAgentDefinition` 编译为 deepagents `SubAgent` dict
（`task` 工具消费）。`ownership != task` 与 `enabled = false` 的定义被跳过，留给未来的
handoff / workflow 编译器。

```mermaid
flowchart TB
    R["SubagentRegistry"] --> D["遍历 definitions"]
    D --> OWN{"ownership?"}
    OWN -- "handoff" --> H["跳过，留给 compile_handoffs"]
    OWN -- "task" --> T{"tools allowlist 指定?"}
    T -- "是" --> F["按 name 从 inherit_tools 过滤"]
    T -- "否" --> I2["继承白名单<br/>find_files / search_files"]
    F --> M["model 解析"]
    I2 --> M
    M --> MD{"model == inherit?"}
    MD -- "是" --> NS["不设置 spec.model"]
    MD -- "否" --> SS["写入 provider:model"]
    NS --> MW["附加 middleware<br/>intent + 工具排除 + tool_output 转换"]
    SS --> MW
    MW --> OUT["deepagents SubAgent dict"]
    OUT --> TG["task 工具 spec"]
```

工具排除规则（`build_tool_exclusion_middleware`）：

- `disallowed_tools` 全部进入 blocked 集合；
- 继承/allowlist 主 Agent 工具时，额外隐藏内置搜索工具 `ls`/`glob`/`grep`（避免与
  `find_files`/`search_files` 重复）；
- `write_todos`/`todo_write`/`todos` 总是被排除（产品级隔离）。

## 运行时时序（task 模式）

```mermaid
sequenceDiagram
    participant M as 主 Agent
    participant TT as task 工具
    participant R as Registry（编译产物）
    participant SA as Subagent（独立 context）

    M->>TT: task(subagent_type=reviewer, ...)
    TT->>R: 查找 reviewer spec
    R-->>TT: system_prompt / model / tools / middleware
    TT->>SA: 独立 context 启动运行
    SA-->>TT: 返回 summary 结果
    TT-->>M: 结果回传，主 agent 保留最终回答所有权
```

## 配置字段

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AGENT_ENABLE_SUBAGENTS` | `true` | 启用子代理 |
| `AGENT_SUBAGENT_TESTER_MODEL` | — | Tester 子代理模型 |
| `AGENT_SUBAGENT_REVIEWER_MODEL` | — | Reviewer 子代理模型 |
| `AGENT_SUBAGENT_RESEARCHER_MODEL` | — | Researcher 子代理模型 |
| `AGENT_ENABLE_CUSTOM_SUBAGENTS` | `true` | 加载 `.synapse/agents/*.md` 自定义子代理 |
| `AGENT_CUSTOM_AGENTS_DIRS` | `[]` | 额外扫描目录（JSON 数组） |
| `AGENT_DISABLE_BUILTIN_SUBAGENTS` | `[]` | 禁用的内置子代理名（JSON 数组） |
| `AGENT_SUBAGENT_DEFAULT_MODEL` | — | 子代理全局默认模型 |
| `AGENT_SUBAGENT_DEFAULT_REASONING_EFFORT` | — | 子代理全局默认推理级别 |
| `AGENT_SUBAGENT_MODEL_OVERRIDES_JSON` | `{}` | 按子代理名覆盖模型（JSON 对象） |
| `AGENT_SUBAGENT_REASONING_EFFORT_OVERRIDES_JSON` | `{}` | 按子代理名覆盖推理级别（JSON 对象） |

### 模型与推理级别解析优先级

TUI 中使用 `/subagent` 打开模型配置页，可以设置全局默认与按名覆盖；这些值持久化到分层
`settings.json`。编译时按以下优先级（高 → 低）解析每个子代理的模型与推理级别：

1. 按名覆盖（TUI 中某个子代理的独立配置）
2. 定义文件 frontmatter 中的 `model` / `reasoning_effort`
3. 全局默认（`(Global defaults)`）
4. 全部未配置 → 继承主 Agent 当前模型与推理级别（`inherit`）

两条轴（模型、推理级别）独立解析：只覆盖其中一条不会影响另一条。当显式指定模型
（或只覆盖推理级别）时，子代理使用独立构建的模型实例；推理级别为 `off` 时关闭
thinking，为具体级别时以对应 `reasoning_effort` 开启。未指定推理级别时继承主 Agent
当前会话设置。保存配置后当前 Agent 会立即重建，无需重启。

`inherit` 表示“该层未配置”，等价于删除该层的值：TUI 编辑框中的 `inherit` 不会写入
任何覆盖，因此该角色的 frontmatter 或全局默认仍然生效。按名覆盖始终优先于
frontmatter；只有选择 `inherit`（或删除覆盖）时才会回退到
frontmatter → 全局默认 → 主 Agent。`reasoning_effort` 的合法值为
`off`/`minimal`/`low`/`medium`/`high`/`max`/`inherit`，配置与 frontmatter 均校验。

## 演进路径：handoff 与 workflow

当前只实现了 task 模式（manager 模式）。定义层的 `ownership` 与 `output_schema` 字段、
注册层的唯一事实来源，为两种未来拓扑预留了接入点；届时只需新增编译器。

```mermaid
flowchart TB
    R["SubagentRegistry<br/>唯一事实来源"] --> C1["compile_task_specs"]
    R --> C2["compile_handoffs"]
    R --> C3["compile_workflow"]

    C1 --> O1["task 工具<br/>manager 模式（现状）"]
    C2 --> O2["handoff 原语<br/>所有权转移（未来）"]
    C3 --> O3["图节点 + 条件边<br/>显式编排（未来）"]

    O1 --> M["混合拓扑<br/>三种模式共存于同一 StateGraph"]
    O2 --> M
    O3 --> M

    style C2 stroke-dasharray: 5 5
    style C3 stroke-dasharray: 5 5
    style O2 stroke-dasharray: 5 5
    style O3 stroke-dasharray: 5 5
```

| 未来模式 | 语义 | 复用本次基础 |
| --- | --- | --- |
| handoff | 子代理接管下一轮响应所有权（OpenAI Agents SDK / Swarm 语义） | 同一 `Definition`/`Registry`；`ownership=handoff` 字段已就位 |
| workflow | 显式图编排：节点 + 条件边 + 结构化 state 契约（LangGraph 语义） | 同一 `Registry`；`output_schema` 字段已就位；底层 StateGraph 原生支持 |

## 兼容性与降级

- 无 `agents/` 目录时，`build_default_subagents()` 输出与重构前完全一致。
- 单个定义文件解析失败只跳过该文件，不影响其余定义与启动。
- `permissions` 字段（deepagents FilesystemPermission）与 shell backend 不兼容，本层不暴露，
  隔离通过工具排除 middleware + system prompt 实现。
