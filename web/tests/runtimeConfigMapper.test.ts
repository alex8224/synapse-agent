/**
 * Focused tests for the `runtime.config.get` -> console mapper and the
 * read-only capability helpers using only the Node built-in test runner.
 *
 * They cover the wire contract of `runtime.config.get` (whitelisted fields),
 * the model-preserving refresh behavior, MCP status derivation, and the fact
 * that the mapper never fabricates `attached`/goal state.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  RUNTIME_CONFIG_READ_ONLY_NOTICE,
  mapRuntimeConfig,
  mcpStatusLabel,
} from '../src/stores/runtimeConfigMapper.ts';
import type { RuntimeConfigResult } from '../src/client/types.ts';

const CONFIG: RuntimeConfigResult = {
  current_model: 'openai:alpha',
  available_models: ['openai:alpha', 'openai:beta'],
  thinking_level: 'high',
  thinking_levels: ['off', 'low', 'high'],
  mcp_servers: [
    { name: 'files', transport: 'stdio', enabled: true, tool_prefix: 'mcp__files' },
    { name: 'git', transport: 'streamable_http', enabled: false, tool_prefix: null },
  ],
  mcp_enabled: true,
  can_set_thinking: false,
  can_toggle_mcp_global: false,
};

test('mapRuntimeConfig maps every whitelisted field (no preserveModel)', () => {
  const patch = mapRuntimeConfig(CONFIG);
  assert.equal(patch.modelName, 'openai:alpha');
  assert.deepEqual(patch.availableModels, ['openai:alpha', 'openai:beta']);
  assert.equal(patch.thinkingLevel, 'high');
  assert.deepEqual(patch.thinkingLevels, ['off', 'low', 'high']);
  assert.equal(patch.mcpEnabled, true);
  assert.equal(patch.canSetThinking, false);
  assert.equal(patch.canToggleMcpGlobal, false);
  assert.equal(patch.mcpStatus, '1 on');
  assert.deepEqual(patch.mcpServers, [
    { name: 'files', transport: 'stdio', enabled: true, toolPrefix: 'mcp__files' },
    { name: 'git', transport: 'streamable_http', enabled: false, toolPrefix: null },
  ]);
});

test('mapRuntimeConfig preserves the session model when preserveModel is set', () => {
  const patch = mapRuntimeConfig(
    { ...CONFIG, current_model: 'project-default' },
    { preserveModel: true },
  );
  assert.ok(!('modelName' in patch), 'refresh must never overwrite the session model');
  assert.deepEqual(patch.availableModels, ['openai:alpha', 'openai:beta']);
});

test('mapRuntimeConfig never fabricates attached or goal state', () => {
  const patch = mapRuntimeConfig(CONFIG);
  for (const server of patch.mcpServers ?? []) {
    assert.ok(!('attached' in server), 'config projection must not claim attachment');
    assert.ok(!('goal' in server));
  }
  assert.ok(!('activeGoal' in patch));
});

test('mapRuntimeConfig mirrors a null thinking level without inventing one', () => {
  const patch = mapRuntimeConfig({ ...CONFIG, thinking_level: null });
  assert.equal(patch.thinkingLevel, null);
});

test('mapRuntimeConfig maps read-only capability flags for the UI', () => {
  const patch = mapRuntimeConfig({
    ...CONFIG,
    can_set_thinking: false,
    can_toggle_mcp_global: false,
  });
  assert.equal(patch.canSetThinking, false);
  assert.equal(patch.canToggleMcpGlobal, false);
  assert.ok(RUNTIME_CONFIG_READ_ONLY_NOTICE.length > 0);
});

test('mcpStatusLabel derives only from real server states', () => {
  const servers = mapRuntimeConfig(CONFIG).mcpServers ?? [];
  assert.equal(mcpStatusLabel(servers, true), '1 on');
  assert.equal(mcpStatusLabel(servers, false), 'off');
  assert.equal(mcpStatusLabel([], true), '0 on');
});
