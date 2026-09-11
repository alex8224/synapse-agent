/**
 * Strict wire decoders for `runtime.session.reconcile` results.
 *
 * The server projects `SessionRecoverabilityView` as one flat object whose
 * field set is fixed.  A malformed, truncated, or cross-version response must
 * never escape as a plausible recovery decision, so every field is type- and
 * bound-checked here before it reaches the store / decision logic.
 */
import type {
  SessionRecoverabilityResult,
  TurnCoverageProbe,
} from './types.ts';

/** Maximum probe turns the server accepts for one snapshot request. */
export const MAX_RECONCILE_PROBE_TURNS = 32;
/** Upper byte bound for one probed turn id. */
export const MAX_RECONCILE_TURN_ID_BYTES = 256;

const RECOVERABILITY_FIELDS = new Set([
  'project_id',
  'thread_id',
  'history_available',
  'history_total_turns',
  'live_epoch',
  'live_latest_sequence',
  'live_oldest_sequence',
  'live_dropped_through',
  'active_turn_id',
  'latest_turn_id',
  'latest_turn_first_sequence',
  'latest_turn_retained_from',
  'latest_turn_intact',
  'probe',
]);

function isNonEmptyString(value: unknown, maxBytes: number): value is string {
  if (typeof value !== 'string' || value.length === 0) return false;
  if (value.includes('\u0000')) return false;
  return new TextEncoder().encode(value).length <= maxBytes;
}

function isNullableString(value: unknown, maxBytes: number): boolean {
  return value === null || isNonEmptyString(value, maxBytes);
}

function isNullableNonNegativeInt(value: unknown): boolean {
  return value === null || (typeof value === 'number' && Number.isInteger(value) && value >= 0);
}

function parseProbe(value: unknown): TurnCoverageProbe {
  if (typeof value !== 'object' || value === null) {
    throw new Error('reconcile probe must be an object');
  }
  const probe = value as Record<string, unknown>;
  if (Object.keys(probe).length !== 2 || typeof probe.covered !== 'boolean') {
    throw new Error('reconcile probe has an invalid shape');
  }
  if (!isNonEmptyString(probe.turn_id, MAX_RECONCILE_TURN_ID_BYTES)) {
    throw new Error('reconcile probe turn_id is invalid');
  }
  return { turn_id: probe.turn_id, covered: probe.covered };
}

/**
 * Strictly decode one `runtime.session.reconcile` result object.
 *
 * Throws on any shape/type/bound violation so the caller can surface the
 * failure as an observable protocol error instead of mis-recovering.
 */
export function parseRecoverabilityResult(value: unknown): SessionRecoverabilityResult {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error('reconcile result must be an object');
  }
  const result = value as Record<string, unknown>;
  const keys = Object.keys(result);
  if (keys.length !== RECOVERABILITY_FIELDS.size || keys.some((key) => !RECOVERABILITY_FIELDS.has(key))) {
    throw new Error('reconcile result has an unexpected field set');
  }
  for (const name of ['history_available', 'latest_turn_intact']) {
    if (typeof result[name] !== 'boolean') {
      throw new Error(`reconcile result field ${name} must be a boolean`);
    }
  }
  for (const name of [
    'history_total_turns',
    'live_latest_sequence',
    'live_oldest_sequence',
    'live_dropped_through',
  ]) {
    const item = result[name];
    if (typeof item !== 'number' || !Number.isInteger(item) || item < 0) {
      throw new Error(`reconcile result field ${name} must be a non-negative integer`);
    }
  }
  for (const name of ['project_id', 'thread_id', 'live_epoch']) {
    if (!isNonEmptyString(result[name], 256)) {
      throw new Error(`reconcile result field ${name} must be a non-empty string`);
    }
  }
  for (const name of ['active_turn_id', 'latest_turn_id']) {
    if (!isNullableString(result[name], 256)) {
      throw new Error(`reconcile result field ${name} must be a string or null`);
    }
  }
  for (const name of ['latest_turn_first_sequence', 'latest_turn_retained_from']) {
    if (!isNullableNonNegativeInt(result[name])) {
      throw new Error(`reconcile result field ${name} must be a non-negative integer or null`);
    }
  }
  if (!Array.isArray(result.probe) || result.probe.length > MAX_RECONCILE_PROBE_TURNS) {
    throw new Error('reconcile probe list is invalid or exceeds the cap');
  }
  return {
    project_id: result.project_id as string,
    thread_id: result.thread_id as string,
    history_available: result.history_available as boolean,
    history_total_turns: result.history_total_turns as number,
    live_epoch: result.live_epoch as string,
    live_latest_sequence: result.live_latest_sequence as number,
    live_oldest_sequence: result.live_oldest_sequence as number,
    live_dropped_through: result.live_dropped_through as number,
    active_turn_id: result.active_turn_id as string | null,
    latest_turn_id: result.latest_turn_id as string | null,
    latest_turn_first_sequence: result.latest_turn_first_sequence as number | null,
    latest_turn_retained_from: result.latest_turn_retained_from as number | null,
    latest_turn_intact: result.latest_turn_intact as boolean,
    probe: result.probe.map(parseProbe),
  };
}

/** Whether a snapshot already reports the given turn id as durable. */
export function isCoveredTurn(
  snapshot: SessionRecoverabilityResult | null,
  turnId: string | null,
): boolean {
  if (!snapshot || !turnId) return false;
  return snapshot.probe.some((probe) => probe.turn_id === turnId && probe.covered);
}
