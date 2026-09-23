/**
 * Workflow wire surface: request params, strict response checks and pure presentation
 * helpers.
 *
 * Same rules as the other `runtime-client` modules: the wire shapes are the generated
 * contract types (never hand-written here), only the declared keys are accepted, and the
 * formatting helpers are pure so they run under `node --test` without a browser.
 *
 * Two things this module deliberately does **not** do:
 *
 * - It never invents a progress percentage. A dynamic program may still expand into more
 *   calls, so the only truthful progress is the list of calls dispatched so far.
 * - It never turns a blocked resume into a "continue" button. The run's own records decide
 *   whether continuing is safe, and the console only reports that verdict.
 */
import type {
  WorkflowCallView,
  WorkflowDraftView,
  WorkflowLimitsView,
  WorkflowRunPage,
  WorkflowRunView,
  JsonValue,
} from './contract.generated.ts';

export type {
  WorkflowCallView,
  WorkflowDraftView,
  WorkflowLimitsView,
  WorkflowRunPage,
  WorkflowRunView,
};

export class MalformedWorkflowPayloadError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'MalformedWorkflowPayloadError';
  }
}

function asRecord(value: unknown, what: string): Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new MalformedWorkflowPayloadError(`${what} must be an object`);
  }
  return value as Record<string, unknown>;
}

function asString(value: unknown, what: string): string {
  if (typeof value !== 'string') {
    throw new MalformedWorkflowPayloadError(`${what} must be a string`);
  }
  return value;
}

function asBoolean(value: unknown, what: string): boolean {
  if (typeof value !== 'boolean') {
    throw new MalformedWorkflowPayloadError(`${what} must be a boolean`);
  }
  return value;
}

function asNumber(value: unknown, what: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new MalformedWorkflowPayloadError(`${what} must be a finite number`);
  }
  return value;
}

function asStringArray(value: unknown, what: string): string[] {
  if (!Array.isArray(value)) {
    throw new MalformedWorkflowPayloadError(`${what} must be an array`);
  }
  return value.map((item, index) => asString(item, `${what}[${index}]`));
}

function optionalString(value: unknown, what: string): string | null {
  if (value === null || value === undefined) return null;
  return asString(value, what);
}

/**
 * Accept only JSON data for a free-form field.
 *
 * The daemon stores a run's result as JSON, so a value that is not JSON data means the
 * payload is not what the contract says; refusing it here keeps a half-shaped object out of
 * the UI instead of letting it render as `[object Object]`.
 */
function asJsonValue(value: unknown, what: string): JsonValue {
  if (value === undefined) return null;
  if (
    value === null ||
    typeof value === 'string' ||
    typeof value === 'boolean' ||
    (typeof value === 'number' && Number.isFinite(value))
  ) {
    return value;
  }
  if (Array.isArray(value)) {
    return value.map((item, index) => asJsonValue(item, `${what}[${index}]`));
  }
  if (typeof value === 'object') {
    const out: Record<string, JsonValue> = {};
    for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
      out[key] = asJsonValue(item, `${what}.${key}`);
    }
    return out;
  }
  throw new MalformedWorkflowPayloadError(`${what} must be JSON data`);
}

export function parseWorkflowCall(value: unknown, what = 'call'): WorkflowCallView {
  const record = asRecord(value, what);
  return {
    call_key: asString(record.call_key, `${what}.call_key`),
    actor_key: asString(record.actor_key, `${what}.actor_key`),
    role: asString(record.role, `${what}.role`),
    status: asString(record.status, `${what}.status`),
    attempts: asNumber(record.attempts, `${what}.attempts`),
    input_tokens: asNumber(record.input_tokens, `${what}.input_tokens`),
    output_tokens: asNumber(record.output_tokens, `${what}.output_tokens`),
    error: optionalString(record.error, `${what}.error`),
  };
}

export function parseWorkflowRun(value: unknown, what = 'run'): WorkflowRunView {
  const record = asRecord(value, what);
  const calls = record.calls;
  if (!Array.isArray(calls)) {
    throw new MalformedWorkflowPayloadError(`${what}.calls must be an array`);
  }
  return {
    run_id: asString(record.run_id, `${what}.run_id`),
    workflow_id: asString(record.workflow_id, `${what}.workflow_id`),
    project_id: asString(record.project_id, `${what}.project_id`),
    thread_id: asString(record.thread_id, `${what}.thread_id`),
    status: asString(record.status, `${what}.status`),
    active: asBoolean(record.active, `${what}.active`),
    resumable: asBoolean(record.resumable, `${what}.resumable`),
    resume_blockers: asStringArray(record.resume_blockers, `${what}.resume_blockers`),
    blocked_calls: asStringArray(record.blocked_calls, `${what}.blocked_calls`),
    resume_detail: asString(record.resume_detail, `${what}.resume_detail`),
    calls: calls.map((item, index) => parseWorkflowCall(item, `${what}.calls[${index}]`)),
    input_tokens: asNumber(record.input_tokens, `${what}.input_tokens`),
    output_tokens: asNumber(record.output_tokens, `${what}.output_tokens`),
    error: optionalString(record.error, `${what}.error`),
    result: asJsonValue(record.result, `${what}.result`),
    created_at: asString(record.created_at, `${what}.created_at`),
    updated_at: asString(record.updated_at, `${what}.updated_at`),
    finished_at: optionalString(record.finished_at, `${what}.finished_at`),
  };
}

export function parseWorkflowDraft(value: unknown, what = 'draft'): WorkflowDraftView {
  const record = asRecord(value, what);
  return {
    workflow_id: asString(record.workflow_id, `${what}.workflow_id`),
    revision: asNumber(record.revision, `${what}.revision`),
    title: asString(record.title, `${what}.title`),
    goal: asString(record.goal, `${what}.goal`),
    roles: asStringArray(record.roles, `${what}.roles`),
    script_hash: asString(record.script_hash, `${what}.script_hash`),
    status: asString(record.status, `${what}.status`),
    approved: asBoolean(record.approved, `${what}.approved`),
    updated_at: asString(record.updated_at, `${what}.updated_at`),
  };
}

export function parseWorkflowRunPage(value: unknown): WorkflowRunPage {
  const record = asRecord(value, 'page');
  const runs = record.runs;
  if (!Array.isArray(runs)) {
    throw new MalformedWorkflowPayloadError('page.runs must be an array');
  }
  return {
    runs: runs.map((item, index) => parseWorkflowRun(item, `page.runs[${index}]`)),
    total: asNumber(record.total, 'page.total'),
  };
}

export function parseWorkflowRunResult(value: unknown): WorkflowRunView {
  return parseWorkflowRun(asRecord(value, 'result').run, 'result.run');
}

export function parseWorkflowDraftResult(value: unknown): WorkflowDraftView {
  return parseWorkflowDraft(asRecord(value, 'result').draft, 'result.draft');
}

/** The label a status token gets in the console. */
export function workflowStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    running: '运行中',
    waiting_approval: '等待审批',
    cancelling: '取消中',
    cancelled: '已取消',
    completed: '已完成',
    failed: '失败',
    uncertain: '结果不确定',
    draft: '草案',
    approved: '已批准',
    discarded: '已放弃',
  };
  return labels[status] ?? status;
}

export interface WorkflowProgress {
  /** Calls whose result was committed. */
  completed: number;
  /** Calls that failed and cannot be retried implicitly. */
  failed: number;
  /** Calls whose outcome is unknown; a resume is blocked while any exists. */
  uncertain: number;
  /** Calls still running. */
  running: number;
  total: number;
}

/**
 * Progress as counts of *dispatched* calls, never a percentage.
 *
 * A workflow expands as it runs, so "5 of 8" would be a claim the run cannot make. The
 * counts here are what the run actually did.
 */
export function workflowProgress(run: WorkflowRunView): WorkflowProgress {
  const progress: WorkflowProgress = {
    completed: 0,
    failed: 0,
    uncertain: 0,
    running: 0,
    total: run.calls.length,
  };
  for (const call of run.calls) {
    if (call.status === 'completed') progress.completed += 1;
    else if (call.status === 'failed') progress.failed += 1;
    else if (call.status === 'uncertain') progress.uncertain += 1;
    else if (call.status === 'running') progress.running += 1;
  }
  return progress;
}

/** One line summarising a run, for a card or a status chip. */
export function workflowSummary(run: WorkflowRunView): string {
  const progress = workflowProgress(run);
  const parts = [`已完成 ${progress.completed} 项`];
  if (progress.running > 0) parts.push(`进行中 ${progress.running} 项`);
  if (progress.uncertain > 0) parts.push(`结果不确定 ${progress.uncertain} 项`);
  if (progress.failed > 0) parts.push(`失败 ${progress.failed} 项`);
  return parts.join(' · ');
}

/**
 * Whether a run may be continued, and why not when it may not.
 *
 * Returns `null` for a run that may continue, so a caller renders a resume control only
 * when the run's own records allow it.
 */
export function workflowResumeHint(run: WorkflowRunView): string | null {
  if (run.resumable) return null;
  // A stopped run says what it *is*; only an active one has a reason to explain.
  if (!run.active) return `该运行${workflowStatusLabel(run.status)}`;
  return run.resume_detail || '该运行不能自动继续';
}

/** Role translation for subagent roles in workflow steps. */
export function workflowRoleLabel(role: string): string {
  const roles: Record<string, string> = {
    architect: '架构师 (architect)',
    planner: '规划者 (planner)',
    implementer: '实现者 (implementer)',
    reviewer: '审阅者 (reviewer)',
    tester: '测试者 (tester)',
    debugger: '调试者 (debugger)',
    researcher: '研究者 (researcher)',
    'release-manager': '发布管理 (release-manager)',
  };
  return roles[role] ?? role;
}

/** Formats token counts into compact representations (e.g. 1.2k, 15.3k, 1.4M). */
export function formatWorkflowTokens(tokens: number): string {
  if (!Number.isFinite(tokens) || tokens <= 0) return '0';
  if (tokens < 1000) return `${Math.round(tokens)}`;
  if (tokens < 1_000_000) {
    const k = (tokens / 1000).toFixed(1);
    return k.endsWith('.0') ? `${k.slice(0, -2)}k` : `${k}k`;
  }
  const m = (tokens / 1_000_000).toFixed(2);
  const trimmed = m.replace(/\.?0+$/, '');
  return `${trimmed}M`;
}

/** Concise timestamp formatted as HH:mm:ss. */
export function formatWorkflowTime(isoString: string): string {
  if (!isoString) return '';
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) return isoString;
  const h = String(date.getHours()).padStart(2, '0');
  const m = String(date.getMinutes()).padStart(2, '0');
  const s = String(date.getSeconds()).padStart(2, '0');
  return `${h}:${m}:${s}`;
}

/** Format elapsed duration between start and end (or current time if active). */
export function formatWorkflowDuration(createdAt: string, finishedAt: string | null): string {
  if (!createdAt) return '';
  const start = new Date(createdAt).getTime();
  if (Number.isNaN(start)) return '';
  const end = finishedAt ? new Date(finishedAt).getTime() : Date.now();
  if (Number.isNaN(end) || end < start) return '0s';
  const diffSec = Math.floor((end - start) / 1000);
  if (diffSec < 60) return `${diffSec}s`;
  const minutes = Math.floor(diffSec / 60);
  const remainingSec = diffSec % 60;
  if (minutes < 60) return `${minutes}m ${remainingSec}s`;
  const hours = Math.floor(minutes / 60);
  const remainingMin = minutes % 60;
  return `${hours}h ${remainingMin}m`;
}
