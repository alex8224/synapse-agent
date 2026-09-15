# Synapse Web 控制台（React + TypeScript + Vite）

面向 runtime daemon 的浏览器控制台前端。生产运行方式与完整命令见仓库根
`README.md`、`docs/web-console/index.md` 与 `docs/web-console/formal-host.md`。

## 布局

窗口是**两列**结构（`src/App.tsx`），不是「全宽顶栏 + 侧栏/主区 + 全宽底栏」：

- 左列 `SideBar` 占满整个视口高度：导航（新建任务 `Ctrl+N`、搜索 `Ctrl+K`，与折叠轨上
  的同名入口是同一个动作）、项目 → 会话树（会话树**不显示滚动条但可正常滚动**：
  wheel / touch / 键盘，跨浏览器），底部是工作区身份行与设置/上下文操作入口。
- 右列是「工作区列」，自上而下为 `TopBar`（三轨栅格：左轨项目与分支，中轨会话标题
  ——两侧等宽 `1fr` 使其**真正居中于工作区列**、不随左右内容宽度漂移、窄屏也不重叠，
  右轨变更统计 `+N -M`（`runtime.git.status` 的**真实 tracked 增删行数**，不是文件数）；
  分支 chip 与统计 chip 都是按钮、都能打开只读 Git Explorer，窄屏隐藏次要的项目 chip）、
  `RuntimeDiagnosticsBanner`（仅降级时出现）、`Transcript`、作为最后一行占位的 `CommandInput`，
  以及 `BottomBar`（运行态与用量遥测、MCP、goal）。输入区**不是**浮层：它占自己的一行，转录
  滚动容器在其上方结束，因此自动跟随到底部时最新一行就在可见底边上（旧的 `absolute bottom-0`
  浮层会把最新内容盖住，需要手动上滚才能看见）。

聊天列与输入卡片共用同一套留白几何（`src/index.css` 的 `.console-gutter` + `.console-column`），
但**宽度是解耦的**：桌面端（≥ `lg`，1024px）每侧留白为工作区宽度的 10%，聊天列取工作区的
**80%** 且不设上限（更宽的工作区真的更宽）；输入卡片在同一中轴上另受 `--composer-max`（48rem）
封顶，超过上限后不再增长，这样单行输入不会横跨整个宽窗口。窄屏每侧退回固定
的 `2rem`，两者都近全宽。transcript 的滚动容器**不显示滚动条**（`src/index.css` 的
`.no-scrollbar`，与侧栏会话树同一套规则），滚动本身不受影响（wheel / touch / 键盘）。
`.no-scrollbar` 的 `scrollbar-width` + `-ms-overflow-style` + `::-webkit-scrollbar`
三族规则按类名限定：未限定的 `::-webkit-scrollbar` 会把应用内所有滚动条一起隐藏（含嵌套块自己
需要的）。聊天区里的运行日志（thought / tools / info）是紧凑的次要
层级，展开后才成为面板，助手回复保持正文排版。Markdown 表格按正文可读字号渲染（`text-sm`，
14px；表头同字号、只用字重区分），单元格留出适度 padding，宽表在自身容器内横向滚动、不撑破
阅读列；每个单元格另有 `8rem` 宽度下限（`src/index.css` 的 `.markdown-body th/td`，刻意声明在
`@layer utilities` 之外以压过单元格上的工具类），否则自动表格布局会把短列一路压到最小内容宽度
——中文可逐字断行，最小内容宽度就是一个字，`B. 新功能文件` 会渲染成五行竖排。下限之和超过阅读列
时表格仍走自身的横向滚动，不会被裁掉。代码块与侧栏尺寸不变。

这些布局不变量由 `tests/shellLayout.test.ts`、`tests/topBarLayout.test.ts`、
`tests/transcriptLayoutGuard.test.ts` 与 `tests/markdownTableGuard.test.ts` 静态守护。

**子代理步骤是卡片，不是更多工具行。** runtime 送来的工具批次是平铺的，`Transcript` 先按调用关系
把它折成渲染节点（`src/stores/transcriptLabels.ts` 的 `groupToolsForView`：`task` 调用，或 history
投影里带 `subagentName` 的行，开启一个分组；被标记为嵌套的行——`sub`，或 `parentId` 指向那次调用
——落进该分组，其余仍是主代理的平铺行）。分组渲染为一张可折叠的子代理卡片：卡面取中性的抬升层
`bg-raised`（`--surface-raised`，深色主题下是 `#383838`，不再是紫色底——紫色只留给子代理标记与
`@子代理名` 标签）；卡头依次是子代理标记（紫色 `Bot20Regular` 图标）、`@子代理名`、目标、
`N 步骤`、带图标的状态徽标（`运行中` 为旋转的 `SpinnerIos20Regular`，`完成` / `失败` 各带自己的
图标）与展开箭头；卡体沿一条竖直导轨（`border-l border-line`）列出该子代理自己的每一步，每步在
导轨上有一个显示该步自身状态的小圆（运行中同样旋转），行本身与主代理的工具行完全一致（同一个
渲染器）。卡片默认展开，折叠状态是**行内视图状态**（不写回 store，所以不会重建 `messages` 数组、
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
| 输入区 | 单张紧凑卡片，宽度另受 `--composer-max` 封顶（比聊天列窄，同中轴）；文本区在上、控制行共用同一表面，聚焦时卡片描边转为强调色，字段本身不画焦点环；发送/停止为圆形图标按钮；模型/推理选项为可键盘操作的按钮，窄屏工具栏可换行；控制行左侧 `+` 为「添加项目」（见下节） |

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
| 删除 | `runtime.session.delete` | 只删除会话记录（元数据与 goal）。checkpoint 与 transcript 仍保留在磁盘上，确认框与提示都会明确写出这一点，不会宣称「对话已删除」；运行中的会话由服务端原子拒绝（`conflict`），前端不会自动 cancel |
| 搜索 | `runtime.session.search` | 服务端元数据搜索（title/summary/thread_id/model），不是对话全文搜索；分页与服务端一致，输入竞态由 generation 计数丢弃过期结果 |

## 图片附件（输入区）

图片不会内联进任何帧：前端先 `runtime.attachments.begin` 声明大小与 MIME，再按
服务端返回的 chunk 预算分块 `runtime.attachments.append`（每块 ≤ 256 KiB，base64），
最后 `runtime.attachments.finish` 校验并落盘；此后只引用不透明的 `attachment_id`。

| 操作 | wire 方法 | 说明 |
|---|---|---|
| 粘贴/拖放 | — | 把文件粘贴到输入卡片，或直接拖放到卡片上（`+` 按钮已改作「添加项目」，不再点选图片）；只接受图片，每次最多 8 张、每张 ≤ 4 MB，被拒的文件名与原因会显示在输入区 |
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
| `MAX_STORED_ATTACHMENTS_PER_SESSION` | 128 | 单会话累计存储张数（前端不做累计配额提示，超出由服务端拒绝） |
| `MAX_ATTACHMENTS_PER_PROJECT` | 128 | 单项目累计存储张数 |
| `MAX_CHUNK_BYTES` | 256 KiB | 单块解码上限（`ATTACHMENT_CHUNK_BYTES`） |
| `DEFAULT_READ_BYTES` | 64 KiB | 历史缩略图每次读取窗口（`ATTACHMENT_READ_BYTES`） |
| `INCOMPLETE_TTL_SECONDS` | 3600 | 未完成上传在服务端被清理前的静默时长（无后台清理线程，按操作有界 sweep） |

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

图片附件仍可通过**粘贴/拖放**加入输入卡片；点选图片的入口已移除，8 张 / 4 MB 上限与预览行为不变。

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