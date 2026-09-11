# Web 控制台正式宿主、配对认证与部署（第五阶段收口）

> 文档状态：Active
> 验收状态：**有条件可验收**（不是「安全验收通过」；未闭合项与明确不承诺项见 §4、§9）
> 目标：React 生产构建在正式宿主 `synapse-web-console` 下运行；浏览器认证走
> 一次性配对码 + 同源会话 cookie；移除前端硬编码 token/project/workspace
> fallback 与 Vite 开发中间件读取 `test_token.txt` 的运行依赖。

## 1 架构与进程模型

```
[浏览器 / React 单页 (web/dist)]
        │  same-origin HTTP/WS (loopback)
        ▼
[synapse-web-console  薄 aiohttp 宿主]
   - 静态产物（web/dist，--static-dir）
   - POST /api/pair（配对码换会话 cookie） / GET /api/session / POST /api/logout
   - GET /api/runtime-status（只读：daemon 端点 / state dir / 启动提示）
   - /runtime-ws 中继（浏览器 ⇄ daemon，JSON-RPC 帧原样转发；唯一例外见 §4.1）
        │  Authorization: Bearer <daemon token>（服务端持有）
        ▼
[synapse-runtime  daemon]
        ▼
[AgentRuntimeService / SessionRuntime / LLM]
```

- **不新建 agent loop、不旁路数据库**：宿主只做静态 + 配对/会话 + 中继；
  negotiate/open/submit/cancel/steer/watch/history/artifacts/config/approval/
  recovery 全部由 daemon 既有 `AgentRuntimeService` 与 wire 协议执行。
- **项目上下文**：由 `--workspace`（默认当前目录）经与 daemon 相同的用户层
  `ProjectCatalog`（catalog.sqlite）解析出 `project_id`/name/git 分支；
  代码里不硬编码任何项目 ID。未注册的 workspace 启动时报错并给出提示。
- **daemon 发现**：默认读取 daemon 发布的 `<state-dir>/daemon.json`
  （`DaemonLease.publish` 的 `host`/`port`）；也可显式 `--runtime-port` 覆盖。
  daemon token 从 `<state-dir>/token`（或 `--token-file`）读取，只读、绝不创建。

## 2 启动命令与参数（逐项核对 `--help`）

先构建前端产物（`web/dist` 是独立资产，不随 wheel 打包）：

```bash
cd web
npm ci
npm run build
cd ..
```

终端 1（前台 daemon）：

```bash
synapse-runtime --state-dir ~/.synapse/runtime --host 127.0.0.1 --port 0
# 或：python -m synapse.runtime.daemon --state-dir ~/.synapse/runtime --port 0
```

终端 2（正式宿主）：

```bash
synapse-web-console --workspace . --static-dir web/dist \
  --state-dir ~/.synapse/runtime --port 8080
# 或：python -m synapse.web_console.entry --workspace . --static-dir web/dist
```

然后打开 <http://127.0.0.1:8080/> 并输入宿主 stderr 打印的配对码。

### 2.1 `synapse-web-console` 参数

下表与 `synapse-web-console --help`（= `python -m synapse.web_console.entry --help`）
逐项一致，默认值取自 `src/synapse/web_console/config.py`：

| 参数 | 默认 | 校验与语义 |
|---|---|---|
| `--host HOST` | `127.0.0.1` | 绑定地址；只允许 `127.0.0.1`/`localhost`/`::1`，其它值启动失败 |
| `--port PORT` | `8080` | 绑定端口；`0` = 内核分配（实际端口见 stdout 元数据） |
| `--workspace PATH` | 当前目录 | 项目工作区；必须是已存在目录 |
| `--static-dir PATH` | `<workspace>/web/dist` | 已构建静态产物目录；必须存在且含 `index.html`，否则启动失败 |
| `--state-dir PATH` | `~/.synapse/runtime` | daemon 状态目录（`daemon.json` 发现 + token 读取） |
| `--token-file PATH` | `<state-dir>/token` | daemon token 文件；**只读**，缺失即启动失败且绝不创建 |
| `--catalog-path PATH` | 无（用用户层 catalog） | 覆盖项目 catalog 数据库路径 |
| `--runtime-host HOST` | `127.0.0.1` | daemon WS host；只允许 loopback，远程 daemon 不支持 |
| `--runtime-port PORT` | 无（读 `daemon.json`） | 覆盖 daemon 端口；必须 `1..65535`（拒绝 `0`） |
| `--max-message-bytes INT` | `1048576` | 单帧上限；`1024..8388608` |
| `--session-ttl-seconds INT` | `43200`（12h） | 会话 cookie 生命周期；正整数 |
| `--pair-ttl-seconds INT` | `300` | 配对码生命周期；正整数 |
| `--max-sockets INT` | `16` | 并发中继上限；`1..256` |
| `--max-body-bytes INT` | `4096` | `/api/*` 请求体上限；`64..1048576` |
| `--ws-heartbeat-seconds INT` | `30` | WS ping 间隔；`0` 关闭；`0..3600` |

### 2.2 `synapse-runtime`（daemon）参数

与 `synapse-runtime --help` 一致：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--host HOST` | `127.0.0.1` | 绑定地址；daemon 自身只校验「非空字符串」，**不强制 loopback**（见 §9） |
| `--port PORT` | `0` | `0` = 内核分配 |
| `--state-dir PATH` | `~/.synapse/runtime` | 发布 `daemon.json`/`daemon.lock`，默认 token 也在此 |
| `--token-file PATH` | `<state-dir>/token` | 自定义 token 文件；缺失时 daemon 原子创建随机 token |

daemon 前台运行、持有 state dir 锁，收到 SIGINT/SIGTERM 退出；不提供
install/start/stop/status 控制。

### 2.3 启动输出契约

- stdout：**恰好一行** JSON 元数据（不含配对码与 token）：

```json
{"schema_version":1,"host":"127.0.0.1","port":8080,"url":"http://127.0.0.1:8080/","websocket":"ws://127.0.0.1:8080/runtime-ws","project_id":"…","workspace":"…","pairing_required":true}
```

- stderr：固定格式的配对码行（脚本与测试按此前缀抓取）：

```
synapse-web-console: pairing code XXXXXXXX (expires in 300s; open http://127.0.0.1:8080/ and enter it)
```

配对码为 8 字符 Crockford base32（字母表 `0123456789ABCDEFGHJKMNPQRSTVWXYZ`，
不含 I/L/O/U，40 bit 熵），单次使用，只存在于宿主进程内存与 stderr；不落盘、
不进 stdout JSON、不写日志文件（`access_log=None`）。

## 3 配对与会话契约

| 方法 | 路径 | 语义 | 成功 | 失败 |
|---|---|---|---|---|
| `POST` | `/api/pair` | 用配对码换会话（体 `{"code":"XXXXXXXX"}`） | 200 `{"project":{…}}` + `Set-Cookie` | 400 体不合法 / 401 码无效 / 403 CSRF 链 / 405 方法错 / 413 体过大 / 415 Content-Type / 429 限速 |
| `GET` | `/api/session` | 查询当前会话与项目上下文 | 200 `{"project":{…},"expires_in":<int 秒>}` | 401 无/无效会话 |
| `GET` | `/api/runtime-status` | **只读** daemon 状态（daemon 不可用时给出可操作提示） | 200 `{"runtime":{"endpoint":{"host":…,"port":…}\|null,"state_dir":…,"hint":"start synapse-runtime --state-dir …"}}` + `Cache-Control: no-store` | 401 无/无效会话 / 403 Host 不在允许表 / **405**（`POST`） |
| `POST` | `/api/logout` | 作废全部会话（体 `{}`） | 204（无体） | 401 / 403 / 405 |
| `GET`/`POST` | `/api/bootstrap` | 已删除：恒 405 | — | 405 `{"error":"method not allowed"}`，且绝不 `Set-Cookie` |

`GET /api/runtime-status` 是**只读**端点：需要有效会话 cookie，且 `Host` 必须命中
loopback 允许表（主机名 ∈ {`127.0.0.1`,`localhost`,`::1`} 且端口 == 实际绑定端口）；
它不改变任何状态，因此不要求 `Origin`/`Sec-Fetch-Site`。响应只含 state dir 与已可发现的
loopback daemon 端点（`daemon.json` 缺失或端点不可解析时为 `null`）以及
`start synapse-runtime --state-dir …` 提示；**不含** daemon bearer、token 文件路径或
任何 token 字样。`POST /api/runtime-status` → 405。
该端点补上「daemon 不可用」时的可操作信息：`/runtime-ws` 的关闭原因仍是冻结的通用
文案 `runtime daemon unavailable`，不随宿主状态变化。

状态变更端点（`pair`/`logout`）必须走完整 CSRF 链，判定顺序为：Host 允许表
（loopback 且端口 == 实际绑定端口）→ `Sec-Fetch-Site` ∈ {`same-origin`,`none`}
→ `Origin` **必须存在**且精确匹配有效 origin
（`http://{127.0.0.1|localhost|[::1]}:<绑定端口>`）→
`Content-Type: application/json` → 头 `X-Synapse-Console: 1` →
体 ≤ `--max-body-bytes` → 业务校验（常量时间比对 + 限速）。

注意：`POST /api/logout` 也必须带 `Content-Type: application/json` 与
`X-Synapse-Console: 1`（体可为 `{}`），否则分别返回 415 / 403。

拒绝响应统一为 `{"error":"<短静态原因>"}` + `Cache-Control: no-store` +
`X-Content-Type-Options: nosniff`，**不回显** Origin、提交的码、Host 原文或内部
路径；任何路径都**不返回** `Access-Control-Allow-*`。

会话 cookie 契约：

| 属性 | 值 |
|---|---|
| 名称 / 值 | `synapse_web_session` / `secrets.token_urlsafe(32)` |
| `HttpOnly` / `SameSite` / `Path` | 必须 / `Strict` / `/` |
| `Domain` / `Secure` | **不设置**（host-only；本切片仅 http，设 `Secure` 会让浏览器直接丢弃 cookie） |
| `Max-Age` | `--session-ttl-seconds`（默认 43200） |
| 签发时机 | **仅** `POST /api/pair` 成功时（`GET /api/bootstrap` 不再签发） |
| 上限 / 过期 | 注册表 ≤ 8（先清过期再淘汰最旧）；TTL 到期后 HTTP → 401、WS 升级 → 403 |
| 持久化 | 不持久化（内存注册表）；宿主重启 → 全部会话失效 → 重新配对 |

不变式：只要当前不存在有效会话，宿主就持有一个未过期未消费的配对码且已打印到
stderr（启动、码 TTL 到期、logout、暴力失败达阈值 5 次/60s 时重印）。

## 4 安全边界（如实，不夸大）

已建立的边界：

| 机制 | 现状 |
|---|---|
| loopback-only 绑定 | host 配置层只允许 127.0.0.1 / localhost / ::1；拒绝 0.0.0.0，不自动开放 LAN/公网 |
| 配对码（真正的门） | 单次使用、TTL 300s、失败限速 5 次/60s、40 bit、`hmac.compare_digest`、仅内存 + stderr |
| Host 允许表 | 所有请求的 `Host` 主机名必须在 loopback 集合内且端口 == 绑定端口 ⇒ 防 DNS-rebinding 与端口漂移 |
| 同源检查 | 状态变更端点要求 `Origin` 存在且精确匹配（含端口）；WS 升级同样要求 |
| 会话 cookie | HttpOnly + SameSite=Strict + Path=/；值不可猜测；绝不放进 URL |
| WS 中继鉴权 | 升级前完成 Host → Origin → 会话 → 并发上限四步，失败返回普通 HTTP 403/403/403/503 |
| token 持有 | daemon bearer 只在宿主进程内存；静态/配对/错误页/中继帧/stdout 均不含它 |
| 静态路径 | 解码后含 `..`/反斜杠/NUL 直接 404；文件与目录 index 的符号链接逃逸 404 且不抛异常 |
| 资源上限 | 帧上限 `--max-message-bytes`、并发上限 `--max-sockets`、HTTP 体上限 `--max-body-bytes`、**中继出站缓冲上界**（64 帧 / 8 MiB，取先到者）、每帧发送超时、daemon 握手超时 |
| 缓存规则 | `index.html` 与 SPA 回退 `no-store`；`assets/**`（内容哈希）`immutable`；其它静态 `no-cache`；404/405/异常 `no-store` |

浏览器断开不会取消 daemon 中运行中的 turn（只有显式 `runtime.turn.cancel`
取消）；中继在任一端关闭后立即关闭另一端并清理任务。

**明确不承诺**（不要把 `Host`/`Origin` 检查表述为「严格同源即可信」）：

- `Host`、`Origin`、`Sec-Fetch-Site` 都是客户端可伪造的头，只算纵深防御；同机
  另一进程可伪造它们，真正的门是配对码。
- 不支持 TLS / 反向代理：外部端口 ≠ 绑定端口时 Origin 校验必然失败；将来若支持，
  必须同时加显式 `--public-origin`、cookie `Secure`、拒绝非 https 的配对请求。
- 会话不持久化：宿主重启后必须重新配对。
- 会话 TTL 到期**不主动断开**已建立的中继（仅新 HTTP/WS 请求被拒）。
- 历史投影（`transcript.sqlite`）与实时事件 broker **不承诺**跨存储原子一致
  （见 `docs/sessions.md` 的「不承诺原子」）。
- 配对码经 stderr 传递：若把宿主 stderr 重定向到持久日志或 CI 输出，码可能被记录。
- Windows 下 token 文件符号链接不被拒绝（无 `O_NOFOLLOW`）；Linux/macOS 会拒绝。
- 背压是**两层**：①显式有界出站缓冲（`RELAY_MAX_PENDING_FRAMES = 64`、
  `RELAY_MAX_PENDING_BYTES = 8 MiB`，**取先到者**；超限是确定性事件——中继立即结束、
  排队帧被丢弃并计数、两侧 socket 关闭）；②既有的传输层流控与每帧发送超时
  （`send_timeout_seconds`，默认 30s）。该上界是**进程内缓冲上界**，不含 aiohttp /
  websockets 传输层缓冲与内核 socket 缓冲，**不等于进程 RSS 上界**。
- 跨项目请求由宿主以 typed `not_found` 拒绝，这是对「中继原样转发」的**有意例外**，
  且有未闭合的残余风险（见 §4.1）。
- daemon 自身不强制 loopback 绑定（`DaemonConfig.host` 仅校验非空）；宿主侧已收紧
  为只接受 loopback daemon 目标，但 daemon 的系统性约束未在本切片收口。

### 4.1 跨项目请求的宿主侧拒绝（对 A6「中继原样转发」的有意例外）

这是本切片**唯一**一处对「中继原样转发」的例外，属安全相关行为变更，如实披露如下。

| 项 | 现状 |
|---|---|
| 行为 | 浏览器发出的、指向**非宿主自身项目**的 JSON-RPC 请求，由宿主直接以 typed 错误拒绝：`code=-32000`、`data.service_code="not_found"`、`meta.wire_version="1"`（复用 daemon 自己的 `protocol.encode_error`，不自行拼信封）。**拒绝帧不进入 daemon**，因此 daemon 侧不会为该项目建立 manager/session |
| 为什么需要 | daemon 用一个 bearer 认证宿主，并按**精确 project_id** 解析 catalog 中**任意**已注册项目（`CatalogProjectProvider` + 无 project 维度的 `DaemonAuthorizer`，协商期也没有项目维度）。下游没有任何机制把这条中继连接限制到宿主自己的项目；没有该守卫时，为项目 A 配对的浏览器可以打开项目 B 的会话 |
| 例外范围（压到最小） | ① 只检查**浏览器 → daemon** 一个方向，daemon → 浏览器完全不动；② 只读取请求 `id` 与白名单位置 `session.project_id`、`project_id`、`ref.session.project_id`（即 `protocol.decode_params` 中全部会参与路由的 project_id 位置）；③ 不做递归扫描、不改写任何字段，被接受的帧仍**逐字节**转发 |
| 不是存在性 oracle | 对任何非宿主项目（无论是否已注册）返回同一个 `not_found`；拒绝响应不含其它项目的 project_id / workspace 路径 / token |

**已知残余风险（不承诺已闭合）**：

- **对不可读/非文本帧 fail-open**：不是 JSON、顶层不是对象、`id` 既非 `str` 也非
  `int`（`bool`/`float` 也落在这一类）、`params` 不是对象的帧一律原样转发；守卫只检查
  `WSMsgType.TEXT`，二进制帧不检查。
- **白名单与协议无源码级绑定**：三个位置是手工维护的常量，没有任何测试把
  `SCOPE_PROJECT_POSITIONS` 与 `protocol.decode_params` 的方法表绑定；若协议新增一个把
  `project_id` 放在新位置的方法，守卫**不会**自动覆盖。
- 因此「跨项目请求被拒」目前**依赖 daemon 侧的严格校验继续存在**：守卫 fail-open 的
  每一种形状都必须在到达 router/manager 之前被 daemon 以 `-32600`/`-32602`/`-32700`
  拒绝。若 daemon 将来放宽（接受 `bool`/`float` 型 `id`、允许 `params` 非对象、允许重复
  键或额外字段），其中若干形状会立刻变成绕过路径。
- 守卫用默认 `json.loads`（保留最后一个同名键），而 daemon 用
  `_reject_duplicate_keys` 拒绝重复键 ⇒ 重复键帧必定被 daemon 拒绝、不构成绕过，但两者
  解析器语义不完全一致。
- `host.scope_rejections` 只是进程内计数（无持久化、无日志输出），仅供测试/调试观测。

**不承诺**：「多项目越权问题已端到端穷尽验证」。当前结论仅是「已覆盖的对抗形状中未
发现可利用路径」，且依赖上述 daemon 侧严格校验继续成立。

## 5 开发模式（Vite）与生产模式的区别

| 项 | 生产 | 开发（`npm run dev`） |
|---|---|---|
| 页面来源 | 宿主提供 `--static-dir` 静态产物 | Vite dev server（`http://127.0.0.1:5173`） |
| 网络路径 | 浏览器直连同源宿主 | `/api` 与 `/runtime-ws` 经 Vite 代理到宿主（`SYNAPSE_WEB_CONSOLE_URL`，默认 `http://127.0.0.1:8080`） |
| `Origin` | 浏览器发送真实 origin，宿主精确匹配 | 代理把转发请求的 `Origin` 重写为宿主 origin（**dev-only 的代理侧重写；宿主不因 dev 放宽任何校验**） |
| cookie 宿主 | 宿主 origin | dev origin（`localhost:5173`），经代理转发，`Path=/`、host-only、HttpOnly 仍成立 |
| 配对 | 必需 | 同样必需（从宿主 stderr 读码） |

开发流程（宿主必须先运行）：

```bash
synapse-web-console --workspace . --static-dir web/dist --port 8080 &
cd web
SYNAPSE_WEB_CONSOLE_URL=http://127.0.0.1:8080 npm run dev   # http://127.0.0.1:5173
```

Vite 配置（`web/vite.config.ts`）的边界：

- 只代理 `/api` 与 `/runtime-ws` 两条路径；目标必须是 loopback http(s) 裸 origin
  （不接受内嵌凭据或带路径/查询），否则 `npm run dev` / `npm run build` 在
  **配置加载阶段**直接失败；
- 不直连 daemon 端口、不注入 `Authorization` 或任何凭据、不读任何 token 文件、
  不提供绕过配对的中间件、不把配对码注入前端；
- `server.host` 保持默认 loopback 绑定（不设为 `0.0.0.0`/LAN 地址）。

因此开发代理**不构成认证旁路**：它只把浏览器的同源请求转发到同一个正式宿主，
认证判定仍在宿主侧；宿主未启动时前端会明确报错（无静默 fallback）。

## 6 打包与静态资产部署（wheel 不含 `web/dist`）

事实（`uv build` 实测，见 §8）：wheel 只包含 `synapse/**` 的 Python 模块
（含 `synapse/web_console/*.py`）与 `dist-info`，**不包含** `web/dist` 前端产物。

按优先级选择部署方式：

1. **源码检出**：`cd web && npm ci && npm run build`，使用默认
   `<workspace>/web/dist`（不传 `--static-dir`）。
2. **打包部署（推荐）**：把 `web/dist` 作为独立资产分发到任意路径，显式指定：

```bash
npm --prefix web ci && npm --prefix web run build
# 分发 web/dist 到部署机（例如 /srv/synapse/console）
synapse-web-console --workspace /srv/project --static-dir /srv/synapse/console \
  --state-dir ~/.synapse/runtime --port 8080
```

3. **缺资产**：启动直接失败并给出可操作错误（不会出现「启动成功但页面 404」）：

```
synapse-web-console: unable to start: static build directory not found: <path>; build web/ first or pass --static-dir
synapse-web-console: unable to start: static build directory has no index.html: <path>; build web/ first or pass --static-dir
```

wheel 声明的 console script 与 `pyproject.toml`、各自 `--help` 一致：
`synapse`、`synapse-web`、`synapse-web-console`、`synapse-acp`、`synapse-runtime`。

## 7 保留的兼容入口

- `synapse-web`（textual-serve 把 TUI 渲染进浏览器，`--host` 默认 `localhost`、
  `--port` 默认 `8000`，另有 `--workspace`/`--public-url`）**保持不变、未被替换**，
  仍可用。
- 两个入口语义不同、并存：`synapse-web` = 浏览器里的 TUI；`synapse-web-console`
  = React 控制台正式宿主（配对 + 静态 + 中继）。
- `synapse`、`synapse-acp`、`synapse-runtime` 三个入口不受影响；Python/TS runtime
  客户端与既有 wire 协议未改动。

## 8 测试与验证（全部离线 loopback）

**本轮文档同步实际执行的命令**（工作区根目录，`uv run --no-sync`；本轮**未运行任何
pytest**，含单文件与 `--collect-only`）：

| 命令 | 结果（2026-09-10 本轮实测） | 退出码 |
|---|---|---|
| `uv run --no-sync ruff check src tests` | `All checks passed!` | 0 |
| `uv run --no-sync mkdocs build` | 成功（均以 `Documentation built in … seconds` 收尾）；无 WARNING/ERROR，仅有既有 anchor INFO，见 §9 | 0 |
| `uv run --no-sync python -m synapse.web_console.entry --help` | 与 §2.1 参数表逐项一致 | 0 |
| `uv run --no-sync python -m synapse.runtime.daemon --help` | 与 §2.2 参数表逐项一致 | 0 |
| `uv run --no-sync python -m synapse.web --help` | 与 §7 描述一致（`--host` 默认 `localhost`、`--port` 默认 `8000`，另有 `--workspace`/`--public-url`） | 0 |

**用例数与结果引自既有交接（本轮未重跑，不作为本轮独立证据）**：

| 命令 | 结果 | 出处 |
|---|---|---|
| `pytest tests/test_web_console_host.py tests/test_web_console_security.py tests/test_web_console_vertical.py -q` | `102 passed in 153.09s`（宿主 25 + 安全负向 45 + 真实 daemon 纵向 32） | `.tmp/phase5-d3-scope-reverify-handoff.md` §4.1 |
| `pytest tests/test_web_console_vertical.py -q` | `32 passed` 连续 2 次（63.66s / 61.36s），0 xfailed | 同上 |
| `pytest tests/test_web_console_host.py tests/test_web_console_security.py -q` | `70 passed in 97.54s` | `.tmp/phase5-f2-final-acceptance-handoff.md` §4.1 |
| 全量 `pytest -q` | `2875 passed, 2 skipped`（退出码 0） | `.tmp/phase5-d3-scope-reverify-handoff.md` §4.1；**本轮未复核** |
| `cd web && npm test` / `npm run lint` | 67 passed / 0 fail；`Found 0 warnings and 0 errors.` | `.tmp/phase5-f2-final-acceptance-handoff.md` §4.1 |
| `uv build` | `Successfully built dist\synapse_cli_agent-0.1.44.tar.gz` / `dist\synapse_cli_agent-0.1.44-py3-none-any.whl`；wheel 含 `synapse/web_console/*.py` 与 5 个 console script，不含 `web/dist` | 同上（§4.1 第 6 条） |

> 口径：本切片是「**有条件可验收**」，不是「安全验收通过」「全量回归通过」；
> 未闭合项与明确不承诺项见 §4、§9。

覆盖范围：

- `tests/test_web_console_host.py`（25 项）：生产 `web/dist` 无 Vite 静态页 +
  中继 RPC；配对/会话/logout/runtime-status；严格同源（含端口）与 Host 校验；
  缓存规则；CLI 参数；daemon.json 发现；catalog 项目解析；`read_existing_token`
  只读不创建；有界出站缓冲（帧/字节双上界）与慢消费者断开；跨项目作用域守卫；
  停止处理器 fallback。
- `tests/test_web_console_security.py`（45 项）：A1–A6/A8 的负向用例。**不统一声明**
  「每条都断言拒绝发生在触及敏感业务之前」：只有其中一部分（例如 `test_b_a1_08`、
  `test_b_a1_10`、`test_b_a2_05`、`test_b_a3_05`、`test_b_a4_05`、`test_b_a5_04`、
  `test_b_a8_05`、`test_b3_1_*`）显式断言无 `Set-Cookie`、daemon 0 连接 0 帧、
  响应体无 token/回显。
- `tests/test_web_console_vertical.py`（32 项）：真实 `RuntimeDaemon` +
  `RuntimeWebSocketServer` + `AgentRuntimeService` + 真实宿主进程的纵向闭环
  （negotiate/open/submit/watch/cancel、断线语义、会话 TTL、资源边界、仅 loopback、
  出站缓冲高水位实测、跨项目拒绝、`/api/runtime-status` 门禁、Windows 优雅退出）。
- TypeScript（`cd web && npm test`）：67 项通过（配对请求形状、会话恢复、
  未认证禁 RPC、dev 代理边界、源码守卫）；`npm run lint` 0 warning 0 error。

## 9 已知边界与未决项

- 仅 loopback 单用户；会话注册表在宿主进程内存（宿主重启失效，需重新配对）。
- 本切片不支持 TLS / 反向代理（外部端口 ≠ 绑定端口）；将来支持时必须同时加
  显式 `--public-origin`、cookie `Secure`、拒绝非 https 的配对请求。
- daemon 侧 `DaemonConfig.host` 仍只校验非空、未强制 loopback（宿主侧已收紧为
  只接受 loopback daemon 目标）；建议后续单独收口。
- 背压上界是**进程内缓冲**上界（64 帧 / 8 MiB，取先到者），不含 aiohttp/内核 socket
  缓冲，**不是进程 RSS 上界**；真实栈实测高水位 8,135,967 字节（7.759 MiB，上界
  8,388,608），且只覆盖 daemon → 浏览器方向的慢消费者（反向复用同一实现，无单独用例）。
- 跨项目请求的宿主侧拒绝见 §4.1：对不可读/非文本帧 fail-open、白名单与
  `protocol.decode_params` 无源码级绑定，安全性依赖 daemon 的严格校验。
- Windows 下 token 文件符号链接不被拒绝（无 `O_NOFOLLOW`）；Linux/macOS 会拒绝。
- 活动 WS 不因会话 TTL 到期被断开。
- 前端 `PairingGate` 只有类型检查与源码级断言，无 DOM 渲染测试（`web/` 未引入
  jsdom/测试渲染依赖）；配对失败只能靠重新输入码或刷新页面恢复。
- 文档构建保留既有 anchor INFO：`tutorial.md` 的 16 个 `#N-…` 链接找不到对应
  锚点，`codex-session-discovery-phase-1.md` 含绝对路径链接 `/src/synapse/…`；
  这些在本切片之前即存在，未修改。
- 历史投影 ↔ 实时 broker 跨存储原子一致依旧不承诺（见 `docs/sessions.md`）；
  本切片不扩大任何服务端保证。
- 已做但范围有限：真实浏览器（Chrome 152）CDP 实测仅覆盖跨站 WS cookie 与 dev 代理
  `Origin` 重写、以及经代理/直连访问真实宿主的同码同体校验；结论不可移植到
  Firefox/Safari，也无 DOM 级前端渲染测试。
- 未做（超出本切片范围）：反向代理/TLS 部署文档、多项目切换 UI、会话注册表持久化、
  前端消费 `/api/runtime-status`（宿主侧已完成，UI 仍只显示冻结的通用关闭文案）、
  发布与 scratch/图片清理。