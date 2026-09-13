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
  `RuntimeDiagnosticsBanner`（仅降级时出现）、`Transcript`、固定在底部的 `CommandInput`，
  以及 `BottomBar`（运行态与用量遥测、MCP、goal）。

聊天列与输入卡片共用同一套阅读几何（`src/index.css`）：外层 `.console-gutter` 负责留白，
桌面端（≥ `lg`，1024px）每侧为工作区宽度的 10%，因此内层 `.console-column` 正好是工作区的
**约 80%**，且**没有 `rem` 硬上限**，所以更宽的工作区会真的更宽、不会被锁死；窄屏每侧退回固定
的 `2rem`，列宽近全宽。transcript 的滚动容器保留对称滚动条槽位
（`[scrollbar-gutter:stable_both-edges]`），因此出现滚动条时聊天列两侧对称内缩一个滚动条
宽度、中心线仍与输入卡片重合（不会向一侧偏移）。侧栏的会话树则相反：
滚动可用但不显示滚动条（`src/index.css` 的 `.sidebar-scroll`，`scrollbar-width` +
`-ms-overflow-style` + `::-webkit-scrollbar` 三族规则，按类名限定），240px 轨道边缘保持干净，
且不影响 transcript 的可见滚动条。聊天区里的运行日志（thought / tools / info）是紧凑的次要
层级，展开后才成为面板，助手回复保持正文排版。Markdown 表格按正文可读字号渲染（`text-sm`，
14px；表头同字号、只用字重区分），单元格留出适度 padding，宽表在自身容器内横向滚动、不撑破
阅读列；代码块与侧栏尺寸不变。

这些布局不变量由 `tests/shellLayout.test.ts`、`tests/topBarLayout.test.ts`、
`tests/transcriptLayoutGuard.test.ts` 与 `tests/markdownTableGuard.test.ts` 静态守护。
布局与排版层级之外，本轮唯一的功能改动是
在只读 `runtime.git.status` 上**增量**加了 `insertions` / `deletions` 两个字段（`git diff
--numstat HEAD` 的真实 tracked 增删行数，见下），旧字段与旧接口保持兼容；不宣称与参考截图
像素级一致。

静态守护之外，`tests/shellLayout.verify.ts` 会用真实宿主 + 无头 Chrome 量一遍渲染结果
（侧栏是否全高、顶栏/底栏是否只属于右列、输入卡片是否落在聊天列的同一中心线上、窄窗是否
横向溢出），并在 1920 / 1440 / 900 / 640 四个视口核对阅读列宽度：桌面端约为工作区宽度的
80% 且**大于旧的 60rem 上限**（证明没有被 rem 锁死），窄屏回到近全宽。它需要本机有
Chrome/Edge，且不属于 `npm test`：

```bash
node tests/shellLayout.verify.ts
```

## 开发

开发只依赖 Vite 做静态热更新与受控代理；它不读取任何 token 文件，也不实现
业务中间件。先启动正式宿主（提供配对/会话 API 与 `/runtime-ws` 中继），
再启动 dev server：

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
| 选择/拖放 | — | 回形针按钮打开文件选择器，或把文件拖到输入卡片；只接受图片，每次最多 8 张、每张 ≤ 4 MB，被拒的文件名与原因会显示在输入区 |
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