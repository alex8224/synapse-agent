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
