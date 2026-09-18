/**
 * Strict decoders for the read-only Codex usage / reset-credit surface
 * (`runtime.codex.usage.get`, `runtime.codex.reset_credits.get`,
 * `runtime.codex.reset_credits.consume`).
 *
 * Same rule as the git / artifact decoders: only the declared keys are accepted,
 * every value is type-checked, and anything else raises instead of reaching the
 * UI as a half-shaped object.  That is also the redaction rule of this surface:
 * the decoders rebuild each view field by field from the declared keys, so an
 * OAuth credential (an access token, an account id, a `Authorization`-shaped
 * field) can never be forwarded to a component even if a peer starts sending one
 * next to the real fields.
 *
 * Every numeric field is bounded: timestamps are Unix *seconds* (the TUI reads
 * the same endpoints with `time.time()`), a window length is a minute count, and
 * text is length-capped, so one hostile payload cannot make the strip paint an
 * unbounded string or a countdown of `1e300`.
 *
 * `runtime.config.get` gains one *optional* field, `codex_usage_enabled`, and
 * that read is deliberately tolerant (see `readCodexUsageConfig`): a peer that
 * predates the field omits it, and an omitted field means "the entry stays
 * hidden and no usage RPC is ever sent" — never "assume enabled".
 *
 * Pure and dependency-free so it runs under `node --test`, and DOM-free so it
 * stays inside the host-agnostic protocol core.
 */
import type { SessionRef } from './types.ts';
import type {
  CodexConsumeResult,
  CodexResetCredit,
  CodexResetCreditsView,
  CodexUsageView,
  CodexUsageWindow,
  ConsumeCodexResetCommand,
} from './contract.generated.ts';

export type { CodexConsumeResult, CodexResetCreditsView, CodexUsageView };
export type CodexUsageWindowView = CodexUsageWindow;
export type CodexResetCreditView = CodexResetCredit;
export type CodexConsumeOutcome = CodexConsumeResult['outcome'];
/** Narrow the generated command to the only confirmation the wire accepts. */
export type ConsumeCodexResetParams = Omit<ConsumeCodexResetCommand, 'confirmed'> & {
  confirmed: true;
};

/** `runtime.codex.usage.get`. */
export const CODEX_USAGE_METHOD = 'runtime.codex.usage.get';
/** `runtime.codex.reset_credits.get`. */
export const CODEX_RESET_CREDITS_METHOD = 'runtime.codex.reset_credits.get';
/** `runtime.codex.reset_credits.consume` (the only write on this surface). */
export const CODEX_RESET_CONSUME_METHOD = 'runtime.codex.reset_credits.consume';

export class MalformedCodexUsagePayloadError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'MalformedCodexUsagePayloadError';
  }
}

/**
 * Bounds.  Every one of them is a *rejection* threshold, not a clamp: a value
 * outside the range means the payload is not the declared shape.
 */
const MAX_TIMESTAMP_SECONDS = 4_102_444_800; // 2100-01-01T00:00:00Z
const MAX_WINDOW_MINUTES = 527_040; // one year
const MAX_COUNT = 1000;
const MAX_CREDITS = 200;
const MAX_ID_LENGTH = 128;
const MAX_TOKEN_LENGTH = 64;
const MAX_MODEL_LENGTH = 256;
const MAX_TITLE_LENGTH = 256;
const MAX_DESCRIPTION_LENGTH = 1024;

const SESSION_KEYS = ['project_id', 'thread_id'] as const;
const WINDOW_KEYS = ['used_percent', 'window_minutes', 'reset_at'] as const;
const USAGE_KEYS = [
  'session',
  'model',
  'primary',
  'secondary',
  'captured_at',
  'available_reset_count',
] as const;
const CREDIT_KEYS = [
  'id',
  'reset_type',
  'status',
  'granted_at',
  'expires_at',
  'title',
  'description',
] as const;
const CREDITS_KEYS = ['session', 'model', 'available_count', 'credits'] as const;
const CONSUME_KEYS = ['session', 'model', 'command_id', 'outcome'] as const;

const OUTCOMES: readonly CodexConsumeOutcome[] = [
  'reset',
  'alreadyRedeemed',
  'nothingToReset',
  'noCredit',
  'unknown',
];

function asRecord(value: unknown, what: string): Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new MalformedCodexUsagePayloadError(`${what} must be an object`);
  }
  return value as Record<string, unknown>;
}

function exactKeys(record: Record<string, unknown>, keys: readonly string[], what: string): void {
  const actual = Object.keys(record).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length || actual.some((key, i) => key !== expected[i])) {
    throw new MalformedCodexUsagePayloadError(`${what} has unexpected keys`);
  }
}

/** A non-empty string within `max` characters. */
function text(value: unknown, what: string, max: number): string {
  if (typeof value !== 'string' || value === '') {
    throw new MalformedCodexUsagePayloadError(`${what} must be a non-empty string`);
  }
  if (value.length > max) {
    throw new MalformedCodexUsagePayloadError(`${what} is longer than ${max} characters`);
  }
  return value;
}

function nullableText(value: unknown, what: string, max: number): string | null {
  if (value === null) return null;
  return text(value, what, max);
}

/** A finite number `>= 0` (percent, minute count, timestamp seconds). */
function number(value: unknown, what: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) {
    throw new MalformedCodexUsagePayloadError(`${what} must be a finite number >= 0`);
  }
  return value;
}

/** Unix seconds: finite, non-negative and inside a plausible calendar range. */
function timestamp(value: unknown, what: string): number {
  const seconds = number(value, what);
  if (seconds > MAX_TIMESTAMP_SECONDS) {
    throw new MalformedCodexUsagePayloadError(`${what} is not a plausible Unix timestamp`);
  }
  return seconds;
}

function nullableTimestamp(value: unknown, what: string): number | null {
  if (value === null) return null;
  return timestamp(value, what);
}

function count(value: unknown, what: string): number {
  const n = number(value, what);
  if (!Number.isInteger(n) || n > MAX_COUNT) {
    throw new MalformedCodexUsagePayloadError(`${what} must be an integer <= ${MAX_COUNT}`);
  }
  return n;
}

function nullableCount(value: unknown, what: string): number | null {
  if (value === null) return null;
  return count(value, what);
}

function percent(value: unknown, what: string): number | null {
  if (value === null) return null;
  const n = number(value, what);
  if (n > 100) {
    throw new MalformedCodexUsagePayloadError(`${what} must be a percentage <= 100`);
  }
  return n;
}

function windowMinutes(value: unknown, what: string): number | null {
  if (value === null) return null;
  const n = number(value, what);
  if (!Number.isInteger(n) || n <= 0 || n > MAX_WINDOW_MINUTES) {
    throw new MalformedCodexUsagePayloadError(`${what} must be a positive integer <= ${MAX_WINDOW_MINUTES}`);
  }
  return n;
}

function parseSession(value: unknown): SessionRef {
  const record = asRecord(value, 'codex usage session');
  exactKeys(record, SESSION_KEYS, 'codex usage session');
  return {
    project_id: text(record['project_id'], 'session project_id', MAX_ID_LENGTH),
    thread_id: text(record['thread_id'], 'session thread_id', MAX_ID_LENGTH),
  };
}

function parseWindow(value: unknown, what: string): CodexUsageWindowView | null {
  if (value === null) return null;
  const record = asRecord(value, what);
  exactKeys(record, WINDOW_KEYS, what);
  return {
    used_percent: percent(record['used_percent'], `${what} used_percent`),
    window_minutes: windowMinutes(record['window_minutes'], `${what} window_minutes`),
    reset_at: nullableTimestamp(record['reset_at'], `${what} reset_at`),
  };
}

/** Decode one `runtime.codex.usage.get` result. */
export function parseCodexUsageView(payload: unknown): CodexUsageView {
  const record = asRecord(payload, 'codex usage');
  exactKeys(record, USAGE_KEYS, 'codex usage');
  return {
    session: parseSession(record['session']),
    model: text(record['model'], 'codex usage model', MAX_MODEL_LENGTH),
    primary: parseWindow(record['primary'], 'codex usage primary'),
    secondary: parseWindow(record['secondary'], 'codex usage secondary'),
    captured_at: timestamp(record['captured_at'], 'codex usage captured_at'),
    available_reset_count: nullableCount(
      record['available_reset_count'],
      'codex usage available_reset_count',
    ),
  };
}

function parseCredit(value: unknown): CodexResetCreditView {
  const record = asRecord(value, 'codex reset credit');
  exactKeys(record, CREDIT_KEYS, 'codex reset credit');
  return {
    id: text(record['id'], 'credit id', MAX_ID_LENGTH),
    reset_type: text(record['reset_type'], 'credit reset_type', MAX_TOKEN_LENGTH),
    status: text(record['status'], 'credit status', MAX_TOKEN_LENGTH),
    granted_at: nullableTimestamp(record['granted_at'], 'credit granted_at'),
    expires_at: nullableTimestamp(record['expires_at'], 'credit expires_at'),
    title: nullableText(record['title'], 'credit title', MAX_TITLE_LENGTH),
    description: nullableText(record['description'], 'credit description', MAX_DESCRIPTION_LENGTH),
  };
}

/** Decode one `runtime.codex.reset_credits.get` result. */
export function parseCodexResetCreditsView(payload: unknown): CodexResetCreditsView {
  const record = asRecord(payload, 'codex reset credits');
  exactKeys(record, CREDITS_KEYS, 'codex reset credits');
  const raw = record['credits'];
  if (!Array.isArray(raw)) {
    throw new MalformedCodexUsagePayloadError('codex reset credits must be an array');
  }
  if (raw.length > MAX_CREDITS) {
    throw new MalformedCodexUsagePayloadError(`codex reset credits holds more than ${MAX_CREDITS} rows`);
  }
  return {
    session: parseSession(record['session']),
    model: text(record['model'], 'codex reset credits model', MAX_MODEL_LENGTH),
    available_count: count(record['available_count'], 'codex reset credits available_count'),
    credits: raw.map(parseCredit),
  };
}

/** Decode one `runtime.codex.reset_credits.consume` result. */
export function parseCodexConsumeResult(payload: unknown): CodexConsumeResult {
  const record = asRecord(payload, 'codex consume result');
  exactKeys(record, CONSUME_KEYS, 'codex consume result');
  const outcome = record['outcome'];
  // A future outcome verb is *not* forwarded as an open string: the caller has
  // to treat it as "unknown", which is exactly what a parse failure maps to.
  if (typeof outcome !== 'string' || !OUTCOMES.includes(outcome as CodexConsumeOutcome)) {
    throw new MalformedCodexUsagePayloadError('codex consume result outcome is not a known verb');
  }
  return {
    session: parseSession(record['session']),
    model: text(record['model'], 'codex consume model', MAX_MODEL_LENGTH),
    command_id: text(record['command_id'], 'codex consume command_id', MAX_ID_LENGTH),
    outcome: outcome as CodexConsumeOutcome,
  };
}

/** The `runtime.config.get` facts this surface needs. */
export interface CodexUsageConfigGate {
  /**
   * The server decided the session's effective model is an enabled OAuth
   * provider.  Only the literal `true` enables the entry: a peer that predates
   * the field (or sends anything else) keeps it hidden and sends no usage RPC.
   */
  enabled: boolean;
  /** The server's effective model, when it reported a usable one. */
  model: string | null;
}

/**
 * Read the optional `codex_usage_enabled` gate out of a `runtime.config.get`
 * result.
 *
 * Tolerant by design — the field is newer than the rest of the view, so this
 * read never throws and never guesses: an absent/mistyped field is "disabled".
 * The auth verdict is the *server's* (it knows whether the selected profile uses
 * Codex OAuth and whether the provider is usable); the console deliberately does
 * not infer it from a model name.
 */
export function readCodexUsageConfig(view: unknown): CodexUsageConfigGate {
  if (view === null || typeof view !== 'object' || Array.isArray(view)) {
    return { enabled: false, model: null };
  }
  const record = view as Record<string, unknown>;
  const model = record['current_model'];
  return {
    enabled: record['codex_usage_enabled'] === true,
    model:
      typeof model === 'string' && model !== '' && model.length <= MAX_MODEL_LENGTH
        ? model
        : null,
  };
}
