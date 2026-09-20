/**
 * Strict decoders and pure helpers for the window-capture surface
 * (`runtime.screenshot.status` / `.settings.open` / `.capture` / `.cancel`).
 *
 * Same rule as the git and external-app decoders: only the declared keys are
 * accepted, every value is type-checked, and anything else raises instead of
 * reaching the UI as a half-shaped object.  Pure and dependency-free so it runs
 * under `node --test`.
 *
 * The console never talks to the capture tool: this module only shapes what the
 * runtime already derived (availability, a task state, finalized attachment
 * ids).  The helpers below are the console's *presentation* policy — the
 * bounded settings defaults, the state labels, the named error wording — never
 * a second source of truth about what the tool can do.
 */

import type { ScreenshotSettings } from './contract.generated.ts';

/** The closed set of capture task states the console renders. */
export type ScreenshotTaskState =
  | 'idle'
  | 'queued'
  | 'running'
  | 'completed'
  | 'cancelled'
  | 'failed'
  | 'target_required';

const SCREENSHOT_TASK_STATES: readonly ScreenshotTaskState[] = [
  'idle',
  'queued',
  'running',
  'completed',
  'cancelled',
  'failed',
  'target_required',
];

/** One console capture never becomes more than this many composer images. */
export const SCREENSHOT_MAX_FRAMES = 8;
/** Mirrors the Python decoder's count ceiling (`MAX_SCREENSHOT_COUNT`). */
export const SCREENSHOT_MAX_COUNT = 600;

/** The bounded capture parameters, flattened for the console's own settings. */
export interface ScreenshotSettingsView {
  count: number;
  intervalMs: number;
  startDelayMs: number;
  maxEdge: number;
  ttlSeconds: number;
  frameTimeoutMs: number;
  allowReuse: boolean;
}

/** The tool's own defaults (mirrors the Python `ScreenshotSettings` defaults). */
export const DEFAULT_SCREENSHOT_SETTINGS: ScreenshotSettingsView = {
  count: 1,
  intervalMs: 250,
  startDelayMs: 0,
  maxEdge: 0,
  ttlSeconds: 300,
  frameTimeoutMs: 5000,
  allowReuse: true,
};

/** One finalized screenshot frame as the composer references it. */
export interface ScreenshotFrameView {
  attachmentId: string;
  name: string;
  mime: string;
  size: number;
  revision: string | null;
}

/** The resident capture task snapshot the console renders. */
export interface ScreenshotStatusView {
  taskId: string;
  state: ScreenshotTaskState;
  requested: number;
  captured: number;
  attachments: ScreenshotFrameView[];
  /** Whether the host capture tool is runnable at all (build present, Windows). */
  available: boolean;
  /** Why the tool is unavailable, when it is. */
  unavailableReason: string;
  errorCode: string | null;
  errorMessage: string | null;
}

/** The resident tool status the console renders. */
export interface ScreenshotToolView {
  available: boolean;
  platform: string;
  reason: string;
  version: string | null;
  busy: boolean;
  activeTaskId: string | null;
}

/** The immediate snapshot returned when a capture is queued. */
export interface ScreenshotStartView {
  taskId: string;
  state: ScreenshotTaskState;
  requested: number;
}

/** Where a capture task was started, so a result can be bound to it. */
export interface ScreenshotOrigin {
  projectId: string;
  threadId: string;
  generation: number;
}

/** A wire projection the decoders rejected (never a raw payload). */
export class MalformedScreenshotError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'MalformedScreenshotError';
  }
}

function asRecord(value: unknown, what: string): Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new MalformedScreenshotError(`${what} must be an object`);
  }
  return value as Record<string, unknown>;
}

function exactKeys(record: Record<string, unknown>, keys: readonly string[], what: string): void {
  const actual = Object.keys(record).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length || actual.some((key, i) => key !== expected[i])) {
    throw new MalformedScreenshotError(`${what} has unexpected keys`);
  }
}

function text(value: unknown, what: string): string {
  if (typeof value !== 'string') {
    throw new MalformedScreenshotError(`${what} must be a string`);
  }
  return value;
}

function flag(value: unknown, what: string): boolean {
  if (typeof value !== 'boolean') {
    throw new MalformedScreenshotError(`${what} must be a boolean`);
  }
  return value;
}

function count(value: unknown, what: string): number {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 0) {
    throw new MalformedScreenshotError(`${what} must be a non-negative integer`);
  }
  return value;
}

function nullableText(value: unknown, what: string): string | null {
  if (value === null) return null;
  return text(value, what);
}

function state(value: unknown, what: string): ScreenshotTaskState {
  const raw = text(value, what);
  if (!SCREENSHOT_TASK_STATES.includes(raw as ScreenshotTaskState)) {
    throw new MalformedScreenshotError(`${what} is not a known capture state`);
  }
  return raw as ScreenshotTaskState;
}

/** Whether a task state can no longer change. */
export function isTerminalScreenshotState(value: ScreenshotTaskState): boolean {
  return value === 'completed' || value === 'cancelled' || value === 'failed' || value === 'target_required';
}

function parseFrame(value: unknown, what: string): ScreenshotFrameView {
  const record = asRecord(value, what);
  exactKeys(record, ['attachment_id', 'name', 'mime', 'size', 'revision'], what);
  return {
    attachmentId: text(record['attachment_id'], `${what}.attachment_id`),
    name: text(record['name'], `${what}.name`),
    mime: text(record['mime'], `${what}.mime`),
    size: count(record['size'], `${what}.size`),
    revision: nullableText(record['revision'], `${what}.revision`),
  };
}

/** Decode one `runtime.screenshot.status` result. */
export function parseScreenshotStatus(raw: unknown): ScreenshotStatusView {
  const record = asRecord(raw, 'screenshot status');
  exactKeys(
    record,
    [
      'session',
      'task_id',
      'state',
      'requested',
      'captured',
      'attachments',
      'available',
      'unavailable_reason',
      'error_code',
      'error_message',
    ],
    'screenshot status',
  );
  const attachments = record['attachments'];
  if (!Array.isArray(attachments)) {
    throw new MalformedScreenshotError('screenshot status.attachments must be an array');
  }
  return {
    taskId: text(record['task_id'], 'screenshot status.task_id'),
    state: state(record['state'], 'screenshot status.state'),
    requested: count(record['requested'], 'screenshot status.requested'),
    captured: count(record['captured'], 'screenshot status.captured'),
    attachments: attachments.map((item, index) => parseFrame(item, `screenshot status.attachments[${index}]`)),
    available: flag(record['available'], 'screenshot status.available'),
    unavailableReason: text(record['unavailable_reason'], 'screenshot status.unavailable_reason'),
    errorCode: nullableText(record['error_code'], 'screenshot status.error_code'),
    errorMessage: nullableText(record['error_message'], 'screenshot status.error_message'),
  };
}

/** Decode one `runtime.screenshot.settings.open` result (the tool status). */
export function parseScreenshotToolStatus(raw: unknown): ScreenshotToolView {
  const record = asRecord(raw, 'screenshot tool status');
  exactKeys(
    record,
    ['available', 'platform', 'reason', 'version', 'busy', 'active_task_id'],
    'screenshot tool status',
  );
  return {
    available: flag(record['available'], 'screenshot tool status.available'),
    platform: text(record['platform'], 'screenshot tool status.platform'),
    reason: text(record['reason'], 'screenshot tool status.reason'),
    version: nullableText(record['version'], 'screenshot tool status.version'),
    busy: flag(record['busy'], 'screenshot tool status.busy'),
    activeTaskId: nullableText(record['active_task_id'], 'screenshot tool status.active_task_id'),
  };
}

/** Decode one `runtime.screenshot.capture` result. */
export function parseScreenshotStart(raw: unknown): ScreenshotStartView {
  const record = asRecord(raw, 'screenshot capture');
  exactKeys(record, ['session', 'task_id', 'state', 'requested', 'settings'], 'screenshot capture');
  return {
    taskId: text(record['task_id'], 'screenshot capture.task_id'),
    state: state(record['state'], 'screenshot capture.state'),
    requested: count(record['requested'], 'screenshot capture.requested'),
  };
}

/** Decode one `runtime.screenshot.cancel` result. */
export function parseScreenshotCancel(raw: unknown): { taskId: string; state: ScreenshotTaskState; cancelled: boolean } {
  const record = asRecord(raw, 'screenshot cancel');
  exactKeys(record, ['session', 'task_id', 'state', 'cancelled'], 'screenshot cancel');
  return {
    taskId: text(record['task_id'], 'screenshot cancel.task_id'),
    state: state(record['state'], 'screenshot cancel.state'),
    cancelled: flag(record['cancelled'], 'screenshot cancel.cancelled'),
  };
}

/** Project the console's settings onto the wire object (every member bounded). */
export function toWireSettings(view: ScreenshotSettingsView): ScreenshotSettings {
  return {
    count: clampCount(view.count),
    interval_ms: Math.max(0, Math.trunc(view.intervalMs)),
    start_delay_ms: Math.max(0, Math.trunc(view.startDelayMs)),
    max_edge: Math.max(0, Math.trunc(view.maxEdge)),
    ttl_seconds: Math.max(1, Math.trunc(view.ttlSeconds)),
    frame_timeout_ms: Math.max(0, Math.trunc(view.frameTimeoutMs)),
    allow_reuse: view.allowReuse,
  };
}

/** Clamp a requested count into the tool's range. */
export function clampCount(count: number): number {
  if (!Number.isFinite(count)) return 1;
  return Math.min(SCREENSHOT_MAX_COUNT, Math.max(1, Math.trunc(count)));
}

/** The name the tool's refusal should render as, in the console's own words. */
export function screenshotErrorMessage(code: string | null, fallback: string): string {
  switch (code) {
    case 'target_required':
      return '尚未选择窗口：请在截图工具中选择后重试';
    case 'target_closed':
      return '目标窗口已关闭，截图失败';
    case 'target_minimized':
      return '目标窗口已最小化，截图失败';
    case 'target_not_found':
      return '找不到目标窗口，请重新选择';
    case 'target_identity_mismatch':
      return '目标窗口已变化，请重新选择';
    case 'capture_busy':
      return '截图工具正忙，请稍后重试';
    case 'capture_failed':
      return '截图失败';
    case 'no_frame':
      return '未能截取到画面';
    case 'no_new_frame':
      return '目标窗口没有产生新画面';
    case 'timeout':
      return '截图超时';
    case 'screenshot_unavailable':
      return '窗口截图工具不可用';
    case 'screenshot_failed':
      return '截图任务失败';
    default:
      return fallback;
  }
}

/** The short label the banner paints for one state. */
export function screenshotStateLabel(value: ScreenshotTaskState): string {
  switch (value) {
    case 'idle':
      return '空闲';
    case 'queued':
      return '排队中';
    case 'running':
      return '截图中';
    case 'completed':
      return '已完成';
    case 'cancelled':
      return '已取消';
    case 'failed':
      return '失败';
    case 'target_required':
      return '等待选择窗口';
  }
}

/**
 * Whether a finished result still belongs to the draft it was started from.
 *
 * A result may only be filled into the composer when the reader is still in the
 * originating session *and* has not sent a new draft since (the generation
 * changed).  Otherwise it stays a visible, confirmable result instead of being
 * dropped into a fresh draft the reader never asked to fill.
 */
export function sameScreenshotOrigin(
  origin: ScreenshotOrigin,
  current: ScreenshotOrigin,
): boolean {
  return (
    origin.projectId === current.projectId &&
    origin.threadId === current.threadId &&
    origin.generation === current.generation
  );
}
