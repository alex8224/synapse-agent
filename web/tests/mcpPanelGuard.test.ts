/**
 * Source guards for the MCP panel's truthfulness contract.
 *
 * The panel may only describe attachment from the runtime view helpers (fed by
 * a reload result), never from the configured `enabled` flag, and it must offer
 * the same tool-whitelist save the TUI panel does. The store, in turn, must
 * attach the session's MCP servers on attach instead of leaving the console
 * with a configured-but-empty toolset.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));
const panel = readFileSync(join(here, '..', 'src', 'components', 'McpPanel.tsx'), 'utf8');
const store = readFileSync(join(here, '..', 'src', 'stores', 'useConsoleStore.ts'), 'utf8');

test('the panel derives its phase from the runtime view helpers', () => {
  assert.ok(panel.includes('mcpServerPhase('), 'the panel must use mcpServerPhase');
  assert.ok(panel.includes("'启动中…'"), 'the connecting phase must be rendered');
  assert.ok(panel.includes('未连接'), 'an enabled-but-unattached server must say so');
  assert.ok(
    panel.includes('saveMcpTools('),
    'the panel must be able to persist a tool whitelist (TUI parity)',
  );
  assert.ok(
    panel.includes('refreshMcpRuntime('),
    'the panel must be able to re-attach the enabled servers',
  );
});

test('the store attaches MCP when a session is attached', () => {
  assert.ok(
    store.includes('void refreshMcpRuntime(epoch)'),
    'attachToSession must trigger the MCP attach/refresh',
  );
  assert.ok(
    store.includes('mcpRuntimeKnown'),
    'the store must track whether runtime state was ever reported',
  );
});
