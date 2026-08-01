# 权限与安全

Synapse 默认以 **自动放行** 模式运行（`auto_approve=True`），适合受信任的本地开发环境。同时提供多层安全机制。

## 安全层级

```
CLI flags > Env 变量 > .env > 代码默认值
```

## 审批模式

### 自动放行（默认）

```bash
# 默认行为：所有操作自动执行，无需确认
synapse tui -w .
```

### 人工审批 (HITL)

```bash
# 每次工具调用前需要人工确认
synapse tui -w . --require-approval
```

或通过环境变量：

```bash
export AGENT_REQUIRE_APPROVAL=true
export AGENT_AUTO_APPROVE=false
```

## 只读模式

禁止 Agent 修改文件或执行命令，只能读取和分析。

```bash
synapse tui -w . --readonly
# 或
export AGENT_READONLY=true
```

只读模式通过 harness 工具排除实现，会移除 `write_file`、`edit_file`、`patch`、
`execute` 等写入或执行类工具。

## 文件系统权限

可以限制 Agent 只能访问特定路径或禁止访问敏感目录：

```bash
# 禁止访问某些路径（JSON 数组）
export AGENT_DENY_FS_PATHS='["/etc", "/home/user/.ssh"]'

# 启用文件系统权限检查
export AGENT_ENABLE_FS_PERMISSIONS=true
```

!!! warning "注意"
    `FilesystemPermission` 在 `LocalShellBackend` 下默认禁用，因为权限中间件不支持带命令执行的 backend。如需限制访问，优先使用 `AGENT_READONLY` 或 `AGENT_EXCLUDED_TOOLS`。

## 工具排除

可以按名称排除特定工具：

```bash
# 排除 execute 和 write_file 工具
export AGENT_EXCLUDED_TOOLS='["execute", "write_file"]'
```

## 命令黑名单

默认启用命令黑名单（`ENABLE_COMMAND_BLACKLIST=true`），会阻止危险的 shell 命令：

- `rm -rf /`
- `chmod 777`
- 其他破坏性命令

设为 `false` 可关闭：

```bash
export ENABLE_COMMAND_BLACKLIST=false
```

!!! danger "注意"
    关闭命令黑名单 + 自动放行模式下，Agent 可以执行任何 shell 命令。仅在你完全信任工作目录内容时才这样做。

## 安全最佳实践

1. **生产环境开启审批** — `AGENT_REQUIRE_APPROVAL=true`
2. **审查关键操作时用只读模式** — `synapse tui --readonly`
3. **不要把密钥放在 `AGENTS.md` 中** — 用 `.env` 或 `models.json` 的 `api_key_env`
4. **`.coding-agent/` 加入 `.gitignore`** — 会话数据不应提交到版本库
5. **定期审计会话记录** — `synapse sessions list` 查看历史
6. **自定义命令黑名单** — 根据需要补充危险命令模式

## 相关配置

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AGENT_REQUIRE_APPROVAL` | `false` | 启用人工审批 |
| `AGENT_AUTO_APPROVE` | `true` | 自动放行 |
| `AGENT_SAFETY_PROFILE` | `dev-autopass` | 安全策略 |
| `AGENT_READONLY` | `false` | 只读模式 |
| `AGENT_DENY_FS_PATHS` | `[]` | 禁止访问路径 |
| `AGENT_ENABLE_FS_PERMISSIONS` | `false` | 启用 FS 权限 |
| `AGENT_EXCLUDED_TOOLS` | `[]` | 排除工具列表 |
| `ENABLE_COMMAND_BLACKLIST` | `true` | 命令黑名单 |