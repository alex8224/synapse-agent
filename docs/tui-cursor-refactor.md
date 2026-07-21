# TUI Cursor 风格重构方案

> 状态：主路径已实施（Phase 0–3）；残留见 `step.md`  
> 进度：见仓库根目录 `step.md`  
> 对标：Cursor Agent Chat 工具时间线 / Thought / 文件预览  
> 范围：`coding_agent.ui.tui` + `coding_agent.ui.stream` Sink 契约  
> 非目标：像素级复刻 Cursor 全部 IDE chrome；不改 agent 运行时语义

---

## 1. 目标

把现有「RichLog 追加流水」TUI，重构成 **Cursor 式 transcript 时间线**：

| 区块 | 目标表现 |
|---|---|
| 顶栏 | `≡ workspace` + 右侧 token / model |
| 用户 | 灰条 `› prompt` + 时间戳 |
| Thought | `◆ Thought for Xs`，默认可折叠，可展开全文 |
| 工具组 | 组头 `▾ Read 22 files, Searched 1 pattern`（灰条可折叠） |
| 工具明细 | 组内 `◆ Read design.md` / `◆ Read README.md` |
| 工具预览 | 选中/展开某条时显示截断正文（带行号风格） |
| 回答 | 主区干净 Markdown，无 `Assistant:` 标签 |
| 输入 | `› Build anything`，无 footer chrome |

**成功标准**：同一轮「读多文件 + 搜索」在 UI 上应接近参考截图结构，而不是一行聚合摘要。

---

## 2. 现状与根因

### 2.1 现状（简表）

| 能力 | 现状 |
|---|---|
| 用户条 / 顶栏 / 输入 | 基本对齐 |
| Thought | 有标题 + 截断正文；`Ctrl+E` 只能追加，不能真正折叠 |
| 工具 | 仅 batch 结束后一行 `◆ Read N files…` |
| 单工具路径 | 未渲染（args 被丢掉） |
| 结果预览 | 无（`tool_result` 只有短 status） |
| 交互折叠 | 无（RichLog 追加后不可改写） |

### 2.2 根因

1. **数据契约过窄**  
   `StreamSink.tool_result(name, status)` 只传摘要字符串，UI 拿不到 path / 正文预览。

2. **渲染模型过粗**  
   `TextualStreamSink` 把一批工具压成一个 summary，丢失明细。

3. **组件选型限制**  
   主区 `RichLog` 适合不可变 transcript，不适合「组内折叠 / 选中预览 / 原地更新」。

---

## 3. 设计原则

| 原则 | 说明 |
|---|---|
| 先数据后 UI | 先扩 Sink 契约与 tool 事件模型，再换组件 |
| 渐进可回退 | CLI `ConsoleStreamSink` 行为保持；TUI 可选新路径 |
| 默认信息密度对齐 Cursor | 组头聚合 + 默认展开明细；预览截断，不 dump 全文 |
| 小步可测 | 每阶段有单测；不依赖真实 LLM |
| 不阻塞 agent | UI 失败不得影响 `stream_agent` 主路径 |

---

## 4. 目标信息架构

```text
┌─ topbar ──────────────────────────────── tokens ─┐
│ ≡ workspace · model                              │
├─ transcript (scroll) ────────────────────────────┤
│ [user bar] › prompt                    5:59 PM   │
│                                                  │
│  ◆ Thought for 1.4s                        ▾/▸  │
│    <dim thought body when expanded>              │
│  │                                               │
│  ▾ Read 22 files, Searched 1 pattern     [group] │
│  │ ◆ Read design.md                              │
│  │ ◆ Read README.md          ← selected          │
│  │   ┌─ preview (truncated, line-ish) ─────────┐  │
│  │   │ 1  # title ...                        │  │
│  │   │ …                                     │  │
│  │   └───────────────────────────────────────┘  │
│  │ ◆ Read pyproject.toml                         │
│  │                                               │
│  <Markdown answer>                               │
├─ status (1 line, busy only) ─────────────────────┤
├─ input › Build anything ─────────────────────────┤
└──────────────────────────────────────────────────┘
```

### 4.1 时间线语义

- 左侧 `│` / `◆` 表示 **同一 turn 内事件顺序**，不是文件系统树。
- Thought、ToolGroup、Answer 都是 turn 内节点。
- ToolGroup 内部是 **扁平明细列表**（按 tool call 顺序），不是按目录分组。

---

## 5. 数据模型

### 5.1 新增（建议放 `ui/timeline.py` 纯逻辑，无 Textual 依赖）

```python
@dataclass
class ToolItem:
    id: str                 # 稳定 id：msg_id+index 或 uuid
    name: str               # read_file / grep / execute ...
    category: str           # read|edit|list|search|run|task|other
    label: str              # "Read README.md" / "Run pytest -q"
    path: str | None        # 主路径（若有）
    status: str             # ok / error / running
    preview: str | None     # 结果截断正文（可空）
    error: bool = False

@dataclass
class ToolGroup:
    id: str
    summary: str            # "Read 22 files, Searched 1 pattern"
    items: list[ToolItem]
    collapsed: bool = False
    running: bool = True

@dataclass
class ThoughtBlock:
    elapsed_s: float
    body: str
    collapsed: bool = False   # Cursor 默认更像展开摘要；可配置

@dataclass
class TurnModel:
    user_text: str
    thought: ThoughtBlock | None
    tool_groups: list[ToolGroup]
    answer_md: str | None
```

### 5.2 展示文案规则（对齐截图）

| category | 单条 label | 组头短语 |
|---|---|---|
| read | `Read {basename}` | `Read N file(s)` |
| edit | `Edited {basename}` | `Edited N file(s)` |
| list | `Listed {path/dir}` | `Listed N dir(s)` |
| search | `Searched {pattern}` | `Searched N pattern(s)` |
| run | `Run {cmd/desc}` | `Ran N command(s)` |
| other | `{name}` | 原名拼接 |

组头：按 **首次出现类别顺序** 拼接，如  
`Listed 1 dir, Read 1 file` / `Read 22 files, Searched 1 pattern`。

路径显示优先 `basename`；同名冲突时显示短相对路径。

---

## 6. StreamSink 契约扩展

### 6.1 现状

```text
tool_calls_started(calls, parallel)
tool_result(name, status, *, sub=False)
```

### 6.2 目标契约（向后兼容）

保持旧方法可用；TUI 走增强路径：

```python
class StreamSink(Protocol):
    # 既有
    def tool_calls_started(self, calls: list[Any], *, parallel: bool) -> None: ...
    def tool_result(self, name: str, status: str, *, sub: bool = False) -> None: ...

    # 新增（可选实现；stream 侧 duck-type 检测）
    def tool_item_started(self, item: ToolItem) -> None: ...
    def tool_item_finished(
        self,
        item_id: str,
        *,
        status: str,
        preview: str | None = None,
        error: bool = False,
    ) -> None: ...
    def tool_group_closed(self, group_id: str) -> None: ...
```

### 6.3 stream.py 改造点

| 点 | 改动 |
|---|---|
| tool call 出现 | 解析 `name/args` → 构造 `ToolItem`（含 path/label） |
| tool message 到达 | 取 content，经截断生成 `preview`；`status` 仍可用 `summarize_tool_result` |
| 并行批 | 同一 `ToolGroup` 内累加 items；pending=0 时 `tool_group_closed` |
| 兼容 | 若 sink 无新方法，回退旧 `tool_calls_started` + `tool_result` |

### 6.4 preview 策略

| 规则 | 值 |
|---|---|
| 最大字符 | 2_000（可配置） |
| 最大行数 | 40 |
| 二进制/空 | 不展示预览，仅 status |
| 错误 | preview 放错误摘要，行标红 |
| 大文件 read | 只展示模型实际返回片段，不二次读盘 |

> 注意：预览内容来自 **tool message content**，不在 UI 层重新 `open()` 文件（避免权限/路径语义分叉）。

---

## 7. UI 组件方案

### 7.1 分层

```text
stream_agent
    └─ TextualStreamSink          # 事件 → TurnModel 变更
           └─ CodingAgentApp
                  ├─ TopBar
                  ├─ TranscriptView     # 可滚动容器
                  │    ├─ UserBar
                  │    ├─ ThoughtWidget  (Collapsible)
                  │    ├─ ToolGroupWidget(Collapsible)
                  │    │     ├─ ToolItemRow
                  │    │     └─ PreviewPanel
                  │    └─ AnswerBlock (Markdown)
                  ├─ StatusLine
                  └─ PromptInput
```

### 7.2 组件选型

| 区域 | 组件 | 原因 |
|---|---|---|
| 完成的历史 turn | 可冻结为 RichLog/Static 快照 | 省内存、少重排 |
| 当前 turn | Vertical + Collapsible/Static | 需要折叠与原地更新 |
| 工具预览 | Static/RichLog 小窗 | 显示截断代码 |
| 输入 | Input | 保持 |

**关键决策**：  
不要把整轮 transcript 永远塞进单一 `RichLog`。  
采用 **历史快照 + 当前 turn 活动树** 双区：

1. `#history`：已完成 turn 的只读渲染（写入后不改）
2. `#live`：当前 turn 的可更新 widgets

turn 结束后，把 `#live` 内容 **commit 进 `#history`** 并清空 live。

### 7.3 交互绑定

| 按键 | 行为 |
|---|---|
| `Ctrl+E` | 切换最近 Thought 折叠 |
| `Ctrl+T` | 切换最近 ToolGroup 折叠 |
| `Enter`（Input） | 提交 prompt |
| `Ctrl+L` | 清屏 |
| `Ctrl+C/Q` | 退出 |

点击折叠：若 Textual Collapsible 可用则优先鼠标；键盘为保底。

### 7.4 视觉 token（保持现有 palette）

| 用途 | 色 |
|---|---|
| 背景 | `#1a1b1e` |
| 顶栏/底暗 | `#121316` |
| 用户灰条 | `#2b2d31` |
| 主文字 | `#e8eaed` |
| 次级/Thought | `#9aa0a6` |
| 更弱 | `#5f6368` |
| 成功/Run | `#81c995` |
| 文件名强调 | 橙/蓝（如 `#f4b183` / `#8ab4f8`） |
| 错误 | red |

---

## 8. 模块与文件变更清单

| 文件 | 动作 | 说明 |
|---|---|---|
| `src/coding_agent/ui/timeline.py` | **新增** | 纯数据模型 + label/summary 算法 + preview 截断 |
| `src/coding_agent/ui/stream.py` | 改 | 可选增强 tool 事件；兼容旧 sink |
| `src/coding_agent/ui/tui.py` | 大改 | history/live 双区；ToolGroup/Thought widgets |
| `src/coding_agent/ui/widgets/` | **可选新增** | 若 tui.py 过大则拆 `thought.py` / `tool_group.py` |
| `tests/test_timeline.py` | **新增** | label/summary/preview 纯逻辑单测 |
| `tests/test_tui_sink.py` | 改 | 覆盖 item 级事件与聚合 |
| `tests/test_stream_tools.py` | **可选** | mock tool messages → sink 调用序列 |
| `docs/tui-cursor-refactor.md` | 本文件 | 方案与计划 |

**不改**：agent 图、tools 实现、backend、审批语义。

---

## 9. 分阶段实施计划

### Phase 0 — 冻结契约与纯逻辑（0.5d）

- [ ] 新增 `timeline.py`：`ToolItem` / `ToolGroup` / summary & label 算法
- [ ] 单测：截图同款文案  
  - `Listed 1 dir, Read 1 file`  
  - `Read 22 files, Searched 1 pattern`  
  - `Read README.md`
- [ ] preview 截断单测

**验收**：无 Textual 依赖的纯测全绿。

### Phase 1 — 扩展 stream 事件（0.5–1d）

- [ ] stream 在 tool call 时生成 `ToolItem`
- [ ] tool message 时填充 `preview` + 最终 status
- [ ] duck-type：新 sink 用 item API；旧 sink 仍走 name/status
- [ ] CLI 输出保持可读（可仍用旧路径）

**验收**：单元测试断言 sink 调用顺序；CLI 冒烟无回归。

### Phase 2 — TUI 明细列表（无真折叠交互）（1d）

- [ ] `TextualStreamSink` 按 group 缓存 items
- [ ] 渲染：

```text
  ▾ Read 22 files, Searched 1 pattern
  ◆ Read design.md
  ◆ Read README.md
  ◆ Read pyproject.toml
```

- [ ] 单条下若有 preview：默认折叠，仅显示最近 1 条或错误条的短预览
- [ ] 历史仍可先写 RichLog（实现快）

**验收**：对照截图结构接近；测试覆盖 sink → app 调用。

### Phase 3 — 真折叠 + 选中预览（1–2d）

- [ ] history/live 双区布局
- [ ] Thought / ToolGroup 使用可折叠组件
- [ ] 选中 tool item 展开 PreviewPanel（行号样式可选）
- [ ] `Ctrl+E` / `Ctrl+T` 绑定
- [ ] turn end：live commit → history

**验收**：手工 `coding-agent tui` 跑一轮读多文件，交互符合截图。

### Phase 4 — 抛光与清理（0.5d）

- [ ] 连续左侧 timeline 轨道视觉
- [ ] 文件名着色、Run 绿色 diamond
- [ ] token 顶栏刷新
- [ ] 删除过时聚合-only 代码路径
- [ ] README/AGENTS 补充 TUI 快捷键
- [ ] ruff + pytest 全绿

---

## 10. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| RichLog 无法原地更新 | 折叠体验差 | Phase 3 上 live widgets |
| tool message 过大 | UI 卡顿 / 内存 | 严格 preview 截断；不存全文 |
| 并行 tool 顺序乱 | 明细顺序跳动 | 以 call 发起顺序为 item 序；完成只更新 status |
| 子 agent 工具刷屏 | 噪声 | `sub=True` 默认折叠到 `Launched N subagents` 组 |
| Textual 版本差异 | Collapsible API 不稳 | 先用 Static + 按键切换，再升鼠标折叠 |
| 契约变更破坏 CLI | 回归 | duck-type + 旧方法保留 |

---

## 11. 非目标（本轮不做）

- 复刻 Cursor 的 diff 视图 / 应用补丁 UI
- 工具结果完整分页浏览器
- 远程/多会话侧边栏
- 主题系统（只固定 dark palette）
- 将 CLI rich 输出也改成同一套折叠组件

---

## 12. 验收清单（最终）

- [ ] 多文件读取显示为 **组头 + 明细**，不是单行 “Read N files”
- [ ] 明细 label 含 basename（`Read README.md`）
- [ ] 至少支持展开 1 条 tool 的截断 preview
- [ ] Thought 可折叠/展开
- [ ] 回答区仍是干净 Markdown
- [ ] 输入 `› Build anything`；无 footer
- [ ] `tests/test_timeline.py` + `tests/test_tui_sink.py` 通过
- [ ] `ruff check` 通过
- [ ] `coding-agent tui` 手工一轮无 traceback

---

## 13. 建议执行顺序（开工指令）

1. 先合 **Phase 0**（纯逻辑，风险最低）  
2. 再 **Phase 1**（stream 契约，CLI 兼容）  
3. **Phase 2** 先给“看起来像”的明细列表（快速可见）  
4. **Phase 3** 再上真折叠与 preview  
5. **Phase 4** 抛光

默认开工范围：用户确认本方案后，从 Phase 0 开始实现。

---

## 14. 开放问题（实现前确认）

| # | 问题 | 默认建议 |
|---|---|---|
| Q1 | ToolGroup 默认展开还是折叠？ | **展开明细**（对齐截图） |
| Q2 | preview 默认展示几条？ | 仅 **当前选中 1 条**；无选中时不展示 |
| Q3 | Thought 默认展开还是只标题？ | **标题 + 短摘要 1 段**；全文折叠 |
| Q4 | 是否拆 `ui/widgets/`？ | Phase 3 若 `tui.py` > ~800 行再拆 |
| Q5 | 历史 turn 是否允许回看折叠？ | 第一版 **冻结快照**（展开态按 commit 时状态） |

---

## 15. 变更记录

| 日期 | 说明 |
|---|---|
| 2026-03-22 | 初稿：对照 Cursor 截图差距，给出数据模型 / Sink / 分阶段计划 |
