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
