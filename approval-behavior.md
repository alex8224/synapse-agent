# 审批（Approval）行为说明

结论：打开审批后，agent 在执行**写操作类工具前会暂停等待用户决定**，而不是直接拒绝或跳过。

依据代码：`src/synapse/runtime/safety.py`、`src/synapse/runtime/hitl.py`、`src/synapse/app/agent.py`、`src/synapse/ui/turn/controller.py`、`src/synapse/commands/slash_cmds.py`。

## 如何正确打开审批

| 方式 | 说明 |
| --- | --- |
| 环境变量 `AGENT_SAFETY_PROFILE=dev-approve` | 推荐方式（可在 `.env` 或系统环境变量中设置） |
| TUI 内 `/safety dev-approve` | 切换后重建 agent |
| 档位别名 | `approve` / `hitl` / `dev_approve` 均映射到 `dev-approve` |

> 注意：仅设 `AGENT_REQUIRE_APPROVAL=true` 或 CLI `--require-approval` **不会生效**。
> agent 构建时 `apply_safety_to_settings` 会按 `safety_profile`（默认 `dev-autopass`）把 `require_approval` 强制改回 `false`。
> 必须切换到 `dev-approve` 档。

## 打开后的行为

| 阶段 | 表现 |
| --- | --- |
| 需要审批的工具 | 仅 `execute`、`write_file`、`edit_file`、`patch` 四类（`build_interrupt_on` 返回）；`read_file`、`search_files` 等只读工具照常执行，不拦截 |
| 触发时机 | agent 一旦尝试调用上述工具，LangGraph 在**执行前**中断（interrupt），回合状态变为 `WAITING_APPROVAL` |
| TUI 界面 | 顶部活动区显示 `waiting / approval`；转录区打印待批清单：工具名、参数预览、描述，末尾提示 `decide: /approve | /reject [reason]` |
| 用户操作 | `/approve` 放行；`/reject [reason]` 拒绝（可附理由） |

## 审批决策的两种结果

| 决策 | 行为 |
| --- | --- |
| `/approve` | 恢复图执行，工具真正运行，agent 继续后续步骤 |
| `/reject [reason]` | 工具不执行，拒绝信息（默认 `Rejected by user via /reject` 或自定义 reason）作为反馈回传给模型，agent 会据此调整方案或换工具重试 |

## 其他要点

| 项 | 说明 |
| --- | --- |
| 多个工具同时待批 | `build_decisions` 会按待批数量生成等量决策；一次 `/approve` 放行全部 |
| 无待批时 | `/approve` 或 `/reject` 会提示 `no pending approval`，不做事 |
| 黑名单 | `dev-approve` 档 `enable_command_blacklist=True`，但 `check_command` 目前仅定义、未接入运行时；`rm -rf` 等危险命令不会自动拦截，仍走人工审批 |
| ACP 客户端 | 通过 `_request_permission_decisions` 请求权限，超过 `_max_permission_turns` 时报 `maximum permission resume turns exceeded` |

## 一句话总结

开启审批（`dev-approve`）后，agent 每次写文件或执行命令前都会停下等待用户 `/approve` 或 `/reject`，只读操作不受影响。
