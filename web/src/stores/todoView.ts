/**
 * Pure parsing for the todo panel.
 *
 * The runtime already normalises a `write_todos` call into a checklist and puts
 * it on the tool item's `preview` (`runtime/timeline.py`): one `✓`/`●`/`○` line
 * per item, a `— done N · doing N · todo N` summary and a `… +N more` marker when
 * the preview itself was capped.  This mirrors that parser (`parse_todo_preview_lines`)
 * so the console can render the same list without a new wire field.
 */
import type { TranscriptMessage } from './historyMapper.ts';

export type TodoKind = 'done' | 'active' | 'pending';

export interface TodoItem {
  kind: TodoKind;
  content: string;
}

export interface TodoView {
  items: TodoItem[];
  done: number;
  active: number;
  pending: number;
  /** Items the runtime preview dropped past its own cap (`… +N more`). */
  omitted: number;
}

/** Tool names that carry a todo checklist, mirroring the runtime's set. */
const TODO_TOOL_NAMES = new Set(['write_todos', 'todo_write', 'todos']);

/** Marks the runtime writes, plus the older ASCII forms it still parses. */
const KIND_BY_MARK: Record<string, TodoKind> = {
  '✓': 'done',
  '●': 'active',
  '○': 'pending',
  x: 'done',
  X: 'done',
  '~': 'active',
  '…': 'active',
  '·': 'pending',
  ' ': 'pending',
  '-': 'pending',
};

const MARK_BY_KIND: Record<TodoKind, string> = { done: '✓', active: '●', pending: '○' };

/** Map a raw todo status onto the three kinds, mirroring `todo_status_kind`. */
function kindOfStatus(status: unknown): TodoKind {
  const key = String(status ?? 'pending')
    .trim()
    .toLowerCase()
    .replace(/[\s-]+/g, '_');
  if (['completed', 'complete', 'done', 'finished', 'closed', 'resolved'].includes(key)) {
    return 'done';
  }
  if (['in_progress', 'active', 'doing', 'running', 'started', 'current'].includes(key)) {
    return 'active';
  }
  return 'pending';
}

/** Normalise a `write_todos` argument list, mirroring the runtime's `extract_todos`. */
export function extractTodos(args: unknown): TodoItem[] {
  if (args === null || typeof args !== 'object') return [];
  const raw = (args as Record<string, unknown>)['todos'];
  if (!Array.isArray(raw)) return [];
  const items: TodoItem[] = [];
  for (const entry of raw) {
    if (entry !== null && typeof entry === 'object' && !Array.isArray(entry)) {
      const record = entry as Record<string, unknown>;
      const content = String(
        record['content'] ?? record['text'] ?? record['title'] ?? '',
      ).trim();
      items.push({ kind: kindOfStatus(record['status']), content });
    } else if (typeof entry === 'string' && entry.trim() !== '') {
      items.push({ kind: 'pending', content: entry.trim() });
    }
  }
  return items;
}

/**
 * The checklist preview for one tool call, or `null` when it is not a todo tool.
 *
 * The same shape the runtime writes (`format_todos_preview`), so a projected
 * history row and a live event parse identically — without it the todo panel
 * could only ever see the turns this page watched live.
 */
export function todoPreviewFromArgs(
  toolName: string,
  args: unknown,
  maxItems = 16,
): string | null {
  if (!TODO_TOOL_NAMES.has((toolName ?? '').toLowerCase())) return null;
  const todos = extractTodos(args);
  if (todos.length === 0) return null;
  const rows = todos
    .slice(0, maxItems)
    .map((item) => `${MARK_BY_KIND[item.kind]} ${item.content}`);
  if (todos.length > maxItems) rows.push(`… +${todos.length - maxItems} more`);
  const count = (kind: TodoKind) => todos.filter((item) => item.kind === kind).length;
  rows.push(`— done ${count('done')} · doing ${count('active')} · todo ${count('pending')}`);
  return rows.join('\n');
}


/** Parse one stored checklist preview; `null` when it holds no items. */
export function parseTodoPreview(preview: string | null | undefined): TodoView | null {
  const text = (preview ?? '').trim();
  if (text === '') return null;
  const items: TodoItem[] = [];
  let omitted = 0;
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    if (line === '') continue;
    if (line.startsWith('—')) continue;
    if (line.startsWith('…')) {
      // "… +3 more": the runtime capped the preview, so say so rather than
      // pretending the list is complete.
      const more = /\+(\d+)\s+more/.exec(line);
      if (more !== null) omitted = Number(more[1]) || 0;
      continue;
    }
    const head = line[0];
    if (KIND_BY_MARK[head] !== undefined && (line.length === 1 || line[1] === ' ')) {
      items.push({ kind: KIND_BY_MARK[head], content: line.slice(1).trim() || '(empty)' });
      continue;
    }
    // Legacy form: "[x] content" / "[~] content" / "[ ] content".
    if (head === '[' && line.slice(0, 4).includes(']')) {
      const close = line.indexOf(']');
      const kind = KIND_BY_MARK[line.slice(1, close)] ?? 'pending';
      items.push({ kind, content: line.slice(close + 1).trim() || '(empty)' });
    }
  }
  if (items.length === 0) return null;
  const count = (kind: TodoKind) => items.filter((item) => item.kind === kind).length;
  return {
    items,
    done: count('done'),
    active: count('active'),
    pending: count('pending'),
    omitted,
  };
}

/**
 * The newest checklist in the transcript, or `null` when no todo tool ran.
 *
 * The *last* call wins: a todo tool rewrites the whole list, so the most recent
 * preview is the current plan rather than an accumulation of every call.
 */
export function latestTodos(messages: readonly TranscriptMessage[]): TodoView | null {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i];
    if (message.type !== 'tool_group') continue;
    for (let j = (message.tools?.length ?? 0) - 1; j >= 0; j -= 1) {
      const tool = message.tools?.[j];
      if (tool === undefined || !TODO_TOOL_NAMES.has((tool.name ?? '').toLowerCase())) continue;
      const parsed = parseTodoPreview(tool.preview ?? null);
      if (parsed !== null) return parsed;
    }
  }
  return null;
}

/** Header label, mirroring the runtime's `summarize_todos` shape. */
export function todoPanelLabel(view: TodoView): string {
  const total = view.items.length + view.omitted;
  const label = `Todos ${view.done}/${total}`;
  if (view.active > 0) {
    const current = view.items.find((item) => item.kind === 'active');
    const head = current === undefined ? '' : `: ${current.content.slice(0, 40)}`;
    return `${label} · in progress${head}`;
  }
  if (view.done === total && total > 0) return `${label} · all done`;
  return label;
}
