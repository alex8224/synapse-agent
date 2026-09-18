import type { TranscriptMessage } from './historyMapper.ts';
import { ROW_POLICY, isFoldStep } from './transcriptRowPolicy.ts';
import type { ActivityView } from './liveEventReducer.ts';

export interface WorkGroup {
  key: string;
  anchor: TranscriptMessage;
  rows: TranscriptMessage[];
  running: boolean;
}

/**
 * What a fold group is doing right now.
 *
 * The fold header reports this beside the chevron, so a reader can tell a running
 * tool from a finished one (and reasoning from tool use) without opening the fold.
 * `text` is the rendered label; `toolName` / `intent` keep the two parts separate
 * for callers that need them apart.
 */
export interface GroupIntentStatus {
  kind: 'thinking' | 'tool';
  state: 'running' | 'completed' | 'failed';
  toolName?: string;
  intent?: string;
  text: string;
}

/** The label of a tool call, falling back to its name when no intent was lifted. */
function toolIntent(tool: { name: string; label: string }): string | undefined {
  return tool.label && tool.label !== tool.name ? tool.label : undefined;
}

function toolText(tool: { name: string; label: string }): string {
  const intent = toolIntent(tool);
  return intent ? `${tool.name} · ${intent}` : tool.name;
}

/**
 * The intent / activity to paint beside a fold header's chevron.
 *
 * The rows are the authority while the turn has any: a running tool anywhere in the
 * group wins (the group is doing work the reader cannot see yet), otherwise the last
 * row says whether reasoning is still streaming or the turn is between steps.  Only
 * a group with no rows at all -- a turn whose first step has not landed -- falls back
 * to the runtime's transient `activity` phase.  `null` means "nothing to report", so
 * a settled or plain-question turn paints no pill.
 */
export function getGroupIntentStatus(
  group: WorkGroup, activity?: ActivityView | null,
): GroupIntentStatus | null {
  for (const row of group.rows) {
    if (row.type !== 'tool_group' || !row.tools?.length) continue;
    const running = row.tools.find(
      (tool) => tool.status === 'running' || tool.status === 'pending',
    );
    if (!running) continue;
    return {
      kind: 'tool', state: 'running', toolName: running.name,
      intent: toolIntent(running), text: toolText(running),
    };
  }

  const lastIndex = group.rows.length - 1;
  const last = group.rows[lastIndex];
  if (last?.type === 'thought') {
    // The last row is the thought, so no tool row follows it: a still-running group
    // is reasoning.  A live stream also flags itself on the row.
    const laterTool = group.rows.slice(lastIndex + 1).some((row) => row.type === 'tool_group');
    const streaming = last.duration === 'streaming' || last.streaming === true
      || (group.running && !laterTool);
    return streaming
      ? { kind: 'thinking', state: 'running', text: '正在思考...' }
      : { kind: 'thinking', state: 'completed', text: '思考完成' };
  }
  if (last?.type === 'tool_group' && last.tools?.length) {
    const tool = last.tools[last.tools.length - 1];
    return {
      kind: 'tool',
      state: tool.error || tool.status === 'failed' ? 'failed' : 'completed',
      toolName: tool.name,
      intent: toolIntent(tool),
      text: toolText(tool),
    };
  }

  if (activity?.active) {
    if (activity.phase === 'thinking' || activity.phase === 'model') {
      return { kind: 'thinking', state: 'running', text: '正在思考...' };
    }
    if (activity.phase === 'tool') {
      return {
        kind: 'tool', state: 'running',
        text: activity.detail ? `执行工具 · ${activity.detail}` : '执行工具中...',
      };
    }
  }
  return null;
}

/**
 * The fold state a row's own visibility depends on (`Transcript`'s `processMeta`).
 */
export interface RowFold {
  isFirst: boolean;
  isExpanded: boolean;
}

/**
 * Whether a transcript row paints anything at all.
 *
 * A collapsed turn shows one header -- the "已工作 N 秒" strip of its *first* step --
 * and hides every other step of its fold, so a long turn owns N rows of which at most
 * a few are on screen.  The hidden ones must also occupy no space: the plain list
 * rendered them as `null` (no element, so the column's `space-y-5` gave them no gap
 * either), and the windowed list has to keep that promise, because its positioning
 * wrapper exists per *index*, not per painted row.  `Transcript` and its rows both
 * ask this function so the two can never disagree about which rows are hidden.
 */
export function rowPaints(message: TranscriptMessage, fold?: RowFold): boolean {
  const policy = ROW_POLICY[message.type];
  // A step with nothing in it is not a step at all (see `workGroups`): it paints
  // nothing even when the turn is open.
  if (policy.paints !== undefined && !policy.paints(message)) return false;
  if (!policy.step) return true;
  return fold === undefined || fold.isExpanded || fold.isFirst;
}

/** Group by runtime identity, not by assistant narration or the latest steer row. */
export function workGroups(
  messages: readonly TranscriptMessage[], activeTurnId: string | null, running: boolean,
): WorkGroup[] {
  const groups = new Map<string, WorkGroup>();
  let current: WorkGroup | undefined;
  for (const message of messages) {
    const key = message.turnId || (message.type === 'user' ? message.id : current?.key) || message.id;
    if (message.type === 'user' || message.turnId || !current) {
      current = groups.get(key);
      if (!current) {
        current = { key, anchor: message, rows: [], running: false };
        groups.set(key, current);
      }
    }
    if (isFoldStep(message)) {
      current.rows.push(message);
    }
  }
  for (const group of groups.values()) {
    group.running = running && !group.anchor.work?.ended && (
      activeTurnId ? group.key === activeTurnId : group.anchor.work?.startedAt !== undefined
    );
  }
  return [...groups.values()];
}

/** Terminal seconds are authoritative; never estimate elapsed time from tool count. */
export function workSeconds(group: WorkGroup, now: number): number | undefined {
  const work = group.anchor.work;
  if (group.running && work?.startedAt !== undefined) {
    return Math.max(0, Math.floor((now - work.startedAt) / 1000));
  }
  return work?.elapsed === undefined ? undefined : Math.max(0, Math.floor(work.elapsed));
}

export function formatWorkDuration(seconds: number | undefined): string {
  if (seconds === undefined) return '（耗时未知）';
  const sec = Math.max(0, Math.floor(seconds));
  if (sec < 60) return `${sec} 秒`;
  const mins = Math.floor(sec / 60);
  return sec % 60 ? `${mins} 分 ${sec % 60} 秒` : `${mins} 分钟`;
}

/** Bind only an unclaimed submitted anchor; never reuse a completed history turn. */
export function bindWorkTurn(
  messages: TranscriptMessage[], turnId: string, at: number,
): TranscriptMessage[] {
  if (!turnId || messages.some((m) => m.turnId === turnId)) return messages;
  const pending = messages.findLastIndex((m) => m.type === 'user' && !m.turnId && !m.steer && !m.work?.ended);
  if (pending < 0) return messages;
  return messages.map((m, i) => i === pending
    ? { ...m, turnId, work: m.work ?? { startedAt: at, ended: false } } : m);
}

/** Freeze every unfinished row on settlement, including cancelled/failed tool calls. */
export function finishWorkTurn(
  messages: TranscriptMessage[], turnId: string, at: number, elapsed: unknown, status: string,
): TranscriptMessage[] {
  return messages.map((m) => {
    if (m.turnId !== turnId) return m;
    const work = m.work;
    return {
      ...m,
      ...(work ? { work: {
        ...work, ended: true,
        elapsed: typeof elapsed === 'number' && Number.isFinite(elapsed) && elapsed >= 0
          ? elapsed : work.elapsed ?? (work.startedAt === undefined ? undefined : Math.max(0, (at - work.startedAt) / 1000)),
      } } : {}),
      ...(m.streaming ? { streaming: false } : {}),
      ...(m.duration === 'streaming' ? { duration: 'done' } : {}),
      ...(m.type === 'tool_group' ? {
        finished: true,
        tools: m.tools?.map((tool) => tool.status === 'running' || tool.status === 'pending'
          ? { ...tool, status: status === 'turn_completed' ? 'completed' : status === 'turn_failed' ? 'failed' : 'cancelled', error: status === 'turn_failed', subagentStatus: null }
          : tool),
      } : {}),
    };
  });
}
