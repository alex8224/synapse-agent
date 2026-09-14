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
>
> **会话管理 / goal 写 / 图片附件切片**：侧栏项目与会话树、F6 目标对话框、输入区图片
> 上传与历史缩略图都改走 runtime RPC（§2.3），协议面见 §3 与 §3.2；仍未 wire 的后端
> 能力见 `docs/agent-runtime-service/progress.md`（附件 `abort` 的 finalized 保护已由服务端
> 强制，见该文「已修复」小节）。

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
  该 daemon 默认由宿主**按需拉起**：`--state-dir` 下没有运行中的 daemon 时宿主自己起一个、
  退出时停掉；已有一个在跑就复用（`--no-start-runtime` 可关掉，见 `formal-host.md` §1/§2.1）。
- **前端定位**：单页应用（SPA），以无干扰的极简主义（Kinetic Mono 风格）提供接近 TUI 且体验更优的流式对话、思维链折叠、工具调用查看与 Steer 实时插话能力。

---

## 2. 界面人体工学规范 (基于最新 UI 成果)

| 区域 | 规范与要素 |
|---|---|
| **Shell (窗口)** | **两列**：左侧全高导航列 + 右侧「工作区列」（顶栏、主画布、输入卡片、底栏都只属于右列，不再横跨侧栏） |
| **Theme (主题)** | 外观由 `src/index.css` 的一层 CSS 变量决定（调色阶 + 语义角色 + 圆角/字体/阴影），`tailwind.config.js` 把组件已在用的色阶映射到 `rgb(var(--…) / <alpha-value>)`；`[data-theme='…']` 块即可整块替换外观。仓库自带 `fluent-light` / `fluent-dark` 两个示例主题（取值按 Fluent 2 语义近似，可替换为设计稿 token）；组件内不再出现 `bg-white` / 任意 hex，由 `tests/themeContract.test.ts` 守护 |
| **TopBar (顶栏)** | 工作区列顶部的单行 chip，**三轨栅格**（两侧等宽 `1fr`）：左轨 [\|] 侧栏折叠开关 · 项目 · ⎇ Git 分支（含 ↑/↓ 跟踪计数）**紧接变更统计**（脏/干净状态点 + `+N -M` **真实 tracked 增删行数**，读在它描述的分支旁边）；中轨为**会话标题**，因两侧等宽而**真正居中于工作区列**（不随左右 chip 宽度漂移，独立轨道也保证窄屏不重叠）；右轨保留但不再放 chip（两侧等宽是标题居中的依据）；**分支与统计两个 chip 都是按钮、都可打开只读 Git Explorer**；窄屏（< `lg`）隐藏次要的项目 chip，保留标题与分支；工作区路径移至侧栏底部身份行 |
| **SideBar (侧栏)** | 240px 宽度、**占满整个视口高度**，可折叠收起为极简轨；顶部导航含 新建任务 (Ctrl+N) 与 搜索 (Ctrl+K)；项目 → 会话两级树按时间分组；会话树**不显示滚动条但可正常滚动**（wheel / touch / 键盘，跨浏览器）；底栏为工作区身份行 + 纯净设置入口，无头像与通知铃铛杂音 |
| **Transcript (主画布)** | 助手回复（左）与用户提问（右）为正文排版；◆ Thought for Xs / ▾ N tools executed / info 是紧凑的次要日志行（展开后才成面板） · Markdown 代码块与流式输出 |
| **CommandBar (输入区)** | 工作区列**最后一行**的卡片（不是覆盖聊天区的浮层，转录滚动容器在其上方，最新一行就在可见底边上）：与聊天列同宽同边（`.console-gutter` + `.console-column`；转录不显示滚动条，故两侧都不被占用、边缘完全对齐）；文本在上，附件在左下，模型 / 推理强度 / 发送在右下；若有正在运行的任务则浮现 Steer queue 状态 |
| **BottomBar (底栏)** | 工作区列底部常驻（不再横跨侧栏）：MCP 工具池状态 · Agent 活跃态 (● 运行中/○ 空闲) · 中区本轮/会话遥测 · 目标；模型与推理级别选择已移入输入卡片 |

> **布局对齐（本轮）**：窗口改为「全高侧栏 + 右列工作区」两列，顶栏与底栏只属于右列；
> 顶栏为三轨栅格（两侧等宽 `1fr`，会话标题真正居中、窄屏不重叠），侧栏会话树不显示滚动条
> 但可滚动；聊天列与输入卡片共用 `src/index.css` 的阅读几何（外层 `.console-gutter` 留白 +
> 内层 `.console-column` 列宽）：桌面端（≥ `lg`，1024px）每侧留白为工作区宽度的 10%，故列宽正好是
> 工作区的**约 80%**、**无 `rem` 硬上限**（更宽的工作区真的更宽，不再被 60rem 锁死），窄屏每侧退回
> 固定 `2rem`、列宽近全宽；transcript 滚动容器**不显示滚动条**（与侧栏会话树同一套 `.no-scrollbar`，
> 滚动本身不受影响），因此没有滚动条占用宽度，聊天列与输入卡片同宽同边。布局之外，本轮唯一功能改动是在只读 `runtime.git.status` 上**增量**加
> `insertions` / `deletions`（`git diff --numstat HEAD` 的真实 tracked 增删行数），旧字段与
> 接口保持兼容；不宣称与参考截图像素级一致。不变量由 `web/tests/shellLayout.test.ts`、
> `web/tests/topBarLayout.test.ts`、`web/tests/transcriptLayoutGuard.test.ts` 与
> `web/tests/markdownTableGuard.test.ts` 守护（后者守护表格字号/表头/单元格 padding/横向滚动）。

### 2.1 实现状态（实测标注）

下表按上节逐项标注实测结果（环境：`synapse-web-console` + `synapse-runtime`，
Chrome 实测，工作区 `synapse`）。「缺陷」表示影响可用性。

| 区域 | 规范要素 | 实测 | 状态 |
|---|---|---|---|
| Shell | 两列结构（本轮布局对齐） | 左列 `SideBar` 全高（`h-full`，折叠轨同为全高）；右列依次为 `TopBar` / `RuntimeDiagnosticsBanner` / `Transcript` / `CommandInput` / `BottomBar`，顶栏与底栏不再横跨侧栏 | 已实现（布局调整） |
| TopBar | 侧栏折叠开关 | 有（`dock_to_left` 按钮 / Ctrl+B） | 已实现 |
| TopBar | 📁 工作区路径 | **已移至侧栏底部身份行**（顶栏改为会话 chip 行） | 已实现（位置调整） |
| TopBar | ⎇ Git 分支 | 有（`fork_right` + 分支名 + ↑/↓ 跟踪计数；**点击打开只读 Git Explorer**，因此 `runtime.git.status` 尚未读到时也有入口） | 已实现 |
| TopBar | 会话标题 | 有（中轨 chip / tab，位于工作区列中心线；两侧等宽 `1fr` 轨道使其不随左右内容漂移，超长截断） | 已实现（本轮居中） |
| TopBar | 项目 | 有（`runtime.project.list` 里当前项目的目录名，hover 出完整路径；列表未加载时不渲染占位名） | 已实现（本轮新增 chip） |
| TopBar | 变更统计（跟随分支 chip） | 有（脏/干净状态点 + `runtime.git.status` 的**真实 tracked 增删行数** `+N -M`，不是文件数；**点击打开只读 Git Explorer**，与分支 chip 同为入口；行数未知（git 无法作答）时只显示状态点、不渲染数字，避免虚构的 `+0 -0`；不再带 `difference` 图标） | 已实现（本轮移到分支 chip 之后） |
| TopBar | 变更统计口径 | 服务端 `git diff --numstat HEAD`：staged 与 unstaged **合并计一次**（不会重复累加）；**二进制**变更（numstat 报 `-`）与**未跟踪**文件（不在任何 diff 中）不计入行数；`HEAD` 尚未产生提交时与空树比较；git 无法作答时 `insertions`/`deletions` 为 `null`，前端不显示数字而非伪造 `0` | 已实现 |
| TopBar | 右侧上下文/Token 统计指标 | **已移至底栏中区**（见下） | 已实现（位置调整） |
| TopBar | （规范外）`layers` / `terminal` / `folder_open` / `logout` 图标 | **已移至侧栏底部的操作行**（与设置入口同排）：`layers` = 会话信息弹层（工作区/项目/分支/会话/模型/连接/用量）；`terminal` = 运行时诊断弹层（按需读取宿主只读端点）；`folder_open` = 只读工作区文件树 | 已实现（位置调整） |
| SideBar | 240px 宽度 | 240px | 已实现 |
| SideBar | 会话树滚动 | 可滚动但**不显示滚动条**（`src/index.css` 的 `.no-scrollbar`：`scrollbar-width: none` + `-ms-overflow-style: none` + `::-webkit-scrollbar`，按类名限定，未限定的 `::-webkit-scrollbar` 会隐藏应用内所有滚动条）；容器可聚焦，未聚焦任何行时键盘（方向键 / PageUp / PageDown）也能滚动 | 已实现（本轮） |
| Transcript | 滚动条 | **不显示滚动条但可正常滚动**（同一个 `.no-scrollbar`，wheel / touch / 键盘均可用）：阅读列两侧不被滚动条占用，因此与输入卡片同宽同边；自动跟随到底部时最新一行就在可见底边上 | 已实现（本轮） |
| Transcript | 流式自动跟随 | 有：贴底时每次更新都把最新一行带到可见底边；**只有用户的滚轮 / 触摸手势能结束跟随**（滚回底部自动恢复），应用自身的滚动与浏览器的滚动锚定不会被误判为「用户已离开底部」——否则流式期间上方内容高度变化会把视口顶上去，跟随从此永久停住（表现为推理能跟随、推理结束后的工具调用不再跟随） | 已实现（本轮修复） |
| Transcript | 更早历史 | **滚到顶部自动加载上一页**（`scrollTop ≤ 48px` 且 `historyHasMore`、未在加载中时触发，走与按钮同一条 `skipAutoScroll` 守卫路径，不会把视图拽到底部）；「加载更早历史」按钮保留为手动入口与「还有更多」提示 | 已实现（本轮改为滚动触发） |
| TurnRail（左边缘轮次导航） | 当前轮次高亮 | 有：悬停加长变蓝，视口所在轮次播放同一动画；**在底部时固定高亮最后一轮**（末轮很短、其锚点仍在视口内时，纯「锚点在视口顶边之上」的规则会误判为倒数第二轮）；滚回底部自动恢复 | 已实现（本轮修正） |
| SideBar | 折叠收起为「极简工作区」 | 44px 极简轨：展开 / 新建会话 / 搜索（点击即展开并聚焦）/ 已加载计数 | 已实现 |
| SideBar | 新建任务 (Ctrl+N) | 有：展开态顶部是**带文字的**「新建任务」导航行（右侧标 `Ctrl+N`），折叠轨是同一个动作的 `+` 按钮，两者都作用于**当前项目**；`Ctrl+N` 快捷键不变 | 已实现（本轮补上展开态入口） |
| SideBar | 搜索 (Ctrl+K) | 有搜索框（右侧标 `Ctrl+K` 提示，有输入时换成清除按钮）；Ctrl+K 会展开侧栏并聚焦；按标题或 thread_id 过滤 | 已实现 |
| SideBar | 会话按时间分组 | 今天 / 昨天 / 过去 7 天 / 过去 30 天 / 更早；空分组不渲染 | 已实现 |
| SideBar | 项目层级（规范外，本轮新增） | 「项目 → 会话」两级树（对齐 TUI `ProjectDrawer`）：一级＝项目目录名＋当前标记，二级＝该项目的会话（按时间分组，默认展开最近 5 条＋「显示全部」）；只展开当前项目，其余按需懒加载；搜索同时匹配项目名与已加载会话；点其它项目的会话即切换项目（不重启宿主，顶栏工作区/分支随切） | 已实现 |
| SideBar | 每项目「新建会话」 | 会话数与 `+` **悬停/聚焦时浮现**（保留占位，行宽不跳）；`+` 在**该行所属项目**建会话（必要时先切过去），因此移除了原顶部那个语义含糊的全局 `+`（本轮补回的是**带文字说明目标项目**的导航行，不是那个图标 `+`）；折叠轨里的 `+` 仍作用于当前项目 | 已实现（按评审调整） |
| SideBar | 底部身份行 + 纯净设置入口 | 身份行显示当前工作区路径（空值显示「未绑定工作区」，不编造名字）；同一区域是上下文操作行（会话信息 / 工作区文件 / 运行时诊断 / 退出配对）与设置入口，打开设置面板（控制台 / 工作区 / 模型与推理 / MCP / 用量 + 退出配对），Esc 或点击遮罩关闭 | 已实现 |
| SettingsDialog | 项目默认推理等级（规范外，本轮新增） | 「模型与推理」区新增「项目默认」行：显示项目自身默认等级 + 「设为项目默认…」下拉。写入 `runtime.project.thinking.set`（能力位 `project.thinking`），落盘到项目设置层，**只影响此后新建的会话**；成功显示「已写入项目默认：X」，失败显示红色原因（无乐观更新，只有真正落盘才改变显示值）。底栏「推理级别」仍显示**当前会话**的实际值 | 已实现 |
| Transcript | 用户提问 | 在右侧，无气泡框（所在的一侧即角色）；助手回复在左侧，保持正文排版 | 已实现（按评审调整） |
| Transcript | 阅读列宽 | 与输入卡片共用阅读几何（`.console-gutter` + `.console-column`）：桌面端（≥ `lg`，1024px）每侧留白为工作区的 10%，列宽正好约 80% 且**无 `rem` 上限**（更宽的工作区真的更宽，不再被 60rem 锁死），窄屏每侧退回 `2rem`、列宽近全宽；滚动容器保留对称滚动条槽位（`[scrollbar-gutter:stable_both-edges]`），出现滚动条时中心线仍与输入卡片重合 | 已实现（本轮布局对齐） |
| Transcript | ◆ Thought for Xs 折叠思考链 | 按规范文案：`◆ Thought for 0.1s`（流式为 `◆ Thinking...`，历史投影无耗时为 `◆ Thought`），带展开/收起；**折叠态是紧凑的次要日志行**（无填充底色/无边框），展开后才是面板 | 已实现（本轮层级调整） |
| Transcript | ▾ N tools executed 折叠工具栏 | 按规范文案 `N tools executed`（含 parallel 标注）；折叠态同样是紧凑日志行，展开后逐条工具卡片；工具卡片状态徽章已中文化（运行中/等待/完成/失败/错误/已取消） | 已实现 |
| Transcript | 一个工具批次一个工具组（本轮修复） | 实时流里 `tool_batch_started/finished` 就是批次边界：每批工具各自成组，组的位置紧随其前的思考/文本之后，**不再把整轮的工具调用合并进第一个组**（对齐 TUI「思考 → 工具 → 思考 → 工具保持两个真实批次」的冒烟项）；assistant 文本同样按段落分行（每段自己的 `answer_completed` 定稿），尚未收到任何工具项的空组不渲染 | 已实现（本轮修复） |
| Transcript | Markdown 代码块与流式输出 | 围栏代码块（语言标签 + 复制 + 横向滚动 + 按语言高亮，尺寸不变）、标题/列表/引用/表格/行内代码/粗体/链接均正常；**表格正文按正文可读字号**（`text-sm`，14px；表头同字号、只用字重区分），单元格适度 padding，宽表在自身容器内横向滚动、不撑破阅读列；**每个单元格有 `8rem` 宽度下限**（`.markdown-body th/td`，`web/src/index.css`），窄列（如「分组」「类型」）不再被自动布局压成竖排；单元格里的 `<br>` / `<br/>` / `<br />`（任意大小写）渲染为真实换行（`parse.ts` 的 `break` 节点 → React `<br />`），标签本身不会作为文本或标记进入 DOM | 已实现（本轮表格可读性 + 换行修复） |
| Transcript | LaTeX 公式（规范外，本轮改为真实排版） | `$$...$$` 由 **KaTeX** 排版为居中公式块，`$...$` 排版为行内公式。渲染器固定以 `trust: false` / `throwOnError: false` / `maxExpand: 1000` 运行：不可生成链接、不可注入样式、宏展开有上限。未闭合的流式公式与解析失败的公式回退为「公式 + LaTeX 源码」块并写明原因（`公式解析失败，显示源码`），不会出现红色半成品 | 已实现（本轮） |
| Transcript | Mermaid（规范外，本轮改为真实绘图） | mermaid 围栏由 **mermaid** 渲染为 SVG（`import('mermaid')` 懒加载，不进初始包），头部保留「复制源码」。渲染以 `securityLevel: 'strict'` + `htmlLabels: false` 运行，输出再过一遍 DOMPurify SVG profile，并把 SVG 内 `<style>` 的 `@import` / `url()` 中和掉（内联 SVG 的样式表作用于整页）。含 `%%{...}%%` 指令、YAML frontmatter（`---`）、空定义、超 20000 字符的图形**拒绝渲染**并回退为代码块（头部写明原因）；mermaid 抛错同样回退并显示错误首行 | 已实现（本轮） |
| CommandBar | 工作区列最后一行的卡片（不覆盖聊天区） | 有：作为工作区列的独立行占位，转录滚动容器在其上方（旧实现是 `absolute bottom-0` 浮层，最新内容会被输入卡片盖住）；与聊天列同宽同边（`.console-gutter` + `.console-column`，转录不显示滚动条故两侧等宽） | 已实现（本轮改为非覆盖布局） |
| CommandBar | 运行中浮现 Steer queue 状态 | 有（`Steer queue: N queued`） | 已实现 |
| CommandBar | 单一蓝色圆形发送按钮 ↑ | 空闲为蓝色 `↑` 发送（输入为空时禁用）；**运行中变为红色 `■` 停止键**，始终可用，点击调用 `runtime.turn.cancel`。与规范「忙碌自动转为 Steer 插队」有意不同：插话仍由 **Enter** 承担，顶部状态条显示 `运行中 · Steer 队列 N` | 已实现（按评审调整） |
| BottomBar | 模型切换下拉 | 有（19 个可用，可过滤；F2） | 已实现 |
| BottomBar | 推理级别选择 (high/medium/low) | **可写**：新增会话级写端口 `runtime.session.thinking.set`（需 `session.thinking` 能力位），`runtime.config.get` 的 `can_set_thinking` 现为 `true` 且 `thinking_level` 反映会话级设置。前端为乐观更新 + 失败回滚 + 失败原因可见（保留在弹层内）；服务端仍按该会话 `thinking_levels` 白名单校验 | 已实现 |
| BottomBar | MCP 工具池状态 | 有面板与单服务器切换；面板显示**真实运行态**（已停用 / 启动中… / 已连接 / 未连接 / 已启用），可展开工具列表并保存 `include_tools` 白名单，见 §2.2；全局开关只读（虚线边框 + `cursor-not-allowed`） | 已实现（全局开关仍只读） |
| BottomBar | Agent 活跃态 (● 运行中/○ 空闲) | 有，置于底栏最左侧 | 已实现 |
| BottomBar | 中区遥测（规范外，本轮新增） | 顶栏指标整体移入：`↑tokens ↓tokens │ tok/s │ N 步 │ 首字 Xs`，细竖线分隔、`tabular-nums`、hover 出完整明细（缓存占比 / 首字 / 上次调用）；无数据时显示「尚无本轮指标」。**布局定为保持中区居中**：`grid-cols-[1fr_auto_1fr]` + 中区 `justify-self-center`，右列保留为空对称占位列（不改为右对齐） | 已实现 |
| BottomBar | 目标与常用快捷键提示 | **底栏不再显示快捷键行**（已按评审移除，`F1` 打开完整列表，含 Ctrl+N / Ctrl+K）；**目标已接入**：新增只读 `runtime.session.goal`，底栏左区按 TUI 语义渲染 `goal·active 250/1.0k` / `goal·active 42s`（无目标则整段不渲染，hover 出目标原文与用量） | 已实现 |
| TopBar | 工作区文件面板（规范外，本轮新增） | `folder_open` 打开只读文件树 + 文本查看 + **真实行级差异**，走 `runtime.artifacts.stat/list/read`：分页（`next_cursor` + 「加载更多」）、按路径子串过滤（只过滤已加载条目，显示 `已过滤/已加载` 计数）、单块 64 KiB、自动续读止于 256 KiB，之后由「继续读取（+64 KiB）」显式续读、硬上限 4 MiB，界面始终标注已读字节范围与是否 EOF；二进制文件拒绝解码、错误全部可见 | 已实现 |
| TopBar | 文件面板的文本判定口径 | 服务端对未知类型统一报 `application/octet-stream`，前端仅在该默认值（或空值）时按**扩展名**兜底；**点文件视为自带扩展名**（`.gitignore` → `gitignore`、`.dockerignore` → `dockerignore`、`.python-version` → `python-version`），无扩展名的已知文本名（`LICENSE`/`NOTICE`/`Dockerfile`/`Makefile`/`CODEOWNERS` 等）按 basename 兜底。因此点文件与 `LICENSE` 这类文件可正常查看；**未知名字仍拒绝解码**（宁可不读也不乱码），如 `x.bin`、`mystery` | 已实现 |
| TopBar | 文件面板的真实差异（规范外，本轮新增） | 差异比较**两个真实文本**：打开文件时的基线快照 与 当前内容（可用「重新读取」拉取磁盘上的新版本、「重设基准」重设基线），渲染真实行级差异（LCS，含行号与 `+N/-M` 计数）；中段超过 LCS 预算时按整块替换报告并标注「非最小差异」，达到渲染上限标注「已截断」，两侧一致时明确说明「无差异」。wire 无 revision 历史（`revision` 只是 stat 指纹），因此不做跨会话的旧版本比对，见交接报告 | 已实现（客户端基线对比） |

其它实测观察：

- 运行态活动行文案为 `model waiting for model`（`phase` 与 `detail` 语义重复）。
- Markdown 表格列宽仍按内容自动分配，但单元格的 `8rem` 下限（见上）挡住了「窄列被压成竖排」
  这条旧观察：实测 1280 / 768 / 496px 三种阅读列宽下，短标签列都保持 128px 且只占 1 行
  （带 `<br>` 的单元格按作者意图占 2 行）；下限之和超过阅读列时表格在自身容器内横向滚动。
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

### 2.2 MCP 面板运行态（与 TUI 面板对齐）

底栏 MCP 面板（F5，`web/src/components/McpPanel.tsx`）不再只显示配置里的
`enabled` 开关，而是显示该会话的**真实运行态**；运行态只来自
`runtime.session.mcp.reload` 的结果，配置投影与运行态在
`web/src/stores/mcpRuntimeView.ts` 中刻意分开，因此面板不会把「配置开启」
当作「正在运行」。

| 行为 | 实测 |
|---|---|
| 服务器行 | 状态圆点 + 名称 + `transport` + 状态文案 + `ON`/`OFF` 徽标；状态文案为 **已停用** / **启动中…** / **已连接** / **未连接** / **已启用**（运行态未上报时的保守显示） |
| 工具白名单 | 已连接时显示「已连接 N/M 工具」，可展开工具列表逐个勾选并「保存工具选择」，写入该服务器 `include_tools` 后重连该会话 MCP 并刷新状态；全选等于清空白名单（加载全部工具） |
| 重新连接 | 面板顶部按钮重新附着/重载所有已启用服务器并刷新工具列表，连接中显示「连接中…」 |
| 开关语义 | 点击服务器行仍是切换该服务器 `enabled`；全局 MCP 开关保持只读 |
| warnings | 面板底部展示 daemon 上报的 warnings（连接失败原因、`mcp deferred at startup (…)` 等），不再静默丢弃 |
| 底栏标签 | `mcp: 启动中` / `mcp: N on · 未连接` / `mcp: N on · M 已连接` / `mcp: off` |
| 会话 attach | 打开会话时像 TUI 一样在后台自动附着已启用服务器，结果到达前显示「启动中」；切换结果从**下一轮对话**开始生效，面板在连接期间提示这一点 |
| 设置对话框 | SettingsDialog 的 MCP 段落新增「会话连接 M / N 已连接」以及每个服务器的「已连接 X 工具 / 未连接」 |

协议参数与结果字段（`server` 省略＝附着所有已启用服务器、`include_tools`
空数组＝清空白名单、结果新增 `tool_names` 与每服务器 `discovered`/`loaded` 等）
见 `docs/mcp.md` 的「MCP 面板协议」。

### 2.3 会话管理（侧栏）与图片附件（本轮新增）

侧栏的项目 → 会话树与输入区图片附件都直接走 runtime RPC，不在前端拼装数据。

| 面 | wire 方法 | 实测 |
|---|---|---|
| 项目列表 | `runtime.project.list` | 有界分页（`limit` 1..100）；`GET /api/projects` 保留为 deprecated 兼容路由，不再是业务入口 |
| 新建会话 | `runtime.session.create` | 只写元数据行，`thread_id` 由服务端分配并返回；随后仍走 `runtime.session.open` + watch |
| 重命名 | `runtime.session.rename` | 标题 1–120 字符，空白/超长在本地与服务端都被拒 |
| 删除 | `runtime.session.delete` | 只删元数据与 goal；确认框与提示明确写出 checkpoint/transcript 仍保留，不宣称「对话已删除」；运行中会话由服务端原子拒绝（`conflict`），前端不自动 cancel |
| 搜索 | `runtime.session.search` | 服务端**元数据**搜索（title/summary/thread_id/model），不是全文检索；分页与服务端一致，输入竞态由 generation 计数丢弃过期结果 |
| 选择/拖放图片 | — | 回形针或拖放到输入卡片；只接受图片，每次最多 8 张、每张 ≤ 4 MB，被拒文件名与原因显示在输入区 |
| 上传 | `runtime.attachments.begin` / `.append` / `.finish` | 每个附件独立串流并显示进度；分块 ≤ 256 KiB（base64），`finish` 由服务端校验大小与 MIME |
| 取消/移除 | `runtime.attachments.abort`（best-effort） | 只在 `finish` 之前取消/失败时中止；**不对已 finalize 的 ref 调 abort** |
| 发送 | `runtime.turn.submit` 的 `attachment_refs` | 有附件仍在上传时禁止发送；仅附件（文本为空）可提交；单次最多 8 个不透明 id |
| 历史缩略图 | `runtime.attachments.read` | 历史用户消息只带 metadata，缩略图按需分块读取（64 KiB 窗口）并生成 blob URL；卸载/切换会话时 revoke，> 4 MB 或非图片类型不加载 |

切换会话/项目或退出配对会取消当前输入区的上传并 best-effort abort **未完成**的附件；
已 finalize 的附件保留在服务端，刷新历史后仍显示。服务端 `runtime.attachments.abort`
现在**有** finalized 保护：对已 `finish` 的附件（或 `finish` 崩溃窗口里已落盘的
`data.bin`）调用只返回 `removed=False` 且不删除字节，未完成上传仍正常删除；见
`docs/agent-runtime-service/progress.md` 的「已修复」小节。

---

## 3. 通信与协议层契约

严格遵守 docs/agent-runtime-service/s7-wire-protocol.md 规范（该文件是逐方法参数/结果的权威表；契约冻结后 wire 表共 40 个方法）。控制台涉及的子集：

| 协议方法 | 方向 | 用途 |
|---|---|---|
| `runtime.protocol.negotiate` | Request -> Response | 版本协商（versions: [1]） |
| `runtime.session.open` | Request -> Response | 打开或创建会话 {project_id, thread_id} |
| `runtime.session.list` | Request -> Response | 项目内会话元数据分页（无查询参数，见 §3.2） |
| `runtime.session.create` | Request -> Response | 新建会话元数据行（`thread_id` 由服务端分配并返回） |
| `runtime.session.rename` | Request -> Response | 重命名会话标题（1–120 字符） |
| `runtime.session.delete` | Request -> Response | 删除会话元数据与 goal（保留 checkpoint/transcript） |
| `runtime.session.search` | Request -> Response | 会话**元数据**搜索（非全文检索） |
| `runtime.session.history` | Request -> Response | 读取会话转录历史分页 |
| `runtime.session.reconcile` | Request -> Response | 会话可恢复性快照（断线后核对） |
| `runtime.session.get` | Request -> Response | 读取单个会话视图 |
| `runtime.session.close` | Request -> Response | 关闭会话（可选取消活动轮次） |
| `runtime.session.rebind` | Request -> Response | 会话级模型重绑 |
| `runtime.session.thinking.set` | Request -> Response | 会话级推理等级写入 |
| `runtime.project.thinking.set` | Request -> Response | 项目级默认推理等级写入（只影响新建会话） |
| `runtime.project.list` | Request -> Response | 可见项目枚举（服务端计算可见集合、有界分页） |
| `runtime.config.get` | Request -> Response | 运行时配置只读投影（模型/推理/MCP） |
| `runtime.session.mcp.reload` | Request -> Response | MCP 会话附着/开关/工具白名单 |
| `runtime.session.goal` | Request -> Response | 读取会话 goal（无目标返回 null） |
| `runtime.session.goal.set` / `.edit` / `.clear` / `.pause` / `.resume` | Request -> Response | goal 写操作（独立 `session.goal` 能力位，`expected_goal_id` 乐观并发） |
| `runtime.turn.submit` | Request -> Response | 提交对话轮次，获取 CommandReceipt（可带 `attachment_refs`） |
| `runtime.turn.steer` | Request -> Response | 运行时插话，排队注入指令 |
| `runtime.turn.cancel` | Request -> Response | 中断正在执行的任务 |
| `runtime.turn.approval.get` | Request -> Response | 读取待审批项 |
| `runtime.turn.approval.resume` | Request -> Response | 提交 HITL 审批决策 |
| `runtime.events.watch` | Request -> Response + Notifications | 订阅会话事件流（支持带 `after` 游标重连回放） |
| `runtime.events.unwatch` | Request -> Response | 取消事件监听 |
| `runtime.events.read` | Request -> Response | 分页读取历史事件（游标/过滤/扫描上限） |
| `runtime.artifacts.stat` | Request -> Response | 单个 artifact 的元数据 |
| `runtime.artifacts.list` | Request -> Response | 工作区文件树分页枚举 |
| `runtime.artifacts.read` | Request -> Response | 分块读取工作区文件（差异浏览的文本来源） |
| `runtime.attachments.begin` / `.append` / `.finish` / `.abort` | Request -> Response | 图片上传（声明大小/MIME、分块、校验落盘、丢弃） |
| `runtime.attachments.stat` / `.read` | Request -> Response | 已落盘附件的元数据与分块字节 |

### 3.1 控制台实际调用的方法

正式宿主对 JSON-RPC 帧原样中继，控制台当前实际使用：`runtime.protocol.negotiate`、
`runtime.session.open`、`runtime.session.list`、`runtime.session.create`、
`runtime.session.rename`、`runtime.session.delete`、`runtime.session.search`、
`runtime.session.history`、`runtime.session.reconcile`、`runtime.session.rebind`、
`runtime.session.mcp.reload`、`runtime.session.thinking.set`、`runtime.project.thinking.set`、
`runtime.project.list`、`runtime.session.goal`、`runtime.session.goal.set` /
`.edit` / `.clear` / `.pause` / `.resume`、`runtime.config.get`、`runtime.turn.submit`、
`runtime.turn.steer`、`runtime.turn.cancel`、`runtime.turn.approval.resume`、
`runtime.events.watch`、`runtime.events.unwatch`、
`runtime.artifacts.stat`、`runtime.artifacts.list`、`runtime.artifacts.read`、
`runtime.attachments.begin`、`runtime.attachments.append`、`runtime.attachments.finish`、
`runtime.attachments.abort`、`runtime.attachments.read`。

两个方法已在共享客户端实现但当前没有 UI 路径调用，因此不计入「实际使用」：
`runtime.attachments.stat`（`statAttachment`）与 `runtime.turn.approval.get`
（`getPendingApproval`，审批弹窗直接用事件流里的 `approval_required` 载荷）。
`runtime.session.create` 只写元数据、随后仍走原有 `runtime.session.open` + watch 路径；
`GET /api/projects` 与 `GET /api/bootstrap` 不再是业务入口（前者 deprecated，后者已删除）。

### 3.2 明确不调用的面（不声称对等）

- **没有 wire 方法、因此控制台不提供**：会话清理（`prune_empty`）、会话导出（JSON /
  Markdown）、对话**全文**搜索、项目登记/更新、上下文压缩与上下文状态（TUI `/compact`、
  `/context`）、safety / 权限策略读写、工具输出压缩设置（TUI `/compression`）。逐项真源与
  状态见 `docs/agent-runtime-service/progress.md` 的「尚未 wire 的后端能力（真实矩阵）」。
- **UI-only，不需要 RPC**：TUI 的 `/theme` 与 slash 命令解析/补全属终端外观与输入层；
  控制台有自己的主题、输入区与快捷键。
- `runtime.session.list` 没有查询参数，因此侧栏搜索只覆盖**已加载**会话（§2.1 已标注）；
  `runtime.session.search` 才是服务端元数据搜索，且**不是**对话全文搜索。
- 附件方面：控制台只在 `finish` 之前失败/取消时 best-effort `abort`，**从不**对已
  finalize 的 ref 调 abort；服务端 `runtime.attachments.abort` 也有 finalized 保护
  （见 progress.md 的「已修复」小节），因此「不会误删已完成附件」由服务端强制，客户端
  约定只是第一道防线。

## 4. 事件消费

前端在 `web/src/stores/liveEventReducer.ts` 归约实时与回放事件，覆盖与 TUI
（`src/synapse/ui/turn/event_renderer.py`）一致的事件面：

| 事件 | 控制台表现 |
|---|---|
| `activity_started/updated/stopped` | 活动行（phase + detail，含计时） |
| `reasoning_delta/completed` | 思考链（流式；完成时给出耗时） |
| `answer_delta/completed` | 助手回答（每个文本段落一行；完成事件以该段完整文本定稿） |
| `tool_batch_started/finished` | 工具组（一个批次一组；并行标记 / 关闭） |
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
代码/粗体/斜体/删除线/链接。模型常用的硬换行标签 `<br>` / `<br/>` / `<br />`（任意大小写）
解析为类型化的 `break` 节点、由 React 渲染成 `<br />`，因此单元格里可以自己堆叠多行，而标签
本身既不会作为文本显示、也不会作为标记注入。围栏代码块由 `web/src/components/CodeBlock.tsx` 渲染：
语言标签、复制按钮、横向滚动（不换行）、按语言的高亮（`web/src/markdown/highlight.ts`，
覆盖 python/js/ts/json/bash/yaml/sql/rust/go/java/c/cpp/css/html 与 diff，未知语言保持纯文本）。

约束与边界：

- 解析器无第三方依赖，**不生成 HTML**：返回类型化节点由 React 渲染，正文本身没有注入
  路径（公式与图形这两个「输出即标记」的例外见本节末尾）；
- 链接目标经 `sanitizeHref` 过滤，仅保留 http/https/mailto、锚点与相对路径，
  `javascript:` / `data:` / 协议相对 `//host` 一律降级为纯文本；
- 流式未闭合的围栏仍渲染为代码块（`closed=false`，头部标注 streaming）；
- 超长文档（>200k 字符）与超长代码块（>40k 字符）跳过解析/高亮，回退为纯文本/无高亮，
  避免长回答阻塞渲染；
- LaTeX 与 Mermaid 是仅有的两个「输出即标记」的渲染器：KaTeX 输出与经 DOMPurify 清洗的
  mermaid SVG 统一经 `GeneratedHtml`（`src/**` 中唯一一处 `dangerouslySetInnerHTML`）
  注入，其余 Markdown 一律走类型化节点，`tests/markdownRenderGuard.test.ts` 静态守护这一点；
- KaTeX 以 `trust: false` 运行（`\href` / `\includegraphics` / `\htmlStyle` 等不可信命令只
  渲染为红色字面量），mermaid 以 `securityLevel: 'strict'` + `htmlLabels: false` 运行；
- 模型文本能把 CSS 带进 SVG 的通道只有两个，**两个都不渲染**：`%%{...}%%` 指令与 YAML
  frontmatter（`---`）。二者进入同一个 config 对象（`themeCSS` / `fontFamily`），而
  mermaid 的 `sanitizeDirective` 只校验花括号配平、DOMPurify 不解析 CSS，所以只堵其中一个
  会留下另一个；此外渲染结果里 `<style>` 的 `@import` / `url()` 会被中和，作为升级到新版
  mermaid 时的兜底；
- 两个渲染器都不静默降级：流式中、被拒绝、解析失败三种情况都回退为带原因的源码块。
- 依赖口径变化：由「零运行时依赖」改为新增 `mermaid` / `katex` / `dompurify` 三个依赖
  （mermaid 懒加载，KaTeX 与其样式表随主包加载），不再以「不渲染」换取零依赖。
