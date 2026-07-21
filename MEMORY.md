# Project memory (optional)

This file is injected into the agent when present (see `Settings.memory_paths`).

Use it for durable project facts and preferences learned across sessions:

- coding conventions
- preferred test commands
- environment quirks
- decisions the agent should remember

Keep entries short and actionable.

## Agent 行为（用户纠正）
- 问候/闲聊/连通性探测：禁止读技能、扫仓库、write_todos；直接短回复。
- 技能清单与描述、AGENTS.md/MEMORY.md 已注入上下文；不要为“对齐约定”再 read_file 技能或记忆。
- 仅当任务明确匹配某技能且需要其逐步流程时，才读对应 `SKILL.md`。

## HITL (human-in-the-loop)
- Default: off (`dev-autopass`). Enable via `--require-approval` or `/safety dev-approve`.
- interrupt_on: execute / write_file / edit_file when require_approval=True.
- Stream ends with `StreamResult.interrupted` via `has_pending_interrupt`.
- Resume: `/approve` or `/reject [reason]` (TUI calls `run_resume`; chat uses `_resume_hitl`).
- One-shot `run --require-approval` prompts interactive `hitl>` loop.
