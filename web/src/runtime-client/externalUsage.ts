/**
 * Strict decoder for the read-only external ACP usage surface
 * (`runtime.external.usage.get`).
 *
 * Mirrors `codexUsage.ts`: only declared keys are accepted, numeric fields are
 * bounded, and text fields are length-capped so a malformed peer response never
 * reaches the status bar.  DOM-free so it stays inside the protocol core.
 */
import type { SessionRef } from './types.ts';
import type {
  ExternalUsageBucketView,
  ExternalUsageGroupView,
  ExternalUsageView,
  GetExternalUsageParams,
} from './contract.generated.ts';

export type {
  ExternalUsageBucketView,
  ExternalUsageGroupView,
  ExternalUsageView,
  GetExternalUsageParams,
};

/** `runtime.external.usage.get`. */
export const EXTERNAL_USAGE_METHOD = 'runtime.external.usage.get';

export class MalformedExternalUsagePayloadError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'MalformedExternalUsagePayloadError';
  }
}

const MAX_TIMESTAMP_SECONDS = 4_102_444_800; // 2100-01-01T00:00:00Z
const MAX_WINDOW_MINUTES = 527_040;
const MAX_GROUPS = 16;
const MAX_BUCKETS = 16;
const MAX_ID_LENGTH = 128;
const MAX_NAME_LENGTH = 128;
const MAX_DESC_LENGTH = 512;
const MAX_WINDOW_LENGTH = 32;

const SESSION_KEYS = ['project_id', 'thread_id'] as const;
const BUCKET_KEYS = [
  'id',
  'name',
  'window',
  'window_minutes',
  'remaining_percent',
  'reset_at',
] as const;
const GROUP_KEYS = ['name', 'description', 'buckets'] as const;
const VIEW_KEYS = [
  'command_id',
  'session',
  'agent_id',
  'supported',
  'groups',
  'captured_at',
] as const;

function asRecord(value: unknown, what: string): Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new MalformedExternalUsagePayloadError(`${what} must be an object`);
  }
  return value as Record<string, unknown>;
}

function exactKeys(record: Record<string, unknown>, keys: readonly string[], what: string): void {
  const actual = Object.keys(record).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length || actual.some((key, i) => key !== expected[i])) {
    throw new MalformedExternalUsagePayloadError(`${what} has unexpected keys`);
  }
}

function boundedText(value: unknown, what: string, max: number, allowEmpty = false): string {
  if (typeof value !== 'string') {
    throw new MalformedExternalUsagePayloadError(`${what} must be a string`);
  }
  if (!allowEmpty && value.length === 0) {
    throw new MalformedExternalUsagePayloadError(`${what} must not be empty`);
  }
  if (value.includes('\0') || value.length > max) {
    throw new MalformedExternalUsagePayloadError(`${what} is invalid`);
  }
  return value;
}

function optionalText(value: unknown, what: string, max: number): string | null {
  if (value === null) return null;
  return boundedText(value, what, max);
}

function boundedNumber(value: unknown, what: string, max: number): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > max) {
    throw new MalformedExternalUsagePayloadError(`${what} is out of range`);
  }
  return value;
}

function optionalMinutes(value: unknown, what: string): number | null {
  if (value === null) return null;
  if (
    typeof value !== 'number' ||
    !Number.isInteger(value) ||
    value < 1 ||
    value > MAX_WINDOW_MINUTES
  ) {
    throw new MalformedExternalUsagePayloadError(`${what} is invalid`);
  }
  return value;
}

function decodeSession(value: unknown): SessionRef {
  const record = asRecord(value, 'session');
  exactKeys(record, SESSION_KEYS, 'session');
  return {
    project_id: boundedText(record.project_id, 'session.project_id', MAX_ID_LENGTH),
    thread_id: boundedText(record.thread_id, 'session.thread_id', MAX_ID_LENGTH),
  };
}

function decodeBucket(value: unknown): ExternalUsageBucketView {
  const record = asRecord(value, 'bucket');
  exactKeys(record, BUCKET_KEYS, 'bucket');
  return {
    id: boundedText(record.id, 'bucket.id', MAX_ID_LENGTH),
    name: boundedText(record.name, 'bucket.name', MAX_NAME_LENGTH),
    window: boundedText(record.window, 'bucket.window', MAX_WINDOW_LENGTH, true),
    window_minutes: optionalMinutes(record.window_minutes, 'bucket.window_minutes'),
    remaining_percent:
      record.remaining_percent === null
        ? null
        : boundedNumber(record.remaining_percent, 'bucket.remaining_percent', 100),
    reset_at:
      record.reset_at === null
        ? null
        : boundedNumber(record.reset_at, 'bucket.reset_at', MAX_TIMESTAMP_SECONDS),
  };
}

function decodeGroup(value: unknown): ExternalUsageGroupView {
  const record = asRecord(value, 'group');
  exactKeys(record, GROUP_KEYS, 'group');
  if (!Array.isArray(record.buckets) || record.buckets.length > MAX_BUCKETS) {
    throw new MalformedExternalUsagePayloadError('group.buckets is invalid');
  }
  return {
    name: boundedText(record.name, 'group.name', MAX_NAME_LENGTH),
    description: optionalText(record.description, 'group.description', MAX_DESC_LENGTH),
    buckets: record.buckets.map((bucket) => decodeBucket(bucket)),
  };
}

/** Strictly decode one `runtime.external.usage.get` response. */
export function decodeExternalUsageView(raw: unknown): ExternalUsageView {
  const record = asRecord(raw, 'external usage');
  exactKeys(record, VIEW_KEYS, 'external usage');
  if (typeof record.supported !== 'boolean') {
    throw new MalformedExternalUsagePayloadError('external usage.supported must be a boolean');
  }
  if (!Array.isArray(record.groups) || record.groups.length > MAX_GROUPS) {
    throw new MalformedExternalUsagePayloadError('external usage.groups is invalid');
  }
  return {
    command_id: boundedText(record.command_id, 'external usage.command_id', MAX_ID_LENGTH),
    session: decodeSession(record.session),
    agent_id: boundedText(record.agent_id, 'external usage.agent_id', MAX_ID_LENGTH),
    supported: record.supported,
    groups: record.groups.map((group) => decodeGroup(group)),
    captured_at: boundedNumber(
      record.captured_at,
      'external usage.captured_at',
      MAX_TIMESTAMP_SECONDS,
    ),
  };
}
