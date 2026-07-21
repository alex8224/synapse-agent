# 缺失能力清单（验收后更新）

> 基于 `deepagents` 框架当前版本与项目现状。已按「框架优先 → 扩展点 → 产品壳」落地。

---

## 状态总览

| 能力域 | 状态 | 实现位置 |
|--------|------|----------|
| 子 Agent | 已接线 + 隔离 | `subagents.py`（researcher/reviewer 只读权限；tester +run_tests） |
| 工具排除 / 只读 | 已接线 | `harness.py` + `AGENT_READONLY` |
| 文件权限 | 已接线 | `fs_permissions.py` |
| Skills frontmatter | 已增强 | `skills/` + `/skills` |
| Memory 可见 | 已增强 | `MEMORY.md` 路径 + `/memory` |
| 上下文压缩 | 已接线 | auto middleware（deepagents）+ `compact_conversation` + `/compact` `/context` |
| HITL 闭环 | 已接线 | `safety` profiles + `/approve` `/reject` + stream 中断检测 |
| 多模型目录 | 已实现 | `models_registry.py` + CLI `/model` |
| MCP tools 注入 | 已实现 | `mcp_client.py` |
| 会话管理面 | 已实现 | `sessions.py` + slash + 恢复渲染 |
| 单一入口 | 已实现 | `uv tool install` / `synapse` |

---

## 迭代 A/B/C（本轮）

| 迭代 | 内容 | 命令 / 入口 |
|------|------|-------------|
| A | 手动压缩 + 可观测 | `/compact` `/context`；stream 显示 compact 事件 |
| B | HITL 真闭环 | `/safety dev-approve`；`/approve` `/reject`；`run --require-approval` 交互 |
| C | 子 Agent 隔离 + skills/memory 管理 | `/subagents` `/skills` `/memory` |

---

## 仍可后续增强（非阻塞）

| 项 | 说明 |
|----|------|
| Store 跨会话记忆 | LangGraph BaseStore |
| RubricMiddleware | 改完自检 |
| MCP UI 面板 | 当前 slash 已够用 |
| AsyncSubAgent | 本地场景不需要 |
| 单文件 PyInstaller exe | 可选，体积大 |

---

## 验收命令

```bash
uv run pytest
uv run ruff check src tests
uv run synapse --help
# TUI/chat:
# /context /compact /safety /skills /memory /subagents /approve /reject
```
