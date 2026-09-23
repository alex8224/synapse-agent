| `turn_changes` | 该轮的工作区改动（`changes` 有界列表 + `total` 总数；结算时发出，晚于终止事件到达，控制台追加为该轮末尾的改动卡片） |
| Transcript | 每轮改动卡片（本轮新增） | 轮次结束时，该轮末尾出现一张**本轮工作区改动**卡片列表：每行是一个文件（状态图标 / 路径 / 该轮自己的 `+N -M`），点卡片打开只读 Git Explorer 并定位到该文件（与顶栏分支 chip 共用 `openGitExplorer(path)` 入口）。数字来自运行时的按轮次快照差分（`runtime/workspace_changes.py`：轮次前后各取一次工作区状态再相减），**不是**工作区对 `HEAD` 的存量差——所以同一文件在多轮各有一份自己的数字。第二次快照会带上第一次的路径（`carry=before.files`）逐个读盘，所以一轮里把改动提交或暂存（工作区重新变干净）不算改动，不会被当成整文件删除，真正被删掉的文件仍按「删除」报告并给出丢掉的行数。卡片行**不是折叠步骤**（`transcriptRowPolicy` 里 `step: false`），折叠的轮次照样看得到；列表有界（超出时写「共 N 个 · 显示前 M 个」），无法计数的改动（二进制 / 超大）不显示行数。投影里以 `changes` 事件随轮次落库，刷新后卡片仍在；实时由结算时发出的 `turn_changes` 事件补上（**晚于终止事件**到达，控制台按事件自己的 `turn_id` 追加到该轮末尾） | 已实现（本轮） |
| Transcript | 卡片上的单文件撤销（本轮新增） | 每张卡片多一个「撤销」：点它只是**就地武装**（出现「恢复到本轮开始前？」+「确认撤销」），再点「确认撤销」才发一次 `runtime.workspace.revert`。运行时每轮会把改动前的文件内容留一份（`runtime/turn_reverts.py`，存在工作区自己的 `.synapse/turn-snapshots/<thread>/`，有界保留最近 20 轮 / 32 MiB，超出丢弃最旧），撤销就是把那一份写回：本轮新建的文件被删除，本轮删除的文件被写回，本轮改过的文件恢复原内容（改动前**干净**的文件取其在本轮开始时 `HEAD` 所指提交的版本，且此后 `HEAD` 变了就拒绝）。写入是原子的（同目录临时文件 + `os.replace`），符号链接一律拒绝写入，**从不**碰 `HEAD` / 索引 / 其它文件。有回合在运行时拒绝（不替你取消）；文件在本轮之后又被改过则拒绝（不静默丢弃）。成功后卡片显示「已撤销」并去掉行数（那些数字不再描述工作区），同时重读 git 状态；刷新后仍显示「已撤销」，因为历史读取带上该轮被撤销的路径（`HistoryEvent.reverted_paths`），控制台只据此重放、**不**从 `git.status` 反推 | 已实现（本轮） |
| BottomBar | Codex 用量与重置额度（本轮新增） | 左区（activity 与 MCP 之间，与 TUI 同一 `order`）新增**可用性自持**条目：只在服务端确认该会话有效模型是启用的 Codex OAuth profile 时才存在（`runtime.config.get` 的 `codex_usage_enabled`，由 daemon 按会话**实际** profile 的 `auth` 判定，不按模型名猜；旧 peer 缺该字段则隐藏且不发任何 RPC）。行文按 TUI 语义但**不沿用其硬编码窗口名**：`5h 82%/3h · 7d 60%/2d · resets 2`（窗口长度取 RPC 的真实 `window_minutes`，倒计时为本地 1s tick，不发请求）；剩余低于 50% 转红。点开面板（`popover`）显示两个窗口、捕获时间与额度列表，`刷新` 强制绕过 300s 读缓存；列表每行按 `status == available` 且未过期才可兑换。**兑换是真实写操作**：先弹确认「兑换会真实消耗 1 次账户级 Codex 重置额度（不可撤销）」，只有确认按钮发一次 `runtime.codex.reset_credits.consume`（携带用户所见 `expected_model` 与一次性 `command_id`）；取消零请求，结果未知或断线**不自动重试、不换新键重放**，提示刷新核对。窄屏（<768px）策略为「更多」，经既有入口可达。授权为独立能力位 `codex.usage.read`（两个读方法）与 `codex.reset.consume`（兑换），`session.read` 不授权其中任何一个。由 `web/tests/codexUsageClient.test.ts` / `codexUsageController.test.ts` / `codexUsageEntry.test.ts` 守护，`web/tests/codexUsage.verify.ts` 用真实浏览器 + 模拟 runtime 验收（隐藏期完成发现、真实窗口标注、确认前零请求、消费后刷新、禁用后条目与分隔符一并消失、360px 经「更多」可达） | 已实现（本轮） |
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
>
> **「添加项目」切片**：输入区的 `+` 不再打开图片选择器，改为打开「添加项目」对话框
> （`web/src/components/AddProjectDialog.tsx`）：经 `runtime.fs.list` 浏览**宿主**目录，进入目标目录后
> 点「选择此目录并新建会话」经 `runtime.project.register` 登记，再切到该项目并开新会话（§2.3）。
> 图片附件仍可加入输入区——走**粘贴或拖放**（点击选图的入口已移除），8 张 / 4 MB 限额与预览行为不变。
>
> **「输出文件路径可点击」切片**：助手回复里的文件路径会被识别为可点击项
> （`web/src/markdown/filePaths.ts`）：Windows 盘符绝对路径、POSIX 绝对路径、含分隔符的相对路径，
> 以及带已知扩展名的裸文件名都支持，可带可选 `:line:col` 后缀；**行内代码（反引号）里恰好是一个路径时
> 也识别**（模型通常这样写路径）；`e.g.`、`i.e.`、版本号、域名、时间等散文不会被改写。点击后先把路径
> 归一化为工作区相对 POSIX 路径（`toWorkspacePath`，识别工作区前缀、反斜杠与前导 `/`），再**居中打开**
> 只读工作区文件管理器（`ArtifactsPanel` 的 `centered` 模式，宿主 `FileViewerHost`，状态在
> `useConsoleStore.fileViewer`）并渲染该文件；裸文件名先在深度/数量有界的前提下按 basename 搜索
> （`runtime-client/artifactLocate.ts`），找不到则退回工作区根目录并把文件名填入过滤框。居中窗口
> （`FloatingWindow`）可拖动标题栏移动、从右下角缩放、最大化/还原（双击标题栏同效），头部按钮可
> 收起/展开文件树，文件正文随窗口高度撑满（`CodeBlock` 的 `fill` 模式，去掉固定 `max-h`），
> Esc / 点击遮罩 / 关闭按钮关闭。全部仍走只读 `runtime.artifacts.stat/read` 面，未新增后端能力。
> 被工作区忽略规则排除的路径（如 `.gitignore` 里的构建产物 `web/dist/...`）会被明确说明「无法读取」，
> 而不是笼统的 `runtime service error`（前端把 `service_code` 映射为可读原因，见 `artifactErrorMessage`）。
> 标题栏取主题材质 `material-titlebar`（chrome 填充 + 颗粒 + 毛玻璃 + `--material-edge` 顶边光照，
> 但**不含** chrome 的斜向 sheen——那是为整块侧栏/顶栏设计的，压在窄标题栏上在亮色主题里会成蓝色色带），
> 与弹框主体的 `material-flyout` 区分开；进入沿用 `flyout-in`/`scrim-in`，最大化/还原走
> `fluent-window-motion` 过渡（拖动与缩放不加过渡，避免跟手延迟），时长/曲线来自主题变量，
> `prefers-reduced-motion` 下全部关闭。

### 工作流启动与会话用量

在 WebUI 提示模型调用 `create_workflow` 时，actor 资源冷启动在后台线程初始化，
不阻塞共享 Agent 事件循环。工具仍等待最终结果；初始化或进程创建失败会结束运行并
释放项目占用，已创建进程但结果不明确时则保留 `uncertain`，不自动重试。
daemon 重启后失去执行者的旧工作流同样标为 `uncertain`，保留未决调用的证据并暂停
同项目的新消息。用户核查后可在工作流面板显式取消，立即释放项目占用；取消不回滚修改。
被拒绝的提交显示会话/项目忙碌及工作流面板操作提示，明确拒绝时保留消息草稿供重试。
新建会话会立即清空旧会话的累计用量和上下文显示，不以旧值填充等待或失败的初始化。
底栏出现旧用量不等同于实际模型上下文已跨会话共享。

`create_workflow` 的角色来自项目启用的角色注册表，描述只列出已解析服务实际启用的角色；
脚本里的字面角色（含 `role=` 关键字）与声明的 `roles` 在写草稿、建运行前预检，未启用即拒绝
并返回可用角色，动态角色表达式由运行时校验。结构化回答不符合 schema 时只重试一次，
且该次重试禁用写入与 shell 工具，纠正格式不会改动工作区。取消工作流经服务执行、不回滚修改，
未知或已终态的运行按幂等无操作处理。

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
- **前端定位**：单页应用（SPA），主界面采用统一的 Fluent 明暗外观，保留紧凑的流式对话、思维链折叠、工具调用查看与 Steer 实时插话能力。

---

## 2. 界面人体工学规范 (基于最新 UI 成果)

| 区域 | 规范与要素 |
|---|---|
| **Shell (窗口)** | **两列**：左侧全高导航列 + 右侧「工作区列」（顶栏、主画布、输入卡片、底栏都只属于右列，不再横跨侧栏） |
| **Theme (主题)** | 设置 → 外观 → 主题提供 `跟随系统 / 浅色 / 深色`，分别跟随系统或显式启用 `fluent-light` / `fluent-dark`，浅色不再沿用旧外观。首次渲染前应用；显式选择优先，选择持久化在 `localStorage`（键 `synapse.console.appearance`），刷新与重开浏览器后仍生效，存储被禁用时回退到跟随系统。CSS 变量统一颜色、圆角、字体、材质和动效；Segoe UI + Consolas（系统字体回退）、4px 控件圆角、48/32px 外壳、150/200ms 动效、`--focus-ring` 焦点环。主界面的导航/顶栏/输入区/模型选择器/设置与上下文操作使用随包提供的 Fluent SVG 图标及共享 32px 控件、24px 行内按钮、14px UI/12px 辅助排印；会话选中态有左侧标记，输入区工具栏分层且可换行。转录和其他专用面板的图标尚未全面迁移 |
| **TopBar (顶栏)** | 工作区列顶部的单行 chip，**三轨栅格**（两侧等宽 `1fr`）：左轨 [\|] 侧栏折叠开关 · 项目 · ⎇ Git 分支（含 ↑/↓ 跟踪计数）**紧接变更统计**（脏/干净状态点 + `+N -M` **真实 tracked 增删行数**，读在它描述的分支旁边）；中轨为**会话标题**，因两侧等宽而**真正居中于工作区列**（不随左右 chip 宽度漂移，独立轨道也保证窄屏不重叠）；右轨保留但不再放 chip（两侧等宽是标题居中的依据）；**分支与统计两个 chip 都是按钮、都可打开只读 Git Explorer**；窄屏（< `lg`）隐藏次要的项目 chip，保留标题与分支；工作区路径移至侧栏底部身份行 |
| **SideBar (侧栏)** | 240px 宽度、**占满整个视口高度**，可折叠收起为极简轨；顶部导航含 新建任务 (Ctrl+N) 与 搜索 (Ctrl+K)；项目 → 会话两级树按时间分组；会话树**不显示滚动条但可正常滚动**（wheel / touch / 键盘，跨浏览器）；底栏为工作区身份行 + 纯净设置入口，无头像与通知铃铛杂音 |
| **Transcript (主画布)** | 助手回复（左）与用户提问（右）为正文排版，且两者右边缘同线：助手正文限宽列宽 80%，用户行因此右侧内缩 20%，气泡不再伸到助手正文之外；◆ Thought for Xs / info 是紧凑的次要日志行（在左侧、限宽 85%，展开后才成面板）；**工具调用不再按批次折叠**：每次调用是一行紧凑日志行（图标 / 工具名 / 意图 / 路径，无描边无底色），状态只在非「完成」时出现，展开箭头仅在悬停或键盘聚焦时出现，点开该行才显示它自己的结果详情 · Markdown 代码块与流式输出 |
| **CommandBar (输入区)** | 工作区列**最后一行**的卡片（不是覆盖聊天区的浮层，转录滚动容器在其上方，最新一行就在可见底边上）：与聊天列同宽同边（`.console-gutter` + `.console-column`；转录不显示滚动条，故两侧都不被占用、边缘完全对齐）；文本在上，附件预览在其上方一行；控制行**左侧的 `+` 是「添加项目」**（打开宿主目录浏览器，见 §2.3），右侧是模型 / 推理强度 / 发送；若有正在运行的任务则浮现 Steer queue 状态 |
| **BottomBar (底栏)** | 工作区列底部常驻（不再横跨侧栏）：Agent 活跃态 (● 运行中/○ 空闲) · Codex 用量与重置额度（仅 OAuth 会话存在） · MCP 工具池状态 · 中区本轮/会话遥测 · 目标；模型与推理级别选择已移入输入卡片。它是**静态清单 + 条目模块**的宿主（见 §2.1 底栏条目清单） |

> **布局对齐（本轮）**：窗口改为「全高侧栏 + 右列工作区」两列，顶栏与底栏只属于右列；
> 顶栏为三轨栅格（两侧等宽 `1fr`，会话标题真正居中、窄屏不重叠），侧栏会话树不显示滚动条
> 但可滚动；聊天列与输入卡片共用 `src/index.css` 的留白几何（外层 `.console-gutter` 留白 +
> 内层 `.console-column`），但宽度解耦：桌面端（≥ `lg`，1024px）每侧留白为工作区宽度的 10%，聊天列取工作区的
> **80%** 且不设上限；输入卡片在同一中轴上另受 `--composer-max`（48rem）封顶，超过上限后不再增长（单行输入不横跨宽窗口），窄屏每侧退回
> 固定 `2rem`、两者都近全宽；transcript 滚动容器**不显示滚动条**（与侧栏会话树同一套 `.no-scrollbar`，
> 滚动本身不受影响）。布局之外，本轮唯一功能改动是在只读 `runtime.git.status` 上**增量**加
> `insertions` / `deletions`（`git diff --numstat HEAD` 的真实 tracked 增删行数），旧字段与
> 接口保持兼容；不宣称与参考截图像素级一致。不变量由 `web/tests/shellLayout.test.ts`、
> `web/tests/topBarLayout.test.ts`、`web/tests/transcriptLayoutGuard.test.ts` 与
> `web/tests/markdownTableGuard.test.ts` 守护（后者守护表格字号/表头/单元格 padding/横向滚动）。

### 2.1 实现状态（实测标注）

下表按上节逐项标注实测结果（环境：`synapse-web-console` + `synapse-runtime`，
Chrome 实测，工作区 `synapse`）。「缺陷」表示影响可用性。

| 区域 | 规范要素 | 实测 | 状态 |
|---|---|---|---|
| Transcript | 展开的思考面板 = 毛玻璃卡片（本轮新增） | 该面板是全页唯一一处页面内 acrylic：按**角色**取主题材质 `material-card`（填充 `--material-card`、模糊 `--material-blur`、纹理 `--material-sheen` / `--material-grain`），组件里不出现任何字面 `backdrop-blur-*` 或透明度混色；随包主题的 `--material-blur` 为 `none`，因此默认不付合成层代价。注意观感来源：卡片背后是窗格填充与 Mica 渐变（没有内容从它后面滚过），所以「毛玻璃」主要体现在半透明填充 + 颗粒纹理，模糊本身几乎不可见 | 已实现（本轮） |
| Transcript | 页面内卡片的 Fluent 层色（本轮新增） | 聊天区页面内卡片一律「实心层色 + 描边」，不再用半透明填充与 `backdrop-blur`（acrylic 只留给浮层：对话框 / 浮出面板 / 悬停预览；唯一例外是展开的思考面板，它按角色取主题的 `material-card` 材质）。用户气泡改为实心 `colorBrandBackground2` + 中性描边 + 统一 8px 圆角（去掉 `14/14/3/14` 尾角与模糊）；Thought / 工具批次 chip、工具详情行、info 行、历史横幅、「加载更早历史」按钮统一 4px 圆角 + `border-line` + `bg-surface`，hover/active 用新角色 `--surface-hover` / `--surface-pressed`；info 行去掉左侧强调条，改 Fluent MessageBar 形态（状态底 + 状态描边 + 图标）；审批卡片按钮改 32px Fluent 按钮（primary = `--accent`，destructive = `--red-500`）；公式卡片移除专用紫色角色，改用与代码块相同的 canvas / sunken 层色（主题契约删除 `--math-*`，新增 `--surface-hover` / `--surface-pressed` / `--fg-disabled`）；日志行字号从 11px（不在 Fluent 字号阶梯上）提到 caption1 12px，元数据保持 caption2 10px；`themeContract.test.ts` 新增「页面内卡片不得使用 `backdrop-blur`」守卫（只放行 `material-card` 这一处命名例外），`transcriptLayoutGuard.test.ts` 新增字号守卫 | 已实现（本轮） |
| Shell | 两列结构（本轮布局对齐） | 左列 `SideBar` 全高（`h-full`，折叠轨同为全高）；右列依次为 `TopBar` / `RuntimeDiagnosticsBanner` / `Transcript` / `CommandInput` / `BottomBar`，顶栏与底栏不再横跨侧栏 | 已实现（布局调整） |
| Theme (主题) | 浅色主题的 acrylic 配方（本轮修正） | 深色下毛玻璃明显、浅色下看不出来，不是浅色没开模糊：两套主题的 `--material-blur` 相同（`blur(30px) saturate(180%)`），差别在其余三个线索，而它们全是**亮色**线索——白色 sheen、白色内描边高光、亮色噪点——在白色基底上要么恒等于零（`rgba(255,255,255,.09)` 叠在 `rgb(253,253,253)` 上只 +0.1 级，即没有），要么相对对比度只有深色的约 1/5。因此 `fluent-light` 不再复用深色配方，改用「冷调、比页面更暗」的一套：Mica 洗色加饱和（`rgba(146,195,250,.93)` 起；窗口底色饱和度 0.09 → 0.19，原始洗色 0.17 → 0.30）、填充改冷调并降 alpha 让背后洗色透出来（`--material-chrome` / `--material-flyout` 从纯白 0.70 / 0.62 改为 `rgb(242 246 252)` 0.62 / 0.55，`--material-canvas` 0.45 → 0.35，`--material-pane` 0.62 → `rgb(251 253 255)` 0.58）、sheen 换冷调（+0.7 级 → −13.4 级）、grain 0.08 → 0.12、新增主题角色 `--material-edge`（`.material-chrome` / `.material-flyout` / `.material-strip` 里硬编码的白色内高光改为该角色；浅色为亮顶边 + 冷底边，内高光 +1.9 级 → +23.8 级）。深色主题除 flyout 的 1px 内高光由 0.35 统一到 0.25 外未改动；`themeContract.test.ts` 契约列表加入 `--material-edge` | 已实现（本轮） |
| TopBar | 侧栏折叠开关 | 有（Fluent `PanelLeft` 按钮 / Ctrl+B） | 已实现 |
| TopBar | 📁 工作区路径 | **已移至侧栏底部身份行**（顶栏改为会话 chip 行） | 已实现（位置调整） |
| TopBar | ⎇ Git 分支 | 有（`fork_right` + 分支名 + ↑/↓ 跟踪计数；**点击打开只读 Git Explorer**，因此 `runtime.git.status` 尚未读到时也有入口） | 已实现 |
| TopBar | 会话标题 | 有（中轨 chip / tab，位于工作区列中心线；两侧等宽 `1fr` 轨道使其不随左右内容漂移，超长截断） | 已实现（本轮居中） |
| TopBar | 项目 | 有（`runtime.project.list` 里当前项目的目录名，hover 出完整路径；列表未加载时不渲染占位名） | 已实现（本轮新增 chip） |
| TopBar | 变更统计（跟随分支 chip） | 有（脏/干净状态点 + `runtime.git.status` 的**真实 tracked 增删行数** `+N -M`，不是文件数；**点击打开只读 Git Explorer**，与分支 chip 同为入口；行数未知（git 无法作答）时只显示状态点、不渲染数字，避免虚构的 `+0 -0`；不再带 `difference` 图标） | 已实现（本轮移到分支 chip 之后） |
| TopBar | 变更统计口径 | 服务端 `git diff --numstat HEAD`：staged 与 unstaged **合并计一次**（不会重复累加）；**二进制**变更（numstat 报 `-`）与**未跟踪**文件（不在任何 diff 中）不计入行数；`HEAD` 尚未产生提交时与空树比较；git 无法作答时 `insertions`/`deletions` 为 `null`，前端不显示数字而非伪造 `0` | 已实现 |
| Git Explorer | 未跟踪文件的内容（本轮修正） | `git diff` 对**未跟踪**路径什么都不输出，所以以前在左侧列表里点开一个 `??` 文件，右侧只会说「没有差异，请用工作区文件面板查看内容」——列出来了却看不到。现在服务端在 `git diff` 为空且**未加 `--cached`** 时读该文件（有界、二进制安全），按「新文件」生成统一 diff 返回：`--- /dev/null` / `+++ b/<path>` / `@@ -0,0 +1,N @@` + 逐行 `+`，与 git 对新增文件的输出同形。**不会**为了看内容而 `git add --intent-to-add`（索引与仓库保持只读）；符号链接按其目标路径一行显示；空文件与目录仍报「没有差异」；超过 `MAX_DIFF_BYTES` 截断并标 `truncated`；二进制标 `binary`（前端显示「二进制文件，不显示 diff」）。已加进暂存区的文件仍由 git 自己出 diff，不走这条路径 | 已实现（本轮） |
| TopBar | 右侧上下文/Token 统计指标 | **已移至底栏中区**（见下） | 已实现（位置调整） |
| TopBar | （规范外）`layers` / `terminal` / `folder_open` / `logout` 图标 | **已移至侧栏底部的操作行**（与设置入口同排）：`layers` = 会话信息弹层（工作区/项目/分支/会话/模型/连接/用量）；`terminal` = 运行时诊断弹层（按需读取宿主只读端点）；`folder_open` = 只读工作区文件树 | 已实现（位置调整） |
| SideBar | 240px 宽度 | 240px | 已实现 |
| SideBar | 会话树滚动 | 可滚动但**不显示滚动条**（`src/index.css` 的 `.no-scrollbar`：`scrollbar-width: none` + `-ms-overflow-style: none` + `::-webkit-scrollbar`，按类名限定，未限定的 `::-webkit-scrollbar` 会隐藏应用内所有滚动条）；容器可聚焦，未聚焦任何行时键盘（方向键 / PageUp / PageDown）也能滚动 | 已实现（本轮） |
| Transcript | 滚动条 | **不显示滚动条但可正常滚动**（同一个 `.no-scrollbar`，wheel / touch / 键盘均可用）：阅读列两侧不被滚动条占用，因此与输入卡片同宽同边；自动跟随到底部时最新一行就在可见底边上 | 已实现（本轮） |
| Transcript | 流式自动跟随 | 有：贴底时每次更新都把最新一行带到可见底边，并在行高测量修正后继续保持贴底；**只有读者的滚轮 / 触摸 / 键盘手势能结束跟随**（滚回底部自动恢复），应用自身的滚动与浏览器的滚动锚定不会被误判为「用户已离开底部」——否则流式期间上方内容高度变化会把视口顶上去，跟随从此永久停住（表现为推理能跟随、推理结束后的工具调用不再跟随） | 已实现（本轮修复） |
| Transcript | 更早历史 | **滚到顶部自动加载上一页**（`scrollTop ≤ 48px` 且 `historyHasMore`、未在加载中时触发，走与按钮同一条守卫路径）；前插会**保住读者当前看的位置**（按「距底部距离」这个不变量还原 `scrollTop`，并在新行测量完成后继续校正），不会把视图拽到底部也不会跳走；「加载更早历史」按钮保留为手动入口与「还有更多」提示 | 已实现（本轮修复） |
| Transcript | 长会话虚拟滚动（本轮新增） | 只挂载视口附近的若干行（`@tanstack/react-virtual`，估算行高 + `measureElement` 实测校正，上下各留 `OVERSCAN_ROWS` 行），其余行由占位高度撑出滚动条：一个 1,200 行的会话只挂载几十行，整轮替换约 30ms（此前一次 commit 挂载全部行会导致页面卡死）。滚动容器、跟随、贴底闩锁、折叠守卫仍由 `Transcript` 自己持有，虚拟化只决定「哪些行存在」；TurnRail 改为通过 `TranscriptViewport` 句柄按索引取偏移与跳转（离屏轮次没有 `[data-turn-id]` 元素可查），跳转落点应用 `scroll-padding-top` 预留量 | 已实现（本轮） |
| Transcript | 折叠隐藏的行不占空间 | 行间隙只下在**会渲染**的行上（`rowPaints` 同时决定「是否渲染」与「是否留间隙」）：包装元素是按**索引**存在的，若无条件下间隙，折叠轮次里每个被隐藏的步骤都会留下 20px 空白，折叠步数越多空白越大、末尾状态行越漂越远；折叠展开后这些行正常参与布局。与虚拟化前的纯列表行为一致（`null` 行不产生元素，`space-y-5` 不会为它留缝） | 已修复（本轮） |
| TurnRail（左边缘轮次导航） | 当前轮次高亮 | 有：悬停加长变蓝，视口所在轮次播放同一动画；**在底部时固定高亮最后一轮**（末轮很短、其锚点仍在视口内时，纯「锚点在视口顶边之上」的规则会误判为倒数第二轮）；滚回底部自动恢复 | 已实现（本轮修正） |
| SideBar | 折叠收起为「极简工作区」 | 44px 极简轨：展开 / 新建会话 / 搜索（点击即展开并聚焦）/ 已加载计数 | 已实现 |
| SideBar | 新建任务 (Ctrl+N) | 有：展开态顶部是**带文字的**「新建任务」导航行（右侧标 `Ctrl+N`），折叠轨是同一个动作的 `+` 按钮，两者都作用于**当前项目**；`Ctrl+N` 快捷键不变 | 已实现（本轮补上展开态入口） |
| SideBar | 搜索 (Ctrl+K) | 有搜索框（右侧标 `Ctrl+K` 提示，有输入时换成清除按钮）；Ctrl+K 会展开侧栏并聚焦；按标题或 thread_id 过滤 | 已实现 |
| SideBar | 会话按时间分组 | 今天 / 昨天 / 过去 7 天 / 过去 30 天 / 更早；空分组不渲染 | 已实现 |
| SideBar | 项目层级（规范外，本轮新增） | 「项目 → 会话」两级树（对齐 TUI `ProjectDrawer`）：一级＝项目目录名＋当前标记，二级＝该项目的会话（按时间分组，默认展开最近 5 条＋「显示全部」）；只展开当前项目，其余按需懒加载；搜索同时匹配项目名与已加载会话；点其它项目的会话即切换项目（不重启宿主，顶栏工作区/分支随切） | 已实现 |
| SideBar | 每项目「新建会话」 | 会话数与 `+` **悬停/聚焦时浮现**（保留占位，行宽不跳）；`+` 在**该行所属项目**建会话（必要时先切过去），因此移除了原顶部那个语义含糊的全局 `+`（本轮补回的是**带文字说明目标项目**的导航行，不是那个图标 `+`）；折叠轨里的 `+` 仍作用于当前项目 | 已实现（按评审调整） |
| SideBar | 底部身份行 + 纯净设置入口 | 身份行显示当前工作区路径（空值显示「未绑定工作区」，不编造名字）；同一区域是上下文操作行（工作区文件 / 运行时诊断 / 退出配对——**「会话信息」已移至顶栏标题，本区域不再有该图标**）与设置入口，打开设置面板（控制台 / 工作区 / 模型与推理 / MCP / 用量 + 退出配对），Esc 或点击遮罩关闭 | 已实现 |
| SettingsDialog | 项目默认推理等级（规范外，本轮新增） | 「模型与推理」区新增「项目默认」行：显示项目自身默认等级 + 「设为项目默认…」下拉。写入 `runtime.project.thinking.set`（能力位 `project.thinking`），落盘到项目设置层，**只影响此后新建的会话**；成功显示「已写入项目默认：X」，失败显示红色原因（无乐观更新，只有真正落盘才改变显示值）。底栏「推理级别」仍显示**当前会话**的实际值 | 已实现 |
| Transcript | 用户提问 | 在右侧，无气泡框（所在的一侧即角色）；气泡右边缘与助手正文块的右边缘同线（用户行右侧内缩 20%，助手正文限宽列宽 80%，两个字面值必须相加为 100%，由 `transcriptLayoutGuard.test.ts` 钉住）；助手回复在左侧，保持正文排版 | 已实现（本轮对齐右边缘） |
| Transcript | 阅读列宽 | 与输入卡片共用留白几何（`.console-gutter` + `.console-column`）但宽度解耦：桌面端（≥ `lg`，1024px）每侧留白为工作区的 10%，聊天列取工作区的 80% 且不设上限；输入卡片在同一中轴上另受 `--composer-max`（48rem）封顶。窄屏每侧退回 `2rem`、两者都近全宽 | 已实现（本轮布局对齐） |
| Transcript | ◆ Thought for Xs 折叠思考链 | 按规范文案：`◆ Thought for 0.1s`（流式为 `◆ Thinking...`，历史投影无耗时为 `◆ Thought`），带展开/收起；**折叠态是紧凑的次要日志行**（无填充底色/无边框），展开后才是面板 | 已实现（本轮层级调整） |
| Transcript | 工具行（本轮去掉批次分组、入参、卡片外观与状态文案） | **不再有 `N tools executed` 批次折叠行**，并发批次也一样：一次批次的每次调用直接渲染一行紧凑日志行（图标 / 工具名 / 模型给的意图 / 路径），**无描边、无底色**——一批十条调用不再堆成十张同权重卡片（批次折叠行原本是这些卡片的容器，容器消失后卡片外观会让日志与正文同权重）；行内不打印调用入参，也不带「意图」「入参」这类说明性标签。**执行状态不用文字**：进行中的调用在行首是一个旋转指示器（子代理步骤的动画由卡片导轨上的小圆承担，行内不重复），失败只把该行与工具名变红、行内不附原因文案，成功完全静默；只有「已取消」保留一个词。runtime 的 `status` 不作为状态文字渲染——成功时它是 `summarize_tool_result` 的正文摘要（`ok (48 chars, 2 lines)`），失败时是错误首行；**失败原因改为在展开后的详情里以红色首行呈现**（正文已以该原因开头则不重复打印），因此没有 preview 的失败也可展开。行尾不右对齐：状态词与展开箭头紧跟内容，不占固定末位，展开箭头仅在悬停或键盘聚焦时出现。点开单条工具才挂载它的结果详情：**运行类工具（`execute` 等）按终端会话渲染**——`$ 命令` 与输出在同一个滚动区里（命令取自历史投影的 `args`，实时流则从批次事件的 `args_preview` repr 反解），ANSI 颜色还原、不折行；读文件类按代码高亮、其余为纯文本。命令已知但输出未到时（调用还在运行）也能展开读到命令 | 已实现（本轮） |
| Transcript | 一个工具批次一个工具组（本轮修复） | 实时流里 `tool_batch_started/finished` 仍是批次边界，但它现在只是**数据边界**（决定工具行的归属与顺序）：每批工具的行直接紧随其前的思考/文本之后，**不再把整轮的工具调用合并进第一个组**（对齐 TUI「思考 → 工具 → 思考 → 工具保持两个真实批次」的冒烟项）；视觉上不再有批次分组行，**调用之间的间距也与批次无关**（同一批两次调用与相邻两批两次调用都是 2px：批次容器不带垂直 padding，虚拟行在下一可见行同为同轮工具行时把行距由 20px 收到 `pb-0.5`，否则维持转录行 20px 节奏——否则批次边界会以「并发更近」的假象暴露自己）；assistant 文本同样按段落分行（每段自己的 `answer_completed` 定稿），尚未收到任何工具项的空组不渲染 | 已实现（本轮修复 + 本轮去分组） |
| Transcript | Markdown 代码块与流式输出 | 围栏代码块（语言标签 + 复制 + 横向滚动 + 按语言高亮，尺寸不变）、标题/列表/引用/表格/行内代码/粗体/链接均正常；**表格正文按正文可读字号**（`text-sm`，14px；表头同字号、只用字重区分），单元格适度 padding，宽表在自身容器内横向滚动、不撑破阅读列；**每个单元格有 `8rem` 宽度下限**（`.markdown-body th/td`，`web/src/index.css`），窄列（如「分组」「类型」）不再被自动布局压成竖排；单元格里的 `<br>` / `<br/>` / `<br />`（任意大小写）渲染为真实换行（`parse.ts` 的 `break` 节点 → React `<br />`），标签本身不会作为文本或标记进入 DOM | 已实现（本轮表格可读性 + 换行修复） |
| Transcript | LaTeX 公式（规范外，本轮改为真实排版） | `$$...$$` 由 **KaTeX** 排版为居中公式块，`$...$` 排版为行内公式。渲染器固定以 `trust: false` / `throwOnError: false` / `maxExpand: 1000` 运行：不可生成链接、不可注入样式、宏展开有上限。未闭合的流式公式与解析失败的公式回退为「公式 + LaTeX 源码」块并写明原因（`公式解析失败，显示源码`），不会出现红色半成品 | 已实现（本轮） |
| Transcript | Mermaid（规范外，本轮改为真实绘图） | mermaid 围栏由 **mermaid** 渲染为 SVG（`import('mermaid')` 懒加载，不进初始包），头部保留「复制源码」。渲染以 `securityLevel: 'strict'` + `htmlLabels: false` 运行，输出再过一遍 DOMPurify SVG profile，并把 SVG 内 `<style>` 的 `@import` / `url()` 中和掉（内联 SVG 的样式表作用于整页）。含 `%%{...}%%` 指令、YAML frontmatter（`---`）、空定义、超 20000 字符的图形**拒绝渲染**并回退为代码块（头部写明原因）；mermaid 抛错同样回退并显示错误首行。**大图要能看全**：mermaid 默认给根 `<svg>` 写 `width="100%"` 加内联 `max-width`（内联样式压过任何样式表规则），每张图因此都被钉在阅读栏宽度上、宽图被静默缩小、外层滚动容器永远不滚动；控制台在渲染后按图自身的 `viewBox` 尺寸冻结 SVG（`freezeSvgSize`，`src/markdown/mermaid.ts`），卡片默认「适应宽度」、可切「原始大小」（1:1，超出即横向滚动），stage 自身限高 `28rem` 后内部滚动，高图不再把一条转录行撑到整张图的高度。「放大」打开全屏查看器（`MermaidLightbox`，与图片预览共用 `imageZoom.ts` 的缩放/平移算术：滚轮以指针为锚、拖拽平移、`适应窗口 / 1:1 / - / +`、`+ - 0 1`、Esc、双击切换），开图比例不低于 75% 且不高于 1:1；同一张图在文档里只渲染一份（mermaid 的 id 不做命名空间，sequence diagram 除根 id 外还有 `actor0`…`root-7`），放大时卡片清空 stage 并保留实测高度当占位 | 已实现（本轮） |
| CommandBar | 工作区列最后一行的卡片（不覆盖聊天区） | 有：作为工作区列的独立行占位，转录滚动容器在其上方（旧实现是 `absolute bottom-0` 浮层，最新内容会被输入卡片盖住）；与聊天列同宽同边（`.console-gutter` + `.console-column`，转录不显示滚动条故两侧等宽） | 已实现（本轮改为非覆盖布局） |
| CommandBar | 运行中浮现 Steer queue 状态 | 有（`Steer queue: N queued`） | 已实现 |
| CommandBar | 单一蓝色圆形发送按钮 ↑ | 空闲为蓝色 `↑` 发送（输入为空时禁用）；**运行中变为红色 `■` 停止键**，始终可用，点击调用 `runtime.turn.cancel`。与规范「忙碌自动转为 Steer 插队」有意不同：插话仍由 **Enter** 承担，顶部状态条显示 `运行中 · Steer 队列 N` | 已实现（按评审调整） |
| BottomBar | 模型切换下拉 | 有（19 个可用，可过滤；F2） | 已实现 |
| BottomBar | 推理级别选择 (high/medium/low) | **可写**：新增会话级写端口 `runtime.session.thinking.set`（需 `session.thinking` 能力位），`runtime.config.get` 的 `can_set_thinking` 现为 `true` 且 `thinking_level` 反映会话级设置。前端为乐观更新 + 失败回滚 + 失败原因可见（保留在弹层内）；服务端仍按该会话 `thinking_levels` 白名单校验 | 已实现 |
| BottomBar | MCP 工具池状态 | 有面板与单服务器切换；面板显示**真实运行态**（已停用 / 启动中… / 已连接 / 未连接 / 已启用），可展开工具列表并保存 `include_tools` 白名单，见 §2.2；全局开关只读（虚线边框 + `cursor-not-allowed`） | 已实现（全局开关仍只读） |
| BottomBar | Agent 活跃态 (● 运行中/○ 空闲) | 有，置于底栏最左侧 | 已实现 |
| BottomBar | 中区遥测（规范外，本轮新增） | 顶栏指标整体移入：`↑tokens ↓tokens │ tok/s │ N 步 │ 首字 Xs`，细竖线分隔、`tabular-nums`、hover 出完整明细（缓存占比 / 首字 / 上次调用）；无数据时显示「尚无本轮指标」。**布局定为保持中区居中**：`grid-cols-[1fr_auto_1fr]` + 中区 `justify-self-center`，右列保留为空对称占位列（不改为右对齐） | 已实现 |
| BottomBar | 目标与常用快捷键提示 | **底栏不再显示快捷键行**（已按评审移除，`F1` 打开完整列表，含 Ctrl+N / Ctrl+K）；**目标已接入**：新增只读 `runtime.session.goal`，底栏左区按 TUI 语义渲染 `goal·active 250/1.0k` / `goal·active 42s`（无目标则整段不渲染，hover 出目标原文与用量） | 已实现 |
| BottomBar | 底栏条目清单（静态注册契约，本轮新增） | 宿主 `web/src/components/BottomBar.tsx` 只保留条目共有的规则：三轨栅格、**唯一 `openId`**（同键关闭、异键替换，F1／F5／F6 因此天然互斥）、按键映射、遮罩归属、窄屏策略、会话切换／条目不可达即关闭。每个条目是 `web/src/components/bottomBar/*Item.tsx` 导出的一个 `BottomBarItemDefinition`，声明 `region`（left/center/right）、`order`、业务可见性 `visible`、弹层种类 `popover`/`modal`、窄屏策略 `keep`/`compact`/`more` 及自身 Trigger/Content；**新增条目 = 一个模块 + `manifest.tsx` 一行**，宿主不改，且没有可变注册中心、外部插件或用户布局配置。条目各自订阅自己绘制的 store 字段；隐藏条目不进布局（无包装、无分隔符）。`popover` 由宿主 portal 成 `FloatingPanel` 并从该条目自己的 trigger 定位，外点关闭只认「本条目 trigger + 面板」（不是整条底栏、也不是整条轨道），Esc 只由它自己处理；`modal`（`GoalDialog`/`HelpDialog`）自带遮罩、Esc 与焦点往返，宿主不再为它注册第二个监听。F1 条目没有 Trigger（右轨仍是空对称占位列），只在 `consoleShortcuts.ts` 的快捷键表占一行——F1／F5／F6 按键、帮助文案、tooltip 与面板标题里的「(F5)」都取自该表。窄屏（<768px，与外壳同断点）按条目策略收窄：左轨横向滚动不裁剪、中区遥测收成紧凑芯片（点开为完整分段）、策略为「更多」的条目进右侧「更多」菜单，而没有弹层可开的条目不会被丢进「更多」。「更多」菜单走共享的 `useDialogKeyboardNav`（打开即聚焦首行、方向键在行间环绕、关闭时把焦点交回「更多」按钮）；菜单行打开的 modal 即将随菜单卸载，因此该行开弹层前先把焦点交回仍挂载的「更多」按钮，modal 关闭后焦点落回该按钮而不是 `<body>`。命中 `consoleShortcuts` 中带 `key` 的按键一律先 `preventDefault`（长按 F5 不会触发浏览器刷新）再忽略 `repeat`，未认领的按键不拦截。由 `web/tests/bottomBarContract.test.ts`（分区/顺序/可见性/紧凑策略/单 openId/会话切换 + `react-dom/server` 渲染出的轨道）、`web/tests/bottomBarLayout.test.ts`、`web/tests/bottomBarDismiss.test.ts` 守护，`web/tests/bottomBarInteraction.verify.ts` 用真实浏览器验收（F1/F5/F6 开合与互斥、按键重复忽略且仍 `preventDefault`、未认领按键不拦截、外点只关当前弹层、modal 的 Esc 与焦点回归、切换会话确实换到另一会话并关掉旧弹层、360px 下入口仍可达），`web/tests/bottomBarMore.verify.ts` 用 fixture 验收「更多」菜单键盘路径（键盘打开→方向键环绕→选择 modal/popover→Esc→焦点回到「更多」按钮） | 已实现（本轮） |
| TopBar | 工作区文件面板（规范外，本轮新增） | `folder_open` 打开只读文件树 + 文本查看 + **真实行级差异**，走 `runtime.artifacts.stat/list/read`：分页（`next_cursor` + 「加载更多」）、按路径子串过滤（只过滤已加载条目，显示 `已过滤/已加载` 计数）、单块 64 KiB、自动续读止于 256 KiB，之后由「继续读取（+64 KiB）」显式续读、硬上限 4 MiB，界面始终标注已读字节范围与是否 EOF；二进制文件拒绝解码、错误全部可见 | 已实现 |
| TopBar | 文件面板的文本判定口径 | 服务端对未知类型统一报 `application/octet-stream`，前端仅在该默认值（或空值）时按**扩展名**兜底；**点文件视为自带扩展名**（`.gitignore` → `gitignore`、`.dockerignore` → `dockerignore`、`.python-version` → `python-version`），无扩展名的已知文本名（`LICENSE`/`NOTICE`/`Dockerfile`/`Makefile`/`CODEOWNERS` 等）按 basename 兜底。因此点文件与 `LICENSE` 这类文件可正常查看；**未知名字仍拒绝解码**（宁可不读也不乱码），如 `x.bin`、`mystery` | 已实现 |
| TopBar | 文件面板的真实差异（规范外，本轮新增） | 差异比较**两个真实文本**：打开文件时的基线快照 与 当前内容（可用「重新读取」拉取磁盘上的新版本、「重设基准」重设基线），渲染真实行级差异（LCS，含行号与 `+N/-M` 计数）；中段超过 LCS 预算时按整块替换报告并标注「非最小差异」，达到渲染上限标注「已截断」，两侧一致时明确说明「无差异」。wire 无 revision 历史（`revision` 只是 stat 指纹），因此不做跨会话的旧版本比对，见交接报告 | 已实现（客户端基线对比） |

其它实测观察：

- 运行态活动行文案为 `model waiting for model`（`phase` 与 `detail` 语义重复）。
- Markdown 表格列宽仍按内容自动分配，但单元格的 `8rem` 下限（见上）挡住了「窄列被压成竖排」
  这条旧观察：实测 1280 / 768 / 496px 三种阅读列宽下，短标签列都保持 128px 且只占 1 行
  （带 `<br>` 的单元格按作者意图占 2 行）；下限之和超过阅读列时表格在自身容器内横向滚动。
- 历史页超过服务端单页上限（896 KiB）时，客户端按 20→10→5→2→1 自适应缩小重试
  （`readHistoryPage`）；仍失败则渲染可见错误提示，不再静默停在空态。
- 会话列表按 50 条一页**显式**翻页（底部「加载更多」+「已加载 N / 共 M」），不做自动翻页。
- `runtime.session.list` 没有查询参数，因此搜索**只覆盖已加载的会话**；有更多页时底部会
  标注「搜索仅覆盖已加载」，避免把结果误读为全量匹配。
- 指标条的分段规则在 `web/src/stores/usageView.ts` 的 `usageSegments()`：token 对 /
  上下文 / 速率 / 步数各自成段，紧凑条只放主指标，缓存等压缩项进 hover 明细。
- **`context_size` 运行时从不填充**（`runtime/streaming/parser.py` 的 `_note_usage`
  未传该键），所以「上下文」段实际不出现；此时强调落在 token 对上（输入量即上下文占用的
  代理指标）。若后端将来开始上报 `context_size`，该段会自动出现并接管强调。
- 底栏在窄屏（<768px）收成单行：左轨自己横向滚动（`overflow-x-auto` + `no-scrollbar`），
  中区遥测变成紧凑指标芯片（没有数据时显示图标 + `-`，点开是完整分段），右轨只在有条目被
  「更多」收走时才出现。`src/index.css` 的 `@media (max-width: 767px)` 因此不再用
  `nth-child` 隐藏中／右轨——那种写法会把入口裁成不可达。

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

### 2.3 会话管理（侧栏）、添加项目与图片附件（本轮新增）

侧栏的项目 → 会话树、输入区 `+` 的「添加项目」流程与输入区图片附件都直接走 runtime RPC，不在前端拼装数据。

| 面 | wire 方法 | 实测 |
|---|---|---|
| 项目列表 | `runtime.project.list` | 有界分页（`limit` 1..100）；`GET /api/projects` 保留为 deprecated 兼容路由，不再是业务入口 |
| 添加项目（浏览宿主目录） | `runtime.fs.list` | 对话框打开 daemon 的 home 目录（请求不带 `path` 即 home），点条目逐级进入**宿主**的直接子目录（`limit` 默认 200、服务端上限 1000，「上一级」按钮在 `parent` 为 `null` 时禁用）；顶部另有盘符/根按钮（结果里的 `roots`，Windows 为盘符、POSIX 为 `/`）与可编辑路径框（粘贴绝对路径直达），行内「选择」按钮一次点击即可选中该目录；只列目录、不列文件、不递归，`truncated` 为真时提示「仅显示前 N 个子目录」 |
| 添加项目（登记并切换） | `runtime.project.register` | 「选择此目录并新建会话」登记的是**当前浏览目录**：daemon 解析该宿主路径并要求它是已存在目录，按 workspace 路径幂等（重复登记复用同一 `project_id`）；成功后刷新项目列表、切到该项目并新建会话，失败原因留在对话框内 |
| 新建会话 | `runtime.session.create` | 只写元数据行，`thread_id` 由服务端分配并返回；随后仍走 `runtime.session.open` + watch |
| 重命名 | `runtime.session.rename` | 标题 1–120 字符，空白/超长在本地与服务端都被拒 |
| 删除 | `runtime.session.delete` | 删元数据与 goal，并把该 thread 从 checkpoint、transcript projection、全文检索索引与回滚快照里一并清除；确认框写明「不可恢复」，提示按 `retained_history` / `purge_failures` 如实报告（有存储拒绝时点出名字与补救方式，不宣称已清干净）；运行中会话由服务端原子拒绝（`conflict`），前端不自动 cancel |
| 搜索 | `runtime.session.search` | 服务端**元数据**搜索（title/summary/thread_id/model），不是全文检索；分页与服务端一致，输入竞态由 generation 计数丢弃过期结果 |
| 粘贴/拖放图片 | — | **粘贴**到输入卡片或**拖放**文件到卡片；只接受图片，每次最多 8 张、每张 ≤ 4 MB，被拒文件名与原因显示在输入区。点击选图的入口已移除（`+` 现在是「添加项目」） |
| 上传 | `runtime.attachments.begin` / `.append` / `.finish` | 每个附件独立串流并显示进度；分块 ≤ 256 KiB（base64），`finish` 由服务端校验大小与 MIME |
| 取消/移除 | `runtime.attachments.abort`（best-effort） | 只在 `finish` 之前取消/失败时中止；**不对已 finalize 的 ref 调 abort** |
| 发送 | `runtime.turn.submit` 的 `attachment_refs` | 有附件仍在上传时禁止发送；仅附件（文本为空）可提交；单次最多 8 个不透明 id |
| 历史缩略图 | `runtime.attachments.read` | 历史用户消息只带 metadata，缩略图按需分块读取（64 KiB 窗口）并生成 blob URL；卸载/切换会话时 revoke，> 4 MB 或非图片类型不加载 |

切换会话/项目或退出配对会取消当前输入区的上传并 best-effort abort **未完成**的附件；
已 finalize 的附件保留在服务端，刷新历史后仍显示。服务端 `runtime.attachments.abort`
现在**有** finalized 保护：对已 `finish` 的附件（或 `finish` 崩溃窗口里已落盘的
`data.bin`）调用只返回 `removed=False` 且不删除字节，未完成上传仍正常删除；见
`docs/agent-runtime-service/progress.md` 的「已修复」小节。

### 2.4 文件查看器：选中行、图片 / Markdown 预览与文本差异（本轮新增）

同一个只读文件面板（`web/src/components/ArtifactsPanel.tsx`，居中宿主 `FileViewerHost`）在
§2.1 的文本查看与真实差异之上补齐了图片与 Markdown，判定口径都在
`web/src/runtime-client/artifacts.ts`（`web/src/client/artifacts.ts` 只是兼容再导出）：

- **选中行**：被选中的行是真正的 `<button>`（键盘可达，`aria-current` + `data-selected` 宣告选中），
  样式取共享导航 token `ui-nav-row`——主题角色 `--selection-fill` 底色 + `--accent` 左侧强调条，
  浅色与深色主题各自定义、组件不写死颜色；`forced-colors` 下改用 `Highlight` 描边。选中在读取之前
  发布，因此内容加载中与读取失败时标记都保留。
- **图片预览**：`image/png` / `image/jpeg` / `image/gif` / `image/webp` / `image/bmp`，上限
  `ARTIFACT_IMAGE_MAX_BYTES` = **4 MiB**，按 `runtime.artifacts.stat` 元数据在首次读取前判定
  （超限或大小为 0 都给出可见原因）；字节按有界分块顺序读取后生成 blob URL，`object-contain`
  不裁剪，卸载或切换会话时 revoke。**SVG 不支持**：它是可携带脚本与外链的文档，经 blob URL 渲染
  会绕过控制台的 `GeneratedHtml` 清洗，因此 `.svg` 与其它不可预览类型一样按二进制拒绝
  （显示「不读取内容」，绝不解码为文本）。
- **Markdown**：默认渲染预览（共享 `Markdown` 渲染器，仍走类型化节点、不注入标记），工具条可切
  「预览 / 源码」；源码视图沿用已有的 `CodeBlock` 语言高亮（语言由扩展名推出，未知扩展名保持纯文本）。
- **文本与差异**：文本仍按 64 KiB 一块读取，自动续读到 256 KiB 后需显式「继续读取（+64 KiB）」，
  硬上限 4 MiB（`ARTIFACT_HARD_MAX_BYTES`，与图片上限是两个独立常量），页脚始终标注已读范围与
  是否 EOF；「真实差异」是基线快照与当前内容的真实行级 LCS 差异（配「重新读取」/「重设基准」），
  中段过大标注「非最小差异」、达渲染上限标注「已截断」、两侧一致时写明「无差异」。

上述口径由 `web/tests/artifactPreviewGuard.test.ts`（选中行是按钮且先于读取发布、按类型路由、
Markdown 默认预览且源码保留高亮、过期读取不覆盖新选择）与 `web/tests/artifactImages.test.ts`
（光栅白名单不含 SVG、4 MiB 上限先于读取、分块校验、blob URL 生命周期）守护。

### 2.5 打开方式：把文件交给宿主的本机程序（本轮新增）

Git Explorer 与文件查看器的标题栏各有一个 split button（`web/src/components/OpenWithMenu.tsx`）：
左半用当前应用打开选中文件，右半展开应用列表。它把文件**交给宿主机器上的程序**，所以两侧职责分得很清：

- **宿主**（`runtime/service/external_apps.py`）是唯一权威：`runtime.apps.list` 回一张**有界**的
  目录（名称、短名、角色、声称的扩展名、图标字形 id、是否系统默认），`runtime.workspace.open_external`
  接受 `{session, path, app_id?, mode: open|reveal}`。路径必须是工作区相对的，解析后仍要落在会话自己的
  工作区内（symlink 逃逸同样拒绝），文件必须存在；`app_id` 必须是宿主自己枚举出来的 id，**请求里没有
  命令行、没有参数、没有可执行文件路径**——宿主只做一次固定 argv 的启动。
- **应用集合是固定候选表 + 运行时探测**：表里列出常见编辑器 / 终端 / 资源管理器及其各平台探测位置
  （`shutil.which` 或已知安装路径），装了才出现，表里没有的应用不会出现；「系统默认应用」永远可用
  （走操作系统关联），所以读者不会卡死。这是本期**已知限制**：动态枚举（Windows `App Paths`、
  Linux `.desktop`、macOS `/Applications`）留待后续，且不会改变本契约的字段。
- **拒绝是具名的**：`service_code` 取 `external_app_path_invalid`、`external_app_outside_workspace`、
  `external_app_file_missing`、`external_app_unknown`、`external_app_launch_failed`、
  `external_app_unavailable`（无工作区 / 无桌面会话）。控制台把这些翻成一行可读原因渲染在窗口内，
  当前选择回退到可用应用，绝不静默。
- **授权位独立**：读目录要 `apps.list`，启动要 `workspace.open_external`；`git.status`、`apps.list`
  或 `session.read` 都**不**授权启动。启动不修改工作区（与 `workspace.revert` 的唯一写面互不相干）。
- **控制台侧**：菜单 portal 到 `document.body`（两个宿主窗口的标题栏是拖拽把手且裁剪溢出），打开时由它
  在 capture 阶段接管 `Escape`（先关菜单、再关窗口），点击触发器与菜单之外关闭；方向键走行、`Enter`
  打开；「始终用 X 打开 .ext 文件」记在非机密的 `appearance.openWith`，最近用过的应用只在本次会话内记住。
- 守护：`web/tests/externalApps.test.ts`（严格解码、推荐/记忆策略、请求形状）、
  `web/tests/openWithMenuGuard.test.ts`（portal / Escape / 外部点击 / split button / 只对文件开放）、
  `web/tests/openWithMenu.verify.ts`（真浏览器 32 项）、`tests/test_runtime_service_external_apps.py`
  （路径拒绝、应用发现、启动 argv、wire 解码、ACL 隔离）。

### 2.3.2 窗口截图（输入区动作 → 运行时 → 本机工具）

- **边界**：前端从不直连截图工具。运行时通过 CLI（`windows-capture.exe --rpc '<json>'`，固定 argv、
  超时、输出有界）驱动本机的 `windows-capture` 工具，再把它派生成普通附件。工具路径、原始错误码、
  截图字节都不会出现在 wire 上。
- **发现与可用性**：默认发现本仓库的 Release 构建（可用 `SYNAPSE_WINDOWS_CAPTURE_EXE` 覆盖）；
  非 Windows 或未构建时**明确不可用**并给出原因。能力探测只运行 `--version`，**不会启动 GUI**。
- **异步任务**：`runtime.screenshot.capture` 立即返回任务快照，`runtime.screenshot.status` 轮询进度
  （读时把工具可用性一并折进同一投影），`runtime.screenshot.cancel` 幂等取消。控制台默认**不覆盖**
  参数，让工具用自己保存的配置（`override > saved > default`，参数在「截图设置」里选）；带 `settings`
  时才发有界覆盖。已有任务排队/运行中时再次 `capture` 是**幂等**的（返回正在运行的任务），不会起
  第二个任务（工具本身也一次只跑一个）。
- **结果 → 附件**：每一帧按块（≤ 256 KiB）读回，经**既有附件 store** 的 `begin/append/finish`
  校验并 finalize，返回 `attachment_id` + metadata。前端把结果当作 `ready` 行加入输入框，按 id 用
  历史缩略图 loader 预览；**结果绝不自动发送**。数量/大小沿用附件规则（每次最多 8 张、每张 ≤ 4 MB）。
- **提前 `completed` 的兼容**：运行时只在全部帧 finalize 后才发布 `completed`；对仍会提前发布（附件
  尚未/部分回填）的旧 daemon，控制台保留「正在回填截图附件…」反馈，并在**有界短窗口**内自动重读状态，
  帧到齐即自动加入；窗口用尽仍为空时给出明确的「刷新状态」提示（手动刷新会再等一轮）。缩略图与悬停
  放大预览共用按 id 取字节的 hook（primitive 依赖 + 异步世代隔离），重渲染不重复读取。
- **无目标 / 失败**：无目标时工具打开自己的选择界面并返回 `target_required`，控制台提示选择后重试；
  取消、失败、工具退出都落到**命名终态**，绝不假装成功。
- **绑定发起会话与草稿世代**：任务记录发起时的会话与草稿世代；切回原会话且未发送新草稿时结果自动
  加入输入框，否则保留为**可见的待确认项**（「加入输入框」/「丢弃」），不塞进新草稿。
- **授权位独立**：读状态要 `screenshot.read`；打开设置 GUI、开始/取消截图要 `screenshot.control`
  ——`screenshot.read`、`apps.list`、`attachments.write` 都**不**授权启动。
- 守护：`tests/test_runtime_service_screenshot.py`（adapter argv/超时/错误映射、任务生命周期、
  wire 解码、ACL 隔离）、`web/tests/screenshot.test.ts`（严格解码、纯策略、任务 store 行为）。

---

### 2.6 使用统计与工程效能面板（本轮新增）

侧栏底部操作行的 `DataUsage` 入口（与设置入口同排）打开 `UsageDashboardDialog`，只读展示
**当前工作区**的真实用量。数据来自宿主 `GET /api/usage-stats`（见 `formal-host.md` §3.2），
后端 `src/synapse/web_console/usage_stats.py` 直接聚合 `model_request_compression_events`
与 `sessions.sqlite`。

- **口径**：累计吞吐 `total_tokens = provider_input + output`（provider 处理过的全部 prompt
  token，含缓存读写）；净输入 = `provider_input - cache_read - cache_write`，趋势「非缓存读取
  输入」= `provider_input - cache_read`（含 `cache_write`）；输出单独统计，reasoning 已含在
  provider 输出中、**不重复累加**；日期为 UTC 闭区间，`today`/`7d`/`30d`/`all`/`custom` 统一
  过滤所有面板；聚合流式读取、不按 5000 截断；自定义区间最长 3660 天，热力图为连续日历日
  （超宽区间只显示末 366 天并在 UI 标明，累计不受影响）。
- **真实数据**：热力图按真实日期记录并切换 Token / 会话；趋势与「按 Token 排序」会话排行
  来自真实事件；`project` 选项来自宿主 catalog 的可见项目，选择项目或「全部」都会读取对应工作区数据
  （浏览器不能提交任意路径，未知 `project` 返回 400）。
- **无来源即留白**：费用 / 代码变更行数 / 工具平均耗时显示 `—`（载荷里为 `null`）；
  Agent/角色维度停用（`breakdowns.agent` 为 `null`）；工具统计仅覆盖被压缩并写入引用存储的
  工具输出，载荷以 `partial` + `scope_note` 标明**不代表全部工具调用**。
- **前端**：移除演示 fallback 与伪造常量；严格校验完整载荷；单一 effect 触发请求、草稿日期
  点「筛选」后才查询、`AbortController` + stale 保护；loading / error / empty 明确且不保留
  旧筛选数据误导；自定义日期默认取当天。由 `web/tests/usageDashboard.test.ts` 与
  `tests/test_web_console_usage_stats.py` 守护。

## 3. 通信与协议层契约

严格遵守 docs/agent-runtime-service/s7-wire-protocol.md 规范（该文件是逐方法参数/结果的权威表；契约冻结后 wire 表共 55 个方法）。控制台涉及的子集：

| 协议方法 | 方向 | 用途 |
|---|---|---|
| `runtime.protocol.negotiate` | Request -> Response | 版本协商（versions: [1]） |
| `runtime.session.open` | Request -> Response | 打开或创建会话 {project_id, thread_id} |
| `runtime.session.list` | Request -> Response | 项目内会话元数据分页（无查询参数，见 §3.2） |
| `runtime.session.create` | Request -> Response | 新建会话元数据行（`thread_id` 由服务端分配并返回） |
| `runtime.session.rename` | Request -> Response | 重命名会话标题（1–120 字符） |
| `runtime.session.delete` | Request -> Response | 删除会话元数据与 goal，并清除该 thread 的 checkpoint / transcript / 检索索引 / 回滚快照 |
| `runtime.session.search` | Request -> Response | 会话**元数据**搜索（非全文检索） |
| `runtime.session.history` | Request -> Response | 读取会话转录历史分页 |
| `runtime.session.reconcile` | Request -> Response | 会话可恢复性快照（断线后核对） |
| `runtime.session.get` | Request -> Response | 读取单个会话视图 |
| `runtime.session.close` | Request -> Response | 关闭会话（可选取消活动轮次） |
| `runtime.session.rebind` | Request -> Response | 会话级模型重绑 |
| `runtime.session.thinking.set` | Request -> Response | 会话级推理等级写入 |
| `runtime.project.thinking.set` | Request -> Response | 项目级默认推理等级写入（只影响新建会话） |
| `runtime.project.list` | Request -> Response | 可见项目枚举（服务端计算可见集合、有界分页） |
| `runtime.project.register` | Request -> Response | 登记宿主目录为项目（catalog scope、按 workspace 路径幂等） |
| `runtime.fs.list` | Request -> Response | 宿主目录浏览（catalog scope、只读有界、只列直接子目录） |
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
| `runtime.apps.list` | Request -> Response | 宿主可启动的本机应用目录（图标字形 + 声称的扩展名，有界；catalog 作用域，`apps.list`） |
| `runtime.workspace.open_external` | Request -> Response | 用指定应用打开一个工作区相对路径（`mode: open` 或 `reveal`；授权位 `workspace.open_external`） |
| `runtime.screenshot.status` | Request -> Response | 截图工具可用性 + 当前会话截图任务快照（只读；授权位 `screenshot.read`） |
| `runtime.screenshot.settings.open` | Request -> Response | 打开截图工具自身设置窗口（授权位 `screenshot.control`） |
| `runtime.screenshot.capture` | Request -> Response | 用有界 `settings` 排队异步截图任务（幂等；授权位 `screenshot.control`） |
| `runtime.screenshot.cancel` | Request -> Response | 取消当前会话截图任务（幂等；授权位 `screenshot.control`） |

### 3.1 控制台实际调用的方法

正式宿主对 JSON-RPC 帧原样中继，控制台当前实际使用：`runtime.protocol.negotiate`、
`runtime.session.open`、`runtime.session.list`、`runtime.session.create`、
`runtime.session.rename`、`runtime.session.delete`、`runtime.session.search`、
`runtime.session.history`、`runtime.session.reconcile`、`runtime.session.rebind`、
`runtime.session.mcp.reload`、`runtime.session.thinking.set`、`runtime.project.thinking.set`、
`runtime.project.list`、`runtime.project.register`、`runtime.fs.list`、`runtime.session.goal`、
`runtime.codex.usage.get`、`runtime.codex.reset_credits.get`、`runtime.codex.reset_credits.consume`、
`runtime.session.goal.set` /
`.edit` / `.clear` / `.pause` / `.resume`、`runtime.config.get`、`runtime.turn.submit`、
`runtime.turn.steer`、`runtime.turn.cancel`、`runtime.turn.approval.resume`、
`runtime.events.watch`、`runtime.events.unwatch`、
`runtime.artifacts.stat`、`runtime.artifacts.list`、`runtime.artifacts.read`、
`runtime.attachments.begin`、`runtime.attachments.append`、`runtime.attachments.finish`、
`runtime.attachments.abort`、`runtime.attachments.read`、`runtime.apps.list`、
`runtime.workspace.open_external`。

其中 `runtime.workspace.revert` 是控制台唯一**写读者自己文件**的方法（改动卡片上的撤销）：
请求为 `{session, turn_id, path}`，结果是 `{action: restore|delete|already_reverted, bytes_written}`。
它只还原卡片上那一个 workspace 相对路径，且只在文件仍等于本轮留下的内容时才写；授权位是独立的
`workspace.revert`（`git.status` / `session.read` 都不授权它）。拒绝是**具名**的：错误载荷的
`service_code` 给出条件（`revert_turn_running`、`revert_content_drift`、`revert_head_moved`、
`revert_record_expired`、`revert_path_not_in_turn`、`revert_before_unknown`、`revert_content_not_kept`、
`revert_no_before_content`、`revert_symlink_refused`、`revert_too_large`、`revert_write_failed`、
`revert_turn_state_unknown`、`revert_workspace_unavailable`），控制台据此给出可读文案并在转录里
显示，而不是吞掉。它**从不**碰 `HEAD`、索引或任何其它文件。

两个方法已在共享客户端实现但当前没有 UI 路径调用，因此不计入「实际使用」：
`runtime.attachments.stat`（`statAttachment`）与 `runtime.turn.approval.get`
（`getPendingApproval`，审批弹窗直接用事件流里的 `approval_required` 载荷）。
`runtime.session.create` 只写元数据、随后仍走原有 `runtime.session.open` + watch 路径；
`GET /api/projects` 与 `GET /api/bootstrap` 不再是业务入口（前者 deprecated，后者已删除）。

### 3.2 明确不调用的面（不声称对等）

- **没有 wire 方法、因此控制台不提供**：会话清理（`prune_empty`）、会话导出（JSON /
  Markdown）、对话**全文**搜索、上下文压缩与上下文状态（TUI `/compact`、
  `/context`）、safety / 权限策略读写、工具输出压缩设置（TUI `/compression`）。逐项真源与
  状态见 `docs/agent-runtime-service/progress.md` 的「尚未 wire 的后端能力（真实矩阵）」。
  项目登记/更新**不再是缺口**：`runtime.project.register` 已 wire（宿主目录浏览用
  `runtime.fs.list`），控制台经 §2.3 的「添加项目」流程使用它们。
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
| `tool_batch_started/finished` | 工具行的批次边界（一个批次一组，决定行的归属与顺序；并行标记与批次折叠行已取消），批次开始时先按 `calls` 预置待执行行 |
| `tool_started/updated/finished` | 工具项状态、路径、结果预览、错误 |
| `tool_result` | 旧式工具完成（按 call_id 关联到已有项） |
| `subagent_status_changed` | 父工具项上的子代理阶段 |
| `usage_updated` | 顶栏 token / 上下文 / 速率 / 步数指标 |
| `info` | 提示行（payload 为纯字符串） |
| `approval_required` / `turn_waiting_approval` | 审批弹窗 / 保持运行态 |
| `turn_completed/cancelled/failed` | 轮次结束（失败原因以提示行呈现） |

`plan_updated` / `plan_removed` / `diff_updated` 与 TUI 一致地不渲染（无 sink 表示）。
工具结果只呈现运行时的有界 `preview`；完整工具输出不在事件流中。

### “已工作”分组与刷新恢复

- 同一运行轮次只有一个分组头；中途的助手说明、工具批次和插话不会另开秒表。
- 完成、取消、失败后按运行时的真实耗时定格，并随历史投影保存；旧历史没有耗时
  元数据时显示“耗时未知”，不按工具数量估算秒数。
- 刷新后从历史恢复工具调用的结果预览和失败状态。同一标签页内，分组及工具详情的
  展开偏好按项目、会话、轮次隔离保存（最多保留最近 100 轮的视图元数据）。缓存不含
  消息正文、工具输出或凭据；退出登录时清除，禁用浏览器存储不影响历史读取。
- 运行中刷新仅重放仍完整保留的当前轮事件，不重放已完成的整段会话；计时从该标签页
  已记录的起点继续。首次从别处打开运行轮次且没有起点时不伪造耗时。早期事件已过期
  时明确提示恢复不完整，已保存历史仍可读取。

## 5. 文本与代码块渲染

助手回答与思考链按 Markdown 渲染（`web/src/components/Markdown.tsx` +
`web/src/markdown/parse.ts`）：标题、列表（含嵌套）、引用、水平线、GFM 表格，以及行内
代码/粗体/斜体/删除线/链接。模型常用的硬换行标签 `<br>` / `<br/>` / `<br />`（任意大小写）
解析为类型化的 `break` 节点、由 React 渲染成 `<br />`，因此单元格里可以自己堆叠多行，而标签
本身既不会作为文本显示、也不会作为标记注入。围栏代码块由 `web/src/components/CodeBlock.tsx` 渲染：
语言标签、复制按钮、横向滚动（不换行）、按语言的高亮（`web/src/markdown/highlight.ts`，
覆盖 python/js/ts/json/bash/yaml/sql/rust/go/java/c/cpp/css/html 与 diff，未知语言保持纯文本）。

图片引用 `![alt](src)` 解析为类型化的 `image` 节点：来源分类在
`web/src/markdown/imageRefs.ts`（纯函数），行为在 `web/src/components/MarkdownImage.tsx`。
**本地工作区路径**走 artifact 面（stat → 类型与体积校验 → 有界分块读字节）并渲染为图片，
点击用共享的 `ImageLightbox` 放大（复用同一个 blob URL，不重复读盘）；**远程 `http(s)` 图片
不加载**，只渲染成链接；`data:` 载荷与其它 scheme 直接拒绝。

约束与边界：

- 解析器无第三方依赖，**不生成 HTML**：返回类型化节点由 React 渲染，正文本身没有注入
  路径（公式与图形这两个「输出即标记」的例外见本节末尾）；
- 链接目标经 `sanitizeHref` 过滤，仅保留 http/https/mailto、锚点与相对路径，
  `javascript:` / `data:` / 协议相对 `//host` 一律降级为纯文本；
- 图片同样按来源分流，且**只有本地路径会被读取**：raster 白名单（png/jpeg/gif/webp/bmp，
  刻意排除 SVG）且单图不超过 4 MiB，超限或类型不符时回退为可点击的文件引用按钮加一行原因，
  不会出现裂图；远程图片只出链接（带 `rel="noreferrer noopener"`），因为加载它会向第三方
  暴露「这条回答被读过」，而控制台没有别的远程图片通道；
- 图片解析带 250ms 静默期：转录每来一个 token 就重解析一次，等引用稳定后再读盘，避免流式期间
  按 token 触发 stat；
- 点击图片打开的预览（`ImageLightbox`，转录图片与附件缩略图共用）支持缩放与平移：默认按整图
  适应，但**不低于原图 75%**、且不超过 1:1（小图不放大），因此一张 2200px 宽的图会以约 1650px
  打开，而不是被压进面板变成缩略图；超出舞台时可拖拽平移，偏移经 `clampOffset` 夹取，图片不会
  被拖出视野；滚轮以指针为锚点缩放，标题栏提供 适应 / 1:1 / − / + 与当前百分比，键盘 `+` `-`
  `0` `1`、双击在适应与 1:1 之间切换，缩放范围 10%–400%；**每次缩放固定 5 个百分点**（加/减，
  不是乘比例），所以读数始终是 75% → 80% → 85% 这样的整数步进，且反复缩放不会漂移；滚轮按
  累积位移折算档位（一档 = 100px / 3 行 / 1 页，`deltaMode` 三种单位都归一化），触控板的一串
  小位移会先累积到一档再走一步，不会一划就穿到底。缩放算术集中在
  `src/components/imageZoom.ts`（纯函数）：`tests/imageZoom.test.ts` 做行为测试，
  `tests/imageZoomGuard.test.ts` 钉住接线，`tests/imageZoom.verify.ts` 用真实浏览器验收 16 项；
- `tests/markdownImageGuard.test.ts` 静态守护这条边界：只有已解析的 blob URL 能进 `<img>`，
  远程来源只出链接，且该路径没有任何网络调用；
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
  （mermaid 懒加载，KaTeX 与其样式表随主包加载），不再以「不渲染」换取零依赖；长会话
  虚拟滚动另加 `@tanstack/react-virtual`（无传递依赖，约 15 kB，随主包加载）。
