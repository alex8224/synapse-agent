/**
 * Pure display labels for transcript rows.
 *
 * The design spec names two rows explicitly (`Thought for Xs`,
 * `N tools executed`); their leading glyph is a real icon now (`thoughtIcon` /
 * `toolGroupIcon`), so the labels carry words only.  The runtime status strings
 * are English, so they are mapped to the Chinese vocabulary the rest of the
 * console uses.  Unknown values fall back to the raw string instead of being
 * hidden.
 */

/** Human label for one runtime tool status. */
export function toolStatusLabel(status: string): string {
  const value = (status || '').toLowerCase();
  if (value === 'running') return '运行中';
  if (value === 'pending') return '等待';
  if (value === 'completed') return '完成';
  if (value === 'failed') return '失败';
  if (value === 'error') return '错误';
  if (value === 'cancelled' || value === 'canceled') return '已取消';
  return status;
}

/** Tool group header, e.g. `3 tools executed` (the spec wording). */
export function toolGroupLabel(count: number, parallel = false): string {
  const noun = count === 1 ? 'tool' : 'tools';
  return `${count} ${noun} executed${parallel ? ' (parallel)' : ''}`;
}

/**
 * Reasoning row label.
 *
 * `duration` is `undefined` for a projected history row, `streaming` while the
 * live chain is still open, and a formatted duration once it completes.
 */
export function thoughtLabel(duration?: string): string {
  if (duration === 'streaming') return 'Thinking...';
  if (duration === undefined || duration === '' || duration === 'done') return 'Thought';
  return `Thought for ${duration}`;
}

/** Expand/collapse affordance shown next to a collapsible row label. */
export function expandHint(expanded: boolean): string {
  return expanded ? '(收起)' : '(展开)';
}

/**
 * Material Symbols glyph for a reasoning row.
 *
 * A thinking head while the chain is closed, the wired one while it is still
 * running -- the same family as the console's reasoning-level control, so the
 * row reads as "the agent's thinking" rather than as another fold chevron.
 */
export function thoughtIcon(streaming: boolean): string {
  return streaming ? 'neurology' : 'psychology';
}

/** What a tool-batch header reports about its own items. */
export interface ToolGroupCounts {
  running: number;
  failed: number;
}

/**
 * Material Symbols glyph for a tool-batch header: the batch's own outcome, so
 * the row reads at a glance instead of only through its coloured counters.
 */
export function toolGroupIcon(counts: ToolGroupCounts): string {
  if (counts.failed > 0) return 'error';
  if (counts.running > 0) return 'progress_activity';
  return 'build';
}
