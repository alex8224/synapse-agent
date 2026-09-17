# Synapse Web 控制台（React + TypeScript + Vite）

面向 runtime daemon 的浏览器控制台前端。生产运行方式与完整命令见仓库根
`README.md`、`docs/web-console/index.md` 与 `docs/web-console/formal-host.md`。

## 布局

桌面窗口是**两列**结构（`src/App.tsx`），不是「全宽顶栏 + 侧栏/主区 + 全宽底栏」。响应式布局分三档：

| 宽度 | 导航与内容 |
| --- | --- |
| ≥1024px | 保留桌面侧栏展开／折叠偏好及居中阅读列 |
| 768–1023px | 默认 44px 紧凑导航，可手动展开，不改写桌面偏好 |
| <768px | 默认关闭的覆盖式导航抽屉，正文不为侧栏让位；左右留白 12px（另计安全区） |

手机导航支持遮罩、关闭按钮、Esc、焦点约束与关闭后恢复焦点；切换项目或会话会关闭抽屉。
手机顶栏是两轨：导航按钮 + 会话标题，Git 入口收成 44px 的图标按钮（保留 `aria-label` 与 `title`）；
底栏收成单行：左轨自己横向滚动（不裁剪任何入口），中区遥测收成紧凑指标芯片（点开是完整分段），
窄屏策略为「更多」的条目进右侧「更多」菜单（该入口不随左轨滚动）。Git／文件预览在手机上使用列表与详情切换，
列表占满宽度，选中文件后切到详情并显示「返回文件列表」。轮次导轨在手机上隐藏（正文太窄）。

- 左列 `SideBar` 占满整个视口高度：导航（新建任务 `Ctrl+N`、搜索 `Ctrl+K`，与折叠轨上
  的同名入口是同一个动作）、项目 → 会话树（会话树**不显示滚动条但可正常滚动**：
  wheel / touch / 键盘，跨浏览器），底部是工作区身份行与设置/上下文操作入口（工作区文件、
  运行时诊断、退出配对；会话信息不在这里，它由顶栏标题打开）。
- 右列是「工作区列」，自上而下为 `TopBar`（三轨栅格：左轨项目与分支，中轨会话标题
  ——两侧等宽 `1fr` 使其**真正居中于工作区列**、不随左右内容宽度漂移、窄屏也不重叠，
  且标题本身是按钮，点击开合「会话信息」面板（锚在标题下方，Esc 关闭；安装态窗口里
  标题带 `wco-caption-controls`，否则标题栏会吞掉这次点击）；
  右轨变更统计 `+N -M`（`runtime.git.status` 的**真实 tracked 增删行数**，不是文件数）；
  分支 chip 与统计 chip 都是按钮、都能打开只读 Git Explorer，窄屏隐藏次要的项目 chip）、
  `RuntimeDiagnosticsBanner`（仅降级时出现）、`Transcript`、浮动的 `CommandInput`，
  以及 `BottomBar`（运行态、MCP、goal 与用量遥测，见下）。输入卡通过 ResizeObserver 发布实际高度，
  转录区据此预留底部空间，避免最新内容被输入卡遮挡。

**只读 Git Explorer 能看未跟踪文件的内容。** `git diff` 对未跟踪路径什么都不输出，所以服务端

**标题栏的「打开方式」把文件交给宿主的本机程序。** Git Explorer 与工作区文件窗口的标题栏各有一个
split button（`OpenWithMenu.tsx`）：左半用当前应用打开选中文件，右半展开宿主枚举出的应用列表
（`runtime.apps.list`：图标字形 + 名称 + 声称的扩展名，按扩展名分「推荐 / 其他应用」，底部还有
「在资源管理器中显示」）。启动走 `runtime.workspace.open_external`，请求只带**工作区相对路径**和宿主
自己发布的 `app_id`，从不带命令行；宿主再校验路径必须落在会话自己的工作区内。

- 应用集合是宿主 `runtime/service/external_apps.py` 里的一张**固定候选表 + 运行时探测**：装了才出现，
  表里没有的编辑器不会出现。兜底的「系统默认应用」永远可用（走操作系统关联），所以不会卡死。
- 图标由宿主给**字形 id**，前端映射到 Fluent 图标；未知 id 降级为按角色的通用图标（不回传可执行文件
  路径，控制台因此学不到宿主的目录结构）。
- 菜单 portal 到 `document.body`：两个宿主窗口的标题栏都是拖拽把手且会裁掉溢出，菜单留在里面会被裁掉、
  还会从搜索框开始拖动窗口。菜单打开时 `Escape` 由它接管（capture 阶段拦截）：第一次关闭菜单，
  第二次才关闭窗口。
- 「始终用 X 打开 .ext 文件」把选择记在 `appearance.openWith`（非机密的浏览器偏好，与主题同处一个
  存储豁免文件）；最近用过的应用只在本次会话内记住，用于让控件停在刚用过的那个应用上。
- 失败不会静默：宿主用具名 `service_code` 回答（路径不合法 / 越界 / 文件已不存在 / 未知应用 / 启动
  失败 / 无桌面会话），控制台据此在窗口内给出一行可读原因，并把当前选择留在可用应用上。
- 验收：`node tests/openWithMenu.verify.ts`（真浏览器 32 项：portal 定位与不出窗口、`Escape` 次序、
  外点关闭、方向键选行、`Enter` 按 id 启动、记住默认应用、具名拒绝、文件窗口同一控件）；静态契约在
  `tests/externalApps.test.ts` 与 `tests/openWithMenuGuard.test.ts`。
（`runtime/service/git.py`）在 diff 为空且未比较暂存区时读该文件（有界、二进制安全），按「新文件」
返回统一 diff（`--- /dev/null` / `+++ b/<path>` / `+行`），前端用同一套 `diffLineClass` 着色——
**不会**为了看内容而 `git add --intent-to-add`，索引与仓库始终只读。符号链接显示其目标，空文件与
目录仍报「没有差异」，超限截断（`truncated`），二进制显示「二进制文件，不显示 diff」。

**底栏是「静态清单 + 条目模块」的宿主。** `BottomBar.tsx` 只拥有所有条目共有的规则：三轨栅格、
唯一的 `openId`（同一个键关闭、另一个键替换，所以 F1／F5／F6 天然互斥）、按键映射、遮罩归属、
窄屏策略，以及「会话切换 / 条目不再可达即关闭」。每个条目是 `src/components/bottomBar/*Item.tsx`
里的一个 `BottomBarItemDefinition`，声明 `region`（left / center / right）、`order`、业务可见性
`visible`、弹层种类（`popover` / `modal`）、窄屏策略（`keep` / `compact` / `more`）以及自己的
Trigger / Content；**新增条目 = 一个模块 + `manifest.tsx` 里一行**，宿主不改，也没有可变注册中心、
外部插件或用户布局文件。条目各自订阅自己绘制的 store 字段（推理增量不会重绘底栏），隐藏的条目
根本不进布局（既不留包装元素，也不留分隔符）。

弹层归属写死在契约里：`popover` 由宿主 portal 成 `FloatingPanel` 并从**该条目自己的 trigger**
定位，外点关闭只认「本条目 trigger + 面板」（绝不整条底栏，也不是整条轨道），Esc 也只由它自己处理；
`modal`（`GoalDialog` / `HelpDialog`）自带遮罩、Esc 与焦点往返，宿主不再为它注册第二个监听。
F1 的条目没有 Trigger（右轨仍是空的对称占位列），只在 `src/components/consoleShortcuts.ts` 的
快捷键表里占一行——F1／F5／F6 的按键、帮助列表文案、触发器 tooltip、面板标题里的「(F5)」都取自
这张表。窄屏（<768px，与外壳同一断点）按条目策略收窄：中区条目默认进「更多」，而没有弹层可开的
条目不会被丢进「更多」（否则就不可达了）。「更多」菜单本身走控制台共享的
`useDialogKeyboardNav`（打开即聚焦首行、方向键在行间移动并环绕、关闭时把焦点交回「更多」按钮）；
菜单行打开的弹层即将随菜单卸载，所以该行在开弹层前先把焦点交回仍然挂载的「更多」按钮，
使 modal 自带的焦点恢复落在该按钮上、而不是落在已卸载的行上（否则关闭后焦点会掉到 `<body>`）。
底栏只认领 `consoleShortcuts` 里带 `key` 的那几行：命中的按键一律 `preventDefault`（长按 F5
不能触发浏览器刷新），随后再忽略 `repeat`；未认领的按键完全不拦截。

**条目可以自己决定「存不存在」**（`availability`）：Codex 用量条目
（`src/components/bottomBar/codexUsageItem.tsx`）只在**服务端**确认该会话的有效模型是启用的 Codex
OAuth profile 时才存在，所以它声明一个 `availability` 源而不是 `visible` 规则。宿主用
`useSyncExternalStore` 在解析布局**之前**过滤清单，于是「不可用」严格等于无包装、无分隔符、
无「更多」行、无快捷键、无残留面板。该源的 `subscribe` 同时负责启动/停止条目自己的控制器
（`src/stores/codexUsage.ts` 的 `codexUsageAvailability`）——如果只有挂载的 Trigger 才能启动它，
条目就永远无法从「隐藏」翻到「显示」。条目状态刻意**不**放进 `useConsoleStore`：它是一个独立
生命周期，控制器只读主 store 的 client / session / model / 连接状态；模型切换是**乐观发布**的
（先改 `modelName`，rebind 之后才落地），所以控制器还读 `modelRevision`——否则 rebind 之前发出的
判定会被上一个 profile 回答，而确认后的模型字符串又没变化、不会再通知任何消费者，条目会一直隐藏
到下一次上下文变化（实践中就是重新加载页面）。

条目画的是**产品自己的标识**（`src/components/bottomBar/CodexMark.tsx`，OpenAI 的六瓣结，内联
单条 path，不引图标包）。两条规则和相邻图标一致：单色（`fill="currentColor"`，由调用方给颜色，
这样「剩余低于 50% 转红」与深色主题都成立，品牌彩色会同时破坏这两者），以及 15px（与两侧
Fluent 图标同一视觉尺寸）。由 `tests/codexUsageEntry.test.ts` 守护。

兑换重置额度是**真实写操作**（消耗该 OAuth 账户的 1 次额度，不可撤销），所以面板先弹确认、
只有确认按钮才会发送一次请求；`unresolved` 记录在写发出**之前**落盘，只有写之后发出的额度读取
或确定的结果才能解除它，因此「结果未知」不会被自动重试、也不会用新幂等键重放。
`tests/codexUsage.verify.ts` 用真实浏览器 + 模拟 runtime 验收（可用性来源在隐藏期完成发现、
窗口按真实 `window_minutes` 标注、兑换零请求直到确认、消费后刷新、禁用后条目与分隔符一并消失、
360px 下经「更多」可达），`tests/codexUsage{Client,Controller,Entry}.test.ts` 覆盖协议解码、
生命周期竞态与确认流程（全部使用 fake transport，绝不触碰真实账户）。

契约与真实渲染的 DOM 由 `tests/bottomBarContract.test.ts` 覆盖（分区/顺序/可见性/紧凑策略/
单 `openId`/会话切换规则，以及用 `react-dom/server` 渲染出的轨道），宿主接线由
`tests/bottomBarLayout.test.ts` 与 `tests/bottomBarDismiss.test.ts` 守护，
`tests/bottomBarInteraction.verify.ts` 用真实浏览器验收（F1/F5/F6 开合与互斥、按键重复被忽略
且仍 `preventDefault`、未认领按键不被拦截、外点只关当前弹层、modal 的 Esc 与焦点回归、切换会话
确实换到另一会话并关掉旧弹层、360px 下各入口仍可达、紧凑指标面板完整可读），
`tests/bottomBarMore.verify.ts` 用 fixture（把真实条目改为「更多」策略）验收「更多」菜单的键盘
路径：键盘打开菜单→方向键移动/环绕→选择 modal→Esc→焦点回到「更多」按钮，选择 popover 同样如此。

聊天列与输入卡片共用同一套留白几何（`src/index.css` 的 `.console-gutter` + `.console-column`），
但**宽度是解耦的**：桌面端（≥ `lg`，1024px）每侧留白为工作区宽度的 10%，聊天列取工作区的
**80%** 且不设上限（更宽的工作区真的更宽）；输入卡片在同一中轴上另受 `--composer-max`（48rem）
封顶，超过上限后不再增长，这样单行输入不会横跨整个宽窗口。平板每侧留白
为 `2rem`，手机为 12px。transcript 的滚动容器**不显示滚动条**（`src/index.css` 的
`.no-scrollbar`，与侧栏会话树同一套规则），滚动本身不受影响（wheel / touch / 键盘）。
`.no-scrollbar` 的 `scrollbar-width` + `-ms-overflow-style` + `::-webkit-scrollbar`
三族规则按类名限定：未限定的 `::-webkit-scrollbar` 会把应用内所有滚动条一起隐藏（含嵌套块自己
需要的）。聊天区里的运行日志（thought / tools / info）是紧凑的次要
层级，展开后才成为面板，助手回复保持正文排版。工具调用不按批次折叠：一个批次的每次调用都直接
渲染一行紧凑日志行（图标、工具名、模型给的意图、路径），**行内不打印调用入参**，也没有描边和底色
——一批十条调用不会堆成十张卡片、抢走正文的视觉重量。**执行状态不用文字表示**：进行中的调用在行首
是一个旋转指示器（子代理步骤的动画由卡片导轨上的小圆承担，行内不重复），失败只把该行与工具名变红、
行内不附任何状态文案，成功则完全静默；只有「已取消」保留一个词。runtime 的 `status` 从来不是可打印
的状态——成功时它是 `summarize_tool_result` 给出的正文摘要（`ok (48 chars, 2 lines)`），失败时是错误
原因——所以两者都不出现在行内，**失败原因移到展开后的详情里**（正文已以该原因开头时不重复打印），
这也让「没有 preview 的失败」依然可展开。行尾也不右对齐：状态词与展开箭头紧跟内容，不占固定末位；
展开箭头只在悬停或键盘聚焦时出现。点开该行才挂载它自己的结果、终端输出或代码预览，还没收到结果的
调用不显示展开箭头。运行类工具（`execute` 等）的详情按**终端会话**渲染：`$ 命令` 是同一
滚动区里的首行（命令来自历史投影的 `args`，实时流则从批次事件携带的 `args_preview` repr 里读回），
下面是程序自己的输出，ANSI 颜色照原样还原、不折行。**调用之间的间距与批次无关**：同一批的两次调用、
相邻两批的两次调用都是 2px，批次边界在视觉上不存在——批次容器自身不再带垂直 padding，虚拟行在
「下一可见行也是同一轮的工具行」时把行距从 20px 收到 2px（`pb-0.5`），其余组合仍是转录行自己的
20px 节奏。Markdown 表格按正文可读字号渲染（`text-sm`，
14px；表头同字号、只用字重区分），单元格留出适度 padding，宽表在自身容器内横向滚动、不撑破
阅读列；每个单元格另有 `8rem` 宽度下限（`src/index.css` 的 `.markdown-body th/td`，刻意声明在
`@layer utilities` 之外以压过单元格上的工具类），否则自动表格布局会把短列一路压到最小内容宽度
——中文可逐字断行，最小内容宽度就是一个字，`B. 新功能文件` 会渲染成五行竖排。下限之和超过阅读列
时表格仍走自身的横向滚动，不会被裁掉。代码块与侧栏尺寸不变。

这些布局不变量由 `tests/shellLayout.test.ts`、`tests/topBarLayout.test.ts`、
`tests/transcriptLayoutGuard.test.ts` 与 `tests/markdownTableGuard.test.ts` 静态守护。

**长会话只挂载视口附近的行。** 一个长会话的投影有上千行，一次 commit 全部挂载会卡死页面，所以
`Transcript` 用 `@tanstack/react-virtual` 做窗口化：行高先按 `ESTIMATED_ROW_PX` 估算，挂载后由
`measureElement` 实测校正，上下各留 `OVERSCAN_ROWS` 行，其余行由占位高度撑出滚动条。滚动容器、
贴底跟随、读者手势闩锁（wheel / touch / **键盘**）与折叠守卫仍是 `Transcript` 自己持有，虚拟化
只决定「哪些行存在」，因此既有滚动语义不变；前插一页时按「距底部距离」这个不变量还原 `scrollTop`
（绝对定位的行没有浏览器滚动锚定可用），并在新行测量完成后继续校正。TurnRail 不再自己查
`[data-turn-id]`——离屏轮次根本没有那个元素——改为通过 `Transcript` 传入的 `TranscriptViewport`
句柄按索引取偏移与跳转，跳转落点手动应用 `scroll-padding-top` 预留量。由
`tests/transcriptVirtualization.test.ts` 静态守护，`tests/transcriptVirtual.verify.ts` 用真实
浏览器验收（1,200 行只挂载几十行、开屏贴底、向上滚动换窗、跳转到尚未挂载的轮次、前插保持视口）。

**折叠隐藏的行不占任何空间**：窗口化后每个「索引」都有一个定位用包装元素，若把行间隙无条件下在
它上面，折叠轮次里每个被隐藏的步骤都会留下 20px 空白（折叠得越多空白越大，末尾状态行也随之漂远）；
`rowPaints` 在虚拟化之前过滤隐藏行，避免离屏隐藏步骤仍保留估算高度或展开时的缓存高度；
渲染、测量与轮次跳转共用过滤后的索引，待完成跳转按消息 ID 跟踪。与纯列表的 `null` 行不占空间一致。
由 `tests/transcriptVirtualization.test.ts` 静态守护、`tests/transcriptFoldSpace.verify.ts`
用真实浏览器验收长折叠轮次、展开后收起及流式追加隐藏步骤。

**每轮的工作区改动以卡片列出**：轮次结束时，运行时会给出"这一轮改了哪些文件、每个文件增删多少行"
（每轮自己的贡献，不是工作区对 `HEAD` 的存量差，见 `runtime/workspace_changes.py`），控制台把它
渲染成该轮末尾的一张卡片列表（`transcriptRows/ChangesRow.tsx`，注册表与折叠策略各一行）。它不是
折叠步骤，所以**折叠的轮次照样看得到**；点某张卡片会打开只读的 Git Explorer 并定位到该文件
（`openGitExplorer(path)`，与顶栏分支 chip 共用入口）。列表有界（超出时写明"共 N 个 · 显示前 M 个"），
无法计数的改动（二进制/超大）不编造 0 行。由 `tests/turnChanges.test.ts`（含"事件晚于终止事件到达"
与刷新后从投影重放）、`tests/transcriptRowContract.test.ts` 与真机 `tests/transcriptChanges.verify.ts` 守护。

**每张卡片可以撤销这一个文件**（`runtime.workspace.revert`）：运行时会记下每轮改动前的文件内容
（`runtime/turn_reverts.py`，存在工作区自己的 `.synapse/turn-snapshots/` 下，有界保留最近若干轮），
撤销就是把那一份写回去——这是控制台里**唯一会写读者自己文件**的动作，所以它只做三件事：只还原卡片
上那一个路径、只在文件**仍然等于本轮留下的内容**时才写（之后被别的轮次或你自己改过就拒绝，绝不静默
丢弃）、**从不碰 `HEAD`、索引或任何其它文件**。写入是原子的（同目录临时文件 + `os.replace`）；本轮
新建的文件撤销时删除；符号链接一律拒绝写入。有回合在运行时直接拒绝（不会替你取消）。点"撤销"只是
**就地武装**，必须再点"确认撤销"才发出请求；成功后卡片显示"已撤销"并去掉行数（那些数字已不再描述
工作区），同时重读 git 状态；被拒绝时按具体原因在转录里给出可读提示（`revert_content_drift` /
`revert_turn_running` / `revert_record_expired` / `revert_head_moved` / `revert_before_unknown` …）。
刷新后卡片仍显示"已撤销"，因为历史读取会带上该轮已被撤销的路径（`HistoryEvent.reverted_paths`），
控制台据此重放同一形状——**不得**自己从 `git.status` 反推撤销状态。由 `tests/turnRevert.test.ts`
与真机 `tests/turnRevert.verify.ts` 守护。

`tests/shellLayout.verify.ts` 另测 360/390/430/640px 手机布局、768/900px 平板和桌面回归。
根布局采用 `100dvh`（保留 `100vh` 回退），视口声明启用安全区和支持浏览器的键盘布局缩放。
iOS Safari、Android 软键盘及安装态 PWA 的安全区仍需真机验收，桌面触控模拟不能替代。

**子代理步骤是卡片，不是更多工具行。** runtime 送来的工具批次是平铺的，`Transcript` 先按调用关系
把它折成渲染节点（`src/stores/transcriptLabels.ts` 的 `groupToolsForView`：`task` 调用，或 history
投影里带 `subagentName` 的行，开启一个分组；被标记为嵌套的行——`sub`，或 `parentId` 指向那次调用
——落进该分组，其余仍是主代理的平铺行）。分组渲染为一张可折叠的子代理卡片：卡面取中性的抬升层
`bg-raised`（`--surface-raised`，深色主题下是 `#383838`，不再是紫色底——紫色只留给子代理标记与
`@子代理名` 标签）；卡头依次是子代理标记（紫色 `Bot20Regular` 图标）、`@子代理名`、目标、
`N 步骤`、带图标的状态徽标（`运行中` 为旋转的 `SpinnerIos20Regular`，`完成` / `失败` 各带自己的
图标）与展开箭头；卡体沿一条竖直导轨（`border-l border-line`）列出该子代理自己的每一步，每步在
导轨上有一个显示该步自身状态的小圆（运行中同样旋转），行本身与主代理的工具行完全一致（同一个
渲染器）。卡片默认折叠，展开后才显示步骤；折叠状态是**行内视图状态**（不写回 store，所以不会重建 `messages` 数组、
也不会把转录拽到底部）。分组规则由 `tests/transcriptLabels.test.ts` 守护，卡面、标记与状态动画由
`tests/transcriptLayoutGuard.test.ts` 守护。

## 主题

外观由**一层 CSS 变量**决定，主题就是一组变量值：换主题不碰任何组件。机制在两处：

- `src/index.css` 的 `:root` 是主题契约——调色阶（`--gray-*`、`--blue-*`、状态色）、
  语义角色（`--surface` / `--surface-canvas` / `--surface-sunken` / `--line` / `--accent` /
  `--on-accent` / `--danger` / `--math-*`）、形状（`--radius-card/control`）、排印
  （`--font-ui` 界面默认、`--font-body` 阅读正文、`--font-mono` 代码）、层级
  （`--shadow-card` / `--shadow-flyout`）、材质（`--material-chrome` / `--material-flyout` /
  `--material-blur`）、动效（`--motion-fast/normal/ease`）与外壳密度（`--chrome-h` /
  `--status-h`）。值写成**通道三元组**（`--gray-200: 229 231 235`），
  这样 Tailwind 的 `bg-gray-200/70` 这类透明度修饰符仍然有效。
- `tailwind.config.js` 把组件**已经在用**的调色阶（`gray` / `blue` / 红黄绿紫状态色）与角色
  映射到 `rgb(var(--…) / <alpha-value>)`，并让圆角（`rounded-card/control`）、层级
  （`shadow-card/flyout`）、`transition-*` 的时长与曲线（`transitionDuration/TimingFunction`
  的 DEFAULT）和外壳高度（`h-chrome` / `h-status`）都取自主题，所以几百处既有类名自动跟随；
  新代码应该用角色名（`bg-surface`、`border-line`、`bg-accent`、`text-on-accent`）而不是某个色阶。
  材质是三个类：`.material-chrome`（侧栏/顶栏/状态条）与 `.material-flyout`（对话框/浮层）
  用 `--material-*` 的填充与背景模糊，`--material-blur` 为 `none` 时不产生合成层。焦点环全局只画一次
  （`:where(button, a[href], input, select, textarea, [tabindex]):focus-visible`，取自
  `--focus-ring`）。

**模态窗口必须走 `Portal`**（`src/components/Portal.tsx`，`createPortal` 到 `document.body`）：
`backdrop-filter` 会让元素成为 `fixed` 后代的**包含块**，所以渲染在侧栏子树里的对话框会被
"钉"到侧栏盒子上（表现为像嵌在侧栏里、位置错乱）——这正是亚克力主题引入后暴露的问题。设置/
目标/Git/灯箱/帮助这些窗口现在都挂在 `body` 下，定位与外壳材质无关。窗口内部**不显示滚动条**
（`.no-scrollbar`，滚轮与键盘照常可用）：对话框里出现滚动条是最像网页的一处。

**材质要能看见**：半透明填充盖在同色不透明面上、或背景模糊后面没有东西，都等于没有效果。
所以 Fluent 主题里 `--mica-backdrop` 给窗口一层极淡的品牌底纹（浏览器没有桌面壁纸可采样，
这是 Mica 的近似），`body` 画这层底纹，而窗口根（`.material-canvas`）、工作区面板
（`.material-pane`）与外壳（`.material-chrome`）都是半透明地盖在它上面——静止时就能看出玻璃感；
对话框/浮层盖在转录之上，背后有真实内容，模糊才真正生效。出厂主题这三项全是不透明 + `none`，
因此零开销、外观不变。

**动效**：`.flyout-in` / `.scrim-in` 让浮层"落位"而不是突然出现（淡入 + 轻微放大，时长与曲线取自
主题的 `--motion-normal` / `--motion-ease`，即 Fluent 的 `durationNormal` 与
`curveDecelerateMid`），并且尊重 `prefers-reduced-motion`。入场是按**材质**挂的
（`material-flyout` 的行必须同时带 `flyout-in`，由守护测试强制），新增浮层不会漏掉。转录区
**不加**动画：那里是流式高频更新，动效会重新引入 CPU 开销。

`[data-theme='…']` 块替换整套外观。控制台使用同一设计语言的两个主题：
`fluent-light` 与 `fluent-dark`（取值按 Fluent 2 语义近似，**可整块替换为设计稿 token**）。
两个示例主题的取值**不是凭记忆写的**：它们逐条取自 `@fluentui/tokens@1.0.0-alpha.22` 的
`webLightTheme` / `webDarkTheme`（可在浏览器里 `import` 该包直接读），每行注释都写明来源 token，
例如 `--surface` ← `colorNeutralBackground1`、`--surface-canvas` ← `colorNeutralBackground2`、
`--line` ← `colorNeutralStroke2`、`--accent` ← `colorBrandBackground`、
`--focus-ring` ← `colorBrandForeground1`（深色下底填色不够亮，焦点环必须用前景色）、
`--on-accent` ← `colorNeutralForegroundOnBrand`、`--danger` ← `colorStatusDangerForeground1`、
状态色阶 ← `colorStatusDanger*/Success*/Warning*` 与 `colorPaletteRed/Green/DarkOrange*`、
`--radius-control` ← `borderRadiusMedium`（4px）、`--radius-card` ← `borderRadiusXLarge`（8px）、
`--motion-fast/normal` ← `durationFast`/`durationNormal`（150/200ms）、
`--motion-ease` ← `curveDecelerateMid`（`cubic-bezier(0, 0, 0, 1)`）、
`--font-ui` ← `fontFamilyBase`、`--font-mono` ← `fontFamilyMonospace`、
`--font-numeric` ← `fontFamilyNumeric`（Bahnschrift，状态条的数字用它）。
公式块（`--math-*`）与外壳密度（48/32px）是产品选择，Fluent web token 里没有对应项，保持自定值。
两个主题同时定义中性面、文本、描边、强调色与状态色，并给出 Fluent 的**几何与排印**：
控件圆角 4px、外壳 48px/32px、Segoe UI 界面字体与 Consolas 代码字体（非 Windows 使用系统回退）、
`blur(30px)` 亚克力、150/200ms 与 `cubic-bezier(0, 0, 0, 1)` 动效曲线。

主界面不是仅换色：导航、会话列表、顶栏、输入区、模型选择器和设置使用共享控件样式。

| 范围 | 统一规则 |
|---|---|
| 图标 | 上述区域及侧栏上下文操作使用 `@fluentui/react-icons` 的 SVG，随包提供，不依赖图标字体 CDN；转录和其他专用面板暂未迁移 |
| 控件 | 常规按钮/输入框 32px，行内操作 24px；hover、pressed、disabled 和键盘焦点保持一致 |
| 排印 | UI 14px、分组/辅助标签 12px；`font-sans` / `font-mono` 均接入主题字体，代码和遥测保留专用字体 |
| 会话列表 | 当前会话使用品牌浅底、左侧标记及 `aria-current`；项目与时间分组有独立层级 |
| 输入区 | 单张紧凑卡片，宽度另受 `--composer-max` 封顶（比聊天列窄，同中轴）；文本区在上、控制行共用同一表面，聚焦时卡片描边转为强调色，字段本身不画焦点环；发送/停止为圆形图标按钮；模型/推理选项为可键盘操作的按钮，窄屏工具栏可换行；控制行左侧 `+` 为「插入图片」（见下节），其右为「添加项目」 |

### 输入区：多行编辑与内联引用

输入区是一个可多行编辑的富文本表面（`src/components/composer/RichComposer.tsx`），但**提交协议没有变化**：
仍是 `submitPrompt(text)` 加 store 自己持有的 `attachment_refs`。富编辑只发生在编辑阶段，提交时由
`composerDocument.ts` 把文档结构投影回纯文本。

| 交互 | 行为 |
|---|---|
| 换行 / 发送 | `Enter` 发送（运行中则排队为插话），`Shift+Enter` 换行；换行由 `insertLineBreak` 显式插入，避免浏览器用 `<div>` 包裹导致文本还原依赖布局 |
| 输入法 | 组合输入（IME）期间 `Enter` 属于候选词，不触发发送或选中 |
| 图片 | 粘贴、拖放或 `+` 点选都走同一条 `handleFiles`；每次最多 8 张、每张 ≤ 4 MB，被拒时显示原因 |
| 图片 Holder | 接受后作为**行内 Pill** 插在光标处（约 24px 高：微缩略图 + 文件名 + 进度 + 移除按钮），鼠标悬停在缩略图上弹出放大预览（`ImagePreviewFlyout`，贴合缩略图上方 8px，视口不足时翻到下方） |
| `@` 引用 | 键入 `@` 就地弹出候选（原生 Fluent 列表：分组标题、`aria-activedescendant` 指示、`.fluent-scrollbar`），`↑` `↓` 移动、`Enter` / `Tab` 插入、`Esc` 关闭 |
| `@` 类别 | 工作区文件（`runtime.artifacts.list` 按查询目录按需读取，不扫描整个工作区）、Agent 技能、运行上下文 |
| 序列化 | 文本原样、换行 `\n`、文件 `@path`、技能 `@skill:name`、上下文 `@context:name`、图片只进 `attachment_refs` 不进文本 |

Pill 是 `contenteditable="false"` 的原子节点，其含义存在按本地 id 索引的注册表里，**不从 DOM 读回**，
因此文件名、进度百分比、按钮文案都不可能混进 prompt。`composerDocument.ts` 的序列化只认结构与注册表，
不认识标签内容。

关于技能与上下文：控制台目前**没有**对应 RPC（`skills/` 由 Python 侧 `synapse.content.skills_catalog`
读取），所以 `mentionCatalog.ts` 里的技能清单是仓库 `skills/<name>/SKILL.md` 的静态镜像，
`tests/composerMentions.test.ts` 会把它与检出目录逐一比对——新增技能必须同时补一行。


保留两列布局、240px 侧栏、44px 折叠轨、聊天列宽度及发送/停止/附件处理逻辑。

读者侧的选择在**设置 → 外观 → 主题**：`跟随系统` / `浅色` / `深色`
（`src/stores/appearance.ts`）。`浅色` 用 `fluent-light`（不再使用旧外观），`深色` 用
`fluent-dark`，`跟随系统` 读 `prefers-color-scheme` 并在系统切换时跟随（监听 media query 的
`change`；只有偏好仍是 `system` 时监听器才动作，显式选择优先）。`src/main.tsx` 在**首次渲染前**
调用 `initAppearance()`，所以不会先画浅色再跳深色。

这个选择**落盘在 `localStorage`**（键 `synapse.console.appearance`），刷新与重开浏览器后
仍然生效。这是 C-12 唯一放行的非凭据例外：该不变量禁止的是**凭据**持久化（前端唯一允许
持有的秘密是宿主下发的 HttpOnly 会话 cookie），而主题名不是秘密。`tests/sourceGuard.test.ts`
只对 `stores/appearance.ts` 与 `stores/transcriptCache.ts` 放行存储 API，其余规则（token 文件、
`Authorization`、URL 里的 token、配对码）对这两个文件照旧生效；`tests/appearance.test.ts`
钉住「写入 → 下次加载恢复」以及「存储缺失/被禁用/值非法时回退到跟随系统」。
浏览器禁用存储（隐私窗口、阻止所有 cookie）时 `readStoredAppearance` 返回 `system` 而不是抛错，
控制台仍能正常启动。

`index.html` 里还有一段**首帧脚本**，在解析阶段就按同一个键设置 `data-theme`：bundle 只在
HTML 解析之后才执行，没有它，已存深色主题每次刷新都会先画一帧浅色。脚本只重复键名与两个主题
id，`tests/appearance.test.ts` 把三者钉在一起；它不参与 `system` 解析（留给 bundle）。

新增主题 = 复制一个 `[data-theme='…']` 块、给出这些变量的值；不需要改 Tailwind 配置或组件。
不变量由 `tests/themeContract.test.ts` 守护：配置引用的变量必须在主题里有定义、示例主题必须
替换掉角色、组件里不得再出现 `bg-white` / `text-white` / 任意 hex（否则那块颜色主题够不到）。

布局与排版层级之外，本轮唯一的功能改动是
在只读 `runtime.git.status` 上**增量**加了 `insertions` / `deletions` 两个字段（`git diff
--numstat HEAD` 的真实 tracked 增删行数，见下），旧字段与旧接口保持兼容；不宣称与参考截图
像素级一致。

静态守护之外，`tests/shellLayout.verify.ts` 会用真实宿主 + 无头 Chrome 量一遍渲染结果
（侧栏是否全高、顶栏/底栏是否只属于右列、输入卡片是否落在聊天列的同一中心线上、窄窗是否
横向溢出），并在 1920 / 1440 / 900 / 640 四个视口核对宽度：聊天列桌面端为工作区的 80% 且
随窗口增长，输入卡片停在其 48rem 上限、始终与聊天列同中轴且不更宽，窄屏两者都近全宽。
它需要本机有 Chrome/Edge，且不属于 `npm test`：

```bash
node tests/shellLayout.verify.ts
```

Fluent 主界面可另运行 `node tests/fluentAppearance.verify.ts`：使用隔离的合成项目/会话，
不连接真实宿主，检查明暗切换、控件尺寸、焦点、菜单和窄屏；截图输出到工作区 `.tmp/`。

## Markdown 渲染：公式与图形

正文一律由 `src/markdown/parse.ts` 解析成类型化节点、再由 React 渲染，**不生成 HTML**。
只有两个渲染器的输出本身就是标记，它们各自承担自己的清洗责任：

模型常写的硬换行标签 `<br>` / `<br/>` / `<br />`（任意大小写）也走类型化节点：解析器产出
`{ type: 'break' }`，`Markdown.tsx` 渲染成 `<br />`。这样表格单元格里可以自己堆叠多行，而
标签本身既不会作为文本显示、也不会作为标记注入（`tests/markdownParser.test.ts` 覆盖各种写法与
否定样例，`<brx>` / `<b r>` / `</br>` 保持纯文本）。

| 输入 | 渲染 | 失败/拒绝时的行为 |
|---|---|---|
| `$$...$$`（块）与 `$...$`（行内） | KaTeX（`src/markdown/tex.ts` + `src/components/MathTex.tsx`） | 回退为「公式 + LaTeX 源码」块并写明原因 |
| ` ```mermaid ` 围栏 | mermaid（`src/components/MermaidBlock.tsx`），`import('mermaid')` 懒加载 | 回退为代码块，头部显示原因 |

边界（`tests/markdownRenderGuard.test.ts` 静态守护）：

- `src/**` 中唯一一处 `dangerouslySetInnerHTML` 在 `src/components/GeneratedHtml.tsx`，
  只接受 KaTeX 输出与经 DOMPurify SVG profile 清洗过的 mermaid SVG；
- KaTeX 固定以 `trust: false` / `throwOnError: false` / `maxExpand: 1000` 运行，
  mermaid 固定以 `securityLevel: 'strict'` + `htmlLabels: false` 运行；
- 含 `%%{...}%%` 指令或 YAML frontmatter（`---`）的图形**不渲染**：这两个是模型文本能把
  CSS（`themeCSS` / `fontFamily`）带进 SVG 的仅有通道，mermaid 只校验 CSS 花括号配平、
  DOMPurify 不解析 CSS，而内联 SVG 的 `<style>` 作用于整页。渲染结果里 `<style>` 的
  `@import` / `url()` 也会被中和，作为 mermaid 升级后的兜底。空定义与超过 20000 字符的
  图形同样拒绝——这些情况都回退为带原因的源码块；
- mermaid 渲染到组件自己的离屏容器（不碰 `document.body`），流式未闭合的围栏在闭合前
  一直是代码块，因此不会出现半张图或红色半成品公式。

Markdown 渲染依赖：`mermaid` / `katex` / `dompurify`；mermaid 只在实际
出现图形时下载（构建产物里是独立 chunk），KaTeX 与其字体随主包加载，不访问任何 CDN。

## 开发

开发只依赖 Vite 做静态热更新与受控代理；它不读取任何 token 文件，也不实现
业务中间件。先启动正式宿主（提供配对/会话 API 与 `/runtime-ws` 中继），再启动 dev
server。宿主默认会按需拉起 runtime daemon（没有运行中的就起一个、退出时停掉；已有的
就复用），所以这一步不需要另开终端跑 `synapse-runtime`：

```bash
# 终端 1：正式宿主（也可以指向任意部署好的宿主）
synapse-web-console --workspace .. --static-dir dist --port 8080
# 终端 2：Vite（默认把 /api 与 /runtime-ws 代理到 http://127.0.0.1:8080）
SYNAPSE_WEB_CONSOLE_URL=http://127.0.0.1:8080 npm run dev
```

开发下同样需要配对：从宿主 stderr 读取
`synapse-web-console: pairing code XXXXXXXX (…)` 行，在
`http://127.0.0.1:5173` 的配对界面输入该码（单次使用，默认 300s 过期）。
宿主未运行时页面仍能打开，但 `GET /api/session` 会失败并在界面上明确报错
（无硬编码 token/project/workspace 回退）。

代理只把 `/api` 与 `/runtime-ws` 转发到宿主，并把转发请求的 `Origin` 重写为
宿主 origin；它不直连 daemon、不注入凭据、不读 token 文件，`server.host` 保持
loopback 默认值，因此不构成认证旁路（宿主也不因 dev 放宽任何校验）。

## 脚本

| 命令 | 作用 |
|---|---|
| `npm run dev` | Vite dev server（仅热更新 + 受控代理） |
| `npm run build` | `tsc -b && vite build`，产物在 `dist/` |
| `npm test` | Node 原生测试（`tests/*.test.ts`） |
| `npm run lint` | oxlint |

`dist/` 是独立构建资产，不随 Python wheel 打包；部署时用
`synapse-web-console --static-dir <dist>` 显式指定。宿主要求该目录存在且含
`index.html`，否则启动失败并给出可操作错误（不会出现「启动成功但页面 404」）。

## 可安装应用（PWA）

控制台可以「安装」成独立窗口的应用：`public/manifest.webmanifest` 与 `public/icon-*.png`
随构建产物落在静态根（Vite 的 `public/` 与宿主 `--static-dir` 的根目录是同一层），
所以它们用**根路径绝对 URL**（`/manifest.webmanifest`、`/icon-192.png`），与 `index.html`
里的引用一致。`index.html` 另给出 `apple-touch-icon`、`theme-color` 与
`apple-mobile-web-app-*`，让 iOS 的「添加到主屏幕」拿到同一套外观。

| manifest 字段 | 值 | 说明 |
|---|---|---|
| `id` / `start_url` / `scope` | `/` | 控制台没有路由，唯一依赖的 URL 就是 origin（`/runtime-ws` 由 origin 推导，不读查询串） |
| `display` | `standalone` | 安装后是独立窗口，不是浏览器标签页 |
| `display_override` | `["window-controls-overlay", "standalone"]` | 顺序即优先级：安装窗口的标题栏由控制台接管（见下），不支持该显示模式的浏览器退回 `standalone` |
| `name` / `short_name` | `Synapse Console` / `Synapse` | |
| `lang` / `theme_color` / `background_color` | `zh-CN` / `#F8F9FA` / `#F8F9FA` | manifest 是静态文件，这几个值只负责**首帧**；运行时的窗口框架色由 `<meta name="theme-color">` 跟随主题更新（见「标题栏」一节） |
| `icons` | `/icon-192.png`、`/icon-512.png`（`any`）+ `/icon-maskable-512.png`（`maskable`） | 192 与 512 的 `any` 加一个 512 的 maskable，即 Chrome 安装入口要求的用途组合 |
| `launch_handler.client_mode` | `focus-existing` | 点通知或任务栏图标回到**已经开着**的那个窗口，而不是再开一份 |
| `shortcuts[0].url` | `/?action=new-session` | 任务栏右键的「新建会话」（见下） |

图标由 `public/favicon.svg` 的品牌标记生成，三个 PNG 已入库、**没有提交生成脚本**：
512×512 用无头 Chrome/Edge 把该 SVG 光栅化（`tests/helpers/cdp.ts` 发现的是同一批可执行
文件），再用 Pillow 缩放出 192 与 512（透明底），maskable 版在同一张 512 图上垫不透明
`#F8F9FA` 底并把标记缩到约 280px 见方，使其落在 maskable 的安全圆内（四角为底色、
中心为标记）。重新生成后必须跑 `npm test`：`tests/pwaManifest.test.ts` 从磁盘读真实文件，
按 PNG 的 IHDR 核对声明尺寸、要求每个图标都是根路径 PNG、`any` 覆盖 192 与 512 且至少
一个 512 的 maskable，并断言每个快捷方式指向的动作这一版真的实现了——图标缺失或尺寸写错
会在单测里失败，而不是在浏览器里表现为「没有安装入口」。

### 标题栏（Window Controls Overlay）

安装窗口里浏览器不再画应用图标与应用名，只把窗口按钮浮在页面上，标题栏交给控制台
**现有的顶栏**接管（不新增标题栏组件）：`src/index.css` 的 `.wco-*` 工具类只在
`@media (display-mode: window-controls-overlay)` 内生效，`src/components/TopBar.tsx` 的
`<header>` 成为窗口拖拽区，左侧 chip 轨道加 `wco-caption-controls` 反向退出拖拽（否则侧栏
按钮、项目与分支 chip 全都点不动），右侧那条本来就空的轨道加 `wco-caption-reserve`，
宽度由 `calc(100vw - env(titlebar-area-x) - env(titlebar-area-width))` 算出，正好让开窗口
按钮。顶栏的高度、三轨道网格与 `.console-pane-inset` 的负 margin 都不变，所以非安装态
（浏览器标签页、不支持该模式的浏览器）布局与今天完全一致。

预留宽度是**算出来的**而不是写死的：用户在窗口 ⋮ 菜单里选「显示标题栏」后 overlay 消失、
`env()` 归零，这条规则自动变成 no-op，全程不需要 JS 或条件渲染。
`tests/windowControlsOverlay.test.ts` 钉住这些约束（规则只在显示模式内、拖拽与 no-drag
成对、预留必须来自 `env()`、顶栏保持三轨道且中间标题仍可拖）。

生效条件：`display_override` 的变更要**重启已安装的应用**（必要时卸载重装，配对 cookie
不受影响），且该模式是 Chromium 桌面独占——Firefox、Safari、iOS 一律退回 `standalone`。

浏览器自带的 overlay（origin 文字 + 展开箭头 + 应用菜单 + 窗口按钮）始终在最上层、吞掉该
区域的点击，**不能由页面隐藏或改样式**：规范里写明了它会在启动后出现，且 overlay 的
`geometrychange` 会让 `titlebar-area-*` 随之变化（控制台因此只用 `env()` 算预留，不做任何
假设）。控制台能做的只有两件事——让出宽度，以及对齐配色。

配色就是 `theme_color`：overlay 的背景色取自 manifest 的 `theme_color`（规范原文
"the window controls overlay would use the `theme_color` from the manifest as the background
color"）。manifest 改不动，所以首帧由它兜底，运行时的切换交给
`<meta name="theme-color">`：`src/stores/appearance.ts` 在主题变化时把它改成浅色
`#F8F9FA` / 深色 `#1F1F1F`（深色画布色，`index.css` 的 `--f-canvas`），
`tests/appearance.test.ts` 钉住「首帧 tag、meta 目标值、manifest 三者一致」。深色主题下若
这条 overlay 仍是浅底，说明该 Chromium 版本不读 meta——那就只能把 manifest 的
`theme_color` 改成静态深色。

### 后台通知与角标

策略是**纯函数**（`src/stores/backgroundAlerts.ts`），浏览器 API 只出现在 React 胶水层
`src/components/BackgroundAlerts.tsx`（渲染 `null`，且只挂在已配对的控制台上，配对界面
不会触发任何权限请求）。判据是 store 状态的**边沿**，不是原始事件流：

| 边沿 | 条件 | 结果 |
|---|---|---|
| `pendingApproval` 由无到有 | 权限已授权且 `document.visibilityState === 'hidden'` | 「需要审批」通知（`tag: synapse-approval`，同类通知互相替换） |
| `runtimeStatus` 由 running 变 idle，且没有待审批 | 同上 | 「任务已完成」通知，正文带会话标题 |
| 快照没变 / 窗口在前台 / 权限未授权 | — | 不发通知：正在看控制台的人不需要被打断，流式增量与重命名也不会触发 |

角标走 Badging API：待审批记 1，加上「已完成但没看过」的轮数；窗口回到前台即清零
（`visibilitychange` 与 `focus` 都算确认，并顺带重读权限，因为用户可能在浏览器自己的界面里
回答了弹窗）。角标是装饰，失败被吞掉、不阻塞控制台。通知点击把已有窗口拉到前台
（冷启动那条路径由 manifest 的 `focus-existing` 覆盖）。权限开关在**设置 → 后台通知**：
显示 `已授权` / `已被拒绝（需在浏览器站点设置里恢复）` / `未请求` / `当前浏览器不支持`，
「启用通知」按钮就是浏览器要求的那个用户手势（打开对话框本身**不会**弹权限框）。

### `?action=new-session` 快捷方式

`src/client/deepLink.ts` 解析查询串：只认 `action=new-session`，其它 `action` 值按「没有动作」
处理（`App.tsx` 因此不改写地址栏）；识别到 `new-session` 时才用 `history.replaceState`
把它从地址栏剥离，刷新不会重放动作。读取时机是**已配对且中继已连接**之后、只读一次，并且
只有启动时已经附着了已有会话才新建会话——启动本身就建了空会话时什么都不做，避免一次点击
建出两个会话。

### 能力边界

- 通知只能由**运行中的控制台页面**发出：本地宿主不使用 Web Push，页面关掉后不会有任何推送。
- 安装成应用后**仍然需要** `synapse-web-console`（及其 daemon）在跑：安装只是外壳，没有服务端。
- 配对 cookie 是宿主**内存**态、默认 12h TTL（`--session-ttl-seconds`）：宿主重启后必须重新
  输入 stderr 打印的配对码，安装的应用没有例外。
- `web/dist` 不在 wheel 里，宿主仍需 `--static-dir web/dist`；manifest 与图标只是该目录下的
  普通静态文件（`no-cache`，不在 `assets/` 下），见 `docs/web-console/formal-host.md` §6.1。
- 浏览器存储只用于两处非秘密的 UI 状态：会话视图缓存（`sessionStorage`）与读者选的主题
  （`localStorage`，键 `synapse.console.appearance`）。C-12 的**凭据**路径仍由
  `tests/sourceGuard.test.ts` 全量守护，仅这两个文件豁免「浏览器凭据持久化」这一条规则；
  通知权限由浏览器自己保存，不属于页面存储。

## 会话管理（侧栏）

侧栏的项目 → 会话树直接使用 runtime 的会话管理接口，而不是本地拼装：

| 操作 | wire 方法 | 说明 |
|---|---|---|
| 项目列表 | `runtime.project.list` | 可见项目枚举（服务端计算可见集合、先过滤再分页，`limit` 1..100）；`GET /api/projects` 仍是 deprecated 兼容路由，不是业务入口 |
| 新建 | `runtime.session.create` | 只写入会话元数据，`thread_id` 由服务端 `allocate_thread_id` 分配并返回，前端不再自行生成 id；随后仍走原有 `runtime.session.open` + watch 路径 |
| 重命名 | `runtime.session.rename` | 标题 1–120 字符，空白或超长在本地与服务端都会被拒绝 |
| 删除 | `runtime.session.delete` | 只删除会话记录（元数据与 goal）。checkpoint 与 transcript 仍保留在磁盘上，确认框与提示都会明确写出这一点，不会宣称「对话已删除」；确认是一个居中模态框（`SessionDeleteDialog`：Portal + 遮罩 + 焦点陷阱，Esc 或点遮罩关闭），在正文里写出目标会话的标题与 `thread_id`，只有「删除」一个决策按钮——默认焦点落在标题栏的关闭控件上，所以刚打开时按 Enter 只会关掉它；运行中的会话由服务端原子拒绝（`conflict`），前端不会自动 cancel，被拒时模态框保持打开并就地显示原因 |
| 搜索 | `runtime.session.search` | 服务端元数据搜索（title/summary/thread_id/model），不是对话全文搜索；分页与服务端一致，输入竞态由 generation 计数丢弃过期结果 |

## 图片附件（输入区）

图片不会内联进任何帧：前端先 `runtime.attachments.begin` 声明大小与 MIME，再按
服务端返回的 chunk 预算分块 `runtime.attachments.append`（每块 ≤ 256 KiB，base64），
最后 `runtime.attachments.finish` 校验并落盘；此后只引用不透明的 `attachment_id`。

| 操作 | wire 方法 | 说明 |
|---|---|---|
| 粘贴 / 拖放 / 点选 | — | 三条入口都走同一条 `handleFiles`：粘贴到输入卡片、直接拖放到卡片上、或用控制行的 `+` 点选文件；只接受图片，每次最多 8 张、每张 ≤ 4 MB，被拒的文件名与原因会显示在输入区 |
| 上传 | `runtime.attachments.begin/append/finish` | 每个附件独立串流并显示进度；「取消/移除」会在块间中止（best-effort `runtime.attachments.abort`）。失败只显示原因，不会把文件名当成 prompt 发送 |
| 发送 | `runtime.turn.submit` 的 `attachment_refs` | 有附件仍在上传时禁止发送；仅附件（文本为空）可以提交；已 finalize 的 ref 不会被前端删除 |
| 历史 | `runtime.attachments.read` | 历史里的用户消息只带 metadata，缩略图按需分块读取并生成 blob URL；卸载或切换会话时 revoke，超过 4 MB 或非图片类型不加载 |

切换会话/项目或退出配对会取消当前输入区的上传并 best-effort abort 未完成的附件；
已 finalize 的附件保留在服务端，刷新历史后仍会显示。

前端镜像的服务端常量（`src/synapse/runtime/service/attachments.py`，两侧取值必须一致）：

| 常量 | 值 | 说明 |
|---|---|---|
| `MAX_ATTACHMENT_BYTES` | 4 MB | 单张图片上限（`ATTACHMENT_MAX_BYTES`） |
| `MAX_ATTACHMENTS_PER_SUBMIT` | 8 | 单次 submit 的 `attachment_refs` 上限（`ATTACHMENT_MAX_COUNT`） |
| `MAX_SESSION_ATTACHMENT_BYTES` | 512 MB | 单会话累计字节上限 |
| `MAX_PROJECT_ATTACHMENT_BYTES` | 512 MB | 单项目累计字节上限 |
| `MAX_CHUNK_BYTES` | 256 KiB | 单块解码上限（`ATTACHMENT_CHUNK_BYTES`） |
| `DEFAULT_READ_BYTES` | 64 KiB | 历史缩略图每次读取窗口（`ATTACHMENT_READ_BYTES`） |
| `INCOMPLETE_TTL_SECONDS` | 3600 | 未完成上传在服务端被清理前的静默时长（无后台清理线程，按操作有界 sweep） |

累计维度**只限字节，不限张数**：曾经的单会话/单项目「累计张数 128」上限已移除。finalized
附件永不回收（历史仍引用它们），所以张数上限会变成永久且不可恢复的墙——项目一旦攒够该张数，
之后**任何**会话（包括新建会话）的粘贴/拖放/点选都会被服务端以 `attachment_quota` 拒绝。
字节上限仍会拒绝超限上传。服务端对每种拒绝只发通用 `message`（`runtime service error`），
具体条件在 `data.service_code` 里，前端由 `attachmentErrorMessage()`
（`src/runtime-client/attachments.ts`）映射成可读原因，上传失败与历史缩略图读取失败共用它：

| `service_code` | 输入区 / 缩略图显示的原因 |
|---|---|
| `attachment_quota` | 附件存储已达服务端上限（按会话/项目的字节配额拒绝）：清理工作区下的 `.synapse/attachments` 后可重试 |
| `attachment_too_large` | 图片超过单张上限（4 MB） |
| `attachment_unsafe` | 图片数据未通过校验：声明的类型与实际内容不符，或文件已损坏 |
| `attachment_not_found` | 附件不存在（服务端已没有这个文件） |
| `attachment_forbidden` | 该附件属于其他会话，无法读取 |
| `attachment_conflict` | 上传状态冲突（分块偏移或大小不一致），请重新上传 |
| `attachment_unavailable` | 附件存储当前不可用 |

未映射的码回退到服务端 `message`，因此未知失败仍然说得出「发生了什么」，只是不够具体。

服务端 `runtime.attachments.abort` **有** finalized 保护：对已 `finish` 的附件（或
`finish` 崩溃窗口里已落盘的 `data.bin`）调用只返回 `removed=False` 且不删除字节，
未完成上传仍正常删除。控制台依旧只在 `finish` 之前取消/失败时调用 abort
（`uploadAttachment`），从不对 finalized ref 调 abort；「不误删已完成附件」现在由
服务端强制，客户端约定只是第一道防线。详见 `docs/agent-runtime-service/progress.md`
的「已修复」小节。

## 添加项目（输入区 `+`）

控制行左侧的 `+` 不再打开图片选择器，而是打开「添加项目」对话框：浏览器无法解析宿主路径，
因此对话框通过 `runtime.fs.list` 逐级浏览**宿主**文件系统（只列直接子目录、有界、只读；
`path=null` 表示 daemon 的 home 目录）。到达目标目录有三种方式：顶部**盘符/根**按钮
（结果里的 `roots`：Windows 为盘符、POSIX 为 `/`）、点条目逐级进入、或把绝对路径**粘贴**到
路径框后「前往」。选中方式也有两种：条目右侧的「选择」按钮（一次点击直接选中该目录），
或底部「选择当前目录并新建会话」。随后对选中的目录调用 `runtime.project.register`
（daemon 校验其为已存在目录并 upsert 用户层 catalog，按路径幂等、复用稳定的 `project_id`）。
注册成功后控制台切到该项目并新建一个会话。

图片附件除**粘贴/拖放**外，也可用控制行的 `+` 点选（`+` 仍是插入图片，不是菜单）；三条入口共用同一
校验与上传管线，8 张 / 4 MB 上限与行内 Pill + 悬停预览行为一致。

## 契约与生成类型

`src/runtime-client/` 是 DOM-free 的协议 core：宿主耦合只经过注入的 `SocketLike`，
默认实现才引用 `WebSocket`，因此模块在无 DOM 库时也能加载并通过类型检查。旧的
`src/client/*.ts` 路径保留为 `export *` 薄壳，新代码从 `src/runtime-client/` 导入。

wire 类型与版本常量不是手写的：`src/runtime-client/contract.generated.ts` 由
`scripts/export_contract_manifest.py` 从 `service/contract_registry.py` 生成（与
`src/synapse/runtime/service/contract_manifest.json` 同源）。`types.ts` 只 re-export
生成物，不要手写 wire DTO；契约边界与兼容规则见
`docs/agent-runtime-service/adr-s-019-contract-freeze.md`。

```bash
# 在仓库根目录执行
uv run --no-sync python scripts/export_contract_manifest.py         # 重新生成两个产物
uv run --no-sync python scripts/export_contract_manifest.py --check # 校验提交内容未漂移
cd web && npx tsc -b
cd web && node --test tests/runtimeContractFixture.test.ts tests/runtimeClientBoundary.test.ts tests/sourceGuard.test.ts
```

跨语言 fixture 位于 `tests/fixtures/runtime_contract/fixtures.json` 与
`tests/fixtures/runtime_contract/v1/`，Python 与 TypeScript 两侧消费同一份。