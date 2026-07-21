# Project memory (optional)

This file is injected into the agent when present (see `Settings.memory_paths`).

Use it for durable project facts and preferences learned across sessions:

- coding conventions
- preferred test commands
- environment quirks
- decisions the agent should remember

Keep entries short and actionable.

## HITL (human-in-the-loop)
- Default: off (`dev-autopass`). Enable via `--require-approval` or `/safety dev-approve`.
- interrupt_on: execute / write_file / edit_file when require_approval=True.
- Stream ends with `StreamResult.interrupted` via `has_pending_interrupt`.
- Resume: `/approve` or `/reject [reason]` (TUI calls `run_resume`; chat uses `_resume_hitl`).
- One-shot `run --require-approval` prompts interactive `hitl>` loop.
