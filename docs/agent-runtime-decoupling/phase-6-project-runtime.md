# P6：ProjectRuntime 与跨项目资源隔离

> 状态：Not started  
> 前置条件：P5 同项目多会话门禁通过。  
> 目标：在单进程、单 Agent loop 中安全运行多个项目。

## 1. 目标

引入 `ProjectRuntime`，把 workspace、Settings、backend、checkpointer、Goal、MCP 和会话数据库变成显式项目作用域。RuntimeManager 的路由键从 `thread_id` 升级为 `SessionRef(project_id, thread_id)`。

## 2. SessionRef 与解析

```python
@dataclass(frozen=True, slots=True)
class SessionRef:
    project_id: str
    thread_id: str

    @property
    def global_id(self) -> str:
        return f"{self.project_id}:{self.thread_id}"
```

解析规则：

- 完整 ID 或唯一前缀成功。
- 同名项目、同标题会话多匹配时返回歧义，不取第一条。
- 全局选择必须同时确定 project 和 thread。
- Catalog 命中后必须回源验证。

## 3. 稳定项目身份

当前 catalog 实际按路径注册，目录移动会生成新 ID。建议增加：

```text
<workspace>/.synapse/project.json
```

包含稳定 `project_id` 和 schema version。迁移策略：

1. 已注册项目首次打开时把 catalog ID 写入项目文件。
2. 项目文件 ID 已存在时，以 ID 更新 catalog 路径。
3. 冲突时 fail closed，并提供显式修复命令。
4. Git remote 只用于展示和辅助诊断，不能作为唯一身份。

## 4. ProjectRuntime

```python
class ProjectRuntime:
    project_id: str
    workspace: Path
    settings: Settings
    session_store: SessionStore
    transcript_projection: TranscriptProjection
    checkpointer: Any
    goal_service: GoalService
    mcp_scope: McpScope
    sessions: dict[str, SessionRuntime]
```

惰性策略：

- 只浏览 catalog 不创建 ProjectRuntime。
- 只读取会话详情时按需打开只读数据源，不构建 Agent。
- 首次提交/恢复运行时才初始化完整资源。
- running session 存在时 ProjectRuntime 不可回收。

## 5. Settings 与环境隔离

当前 legacy `.env` 使用 `load_dotenv(..., override=True)` 修改进程环境，不适合并发项目。

目标：

- `load_project_settings(workspace)` 返回独立 Settings snapshot。
- `.env` 使用 `dotenv_values()` 或等价方式解析为私有 mapping。
- 模型凭据从 Settings/profile 获取。
- backend 子进程使用项目 env mapping 与受控继承环境合并。
- 项目切换不修改 `os.chdir()` 或全局 `os.environ`。

需要审计所有读取进程环境的第三方入口；无法隔离的项必须记录在风险清单。

## 6. Goal 隔离

当前 `init_goal_service()` 是绑定首次 sessions DB 的进程单例。改为：

- `ProjectRuntime.goal_service` 显式实例。
- goal tools/middleware 构建时注入 service/provider。
- 不通过无参数 `get_goal_service()` 查找当前项目。
- listener 和 runtime map 按项目释放。

## 7. MCP 隔离

当前 `_ACTIVE_POOL` 是进程单池，reload 会关闭旧项目连接。目标 registry：

```python
McpPoolKey(project_id, config_digest)
McpPoolRegistry.acquire(key)
McpPoolRegistry.release(key)
```

要求：

- 项目 A reload 不关闭项目 B pool。
- 同一项目相同配置可复用。
- 配置变化创建新 scope，旧 running Agent 持有 lease 至完成。
- shutdown 有序关闭全部 pool。

## 8. 数据和工具隔离

每个 ProjectRuntime 必须独立绑定：

- filesystem backend root。
- shell cwd/env。
- `AGENTS.md`、memory、skills。
- checkpoints/session/search-index/transcript/tool-output。
- session search/read tools。
- RAG/long-term memory（启用时）。
- Git chrome 数据源。

禁止从主进程启动 cwd 推断以上路径。

## 9. Catalog 对账

P6 同步增强：

- 记录 `last_seen_at`、`last_synced_at`、availability。
- 全量 sync 在事务内 upsert 并清理已删除投影。
- missing workspace 不删除历史投影，但标记不可用。
- 项目库仍是真源。

实际 schema 变更需单独 migration 和测试，不允许要求删除 catalog。

## 10. 执行计划

| ID | 工作 | 产物 | 依赖 |
|---|---|---|---|
| P6-01 | SessionRef/resolver | projects/session_ref.py | P5 |
| P6-02 | ProjectRuntime | runtime/projects/runtime.py | P6-01 |
| P6-03 | 稳定 project identity | projects/catalog + project.json | P6-01 |
| P6-04 | Settings/env snapshot | settings loader/backend | P6-02 |
| P6-05 | Goal 注入 | goals/runtime/tools/middleware | P6-02 |
| P6-06 | MCP registry/lease | integrations/mcp_client | P6-02 |
| P6-07 | 数据和工具路径隔离 | agent assembly/runtime | P6-02 |
| P6-08 | RuntimeManager 按 SessionRef 路由 | runtime manager | P6-02 |
| P6-09 | idle 项目资源回收 | project manager | P6-05/P6-06 |
| P6-10 | 双 workspace 测试 | tests | P6-08 |
| P6-11 | 项目隔离门禁 | progress | 全部 |

## 11. 必测隔离场景

- 两项目包含同名文件，Agent 只读取各自 root。
- 两项目使用不同 model profile/settings。
- 两项目使用不同 legacy env 值，shell/model 不串线。
- 两项目 thread_id 相同，checkpoint 和 transcript 不冲突。
- 项目 A reload MCP，项目 B 正在调用 MCP 不受影响。
- 两项目 goal 同时 active，账本不串库。
- 项目目录移动后 project_id 保持稳定。
- 同名项目解析返回歧义。
- missing workspace 不能创建空会话或在启动 cwd 工作。

## 12. 验收标准

- 所有跨项目命令和事件使用 SessionRef。
- 项目切换不修改进程 cwd/global env。
- Goal、MCP、backend 和数据库通过双项目交错测试。
- 项目资源惰性创建且可有序释放。
- 旧项目无需删除本地数据库即可迁移。
- P5 多会话能力在两个项目同时存在时仍正确。

## 13. 风险与回滚

- 风险：第三方库隐式读取全局环境。
  - 缓解：构建时显式注入、审计和隔离测试；必要时仅对特定集成禁用并发。
- 风险：MCP lease 生命周期复杂。
  - 缓解：引用计数、config digest 和 shutdown 测试。
- 回滚：RuntimeManager 可临时限制只打开一个 ProjectRuntime，但不回退 SessionRef 数据模型。
