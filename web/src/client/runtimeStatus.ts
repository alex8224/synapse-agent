import type { FetchLike } from './bootstrap.ts';

/**
 * Read-only runtime diagnostics client (phase-5 C3).
 *
 * The console host serves `GET /api/runtime-status` (session-gated, Host
 * allow-list) with the actionable half of the "runtime daemon is not available"
 * situation: the loopback daemon endpoint it discovered, the state dir
 * `synapse-runtime` has to use, and the `start synapse-runtime ...` hint.  The
 * frozen relay close reason stays untouched, so this endpoint is the only place
 * the browser can read those facts.
 *
 * Boundaries kept by this module (see the C3 task spec):
 *
 * - the request always goes to the fixed same-origin path `RUNTIME_STATUS_PATH`;
 *   nothing from a response (or any earlier response) is ever folded back into
 *   a request URL, query string or body;
 * - only a whitelist of fields is copied into console state, so an over-broad
 *   server response cannot leak anything else into the UI;
 * - every failure (401/403, other HTTP status, network error, malformed body)
 *   degrades to a typed, non-fatal error: the caller keeps its previous copy
 *   and the banner simply stays hidden.
 */

/** Fixed same-origin read-only diagnostics endpoint. */
export const RUNTIME_STATUS_PATH = '/api/runtime-status';

/** Upper bound on every rendered field, so one response cannot flood the UI. */
export const RUNTIME_STATUS_FIELD_LIMIT = 512;

export interface RuntimeEndpoint {
  host: string;
  port: number;
}

/** The whitelisted, non-secret view of the host's runtime diagnostics. */
export interface RuntimeStatusView {
  endpoint: RuntimeEndpoint | null;
  state_dir: string;
  hint: string | null;
}

/**
 * Why the diagnostics read did not produce a view.  All of them are degradable:
 * none is a console error state and none changes the frozen close copy.
 */
export type RuntimeStatusFailureReason = 'unauthorized' | 'http' | 'network' | 'malformed';

export class RuntimeStatusUnavailableError extends Error {
  readonly reason: RuntimeStatusFailureReason;
  readonly status?: number;

  constructor(reason: RuntimeStatusFailureReason, status?: number) {
    super(`runtime status unavailable (${reason})`);
    this.name = 'RuntimeStatusUnavailableError';
    this.reason = reason;
    this.status = status;
  }
}

function capped(value: string): string {
  return value.length > RUNTIME_STATUS_FIELD_LIMIT
    ? value.slice(0, RUNTIME_STATUS_FIELD_LIMIT)
    : value;
}

function optionalText(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  return trimmed === '' ? null : capped(trimmed);
}

/**
 * Strict whitelist copy of one `GET /api/runtime-status` body.  A missing or
 * non-string `state_dir` makes the payload unusable (the host always sends it);
 * an absent/unreadable endpoint degrades to `null` instead, because "the daemon
 * endpoint is unknown" is a real diagnostic answer rather than a broken payload.
 */
export function parseRuntimeStatusPayload(payload: unknown): RuntimeStatusView {
  if (payload === null || typeof payload !== 'object') {
    throw new RuntimeStatusUnavailableError('malformed');
  }
  const runtime = (payload as { runtime?: unknown }).runtime;
  if (runtime === null || typeof runtime !== 'object') {
    throw new RuntimeStatusUnavailableError('malformed');
  }
  const source = runtime as Record<string, unknown>;
  const stateDir = optionalText(source['state_dir']);
  if (stateDir === null) {
    throw new RuntimeStatusUnavailableError('malformed');
  }
  return {
    endpoint: parseEndpoint(source['endpoint']),
    state_dir: stateDir,
    hint: optionalText(source['hint']),
  };
}

function parseEndpoint(value: unknown): RuntimeEndpoint | null {
  if (value === null || typeof value !== 'object') return null;
  const source = value as Record<string, unknown>;
  const host = optionalText(source['host']);
  const port = source['port'];
  if (host === null) return null;
  if (typeof port !== 'number' || !Number.isInteger(port) || port <= 0 || port > 65535) {
    return null;
  }
  return { host, port };
}

/**
 * One read-only diagnostics read.  The request is a plain same-origin GET: no
 * query string, no body, no credential header, no credential in the URL.  A
 * non-2xx answer or an unreadable body is translated into a typed degradable
 * error and never into a thrown console failure.
 */
export async function fetchRuntimeStatus(
  fetchImpl: FetchLike = fetch,
): Promise<RuntimeStatusView> {
  let response: Response;
  try {
    response = await fetchImpl(RUNTIME_STATUS_PATH, {
      method: 'GET',
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
    });
  } catch {
    throw new RuntimeStatusUnavailableError('network');
  }
  if (response.status === 401 || response.status === 403) {
    // Not paired (401) / not the allowed Host (403): the endpoint is gated, and
    // the caller must stay on the previous copy without retrying in a loop.
    throw new RuntimeStatusUnavailableError('unauthorized', response.status);
  }
  if (!response.ok) {
    throw new RuntimeStatusUnavailableError('http', response.status);
  }
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new RuntimeStatusUnavailableError('malformed', response.status);
  }
  return parseRuntimeStatusPayload(payload);
}