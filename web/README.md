# Synapse Web 控制台（React + TypeScript + Vite）

面向 runtime daemon 的浏览器控制台前端。生产运行方式与完整命令见仓库根
`README.md`、`docs/web-console/index.md` 与 `docs/web-console/formal-host.md`。

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