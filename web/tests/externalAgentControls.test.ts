import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

/**
 * The console must never paint an external agent's session with the native
 * model pickers: those read this console's model registry, which knows nothing
 * about the peer. These guards pin the dispatch and the two rules the wire
 * contract depends on — the peer's snapshot replaces the whole set, and a
 * `full_access` launch decision is shown, never toggled from the browser.
 */
const controls = readFileSync(new URL('../src/components/ModelControls.tsx', import.meta.url), 'utf8');
const external = readFileSync(
  new URL('../src/components/ExternalAgentControls.tsx', import.meta.url),
  'utf8',
);
const store = readFileSync(new URL('../src/stores/useConsoleStore.ts', import.meta.url), 'utf8');

test('the composer dispatches to the external controls for an external session', () => {
  assert.match(controls, /state\.externalSession/);
  assert.match(controls, /<ExternalAgentControls session=\{externalSession\}/);
  assert.match(controls, /<NativeModelControls \/>/);
});

test('an external switch adopts the whole snapshot and never patches one value', () => {
  assert.match(store, /options: result\.options,/);
  assert.doesNotMatch(store, /options: externalSession\.options\.map/);
  assert.match(external, /setExternalConfig\(configId, value\)/);
  assert.match(external, /void choose\(option\.id, value\.value\)/);
});

test('full access is displayed, not granted from the console', () => {
  assert.match(external, /session\.permissionMode === 'full_access'/);
  assert.doesNotMatch(external, /permission_mode\s*=|setExternalConfig\([^)]*permission/i);
  assert.doesNotMatch(external, /dangerously-skip-permissions/);
});

test('boolean options are selectable and session switches fence config writes and attach', () => {
  assert.match(external, /option\.bool_value !== null/);
  assert.match(external, /void choose\(option\.id, flag\)/);
  assert.match(store, /isExternalOpenRejection\(err\)/);
  assert.match(store, /await attachExternalSession\(client, session, epoch\)/);
  assert.match(store, /get\(\)\.currentSession\.thread_id !== targetSession\.thread_id/);
});

test('a slow external open reports the wait instead of the native pickers', () => {
  // The launch window is the peer's own `session/new`, which an agent may spend
  // tens of seconds on; the native pickers would be a lie for that session.
  assert.match(controls, /state\.externalPending/);
  assert.match(controls, /if \(externalPending !== null \|\| externalError !== null\)/);
  assert.match(controls, /<ExternalAgentStatus agentName=\{agentName\} error=\{externalError\} \/>/);
  assert.match(external, /export const ExternalAgentStatus/);
  assert.match(external, /正在连接/);
  assert.match(external, /role="status"/);
});

test('a failed external open stays visible after the wait ends', () => {
  // The error used to render only inside the controls that appear once the peer
  // answers, so a failed open left the composer looking idle.
  assert.match(controls, /state\.externalSessionError/);
  assert.match(external, /role="alert"/);
  assert.match(external, /error === null \?/);
  assert.match(store, /externalSessionError:\s*\n?\s*agentId === undefined/);
  assert.match(store, /EXTERNAL_CONNECT_FALLBACK/);
});

test('the pending window is set on both open paths and always cleared', () => {
  // Create: the sidebar picked the agent, so the wait can name it.
  assert.match(store, /externalPending: agentId === undefined \? null : \{ agentId \}/);
  // Resume: the agent id only arrives with the reply.
  assert.match(
    store,
    /store\.setState\(\{ externalPending: \{ agentId: null \}, externalSessionError: null \}\)/,
  );
  // Success, failure and every session switch clear it, or the row would keep
  // claiming a connection that already ended.
  assert.ok((store.match(/externalPending: null/g) ?? []).length >= 4);
});

test('external open rejection matches the wire service_code rather than stripped error text', () => {
  // The JSON-RPC transport strips exception text to "runtime service error",
  // so refresh / re-enter must detect external bindings by `service_code`.
  assert.match(store, /rpc\?\.service_code === 'external_session'/);
});
