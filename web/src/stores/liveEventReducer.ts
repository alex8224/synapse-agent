/**
 * Pure reducer folding one runtime event into console transcript state.
 *
 * Extracted from `useConsoleStore` so the runtime event contract can be
 * exercised directly with the Node built-in test runner (no zustand / React /
 * socket dependency), the same way `historyMapper` and `recoveryDecider` are.
 *
 * Contract:
 * - never mutates the input state;
 * - never throws on a malformed or unknown event, so a newer daemon cannot
 *   break an older console: unknown kinds and unexpected payload shapes are
 *   ignored and only known keys are read.
 *
 * Coverage mirrors the TUI reference consumer of the same event stream
 * (`src/synapse/ui/turn/event_renderer.py`).  `plan_updated` / `plan_removed` /
 * `diff_updated` are intentionally not rendered here either (the TUI ignores
 * them too, for lack of a sink representation).
 */
import type { ApprovalActionView, PendingApprovalView, RuntimeEvent } from '../client/types.ts';
import type { ToolItemView, TranscriptMessage } from './historyMapper.ts';
import { formatUsageMetrics, parseUsagePayload, type UsageView } from './usageView.ts';

/** Transient "what the agent is doing right now" state. */
export interface ActivityView {
  phase: string;
  detail: string;
  /** Epoch ms of the current phase; reset when the runtime sets `reset_timer`. */
  startedAt: number;
  active: boolean;
}

/** The subset of console state a runtime event is allowed to change. */
export interface LiveReducibleState {
  messages: TranscriptMessage[];
  activeTurnId: string | null;
  runtimeStatus: 'idle' | 'running';
  steerQueueCount: number;
  pendingApproval: PendingApprovalView | null;
  activity: ActivityView | null;
  usage: UsageView | null;
  metricsLabel: string;
}

const TOOL_ICON = 'build';

function asRecord(value: unknown): Record<string, any> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, any>)
    : {};
}

function asText(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function toolGroupId(turnId: string): string {
  return `tools-${turnId}`;
}

function elapsedLabel(startedAt: number, at: number): string {
  const seconds = Math.max(0, (at - startedAt) / 1000);
  return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
}

/** Normalize one `ToolItemPayload` (tool_started / tool_updated) into the view model. */
export function toolItemFromPayload(payload: Record<string, any>): ToolItemView {
  const name = asText(payload.name) || 'tool';
  const callId = asText(payload.call_id) || null;
  return {
    id: asText(payload.item_id) || callId || name,
    callId,
    name,
    label: asText(payload.label) || name,
    category: asText(payload.category) || 'other',
    path: asText(payload.path) || null,
    status: asText(payload.status) || 'running',
    preview: asText(payload.preview) || null,
    error: payload.error === true,
    sub: payload.sub === true,
    parentId: asText(payload.parent_id) || null,
    subagentStatus: null,
    subagentName: asText(payload.subagent_name) || null,
    icon: TOOL_ICON,
  };
}

/** A legacy `tool_result` row that had no matching per-item event. */
function legacyToolItem(
  name: string,
  callId: string | null,
  status: string,
  sub: boolean,
): ToolItemView {
  return {
    id: callId ?? `legacy-${name}`,
    callId,
    name,
    label: name,
    category: 'other',
    path: null,
    status,
    preview: null,
    error: status === 'failed' || status === 'error',
    sub,
    parentId: null,
    subagentStatus: null,
    subagentName: null,
    icon: TOOL_ICON,
  };
}

/**
 * Merge an incoming item over a previously known one.
 *
 * A payload update never carries the transient subagent stage, so an explicit
 * `null` there must not erase a stage a `subagent_status_changed` event set.
 */
function mergeToolItem(previous: ToolItemView, incoming: ToolItemView): ToolItemView {
  return {
    ...previous,
    ...incoming,
    subagentStatus: incoming.subagentStatus ?? previous.subagentStatus,
    subagentName: incoming.subagentName ?? previous.subagentName,
  };
}

function upsertToolItem(group: TranscriptMessage, item: ToolItemView): TranscriptMessage {
  const tools = group.tools ?? [];
  const index = tools.findIndex((t) => t.id === item.id);
  const next =
    index === -1
      ? [...tools, item]
      : tools.map((t, i) => (i === index ? mergeToolItem(t, item) : t));
  return { ...group, tools: next };
}

/** Find (or create) the single tool group of this turn and update it. */
function withToolGroup(
  messages: TranscriptMessage[],
  turnId: string,
  timestamp: string,
  mutate: (group: TranscriptMessage) => TranscriptMessage,
): TranscriptMessage[] {
  const id = toolGroupId(turnId);
  const index = messages.findIndex((m) => m.id === id);
  if (index === -1) {
    return [...messages, mutate({ id, type: 'tool_group', timestamp, tools: [] })];
  }
  return messages.map((m, i) => (i === index ? mutate(m) : m));
}

/** Patch one tool item by id across the transcript; a miss is a no-op. */
function patchToolItem(
  messages: TranscriptMessage[],
  itemId: string,
  patch: Partial<ToolItemView>,
): TranscriptMessage[] {
  let changed = false;
  const next = messages.map((m) => {
    if (m.type !== 'tool_group' || !m.tools?.some((t) => t.id === itemId)) return m;
    changed = true;
    return { ...m, tools: m.tools.map((t) => (t.id === itemId ? { ...t, ...patch } : t)) };
  });
  return changed ? next : messages;
}

function closeToolGroup(messages: TranscriptMessage[], turnId: string): TranscriptMessage[] {
  const id = toolGroupId(turnId);
  return messages.map((m) => (m.id === id ? { ...m, finished: true } : m));
}

function appendToThought(
  messages: TranscriptMessage[],
  turnId: string,
  text: string,
  stamp: string,
  at: number,
): TranscriptMessage[] {
  const id = `thought-${turnId}`;
  const index = messages.findIndex((m) => m.id === id);
  if (index === -1) {
    return [
      ...messages,
      {
        id,
        type: 'thought',
        timestamp: stamp,
        content: text,
        duration: 'streaming',
        expanded: true,
        startedAt: at,
      },
    ];
  }
  return messages.map((m, i) =>
    i === index ? { ...m, content: (m.content || '') + text } : m,
  );
}

function appendToAnswer(
  messages: TranscriptMessage[],
  turnId: string,
  text: string,
  stamp: string,
): TranscriptMessage[] {
  const id = `ans-${turnId}`;
  const index = messages.findIndex((m) => m.id === id);
  if (index === -1) {
    return [...messages, { id, type: 'assistant', timestamp: stamp, content: text }];
  }
  return messages.map((m, i) =>
    i === index ? { ...m, content: (m.content || '') + text } : m,
  );
}

function normalizeApprovalActions(value: unknown): ApprovalActionView[] {
  if (!Array.isArray(value)) return [];
  return value.map((raw, index) => {
    const action = asRecord(raw);
    const decisions = Array.isArray(action.allowed_decisions)
      ? action.allowed_decisions.filter((d: unknown): d is string => typeof d === 'string')
      : undefined;
    return {
      index,
      name: asText(action.name),
      args: asRecord(action.args),
      description: asText(action.description),
      allowed_decisions: decisions,
    };
  });
}

/**
 * Fold one event into the transcript state.
 *
 * `now` is injectable so timing-derived values (thought duration, activity
 * timer) are deterministic in tests.
 */
export function reduceRuntimeEvent(
  state: LiveReducibleState,
  event: RuntimeEvent,
  now: () => Date = () => new Date(),
): Partial<LiveReducibleState> {
  const raw: unknown = event.payload;
  const payload = asRecord(raw);
  const kind = event.kind;
  const turnId = typeof event.turn_id === 'string' ? event.turn_id : '';
  const date = now();
  const stamp = date.toLocaleTimeString().slice(0, 5);
  const at = date.getTime();
  const next: Partial<LiveReducibleState> = {};

  if (turnId) next.activeTurnId = turnId;

  if (kind === 'activity_started') {
    next.runtimeStatus = 'running';
    next.activity = {
      phase: asText(payload.phase) || 'thinking',
      detail: asText(payload.detail),
      startedAt: at,
      active: true,
    };
  } else if (kind === 'activity_updated') {
    next.runtimeStatus = 'running';
    next.activity = {
      phase: asText(payload.phase) || state.activity?.phase || 'thinking',
      detail: asText(payload.detail),
      startedAt:
        payload.reset_timer === true || !state.activity ? at : state.activity.startedAt,
      active: true,
    };
  } else if (kind === 'activity_stopped') {
    next.activity = state.activity ? { ...state.activity, active: false } : null;
  } else if (kind === 'reasoning_delta') {
    const text = asText(payload.text);
    if (text) next.messages = appendToThought(state.messages, turnId, text, stamp, at);
  } else if (kind === 'reasoning_completed') {
    const body = asText(payload.text);
    next.messages = state.messages.map((m) => {
      if (m.type !== 'thought' || m.id !== `thought-${turnId}`) return m;
      const duration =
        m.startedAt !== undefined
          ? elapsedLabel(m.startedAt, at)
          : m.duration === 'streaming'
            ? 'done'
            : m.duration;
      return { ...m, content: body || m.content, duration };
    });
  } else if (kind === 'answer_delta') {
    const text = asText(payload.text);
    if (text) next.messages = appendToAnswer(state.messages, turnId, text, stamp);
  } else if (kind === 'answer_completed') {
    const body = asText(payload.text);
    const id = `ans-${turnId}`;
    const existing = state.messages.some((m) => m.id === id);
    if (existing) {
      next.messages = state.messages.map((m) =>
        m.id === id && body ? { ...m, content: body } : m,
      );
    } else if (body) {
      next.messages = [
        ...state.messages,
        { id, type: 'assistant', timestamp: stamp, content: body },
      ];
    }
  } else if (kind === 'tool_batch_started') {
    next.messages = withToolGroup(state.messages, turnId, stamp, (group) => ({
      ...group,
      parallel: payload.parallel === true,
      finished: false,
    }));
  } else if (kind === 'tool_started' || kind === 'tool_updated') {
    const item = toolItemFromPayload(payload);
    next.messages = withToolGroup(state.messages, turnId, stamp, (group) =>
      upsertToolItem(group, item),
    );
  } else if (kind === 'tool_finished') {
    const itemId = asText(payload.item_id);
    if (itemId) {
      const patch: Partial<ToolItemView> = {
        status: asText(payload.status) || 'completed',
        error: payload.error === true,
      };
      const preview = asText(payload.preview);
      if (preview) patch.preview = preview;
      next.messages = patchToolItem(state.messages, itemId, patch);
    }
  } else if (kind === 'tool_result') {
    const name = asText(payload.name) || 'tool';
    const callId = asText(payload.call_id) || null;
    const status = asText(payload.status) || 'completed';
    const sub = payload.sub === true;
    next.messages = withToolGroup(state.messages, turnId, stamp, (group) => {
      const tools = group.tools ?? [];
      const index = tools.findIndex(
        (t) => (callId !== null && t.callId === callId) || t.name === name,
      );
      if (index === -1) {
        return upsertToolItem(group, legacyToolItem(name, callId, status, sub));
      }
      return {
        ...group,
        tools: tools.map((t, i) =>
          i === index
            ? {
                ...t,
                status,
                error: status === 'failed' || status === 'error' || t.error,
                sub: t.sub || sub,
              }
            : t,
        ),
      };
    });
  } else if (kind === 'tool_batch_finished') {
    next.messages = closeToolGroup(state.messages, turnId);
  } else if (kind === 'subagent_status_changed') {
    const parentId = asText(payload.parent_id);
    if (parentId) {
      const patched = patchToolItem(state.messages, parentId, {
        subagentStatus: typeof payload.status === 'string' ? payload.status : null,
      });
      if (patched !== state.messages) next.messages = patched;
    }
  } else if (kind === 'usage_updated') {
    const usage = parseUsagePayload(payload);
    next.usage = usage;
    next.metricsLabel = formatUsageMetrics(usage);
  } else if (kind === 'info' || kind === 'warning') {
    const text =
      typeof raw === 'string' ? raw : asText(payload.message) || asText(payload.text);
    if (text) {
      next.messages = [
        ...state.messages,
        {
          id: `info-${turnId}-${event.sequence}`,
          type: 'info',
          timestamp: stamp,
          content: text,
          infoLevel: kind === 'warning' ? 'warning' : 'info',
        },
      ];
    }
  } else if (kind === 'approval_required') {
    next.pendingApproval = {
      turn_id: turnId,
      actions: normalizeApprovalActions(payload.actions),
    };
  } else if (kind === 'turn_waiting_approval') {
    // Still in flight: the turn is blocked on the human, not idle.
    next.runtimeStatus = 'running';
  } else if (
    kind === 'turn_completed' ||
    kind === 'turn_cancelled' ||
    kind === 'turn_failed'
  ) {
    next.runtimeStatus = 'idle';
    next.steerQueueCount = 0;
    next.activity = null;
    next.pendingApproval = null;
    const error = asText(payload.error);
    if (kind === 'turn_failed' && error) {
      next.messages = [
        ...state.messages,
        {
          id: `fail-${turnId}-${event.sequence}`,
          type: 'info',
          timestamp: stamp,
          content: error,
          infoLevel: 'warning',
        },
      ];
    }
  }

  return next;
}
