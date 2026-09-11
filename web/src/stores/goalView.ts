/**
 * Read-only view of one session's long-running goal (`runtime.session.goal`).
 *
 * Mirrors the TUI bottom bar's `goal_indicator_text`: a compact status token
 * plus, while the goal is active, either budget usage or elapsed time.  Pure
 * formatting so it is exercised directly with the Node test runner.
 */
import { compactCount } from './usageView.ts';

export interface SessionGoalView {
  thread_id: string;
  goal_id: string;
  status: string;
  /** Runtime status label (`active` / `paused` / `stalled` / ...). */
  label: string;
  objective: string;
  token_budget: number | null;
  tokens_used: number;
  time_used_seconds: number;
}

/** `42` -> `42s`, `192` -> `3m12s`, `3900` -> `1h05m`. */
export function formatElapsed(seconds: number): string {
  const total = Math.max(0, Math.trunc(seconds));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${minutes}m${String(total % 60).padStart(2, '0')}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h${String(minutes % 60).padStart(2, '0')}m`;
}

/**
 * Compact status-bar label, or `''` when there is no goal (nothing is rendered
 * then — the bar never shows a placeholder for an absent goal).
 */
export function goalLabel(goal: SessionGoalView | null): string {
  if (goal === null) return '';
  const parts = [`goal·${goal.label}`];
  if (goal.label === 'active') {
    parts.push(
      goal.token_budget === null
        ? formatElapsed(goal.time_used_seconds)
        : `${compactCount(goal.tokens_used)}/${compactCount(goal.token_budget)}`,
    );
  } else if (goal.label === 'complete') {
    parts.push(compactCount(goal.tokens_used));
  }
  return parts.join(' ');
}

/** Hover text: the bounded objective plus the raw counters. */
export function goalTooltip(goal: SessionGoalView | null): string {
  if (goal === null) return '';
  const lines = [goal.objective];
  lines.push(
    goal.token_budget === null
      ? `用量 ${compactCount(goal.tokens_used)} tokens · ${formatElapsed(goal.time_used_seconds)}`
      : `预算 ${compactCount(goal.tokens_used)} / ${compactCount(goal.token_budget)} tokens · ${formatElapsed(goal.time_used_seconds)}`,
  );
  return lines.join('\n');
}

/**
 * Strict whitelist copy of the `runtime.session.goal` result.
 *
 * The result *is* the goal projection, and a thread without a goal answers
 * `null`, so both shapes are handled here and nothing else is copied.
 */
export function parseSessionGoal(payload: unknown): SessionGoalView | null {
  if (payload === null || typeof payload !== 'object') return null;
  const fields = payload as Record<string, unknown>;
  const budget = fields['token_budget'];
  return {
    thread_id: String(fields['thread_id'] ?? ''),
    goal_id: String(fields['goal_id'] ?? ''),
    status: String(fields['status'] ?? ''),
    label: String(fields['label'] ?? fields['status'] ?? ''),
    objective: String(fields['objective'] ?? ''),
    token_budget:
      typeof budget === 'number' && Number.isFinite(budget) ? Math.trunc(budget) : null,
    tokens_used: Number(fields['tokens_used'] ?? 0) || 0,
    time_used_seconds: Number(fields['time_used_seconds'] ?? 0) || 0,
  };
}
