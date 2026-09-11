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
| `runtime.artifacts.list / `read` | Request -> Response | 工作区文件树与代码差异对比浏览 |
