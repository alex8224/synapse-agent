/**
 * Formal console authentication + project-context contract (phase-5 A1/A4).
 *
 * The console is no longer auto-authenticated: `GET /api/bootstrap` is gone
 * (the host must answer 405 and never set a cookie).  The browser first asks
 * `GET /api/session` for an existing session cookie and, when that answers 401,
 * exchanges the one-time pairing code printed by the host on stderr for a
 * session cookie through `POST /api/pair`.
 *
 *   POST /api/pair    {"code": "XXXXXXXX"} -> {"project": {...}} + Set-Cookie
 *   GET  /api/session                      -> {"project": {...}, "expires_in": int}
 *   POST /api/logout                       -> 204
 *
 * Responses never contain the daemon bearer token, environment variables or
 * model secrets.  This client parses payloads strictly and copies only a
 * whitelist of fields, so an over-broad (or accidentally secret-bearing) server
 * response can never leak into console state.
 */

export interface ConsoleProject {
  project_id: string;
  workspace_path: string;
  workspace_name: string | null;
  git_branch: string | null;
}

/** Historical name of the project-context payload (same shape). */
export type BootstrapInfo = ConsoleProject;

export class BootstrapError extends Error {
  readonly status?: number;
  constructor(message: string, status?: number) {
    super(message);
    this.name = 'BootstrapError';
    this.status = status;
  }
}

/**
 * `GET /api/session` answered 401: this browser holds no valid console session
 * yet.  It is the expected "not paired" signal, not a failure.
 */
export class ConsoleAuthRequiredError extends BootstrapError {
  constructor(
    message = 'console session required: pair this browser with the host code',
    status = 401,
  ) {
    super(message, status);
    this.name = 'ConsoleAuthRequiredError';
  }
}

export type FetchLike = (url: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

/** Crockford base32 without I/L/O/U: the host pairing-code alphabet (A8). */
export const PAIRING_CODE_ALPHABET = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';
export const PAIRING_CODE_LENGTH = 8;

/** A3 step 6: the custom header every state-changing console call must carry. */
export const CONSOLE_CSRF_HEADER = 'X-Synapse-Console';
export const CONSOLE_CSRF_HEADER_VALUE = '1';

/**
 * Input-box sanitizer: strips separators, upper-cases, then drops every
 * character outside the Crockford alphabet, so the field can only ever hold a
 * candidate pairing code (A8 / C-09).
 */
export function sanitizePairingCodeInput(raw: string): string {
  const upper = (raw ?? '').toUpperCase().replace(/[\s-]/g, '');
  let out = '';
  for (const ch of upper) {
    if (PAIRING_CODE_ALPHABET.includes(ch)) out += ch;
  }
  return out.slice(0, PAIRING_CODE_LENGTH);
}

/**
 * Strict normalizer used right before a request: separators are removed and the
 * code is upper-cased; anything that is not exactly 8 Crockford characters is
 * rejected (null), so a malformed code is never sent to the host.
 */
export function normalizePairingCode(raw: string): string | null {
  const normalized = (raw ?? '').toUpperCase().replace(/[\s-]/g, '');
  if (normalized.length !== PAIRING_CODE_LENGTH) return null;
  for (const ch of normalized) {
    if (!PAIRING_CODE_ALPHABET.includes(ch)) return null;
  }
  return normalized;
}

function requireString(value: unknown, field: string): string {
  if (typeof value !== 'string' || value.trim().length === 0) {
    throw new BootstrapError(`console payload is missing ${field}`);
  }
  return value.trim();
}

function optionalString(value: unknown): string | null {
  return typeof value === 'string' && value.trim().length > 0 ? value.trim() : null;
}

/**
 * Strict whitelist copy of the `project` object.  Unknown fields (including any
 * accidental `token`) are dropped instead of being forwarded into UI state.
 */
function requireProject(payload: unknown): ConsoleProject {
  if (payload === null || typeof payload !== 'object') {
    throw new BootstrapError('console payload is not an object');
  }
  const project = (payload as { project?: unknown }).project;
  if (project === null || typeof project !== 'object') {
    throw new BootstrapError('console payload is missing project');
  }
  const source = project as Record<string, unknown>;
  return {
    project_id: requireString(source['project_id'], 'project_id'),
    workspace_path: requireString(source['workspace_path'], 'workspace_path'),
    workspace_name: optionalString(source['workspace_name']),
    git_branch: optionalString(source['git_branch']),
  };
}

export interface ConsoleSession {
  project: ConsoleProject;
  /** Server-side session TTL in seconds when advertised; informational only. */
  expires_in: number | null;
}

export function parseConsoleSession(payload: unknown): ConsoleSession {
  const project = requireProject(payload);
  const raw = (payload as { expires_in?: unknown }).expires_in;
  const expires_in = typeof raw === 'number' && Number.isFinite(raw) ? raw : null;
  return { project, expires_in };
}

async function readJson(response: Response, what: string): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    throw new BootstrapError(`${what} returned malformed JSON`, response.status);
  }
}

function networkFailure(what: string, err: unknown): BootstrapError {
  const message = err instanceof Error ? err.message : `${what} request failed`;
  return new BootstrapError(message);
}

/** JSON + CSRF-chain headers shared by both state-changing console endpoints. */
function stateChangingHeaders(): Record<string, string> {
  return {
    Accept: 'application/json',
    'Content-Type': 'application/json',
    [CONSOLE_CSRF_HEADER]: CONSOLE_CSRF_HEADER_VALUE,
  };
}

/** Short static reasons only: never echo the submitted code or a raw response. */
function pairingFailureMessage(status: number): string {
  if (status === 401) return 'pairing code is invalid or expired';
  if (status === 429) return 'too many pairing attempts: wait for a new host code and retry';
  if (status === 403) return 'pairing request rejected (same-origin / CSRF checks failed)';
  if (status === 413) return 'pairing request body is too large';
  if (status === 415) return 'pairing request content type is not supported';
  return `pairing failed with HTTP ${status}`;
}

/** Read-only session probe: restores an already paired browser after a reload. */
export async function fetchConsoleSession(fetchImpl: FetchLike = fetch): Promise<ConsoleSession> {
  let response: Response;
  try {
    response = await fetchImpl('/api/session', {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
    });
  } catch (err) {
    throw networkFailure('session lookup', err);
  }
  if (response.status === 401) throw new ConsoleAuthRequiredError();
  if (!response.ok) {
    throw new BootstrapError(`session lookup failed with HTTP ${response.status}`, response.status);
  }
  return parseConsoleSession(await readJson(response, 'session lookup'));
}

/**
 * Exchange the one-time pairing code for a session cookie.  The code is
 * normalized/validated first, so a malformed code never reaches the host.
 */
export async function pairConsole(
  code: string,
  fetchImpl: FetchLike = fetch,
): Promise<ConsoleProject> {
  const normalized = normalizePairingCode(code);
  if (normalized === null) {
    throw new BootstrapError('pairing code must be 8 Crockford base32 characters');
  }
  let response: Response;
  try {
    response = await fetchImpl('/api/pair', {
      method: 'POST',
      credentials: 'same-origin',
      headers: stateChangingHeaders(),
      body: JSON.stringify({ code: normalized }),
    });
  } catch (err) {
    throw networkFailure('pairing', err);
  }
  if (!response.ok) {
    throw new BootstrapError(pairingFailureMessage(response.status), response.status);
  }
  return requireProject(await readJson(response, 'pairing'));
}

/**
 * Invalidate the console session server-side (all sessions, single-user
 * semantics).  An empty JSON object keeps the request a well-formed
 * `application/json` body for the host CSRF chain.
 */
export async function requestConsoleLogout(fetchImpl: FetchLike = fetch): Promise<void> {
  let response: Response;
  try {
    response = await fetchImpl('/api/logout', {
      method: 'POST',
      credentials: 'same-origin',
      headers: stateChangingHeaders(),
      body: '{}',
    });
  } catch (err) {
    throw networkFailure('logout', err);
  }
  if (!response.ok) {
    throw new BootstrapError(`logout failed with HTTP ${response.status}`, response.status);
  }
}

/**
 * The business socket is always same-origin: scheme/host/port come from the page
 * location and the path is the fixed relay endpoint.  No query string, no
 * fragment and no credential is ever placed in the URL (A5/A6).
 */
export function deriveRuntimeSocketUrl(location: { protocol: string; host: string }): string {
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${scheme}//${location.host}/runtime-ws`;
}
