/**
 * Contract and source guard tests for MCP maintenance settings.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));
const read = (relative: string) => readFileSync(join(here, '..', 'src', relative), 'utf8');

const settingsDialog = read('components/SettingsDialog.tsx');
const mcpSection = read('components/McpMaintenanceSection.tsx');
const runtimeClient = read('runtime-client/SynapseRuntimeClient.ts');

test('SettingsDialog provides tabbed navigation with MCP maintenance', () => {
  assert.ok(settingsDialog.includes('role="tablist"'), 'Settings dialog must have a tablist');
  assert.ok(settingsDialog.includes('常规设置'), 'Settings dialog must have general tab');
  assert.ok(settingsDialog.includes('模型维护'), 'Settings dialog must have model maintenance tab');
  assert.ok(settingsDialog.includes('MCP 维护'), 'Settings dialog must have MCP maintenance tab');
  assert.ok(
    settingsDialog.includes('<McpMaintenanceSection'),
    'Settings dialog must embed McpMaintenanceSection',
  );
  assert.ok(
    settingsDialog.includes("activeTab === 'mcp'"),
    'Settings dialog must support activeTab mcp',
  );
});

test('McpMaintenanceSection uses bundled Fluent icons and shared controls', () => {
  assert.ok(
    mcpSection.includes("from '@fluentui/react-icons'"),
    'MCP maintenance must use Fluent icons',
  );
  assert.ok(
    mcpSection.includes('ui-button'),
    'MCP maintenance must use shared ui-button styling',
  );
  assert.equal(
    /text-white|bg-white|border-white/.test(mcpSection),
    false,
    'MCP maintenance must not hardcode white colors',
  );
});

test('McpMaintenanceSection supports individual start/stop and tool selection', () => {
  assert.ok(
    mcpSection.includes('handleToggle'),
    'MCP maintenance must support individual server toggling',
  );
  assert.ok(
    mcpSection.includes('handleToolToggle'),
    'MCP maintenance must support individual tool selection',
  );
  assert.ok(
    mcpSection.includes('handleSaveTools'),
    'MCP maintenance must support persisting tool whitelist',
  );
  assert.ok(
    mcpSection.includes('<Portal>'),
    'MCP maintenance must open edit/add and delete form in a portalled modal dialog',
  );
});

test('SynapseRuntimeClient exposes all MCP maintenance RPC methods', () => {
  const methods = ['mcpList', 'mcpSave', 'mcpDelete'];
  for (const method of methods) {
    assert.ok(
      runtimeClient.includes(`public async ${method}`),
      `SynapseRuntimeClient must declare ${method}`,
    );
  }
});
