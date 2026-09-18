/**
 * Put a turn's change cards at the end of that turn, wherever the turn now sits.
 *
 * The event arrives after the turn's terminal one, so by then a newer turn may have
 * started: the row belongs to *its* turn, not to the newest one.  Applying the same
 * event twice rewrites the row in place, so a replayed batch cannot stack duplicates.
 */
function appendTurnChanges(
  messages: TranscriptMessage[],
  turnId: string,
  stamp: string,
  changes: TurnChangeView[],
  total: number,
): TranscriptMessage[] {
  const id = `changes-${turnId}`;
  const row: TranscriptMessage = {
    id,
    type: 'changes',
    timestamp: stamp,
    turnId,
    changes,
    changesTotal: total,
  };
  const existing = messages.findIndex((m) => m.id === id);
  if (existing !== -1) return messages.map((m, i) => (i === existing ? row : m));
  const lastOfTurn = messages.findLastIndex((m) => m.turnId === turnId);
  const at = lastOfTurn === -1 ? messages.length : lastOfTurn + 1;
  return [...messages.slice(0, at), row, ...messages.slice(at)];
}

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
 *
 * One stream batch is one visual tool group and one assistant text segment is
 * one answer row, so interleaved reasoning / tools / text keep the order the TUI
 * shows them in.
 */
import type { RuntimeEvent } from '../client/types.ts';
import type { ApprovalActionPayload } from '../runtime-client/contract.generated.ts';
import { turnChangeViews, type ToolItemView, type TranscriptMessage, type TurnChangeView } from './historyMapper.ts';
import { bindWorkTurn, finishWorkTurn } from './turnWork.ts';
import { formatUsageMetrics, parseUsagePayload, type UsageView } from './usageView.ts';

/**
 * Console view of one HITL approval action.
 *
 * The `approval_required` event carries the producer's full
 * `ApprovalActionPayload`; the reducer adds the display index the payload does
 * not carry.  (The `runtime.turn.approval.get` result view is a different,
 * narrower wire shape and is not what this state is built from.)
 */
export interface ApprovalAction extends ApprovalActionPayload {
  index: number;
}

/** Pending HITL approval as the console holds it, built from the live event. */
export interface PendingApproval {
  turn_id: string;
  actions: ApprovalAction[];
}

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
  /** Bounded tombstones for turns observed without a user/history anchor. */
  settledTurnIds?: string[];
  runtimeStatus: 'idle' | 'running';
  steerQueueCount: number;
  pendingApproval: PendingApproval | null;
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

function toolGroupPrefix(turnId: string): string {
  return `tools-${turnId}-`;
}

function answerPrefix(turnId: string): string {
  return `ans-${turnId}-`;
}

/** How many transcript rows of one turn already carry this prefix. */
function countWithPrefix(messages: readonly TranscriptMessage[], prefix: string): number {
  let count = 0;
  for (const message of messages) {
    if (message.id.startsWith(prefix)) count += 1;
  }
  return count;
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
    argsPreview: asText(payload.args_preview) || null,
    error: payload.error === true,
    sub: payload.sub === true,
    parentId: asText(payload.parent_id) || null,
    subagentStatus: null,
    subagentName: asText(payload.subagent_name) || null,
    icon: TOOL_ICON,
  };
}

/** Apply the bounded call arguments carried by `tool_batch_started` to its item. */
function toolCallPreviewItem(
  call: Record<string, any>,
  index: number,
): ToolItemView {
  const name = asText(call.name) || 'tool';
  const callId = asText(call.call_id) || asText(call.id) || null;
  const argsPreview = asText(call.args_preview) || null;
  return {
    id: callId || `batch-call-${index}`,
    callId,
    name,
    label: name,
    category: 'other',
    path: null,
    status: 'pending',
    preview: null,
    error: false,
    sub: false,
    parentId: null,
    subagentStatus: null,
    subagentName: null,
    icon: TOOL_ICON,
    argsPreview,
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
    argsPreview: incoming.argsPreview ?? previous.argsPreview,
    subagentStatus: incoming.subagentStatus ?? previous.subagentStatus,
    subagentName: incoming.subagentName ?? previous.subagentName,
  };
}

function upsertToolItem(group: TranscriptMessage, item: ToolItemView): TranscriptMessage {
  const tools = group.tools ?? [];
  const index = tools.findIndex((t) =>
    t.id === item.id || (t.id === t.callId && t.callId !== null && item.callId !== null && t.callId === item.callId),
  );
  const next =
    index === -1
      ? [...tools, item]
      : tools.map((t, i) => (i === index ? mergeToolItem(t, item) : t));
  return { ...group, tools: next };
}

/**
 * Merge an incoming item over the row that already holds it in this turn; a miss
 * is a no-op.
 *
 * Item ids restart with every turn (`g1-0` is the first call of each turn), so
 * the lookup has to stay inside this turn's groups: matching an older turn's row
 * would swallow the item and it would never appear in the batch that produced it.
 */
function mergeTurnToolItem(
  messages: TranscriptMessage[],
  turnId: string,
  item: ToolItemView,
): TranscriptMessage[] {
  const prefix = toolGroupPrefix(turnId);
  let changed = false;
  const next = messages.map((m) => {
    if (m.type !== 'tool_group' || !m.id.startsWith(prefix)) return m;
    if (!m.tools?.some((t) =>
      t.id === item.id || (t.id === t.callId && t.callId !== null && item.callId !== null && t.callId === item.callId),
    )) return m;
    changed = true;
    return {
      ...m,
      tools: m.tools.map((t) =>
        t.id === item.id || (t.id === t.callId && t.callId !== null && item.callId !== null && t.callId === item.callId)
          ? mergeToolItem(t, item)
          : t,
      ),
    };
  });
  return changed ? next : messages;
}

/**
 * Patch the newest row of this turn matching `match`; a miss is a no-op.
 *
 * A legacy `tool_result` carries no item id, so its row is found by call id (or
 * tool name) inside this turn's groups, newest group first.
 */
function patchTurnToolItem(
  messages: TranscriptMessage[],
  turnId: string,
  match: (tool: ToolItemView) => boolean,
  patch: (tool: ToolItemView) => ToolItemView,
): TranscriptMessage[] {
  const prefix = toolGroupPrefix(turnId);
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i];
    if (message.type !== 'tool_group' || !message.id.startsWith(prefix)) continue;
    const tools = message.tools ?? [];
    const hit = tools.findIndex(match);
    if (hit === -1) continue;
    return messages.map((m, j) =>
      j === i ? { ...m, tools: tools.map((t, k) => (k === hit ? patch(t) : t)) } : m,
    );
  }
  return messages;
}

/**
 * Index of the tool group still collecting items for this turn, or -1.
 *
 * The runtime brackets every model step's tool calls with `tool_batch_started`
 * and `tool_batch_finished`, so one turn holds one group per batch.  Only the
 * newest group may still take rows: a finished one must never be reopened, or a
 * later batch is appended to the group the first batch opened and the tool calls
 * drift away from the reasoning step they followed.
 */
function openToolGroupIndex(messages: TranscriptMessage[], turnId: string): number {
  const prefix = toolGroupPrefix(turnId);
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i];
    if (message.type !== 'tool_group' || !message.id.startsWith(prefix)) continue;
    return message.finished === true ? -1 : i;
  }
  return -1;
}

/**
 * Seal the open group of this turn and append the next one.
 *
 * One stream batch is one visual group, exactly like the TUI reference
 * (`ui/textual_stream_sink.py`, `tool_calls_started`): a batch that never got a
 * clean `tool_batch_finished` is sealed here, so the next batch still starts its
 * own group.
 */
function openToolGroup(
  messages: TranscriptMessage[],
  turnId: string,
  stamp: string,
  parallel: boolean,
): TranscriptMessage[] {
  const prefix = toolGroupPrefix(turnId);
  const sealed = messages.map((m) =>
    m.type === 'tool_group' && m.finished !== true && m.id.startsWith(prefix)
      ? { ...m, finished: true }
      : m,
  );
  return [
    ...sealed,
    {
      id: `${prefix}${countWithPrefix(sealed, prefix) + 1}`,
      type: 'tool_group',
      timestamp: stamp,
      tools: [],
      parallel,
      finished: false,
    },
  ];
}

/** Append an item to this turn's open group, opening a new one when needed. */
function appendToolItem(
  messages: TranscriptMessage[],
  turnId: string,
  stamp: string,
  item: ToolItemView,
): TranscriptMessage[] {
  const index = openToolGroupIndex(messages, turnId);
  if (index !== -1) {
    return messages.map((m, i) => (i === index ? upsertToolItem(m, item) : m));
  }
  const opened = openToolGroup(messages, turnId, stamp, false);
  const last = opened.length - 1;
  return opened.map((m, i) => (i === last ? upsertToolItem(m, item) : m));
}

/** Patch one tool item by id inside this turn's groups; a miss is a no-op. */
function patchToolItem(
  messages: TranscriptMessage[],
  turnId: string,
  itemId: string,
  patch: Partial<ToolItemView>,
): TranscriptMessage[] {
  const prefix = toolGroupPrefix(turnId);
  let changed = false;
  const next = messages.map((m) => {
    if (m.type !== 'tool_group' || !m.id.startsWith(prefix)) return m;
    if (!m.tools?.some((t) => t.id === itemId)) return m;
    changed = true;
    return { ...m, tools: m.tools.map((t) => (t.id === itemId ? { ...t, ...patch } : t)) };
  });
  return changed ? next : messages;
}

/**
 * Kinds that terminate the turn.
 *
 * Exported so the store can refresh what a finished turn changed (the session's
 * cumulative token totals) without repeating the list.
 */
export function isTurnTerminalKind(kind: string): boolean {
  return kind === 'turn_completed' || kind === 'turn_cancelled' || kind === 'turn_failed';
}

/** Mark this turn's open group finished; nothing to do when none is open. */
function closeToolGroup(messages: TranscriptMessage[], turnId: string): TranscriptMessage[] {
  const index = openToolGroupIndex(messages, turnId);
  if (index === -1) return messages;
  return messages.map((m, i) => (i === index ? { ...m, finished: true } : m));
}

/**
 * Id of the reasoning segment currently streaming for one turn.
 *
 * A multi-step turn reasons once per step (before each tool call), and the
 * durable history projection stores those as separate `thought` events.  Feeding
 * every delta of the turn into one per-turn message merged them into a single
 * fold, and the next `reasoning_completed` then replaced the accumulated text
 * with that segment's own body — so earlier segments vanished too.
 *
 * A segment stays open only while it is streaming *and* still the newest row: a
 * tool call, an answer or a completion all end it, so the next delta opens the
 * next segment.
 */
function thoughtIdFor(messages: TranscriptMessage[], turnId: string): string {
  const prefix = `thought-${turnId}-`;
  let lastThought = -1;
  let count = 0;
  for (let i = 0; i < messages.length; i += 1) {
    if (messages[i].id.startsWith(prefix)) {
      lastThought = i;
      count += 1;
    }
  }
  const open = lastThought === -1 ? null : messages[lastThought];
  if (open !== null && open.duration === 'streaming' && lastThought === messages.length - 1) {
    return open.id;
  }
  return `${prefix}${count + 1}`;
}

function appendToThought(
  messages: TranscriptMessage[],
  turnId: string,
  text: string,
  stamp: string,
  at: number,
): TranscriptMessage[] {
  const id = thoughtIdFor(messages, turnId);
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

/**
 * Index of the answer row this turn is still writing, or -1.
 *
 * A multi-step turn prints assistant text before each further tool batch ("let me
 * check X", tools, then the final answer).  Folding every delta of the turn into
 * one per-turn row merged those segments and left the row where the first one
 * started, so the final answer rendered above the tool calls that produced it.
 * A segment stays open until its own `answer_completed` closes it.
 */
function openAnswerIndex(messages: TranscriptMessage[], turnId: string): number {
  const prefix = answerPrefix(turnId);
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i];
    if (message.type !== 'assistant' || !message.id.startsWith(prefix)) continue;
    return message.streaming === true ? i : -1;
  }
  return -1;
}

function appendToAnswer(
  messages: TranscriptMessage[],
  turnId: string,
  text: string,
  stamp: string,
): TranscriptMessage[] {
  const index = openAnswerIndex(messages, turnId);
  if (index !== -1) {
    return messages.map((m, i) =>
      i === index ? { ...m, content: (m.content || '') + text } : m,
    );
  }
  const prefix = answerPrefix(turnId);
  return [
    ...messages,
    {
      id: `${prefix}${countWithPrefix(messages, prefix) + 1}`,
      type: 'assistant',
      timestamp: stamp,
      content: text,
      streaming: true,
    },
  ];
}

/** Close this turn's streaming answer row with its authoritative text. */
function completeAnswer(
  messages: TranscriptMessage[],
  turnId: string,
  body: string,
  stamp: string,
): TranscriptMessage[] {
  const index = openAnswerIndex(messages, turnId);
  if (index !== -1) {
    return messages.map((m, i) =>
      i === index ? { ...m, content: body || m.content, streaming: false } : m,
    );
  }
  if (!body) return messages;
  const prefix = answerPrefix(turnId);
  return [
    ...messages,
    {
      id: `${prefix}${countWithPrefix(messages, prefix) + 1}`,
      type: 'assistant',
      timestamp: stamp,
      content: body,
      streaming: false,
    },
  ];
}

function normalizeApprovalActions(value: unknown): ApprovalAction[] {
  if (!Array.isArray(value)) return [];
  return value.map((raw, index) => {
    const action = asRecord(raw);
    const decisions = Array.isArray(action.allowed_decisions)
      ? action.allowed_decisions.filter((d: unknown): d is string => typeof d === 'string')
      : [];
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

  // A late event from a settled turn must neither revive it nor seize the next
  // turn's status/approval/activity. The anchor is the durable terminal marker.
  const settledTurn = turnId !== '' && (state.settledTurnIds?.includes(turnId) === true ||
    state.messages.some((m) => m.turnId === turnId && m.work?.ended === true));
  if (settledTurn && kind === 'turn_changes') {
    // The one late event that is welcome: it is emitted *as* the turn settles, so it
    // always arrives after the terminal one, and adding the turn's change cards is its
    // whole job.  It touches no status, activity or active-turn state, so it is folded
    // here and returns -- a settled turn stays settled.
    const changes = turnChangeViews(payload.changes);
    if (changes.length > 0) {
      const total = typeof payload.total === 'number' && payload.total > 0
        ? payload.total : changes.length;
      next.messages = appendTurnChanges(state.messages, turnId, stamp, changes, total);
    }
    return next;
  }
  if (settledTurn && !(isTurnTerminalKind(kind) && state.activeTurnId === turnId)) return {};
  const foreignTurn = !!turnId && !!state.activeTurnId && turnId !== state.activeTurnId;
  if (foreignTurn && state.runtimeStatus === 'running' && kind !== 'activity_started') return {};
  const bound = bindWorkTurn(state.messages, turnId, at);
  if (bound !== state.messages) {
    state = { ...state, messages: bound };
    next.messages = bound;
  }
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
    const prefix = `thought-${turnId}-`;
    next.messages = state.messages.map((m) => {
      // Only the segment still streaming is completed; an already-finished one
      // must not be overwritten by a later completion's body.
      if (m.type !== 'thought' || !m.id.startsWith(prefix) || m.duration !== 'streaming') {
        return m;
      }
      const duration = m.startedAt !== undefined ? elapsedLabel(m.startedAt, at) : 'done';
      return { ...m, content: body || m.content, duration };
    });
  } else if (kind === 'answer_delta') {
    const text = asText(payload.text);
    if (text) next.messages = appendToAnswer(state.messages, turnId, text, stamp);
  } else if (kind === 'answer_completed') {
    next.messages = completeAnswer(state.messages, turnId, asText(payload.text), stamp);
  } else if (kind === 'tool_batch_started') {
    // One batch is one group: seal whatever is still open and start the next.
    next.messages = openToolGroup(state.messages, turnId, stamp, payload.parallel === true);
    const calls = Array.isArray(payload.calls) ? payload.calls : [];
    if (calls.length > 0) {
      const groupIndex = next.messages.length - 1;
      next.messages = next.messages.map((message, index) => index === groupIndex
        ? { ...message, tools: calls.map((call, callIndex) => toolCallPreviewItem(asRecord(call), callIndex)) }
        : message);
    }
  } else if (kind === 'tool_started' || kind === 'tool_updated') {
    const item = toolItemFromPayload(payload);
    // A late update for a row that already exists (a subagent item refreshed
    // after its group closed) patches that row instead of opening a new group.
    const merged = mergeTurnToolItem(state.messages, turnId, item);
    next.messages =
      merged !== state.messages ? merged : appendToolItem(state.messages, turnId, stamp, item);
  } else if (kind === 'tool_finished') {
    const itemId = asText(payload.item_id);
    if (itemId) {
      const patch: Partial<ToolItemView> = {
        status: asText(payload.status) || 'completed',
        error: payload.error === true,
      };
      const preview = asText(payload.preview);
      if (preview) patch.preview = preview;
      next.messages = patchToolItem(state.messages, turnId, itemId, patch);
    }
  } else if (kind === 'tool_result') {
    const name = asText(payload.name) || 'tool';
    const callId = asText(payload.call_id) || null;
    const status = asText(payload.status) || 'completed';
    const sub = payload.sub === true;
    const patched = patchTurnToolItem(
      state.messages,
      turnId,
      (t) => callId !== null ? t.callId === callId : t.name === name,
      (t) => ({
        ...t,
        status,
        error: status === 'failed' || status === 'error' || t.error,
        sub: t.sub || sub,
      }),
    );
    next.messages =
      patched !== state.messages
        ? patched
        : appendToolItem(state.messages, turnId, stamp, legacyToolItem(name, callId, status, sub));
  } else if (kind === 'tool_batch_finished') {
    next.messages = closeToolGroup(state.messages, turnId);
  } else if (kind === 'subagent_status_changed') {
    const parentId = asText(payload.parent_id);
    if (parentId) {
      const patched = patchToolItem(state.messages, turnId, parentId, {
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
  } else if (isTurnTerminalKind(kind)) {
    next.runtimeStatus = 'idle';
    next.activeTurnId = null;
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

  if (next.messages) {
    const existing = new Set(state.messages.map((m) => m.id));
    let hasAnchor = next.messages.some((m) => m.turnId === turnId && m.work);
    next.messages = next.messages.map((m) => {
      if (existing.has(m.id) || !turnId) return m;
      const work = hasAnchor ? undefined : { startedAt: at, ended: false };
      hasAnchor = true;
      return { ...m, turnId, ...(work ? { work } : {}) };
    });
  }
  if (isTurnTerminalKind(kind)) {
    next.settledTurnIds = [...(state.settledTurnIds ?? []).filter((id) => id !== turnId), turnId].slice(-100);
    next.messages = finishWorkTurn(next.messages ?? state.messages, turnId, at, payload.elapsed_s, kind);
  }
  return next;
}
