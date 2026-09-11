# Synapse Web Console 设计方案与技术规范

> 文档状态：Active<br>
> 实施状态：In Progress<br>
> 目标：提供脱离终端（TUI/CLI）的轻量、高性能、符合人体工学的现代 Web 控制台，直连 synapse-runtime WebSocket JSON-RPC 服务。

---

> **v5 切片 2 修订**：正式 Web 控制台改经 loopback `synapse-web-console`
> 宿主（静态产物 + 一次性配对码认证 + WebSocket 中继，见 `formal-host.md`），
> 浏览器不再持有 daemon bearer、不再直连 daemon 鉴权，也不再通过
> `GET /api/bootstrap` 自动签发会话（该端点已删除、恒返回 405）。
> 只读诊断端点 `GET /api/runtime-status`（需有效会话 cookie + Host 允许表）见 `formal-host.md` 第 3 节。
> 下文第 1 节的直连 Bearer 架构图与第 3 节协议表为 v4 前的设计叙述，生产
> 部署以 v5 宿主说明为准。

## 1. 架构定位

```
[Browser / Web Console (React + TS)]
                 │
                 │ WebSocket JSON-RPC 2.0 (Bearer Auth)
                 ▼
     [synapse-runtime 守护进程]
                 │
                 ▼
       [AgentRuntimeService]
                 │
                 ▼
      [SessionRuntime / LLM]
```

- **服务端依赖**：直连现有的 synapse.runtime.transport.RuntimeWebSocketServer（默认端口或 --port 启动）。
- **前端定位**：单页应用（SPA），以无干扰的极简主义（Kinetic Mono 风格）提供接近 TUI 且体验更优的流式对话、思维链折叠、工具调用查看与 Steer 实时插话能力。

---

## 2. 界面人体工学规范 (基于最新 UI 成果)

| 区域 | 规范与要素 |
|---|---|
| **TopBar (顶栏)** | 单行极简：[\|] 侧栏折叠开关 · 📁 工作区路径 · ⎇ Git分支(状态灯) · 会话标题 · 右侧上下文/Token统计指标 |
| **SideBar (侧栏)** | 240px 宽度，支持折叠收起为极简工作区；顶部含 + 新建会话 (Ctrl+N) 与搜索 (Ctrl+K)；会话按时间分组；底栏仅保留纯净设置入口，无头像与通知铃铛杂音 |
| **Transcript (主画布)** | 用户提问胶囊 · ◆ Thought for Xs 折叠思考链 · ▾ N tools executed 折叠工具栏 · Markdown 代码块与流式输出 |
| **CommandBar (输入区)** | 悬浮居中卡片；若有正在运行的任务则浮现 Steer queue 状态；单一蓝色圆形发送按钮 ↑（空闲为普通发送，忙碌自动转为 Steer 插话排队） |
| **BottomBar (底栏)** | 底部常驻：模型切换下拉 · 推理级别选择 (high/medium/low) · MCP 工具池状态 · Agent 活跃态 (● 运行中/○ 空闲) · 目标与常用快捷键提示 |

### 2.1 实现状态（实测标注）

下表按上节逐项标注实测结果（环境：`synapse-web-console` + `synapse-runtime`，
Chrome 实测，工作区 `synapse`）。「缺陷」表示影响可用性。

| 区域 | 规范要素 | 实测 | 状态 |
|---|---|---|---|
| TopBar | 侧栏折叠开关 | 有（`dock_to_left` 按钮 / Ctrl+B） | 已实现 |
| TopBar | 📁 工作区路径 | 有 | 已实现 |
| TopBar | ⎇ Git 分支(状态灯) | 有（绿点 + 分支名） | 已实现 |
| TopBar | 会话标题 | 有 | 已实现 |
| TopBar | 右侧上下文/Token 统计指标 | **已移至底栏中区**（见下）；顶栏右侧只保留 `layers`（会话信息）/ `terminal`（运行时诊断）/ `logout` 三个图标 | 已实现（位置调整） |
| TopBar | （规范外）`layers` / `terminal` 图标 | `layers` = 会话信息弹层（工作区/项目/分支/会话/模型/连接/用量）；`terminal` = 运行时诊断弹层（按需读取宿主只读端点） | 已实现 |
| SideBar | 240px 宽度 | 240px | 已实现 |
| SideBar | 折叠收起为「极简工作区」 | 44px 极简轨：展开 / 新建会话 / 搜索（点击即展开并聚焦）/ 已加载计数 | 已实现 |
| SideBar | + 新建会话 (Ctrl+N) | 有 | 已实现 |
| SideBar | 搜索 (Ctrl+K) | 有搜索框；Ctrl+K 会展开侧栏并聚焦；按标题或 thread_id 过滤 | 已实现 |
| SideBar | 会话按时间分组 | 今天 / 昨天 / 过去 7 天 / 过去 30 天 / 更早；空分组不渲染 | 已实现 |
| SideBar | 项目层级（规范外，本轮新增） | 「项目 → 会话」两级树（对齐 TUI `ProjectDrawer`）：一级＝项目目录名＋当前标记，二级＝该项目的会话（按时间分组，默认展开最近 5 条＋「显示全部」）；只展开当前项目，其余按需懒加载；搜索同时匹配项目名与已加载会话；点其它项目的会话即切换项目（不重启宿主，顶栏工作区/分支随切） | 已实现 |
| SideBar | 每项目「新建会话」 | 会话数与 `+` **悬停/聚焦时浮现**（保留占位，行宽不跳）；`+` 在**该行所属项目**建会话（必要时先切过去），因此移除了原顶部那个语义含糊的全局 `+`；折叠轨里的 `+` 仍作用于当前项目 | 已实现（按评审调整） |
| SideBar | 底栏纯净设置入口 | 打开设置面板（控制台 / 工作区 / 模型与推理 / MCP / 用量 + 退出配对），Esc 或点击遮罩关闭 | 已实现 |
| Transcript | 用户提问胶囊 | 有 | 已实现 |
| Transcript | ◆ Thought for Xs 折叠思考链 | 按规范文案：`◆ Thought for 0.1s`（流式为 `◆ Thinking...`，历史投影无耗时为 `◆ Thought`），带展开/收起 | 已实现 |
| Transcript | ▾ N tools executed 折叠工具栏 | 按规范文案 `N tools executed`（含 parallel 标注）；工具卡片状态徽章已中文化（运行中/等待/完成/失败/错误/已取消） | 已实现 |
| Transcript | Markdown 代码块与流式输出 | 围栏代码块（语言标签 + 复制 + 横向滚动 + 按语言高亮）、标题/列表/引用/表格/行内代码/粗体/链接均正常 | 已实现 |
| CommandBar | 悬浮居中卡片 | 有 | 已实现 |
| CommandBar | 运行中浮现 Steer queue 状态 | 有（`Steer queue: N queued`） | 已实现 |
| CommandBar | 单一蓝色圆形发送按钮 ↑ | 空闲为蓝色 `↑` 发送（输入为空时禁用）；**运行中变为红色 `■` 停止键**，始终可用，点击调用 `runtime.turn.cancel`。与规范「忙碌自动转为 Steer 插队」有意不同：插话仍由 **Enter** 承担，顶部状态条显示 `运行中 · Steer 队列 N` | 已实现（按评审调整） |
| BottomBar | 模型切换下拉 | 有（19 个可用，可过滤；F2） | 已实现 |
| BottomBar | 推理级别选择 (high/medium/low) | **仍只读**：`runtime.config.get` 文档明确「无 command DTO、无 write port」，`config_overrides` 也不是已定义的推理级别契约 ⇒ 需要新增后端写端口。只读时以锁图标替代下拉箭头，不再有「可点击」假象 | 未实现（后端依赖） |
| BottomBar | MCP 工具池状态 | 有面板与单服务器切换；全局开关只读（虚线边框 + `cursor-not-allowed`） | 部分实现 |
| BottomBar | Agent 活跃态 (● 运行中/○ 空闲) | 有，置于底栏最左侧 | 已实现 |
| BottomBar | 中区遥测（规范外，本轮新增） | 顶栏指标整体移入：`↑tokens ↓tokens │ tok/s │ N 步 │ 首字 Xs`，细竖线分隔、`tabular-nums`、hover 出完整明细（缓存占比 / 首字 / 上次调用）；无数据时显示「尚无本轮指标」 | 已实现 |
| BottomBar | 目标与常用快捷键提示 | **底栏不再显示快捷键行**（已按评审移除，`F1` 打开完整列表，含 Ctrl+N / Ctrl+K）；**无「目标」**：`queries.py` 明确「the runtime goal object is never exposed」，wire 上无 goals 方法 ⇒ 需要新增后端只读方法 | 部分实现（后端依赖） |

其它实测观察：

- 运行态活动行文案为 `model waiting for model`（`phase` 与 `detail` 语义重复）。
- Markdown 表格列宽按内容自动分配，窄列（如「类型」）会被压成竖排。
- 历史页超过服务端单页上限（256 KiB）时，客户端按 20→10→5→2→1 自适应缩小重试
  （`readHistoryPage`）；仍失败则渲染可见错误提示，不再静默停在空态。
- 会话列表按 50 条一页**显式**翻页（底部「加载更多」+「已加载 N / 共 M」），不做自动翻页。
- `runtime.session.list` 没有查询参数，因此搜索**只覆盖已加载的会话**；有更多页时底部会
  标注「搜索仅覆盖已加载」，避免把结果误读为全量匹配。
- 指标条的分段规则在 `web/src/stores/usageView.ts` 的 `usageSegments()`：token 对 /
  上下文 / 速率 / 步数各自成段，紧凑条只放主指标，缓存等压缩项进 hover 明细。
- **`context_size` 运行时从不填充**（`runtime/streaming/parser.py` 的 `_note_usage`
  未传该键），所以「上下文」段实际不出现；此时强调落在 token 对上（输入量即上下文占用的
  代理指标）。若后端将来开始上报 `context_size`，该段会自动出现并接管强调。

---

## 3. 通信与协议层契约

严格遵守 docs/agent-runtime-service/s7-wire-protocol.md 规范：

| 协议方法 | 方向 | 用途 |
|---|---|---|
| `runtime.protocol.negotiate` | Request -> Response | 版本协商（versions: [1]） |
| `runtime.session.open` | Request -> Response | 打开或创建会话 {project_id, thread_id} |
| `runtime.turn.submit` | Request -> Response | 提交对话轮次，获取 CommandReceipt |
| `runtime.turn.steer` | Request -> Response | 运行时插话，排队注入指令 |
| `runtime.turn.cancel` | Request -> Response | 中断正在执行的任务 |
| `runtime.events.watch` | Request -> Response + Notifications | 订阅会话事件流（支持带 `after` 游标重连回放） |
| `runtime.events.unwatch` | Request -> Response | 取消事件监听 |
| `runtime.events.read` | Request -> Response | 分页读取历史事件（游标/过滤/扫描上限） |
| `runtime.artifacts.list / `read` | Request -> Response | 工作区文件树与代码差异对比浏览 |
| `runtime.artifacts.stat` | Request -> Response | 单个 artifact 的元数据 |
| `runtime.session.get` | Request -> Response | 读取单个会话视图 |
| `runtime.session.close` | Request -> Response | 关闭会话（可选取消活动轮次） |

### 3.1 控制台实际调用的方法

正式宿主对 JSON-RPC 帧原样中继，控制台当前实际使用：`runtime.protocol.negotiate`、
`runtime.session.open`、`runtime.session.list`、`runtime.session.history`、
`runtime.session.reconcile`、`runtime.session.rebind`、`runtime.session.mcp.reload`、
`runtime.config.get`、`runtime.turn.submit`、`runtime.turn.steer`、`runtime.turn.cancel`、
`runtime.turn.approval.resume`、`runtime.events.watch`、`runtime.events.unwatch`。
`runtime.artifacts.*` 尚未接入前端（见 `formal-host.md` §9）。

## 4. 事件消费

前端在 `web/src/stores/liveEventReducer.ts` 归约实时与回放事件，覆盖与 TUI
（`src/synapse/ui/turn/event_renderer.py`）一致的事件面：

| 事件 | 控制台表现 |
|---|---|
| `activity_started/updated/stopped` | 活动行（phase + detail，含计时） |
| `reasoning_delta/completed` | 思考链（流式；完成时给出耗时） |
| `answer_delta/completed` | 助手回答（完成事件以完整文本定稿） |
| `tool_batch_started/finished` | 工具组（并行标记 / 关闭） |
| `tool_started/updated/finished` | 工具项状态、路径、结果预览、错误 |
| `tool_result` | 旧式工具完成（按 call_id 关联到已有项） |
| `subagent_status_changed` | 父工具项上的子代理阶段 |
| `usage_updated` | 顶栏 token / 上下文 / 速率 / 步数指标 |
| `info` | 提示行（payload 为纯字符串） |
| `approval_required` / `turn_waiting_approval` | 审批弹窗 / 保持运行态 |
| `turn_completed/cancelled/failed` | 轮次结束（失败原因以提示行呈现） |

`plan_updated` / `plan_removed` / `diff_updated` 与 TUI 一致地不渲染（无 sink 表示）。
工具结果只呈现运行时的有界 `preview`；完整工具输出不在事件流中。

## 5. 文本与代码块渲染

助手回答与思考链按 Markdown 渲染（`web/src/components/Markdown.tsx` +
`web/src/markdown/parse.ts`）：标题、列表（含嵌套）、引用、水平线、GFM 表格，以及行内
代码/粗体/斜体/删除线/链接。围栏代码块由 `web/src/components/CodeBlock.tsx` 渲染：
语言标签、复制按钮、横向滚动（不换行）、按语言的高亮（`web/src/markdown/highlight.ts`，
覆盖 python/js/ts/json/bash/yaml/sql/rust/go/java/c/cpp/css/html 与 diff，未知语言保持纯文本）。

约束与边界：

- 解析器无第三方依赖，**不生成 HTML**：返回类型化节点由 React 渲染，不存在注入路径；
- 链接目标经 `sanitizeHref` 过滤，仅保留 http/https/mailto、锚点与相对路径，
  `javascript:` / `data:` / 协议相对 `//host` 一律降级为纯文本；
- 流式未闭合的围栏仍渲染为代码块（`closed=false`，头部标注 streaming）；
- 超长文档（>200k 字符）与超长代码块（>40k 字符）跳过解析/高亮，回退为纯文本/无高亮，
  避免长回答阻塞渲染；
- LaTeX 与 Mermaid **未**渲染为图形（TUI 分别用 TeXicode 与 termaid 生成终端图），
  在 web 端按普通代码块显示。
