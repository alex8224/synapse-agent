# ADR-S-016：S8 Runtime daemon ownership

- 状态：Accepted（S8 第二轮硬化）
- 范围：S8 daemon 进程与生命周期管理

## 决策

S8 提供 foreground-only 的 `synapse-runtime`（也支持
`python -m synapse.runtime.daemon`）。composition root 的唯一执行链为：

```text
RuntimeWebSocketServer -> AccessControlledAgentRuntimeService
-> LocalAgentRuntimeService -> RuntimeManagerRouter -> RuntimeManager
-> SessionRuntime -> AgentTurnRuntime
```

daemon 拥有 server、router、global catalog 和单实例 lease。实例是 one-shot 状态机，
状态为 `new -> starting -> running -> stopping -> stopped`；启动是 single-flight，
已停止实例（包括启动失败回滚、启动前 shutdown）永不重启。shutdown 与启动并发时，
shutdown 等待启动完成并加入启动任务创建的同一个收敛任务。关闭先移除 signal handlers，再严格按
`server -> router -> catalog -> lease` 逆序执行；每个资源至多关闭一次，单个失败
不能跳过后续资源，并报告固定的首个异常对象。并发或重复 shutdown 共享同一个后台
收敛任务，调用者取消不会取消收敛。

全局配置通过 `load_global_settings()` 和 `resolved_catalog_path()` 获取。manager
按 catalog 返回的精确 project id 构造；每个 workspace 使用独立的
`load_project_settings` 和 coding-agent factory。daemon 不使用 TUI template agent，
也不直接调用 `ainvoke` 或 `stream_agent`。

## 安全与发现

默认 state directory 为 `user_config_dir()/runtime`，token 文件是 state directory
下的 `token`，或显式 `--token-file`。token 只通过文件取得或生成，不接受 argv/env；
文件使用 exclusive create、单行 UTF-8、POSIX 0600，宽权限、symlink、空值、多行和
超限内容拒绝。认证只接受 exact `Authorization: Bearer <token>`，失败映射为 WebSocket
1008，成功 principal 固定为 `runtime-daemon`。daemon 专用 authorizer 对任意精确
project/thread 授予现有全部 runtime capabilities；其他 subject 默认拒绝。

持有型 `daemon.lock` 是单实例真源：POSIX 使用 `flock`，Windows 使用 1-byte
`msvcrt.locking`。`daemon.json` 仅是发现信息，不用于判断存活、不用于抢锁；包含
schema version 1、pid、host、实际端口、UTC started_at 和 opaque `instance_id`，不含
token。metadata 使用临时文件加 `os.replace` 发布。释放时仅当 instance_id 仍匹配才
删除 metadata，避免旧 owner 删除新 owner 的发现文件。

ready metadata 只在 server 成功 bind 后发布，并由 stdout 输出一行 compact JSON；stdout
写入或 flush 失败同样触发完整启动回滚，其余错误只向 stderr 输出固定脱敏文本。
SIGINT/SIGTERM 共享 stop event；Windows 若支持则额外安装 `SIGBREAK`。优先使用
asyncio loop handler；该模式无法取得旧 callback，因此 partial install 或退出时只移除
本 daemon 安装的 handlers。loop 模式不可用时使用 `signal.signal` fallback；该模式在
partial install 或退出时恢复每个 signal 的旧 handler。

S8 生命周期、signal 和锁的安全验证全部使用进程内注入的 fake server、resource、stop
event、handler 和 barrier；本轮不执行真实 subprocess daemon 或真实 signal 发送测试，
也不以此声称完成真实子进程验证。

## 排除项

S8 不实现 S9 的重连或版本协商，不迁移 CLI/TUI/ACP 消费者，也不提供后台 fork/spawn
或 install/start/stop/status 控制命令。上述工作分别保留给 S9/S10。
