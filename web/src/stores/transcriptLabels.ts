/**
 * Pure display labels for transcript rows.
 *
 * The design spec names two labels explicitly (`◆ Thought for Xs`,
 * `▾ N tools executed`); the runtime status strings are English, so they are
 * mapped to the Chinese vocabulary the rest of the console uses.  Unknown
 * values fall back to the raw string instead of being hidden.
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
  if (duration === 'streaming') return '◆ Thinking...';
  if (duration === undefined || duration === '' || duration === 'done') return '◆ Thought';
  return `◆ Thought for ${duration}`;
}

/** Expand/collapse affordance shown next to a collapsible row label. */
export function expandHint(expanded: boolean): string {
  return expanded ? '(收起)' : '(展开)';
}
