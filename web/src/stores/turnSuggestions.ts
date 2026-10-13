/**
 * Post-turn action suggestion engine (Scheme B: deterministic rules + side-channel
 * model suggestions, merged and deduplicated).
 *
 * Pure and DOM-free so every rule, snapshot extractor, and merge priority can be
 * exercised directly under `node --test`.
 */
import type { TranscriptMessage } from './historyMapper.ts';
import type { SessionGoalView } from './goalView.ts';
import { latestTodos, type TodoView } from './todoView.ts';

export type SuggestedActionMode = 'submit' | 'fill';
export type SuggestedActionSource = 'rule' | 'model';
export type TurnOutcomeKind = 'completed' | 'failed' | 'cancelled';

export interface SuggestedAction {
  id: string;
  label: string;
  prompt: string;
  source: SuggestedActionSource;
  priority: number;
  mode: SuggestedActionMode;
}

export interface ModelSuggestionInput {
  label: string;
  prompt: string;
  mode?: SuggestedActionMode;
}

export interface LastTurnSnapshot {
  /** Stable key identifying this settled turn (`turnId` or anchor message id). */
  turnKey: string;
  turnId: string | null;
  userPrompt: string;
  conclusion: string;
  outcome: TurnOutcomeKind;
  hasChanges: boolean;
  ranTests: boolean;
  hasToolError: boolean;
}

export const MAX_SUGGESTED_ACTIONS = 4;

const CONFIRMATION_TAIL_RE =
  /(?:是否(?:需要|确认|继续|开始|同意|执行|修改)|需要我(?:继续|开始|实现|修改)|请确认|确认后|可以开始(?:实现|修改|执行)?吗|要我(?:现在|继续|开始)|要不要(?:我|继续|开始)|shall i\b|would you like me to\b|do you want me to\b|should i proceed\b)/i;

const TEST_COMMAND_RE = /\b(?:pytest|npm\s+test|node\s+--test|cargo\s+test|unittest|vitest|jest)\b/i;

function normalizeKey(text: string): string {
  return (text ?? '')
    .trim()
    .toLowerCase()
    .replace(/[\s\p{P}\p{S}]+/gu, '');
}

/** Canonical bucket for common synonymous labels so rule + model don't duplicate. */
function canonicalActionBucket(label: string): string {
  const norm = normalizeKey(label);
  if (norm === '继续' || norm === '继续执行' || norm === '继续推进') {
    return '__continue__';
  }
  if (
    norm === '运行测试' ||
    norm === '运行测试验证' ||
    norm === '跑测试' ||
    norm === '执行测试' ||
    norm === '运行单测'
  ) {
    return '__run_tests__';
  }
  if (
    norm === '确认' ||
    norm === '确认执行' ||
    norm === '确认并执行' ||
    norm === '按方案执行' ||
    norm === '开始执行'
  ) {
    return '__confirm_execute__';
  }
  if (norm === '重试' || norm === '重试本轮' || norm === '重新执行') {
    return '__retry__';
  }
  return norm;
}

/**
 * Inspect the tail of `messages` and return a snapshot of the last settled turn,
 * or `null` while the session is empty or a turn is still in flight.
 */
export function extractLastTurnSnapshot(
  messages: readonly TranscriptMessage[],
  runtimeStatus: 'idle' | 'running' | string,
  pendingApproval: unknown = null,
): LastTurnSnapshot | null {
  if (runtimeStatus === 'running' || pendingApproval !== null || messages.length === 0) {
    return null;
  }

  let userIndex = -1;
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const msg = messages[i];
    if (msg.type === 'user' && !msg.steer) {
      userIndex = i;
      break;
    }
  }
  if (userIndex === -1 || userIndex === messages.length - 1) {
    return null;
  }

  const userMsg = messages[userIndex];
  const span = messages.slice(userIndex + 1);

  for (const row of span) {
    if (row.streaming === true || row.duration === 'streaming') {
      return null;
    }
  }

  let conclusion = '';
  let hasChanges = false;
  let ranTests = false;
  let hasToolError = false;
  let hasFailNotice = false;
  let hasCancelledTool = false;

  for (const row of span) {
    if (row.type === 'assistant' && (row.content ?? '').trim() !== '') {
      conclusion = (row.content ?? '').trim();
    } else if (row.type === 'changes' && (row.changes?.length ?? 0) > 0) {
      hasChanges = true;
    } else if (row.type === 'info' && row.infoLevel === 'warning' && row.id.startsWith('fail-')) {
      hasFailNotice = true;
    } else if (row.type === 'tool_group' && row.tools) {
      for (const tool of row.tools) {
        if (
          (tool.category === 'edit' || tool.category === 'write') &&
          !tool.error &&
          tool.status === 'completed'
        ) {
          hasChanges = true;
        }
        if (tool.error || tool.status === 'failed') hasToolError = true;
        if (tool.status === 'cancelled') hasCancelledTool = true;
        if (
          tool.subagentName === 'tester' ||
          TEST_COMMAND_RE.test(tool.label ?? '') ||
          TEST_COMMAND_RE.test(tool.preview ?? '')
        ) {
          ranTests = true;
        }
      }
    }
  }

  let outcome: TurnOutcomeKind = 'completed';
  if (hasFailNotice || (conclusion === '' && hasToolError)) {
    outcome = 'failed';
  } else if (conclusion === '' && hasCancelledTool) {
    outcome = 'cancelled';
  }

  const turnId = userMsg.turnId ?? span.find((r) => r.turnId)?.turnId ?? null;
  const turnKey = turnId ?? userMsg.id;

  return {
    turnKey,
    turnId,
    userPrompt: (userMsg.content ?? '').trim(),
    conclusion,
    outcome,
    hasChanges,
    ranTests,
    hasToolError,
  };
}

/**
 * Deterministic rule engine: derives zero-latency actions from the settled turn's
 * state, todos, goal, workspace changes, and conclusion phrasing.
 */
export function buildRuleSuggestions(
  snapshot: LastTurnSnapshot,
  todos: TodoView | null = null,
  goal: SessionGoalView | null = null,
): SuggestedAction[] {
  const actions: SuggestedAction[] = [];

  if (snapshot.outcome === 'failed' || snapshot.outcome === 'cancelled') {
    actions.push({
      id: 'rule-continue',
      label: '继续',
      prompt: '请继续完成刚才中断或未完成的步骤',
      source: 'rule',
      priority: 100,
      mode: 'submit',
    });
    if (snapshot.userPrompt) {
      actions.push({
        id: 'rule-retry',
        label: '重试本轮',
        prompt: snapshot.userPrompt,
        source: 'rule',
        priority: 95,
        mode: 'submit',
      });
    }
    return actions;
  }

  const tail = snapshot.conclusion.slice(-320);
  if (tail && CONFIRMATION_TAIL_RE.test(tail)) {
    actions.push({
      id: 'rule-confirm',
      label: '确认执行',
      prompt: '确认，请按上述方案继续执行',
      source: 'rule',
      priority: 90,
      mode: 'submit',
    });
  }

  const remainingTodos = (todos?.active ?? 0) + (todos?.pending ?? 0);
  if (remainingTodos > 0) {
    actions.push({
      id: 'rule-todos',
      label: `继续待办 (${remainingTodos})`,
      prompt: '请继续完成待办列表中剩余的任务',
      source: 'rule',
      priority: 88,
      mode: 'submit',
    });
  }

  if (goal && goal.status === 'paused') {
    actions.push({
      id: 'rule-goal',
      label: '继续推进目标',
      prompt: '请继续推进当前会话的长程目标',
      source: 'rule',
      priority: 86,
      mode: 'submit',
    });
  }

  if (snapshot.hasChanges && !snapshot.ranTests) {
    actions.push({
      id: 'rule-test',
      label: '运行测试验证',
      prompt: '请运行与本轮代码改动相关的单元测试进行验证',
      source: 'rule',
      priority: 76,
      mode: 'submit',
    });
    actions.push({
      id: 'rule-review',
      label: '检查本轮改动',
      prompt: '请审查本轮修改的代码，检查是否有边界遗漏或回归风险',
      source: 'rule',
      priority: 65,
      mode: 'submit',
    });
  } else if (snapshot.hasToolError) {
    actions.push({
      id: 'rule-fix-error',
      label: '修复工具报错',
      prompt: '请分析并解决本轮执行过程中出现的工具错误',
      source: 'rule',
      priority: 78,
      mode: 'submit',
    });
  }

  actions.push({
    id: 'rule-continue-default',
    label: '继续',
    prompt: '继续',
    source: 'rule',
    priority: 40,
    mode: 'submit',
  });

  return actions;
}

/** Convert validated model suggestion DTOs into ranked `SuggestedAction` items. */
export function normalizeModelSuggestions(
  items: readonly ModelSuggestionInput[],
): SuggestedAction[] {
  const out: SuggestedAction[] = [];
  for (let i = 0; i < items.length; i += 1) {
    const item = items[i];
    const label = (item?.label ?? '').trim();
    const prompt = (item?.prompt ?? '').trim();
    if (!label || !prompt) continue;
    out.push({
      id: `model-${i + 1}-${normalizeKey(label).slice(0, 16) || i}`,
      label: label.length > 24 ? `${label.slice(0, 23).trimEnd()}…` : label,
      prompt: prompt.slice(0, 240),
      source: 'model',
      // Contextual model actions rank above generic workspace/fallback rules (<=78)
      // while urgent state rules (failed/cancelled/confirmation/todos >=86) stay first.
      priority: 84 - i,
      mode: item.mode === 'fill' ? 'fill' : 'submit',
    });
  }
  return out;
}

/**
 * Merge fixed rule suggestions and model-generated suggestions:
 * sorts by descending priority, deduplicates by canonical label bucket and prompt,
 * and caps at `maxItems`.
 */
export function mergeSuggestedActions(
  ruleActions: readonly SuggestedAction[],
  modelActions: readonly SuggestedAction[],
  maxItems: number = MAX_SUGGESTED_ACTIONS,
): SuggestedAction[] {
  const cap = Math.max(1, Math.trunc(maxItems || MAX_SUGGESTED_ACTIONS));
  const combined = [...ruleActions, ...modelActions].sort((a, b) => b.priority - a.priority);

  const result: SuggestedAction[] = [];
  const seenBuckets = new Set<string>();
  const seenPrompts = new Set<string>();

  for (const action of combined) {
    const label = action.label.trim();
    const prompt = action.prompt.trim();
    if (!label || !prompt) continue;

    const bucket = canonicalActionBucket(label);
    const promptKey = normalizeKey(prompt);
    if (!bucket || !promptKey) continue;
    if (seenBuckets.has(bucket) || seenPrompts.has(promptKey)) {
      continue;
    }

    seenBuckets.add(bucket);
    seenPrompts.add(promptKey);
    result.push(action);
    if (result.length >= cap) {
      break;
    }
  }

  return result;
}

/** Convenience helper to compute the baseline rule actions directly from store state. */
export function computeTurnSuggestionsFromState(
  messages: readonly TranscriptMessage[],
  runtimeStatus: 'idle' | 'running' | string,
  pendingApproval: unknown = null,
  goal: SessionGoalView | null = null,
  modelInputs: readonly ModelSuggestionInput[] = [],
): { snapshot: LastTurnSnapshot | null; actions: SuggestedAction[] } {
  const snapshot = extractLastTurnSnapshot(messages, runtimeStatus, pendingApproval);
  if (snapshot === null) {
    return { snapshot: null, actions: [] };
  }
  const todos = latestTodos(messages);
  const ruleActions = buildRuleSuggestions(snapshot, todos, goal);
  const modelActions = normalizeModelSuggestions(modelInputs);
  return {
    snapshot,
    actions: mergeSuggestedActions(ruleActions, modelActions),
  };
}
