import type { TranscriptMessage } from './historyMapper.ts';

export interface WorkGroup {
  key: string;
  anchor: TranscriptMessage;
  rows: TranscriptMessage[];
  running: boolean;
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
    if (message.type === 'thought' || (message.type === 'tool_group' && message.tools?.length)) {
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
