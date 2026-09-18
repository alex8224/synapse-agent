/**
 * MCP runtime view helpers: the panel may only claim what a reload result said.
 *
 * The config projection never carries attachment, so these helpers are the only
 * place that turns a reload result into 启动中 / 已连接 / 未连接 and into the
 * footer label.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  mcpRuntimePatch,
  mcpRuntimeStatusLabel,
  mcpServerPhase,
} from '../src/stores/mcpRuntimeView.ts';
import type { ReloadMcpResult } from '../src/client/types.ts';

function result(overrides: Partial<ReloadMcpResult> = {}): ReloadMcpResult {
  return {
    command_id: 'c1',
    session: { project_id: 'p', thread_id: 't' },
    server: null,
    enabled: null,
    attached: true,
    active_servers: ['search'],
    tool_count: 1,
    warnings: [],
    tool_names: ['search__query'],
    servers: [
      {
        name: 'search',
        enabled: true,
        attached: true,
        include_tools: ['query'],
        discovered: ['query', 'fetch'],
        loaded: ['search__query'],
      },
      {
        name: 'idle',
        enabled: false,
        attached: false,
        include_tools: [],
        discovered: [],
        loaded: [],
      },
    ],
    ...overrides,
  };
}

test('a reload result is projected per server and stays mergeable', () => {
  const patch = mcpRuntimePatch(result());
  assert.deepEqual(Object.keys(patch.servers), ['search', 'idle']);
  assert.deepEqual(patch.servers.search, {
    attached: true,
    includeTools: ['query'],
    discovered: ['query', 'fetch'],
    loaded: ['search__query'],
  });
  assert.deepEqual(patch.servers.idle.discovered, []);
  assert.equal(patch.toolCount, 1);
  assert.deepEqual(patch.warnings, []);
});

test('malformed server entries and warnings are ignored, never thrown', () => {
  const patch = mcpRuntimePatch(
    result({
      servers: [null as never, { name: '' } as never, { name: 'ok' } as never],
      warnings: ['boom', 7 as never],
    }),
  );
  assert.deepEqual(Object.keys(patch.servers), ['ok']);
  assert.equal(patch.servers.ok.attached, false);
  assert.deepEqual(patch.warnings, ['boom']);
});

test('phases distinguish configured from actually attached', () => {
  const attached = { attached: true, includeTools: [], discovered: ['a'], loaded: ['a'] };
  const missing = { attached: false, includeTools: [], discovered: [], loaded: [] };
  assert.equal(mcpServerPhase({ enabled: false }, attached, false, true), 'disabled');
  // 启动中 wins while the attach RPC is in flight: the previous result is not
  // known to still be current until the new one arrives.
  assert.equal(mcpServerPhase({ enabled: true }, missing, true, true), 'connecting');
  assert.equal(mcpServerPhase({ enabled: true }, attached, true, true), 'connecting');
  assert.equal(mcpServerPhase({ enabled: true }, attached, false, true), 'attached');
  assert.equal(mcpServerPhase({ enabled: true }, missing, false, true), 'unattached');
  // No runtime report yet (older peer): neither claim is allowed.
  assert.equal(mcpServerPhase({ enabled: true }, undefined, false, false), 'enabled');
});

test('the footer label reports connecting, unattached and attached truthfully', () => {
  const servers = [
    { name: 'search', enabled: true },
    { name: 'idle', enabled: false },
  ];
  assert.equal(mcpRuntimeStatusLabel(servers, true, {}, true, false), '启动中');
  assert.equal(mcpRuntimeStatusLabel(servers, false, {}, false, true), 'off');
  assert.equal(mcpRuntimeStatusLabel(servers, true, {}, false, false), '1 on');
  assert.equal(mcpRuntimeStatusLabel(servers, true, {}, false, true), '1 on · 未连接');
  assert.equal(
    mcpRuntimeStatusLabel(
      servers,
      true,
      { search: { attached: true, includeTools: [], discovered: [], loaded: [] } },
      false,
      true,
    ),
    '1 on',
  );
  assert.equal(
    mcpRuntimeStatusLabel(
      [
        { name: 'a', enabled: true },
        { name: 'b', enabled: true },
      ],
      true,
      { a: { attached: true, includeTools: [], discovered: [], loaded: [] } },
      false,
      true,
    ),
    '2 on · 1 已连接',
  );
  assert.equal(mcpRuntimeStatusLabel([], true, {}, false, true), '0 on');
});
