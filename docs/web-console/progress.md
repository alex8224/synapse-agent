# Synapse Web Console 实施进度跟踪

| 阶段 | 任务目标 | 状态 | 说明 |
|---|---|---|---|
| **M1: 方案与规范** | 编写方案文档与进度跟踪文档 | **Completed** | docs/web-console/index.md 与 progress.md 已就绪 |
| **M2: 工程骨架初始化** | 创建 web/ 目录，搭建 Vite + React + TS + Tailwind 工程 | **Completed** | Vite 8 + React 19 + TypeScript + Tailwind CSS 3 编译成功通过 |
| **M3: 协议客户端实现** | 封装 TypeScript 版本 RuntimeWebSocketClient | **Completed** | `web/src/client/` 已实现标准 JSON-RPC 2.0、握手协商、`watchEvents` 与通知处理 |
| **M4: 响应式状态管理** | Zustand 数据流设计与持久化 | **Completed** | `web/src/stores/useConsoleStore.ts` 集中管理会话、时间线、Steer 队列与度量指标 |
| **M5: 界面组件还原** | 实现 TopBar, Collapsible SideBar, Transcript, CommandBar, BottomBar | **Completed** | 100% 还原 Stitch 设计稿 5dbf505b43114db08c1346fc5fef2f4f，无头像、可折叠侧栏、单按钮输入 |
| **M6: 端到端联调测试** | 对接本地后台守护进程，验证流式输出与 Steer 插话 | **Completed** | 静态构建校验通过（`dist/` 产物正常构建）；当时为浏览器直连 daemon，现已被切片 2 的宿主中继 + 配对认证取代 |
| **M7: 正式宿主（第五阶段切片 1）** | `synapse-web-console` loopback 薄宿主（静态 + bootstrap + WS 中继）；移除前端硬编码 token/project/workspace 与 test_token 运行依赖 | **Superseded** | `GET /api/bootstrap` 自动签发会话已在切片 2 被一次性配对码取代；见 `formal-host.md` |
| **M8: 配对认证与部署收口（第五阶段切片 2）** | 一次性配对码 + 同源会话 cookie（`POST /api/pair` / `GET /api/session` / `POST /api/logout`）；前端配对界面与未认证禁 RPC；真实 daemon 纵向测试；文档与部署收口 | **Completed（切片整体口径：有条件可验收）** | Python 102 项（宿主 25 + 安全负向 45 + 真实 daemon 纵向 32）与前端 67 项通过；`uv build`、`mkdocs build`、`ruff` 通过。用例数引自第五阶段 D3/F2 交接（`.tmp/phase5-d3-scope-reverify-handoff.md`、`.tmp/phase5-f2-final-acceptance-handoff.md`），文档同步轮次未重跑；未闭合项与明确不承诺项见 `formal-host.md` §4、§9 |
| **M9: 遗留项收尾** | 补完目标（goal）前端展示；新增**会话级**推理级别写端口（协议 + 服务 + ACL 能力位 + 前端 + 文档）；artifacts 工作区文件树与差异浏览；LaTeX / Mermaid 无依赖显式呈现；底栏中区居中定稿 | **Completed（工程口径）** | 后端新增 `runtime.session.thinking.set`（独立写能力位 `session.thinking`，`can_set_thinking` 转为 `true`）；前端新增 goalView / thinkingLevelWrite / artifactsView / mathBlocks / bottomBarLayout 用例；实测数字与未闭合项见 `.tmp/web-console-remaining-handoff.md` |
| **M10: 增强与收口** | 新增**项目级**默认推理等级写端口（协议 + 服务 + ACL 能力位 `project.thinking` + 项目设置层持久化 + 设置面板入口 + 文档）；artifacts 面板补**真实行级差异**、路径过滤与分块续读；全量回归与依赖评估 | **Completed（工程口径）** | 后端新增 `runtime.project.thinking.set`（写入 `<workspace>/.synapse/settings.json`，只影响此后新建会话，重启后仍生效）与 `runtime.config.get` 的 `project_thinking_level` / `can_set_project_thinking`；前端新增 projectThinking / artifactsDiff 用例与过滤用例；实测数字、差异来源限制与依赖评估见 `.tmp/web-console-remaining-handoff-2.md` |
| **M11: 公式与图形渲染** | 把 M9 只做「显式呈现」的 LaTeX / Mermaid 改为真实渲染：KaTeX 排版公式、mermaid 绘制图形；两者都保持「不静默降级」的回退语义与静态守护 | **Completed** | 新增依赖 `mermaid` / `katex` / `dompurify`；新增 `src/components/{GeneratedHtml,MathTex,MermaidBlock}.tsx`、`src/markdown/{tex,mermaid}.ts`；新增 `tests/{mathRender,mermaidPolicy,markdownRenderGuard}.test.ts`（前端 418 项全绿，`tsc -b` 与 `oxlint` 通过）。浏览器验收（真实宿主 + 真实 daemon + 真实会话历史，CDP）：`fad65eded301` 的 TCP 三次握手 mermaid 渲染为 SVG；`4c2725d6ee8f` 渲染 47 个公式块 + 14 处行内公式、0 个 KaTeX 错误；回退分支（无效图形 / `%%{...}%%` 指令 / YAML frontmatter / 空定义 / 流式 / 解析失败公式）逐个在浏览器核对。评审阶段实测到 frontmatter 是绕过指令拒绝的第二条 CSS 通道（`themeCSS` 里的 `url()` 会进入注入的 `<style>`），已一并拒绝并在 `<style>` 上中和 `@import` / `url()` |
| **M12: 一条命令启动控制台** | 控制台宿主按需拉起 runtime daemon，免去「先开一个终端跑 synapse-runtime」：`synapse-web-console --workspace . --static-dir web/dist --port 8080` 即可用 | **Completed** | 新增 `src/synapse/runtime/daemon/launcher.py`（`ensure_daemon`：pid 存活判定 → 复用或拉起 → 退出时停掉自己起的那一个）与 `--start-runtime/--no-start-runtime`（默认开启）；新增 `tests/test_runtime_daemon_launcher.py` 20 项与 `test_web_console_host.py` 3 项生命周期用例（`test_web_console_host.py` 42 + launcher 20 + 新增 3 全绿；`ruff` 通过）。真实端到端：单条命令拉起 daemon（stderr 两行、stdout 一行 JSON）→ 配对 → WebSocket 中继 `runtime.protocol.negotiate`/`runtime.project.list` 全部通过；已有 daemon 时重启只复用不起第二个（stderr 仅配对行、`daemon.json` pid 不变）。设计上放弃端口探测改用 pid 存活：daemon 只在 socket 绑定后才发布元数据，探测会让它每次多打一条握手失败日志。 |
| **M13: 打开方式（外部程序）** | Git Explorer 与文件查看器的标题栏新增 split button：用宿主本机程序打开选中文件，下拉可选应用（图标 + 名称，按扩展名分「推荐 / 其他应用」），可记住某扩展名的默认应用 | **Completed** | 后端新增 `runtime.apps.list`（`apps.list`，catalog scope、只读有界）与 `runtime.workspace.open_external`（`workspace.open_external`，会话级、`mode: open\|reveal`）：`src/synapse/runtime/service/external_apps.py` 是**固定候选表 + 运行时探测**（装了才出现；表里没有的应用不出现，动态枚举留待后续），路径必须落在会话自己的工作区内（symlink 逃逸拒绝），`app_id` 必须是宿主枚举出的 id，请求里没有命令行，启动不修改工作区；拒绝是具名的（`external_app_path_invalid` / `external_app_outside_workspace` / `external_app_file_missing` / `external_app_unknown` / `external_app_launch_failed` / `external_app_unavailable`）。前端新增 `src/components/OpenWithMenu.tsx` + `src/runtime-client/externalApps.ts`，菜单 portal 到 `document.body` 并在 capture 阶段接管 `Escape`（先关菜单、再关窗口），「始终用 X 打开 .ext」记在 `appearance.openWith`。验证：`tests/test_runtime_service_external_apps.py` 32 项、`web/tests/{externalApps.test.ts,openWithMenuGuard.test.ts}`、真浏览器 `web/tests/openWithMenu.verify.ts` 32 项全绿；契约重新生成且 `--check` 干净（wire 方法 50、capability 35）。已知限制见 `formal-host.md` §9 |
| **M14: 窗口截图（输入区动作接入）** | 输入区动作菜单的「窗口截图」与「截图设置」接入运行时：截图用保存参数创建**异步任务**，前端显示进度并可取消、防重复；无目标时工具打开自己的选择界面并返回 `target_required`；结果按块读回、经**既有附件 store** finalize 为普通附件（不自动发送） | **Completed（工程口径）** | 后端新增 `src/synapse/runtime/service/screenshot.py`（纯 DTO/边界/校验）、`screenshot_tool.py`（`windows-capture` CLI adapter：默认发现本项目 Release exe、固定 argv、超时、输出有界、`--version` 能力探测**不启动 GUI**）、`screenshot_service.py`（daemon 级常驻任务调度：幂等防重复、轮询、取消、逐帧经 `begin/append/finish` 落为附件）；新增 4 个 wire 方法 `runtime.screenshot.{status,settings.open,capture,cancel}` 与 2 个能力位 `screenshot.read` / `screenshot.control`。前端新增 `src/runtime-client/screenshot.ts`（严格解码 + 纯策略）、`src/stores/screenshotTask.ts`（入口内任务生命周期，绑定发起会话与草稿世代）、`src/components/ScreenshotTaskBanner.tsx`（进度/取消/待确认结果，绘在输入卡片上方，卡片本身无截图分支）；动作模块补 `run` 并只走类型化 context。验证：`tests/test_runtime_service_screenshot.py` 49 项、`web/tests/screenshot.test.ts` 33 项、`web/tests/composerActions.test.ts` 更新后全绿、真浏览器 `web/tests/composerActions.verify.ts` 39 项全绿；契约重新生成且 `--check` 干净。非 Windows / 未构建时明确不可用。兼容性补充：对仍会提前发布 `completed`（附件尚未/部分回填）的旧 daemon，控制台在有界短窗口内自动重读并保留「正在回填截图附件…」反馈，帧到齐即自动加入，超时才给「刷新状态」提示；缩略图与悬停放大预览共用按 id 取字节的 hook（`src/components/useAttachmentResource.ts`，primitive 依赖 + 异步世代隔离），重渲染不重复读取 |

> **验收口径**：本切片是「**有条件可验收**」，不构成「安全验收通过」「背压已完整实现」
> 「多项目越权已完整验证」「全量回归通过」等结论；未闭合清单与不承诺项见
> `formal-host.md` §4、§9。


## 全链路联调与验证记录 (M6 成果)
- **S7 协议流事件处理**：完整覆盖 `activity_started`, `reasoning_delta`, `answer_delta`, `tool_started`, `usage_updated`, `approval_required` 及终止事件。
- **Steer 实时插话**：输入框在运行态下自动识别并排队注入 `runtime.turn.steer`。
- **HITL 人机协同审批**：新增审批弹窗与决策调用 (`runtime.turn.approval.resume`)，支持 `allow_once` 与 `reject_once`。
- **CDP 自动化验证**：侧边栏折叠/展开、消息输入提交、页面无控制台报错，截图保存在 `web_console_m6_verified.png`。


## Runtime 全链路打通阶段进展 (2026-09-07，历史记录)

> 本节记录切片 1 之前的做法。其中「Vite 代理注入 Authorization」与
> 「`/api/project-meta` 直读 catalog.sqlite」两条路径**已删除**，不再是受支持的
> 部署方式；生产路径见 `formal-host.md`。

- **（已删除）Vite 反向代理与鉴权桥接**：早期在 `web/vite.config.ts` 用 `/runtime-ws`
  代理 + `proxyReqWs` 钩子把客户端 token 转成 `Authorization: Bearer` 头。该做法让
  开发代理持有凭据，已被移除；现在凭据只存在于宿主进程，Vite 仅重写 `Origin`。
- **（已删除）项目路由与元数据打通**：早期经 `/api/project-meta` 读 catalog.sqlite
  取项目 ID。现在项目上下文由宿主经 `--workspace` 用 `ProjectCatalog` 解析，前端
  不再有该端点。
- **Transcript 纯数据驱动重构**：完全剥离静态 Mock 演示数据，实现历史消息
  （`runtime.events.read`）回放与当前会话流式事件（`activity_started`、
  `reasoning_delta`、`answer_delta`、`tool_started`）实时上屏。
- **交互与快捷键**：App 级挂载 Ctrl+B（折叠侧边栏）与 Ctrl+C（中断当前运行中的
  Turn），打通 `runtime.turn.cancel`。
- **端到端执行验证**：全链路测试（协商、打开会话、订阅事件、提交 Turn、接收真实
  模型活动与流式事件）成功。
