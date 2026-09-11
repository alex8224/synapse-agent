/**
 * Pure view helpers for the MCP panel's *runtime* state.
 *
 * The config projection (`runtimeConfigMapper`) only ever knows the configured
 * `enabled` flag; attachment, discovered tools and warnings come exclusively
 * from an MCP reload result.  Keeping the two apart is what lets the panel say
 * "已启用 · 未连接" instead of pretending a configured server is running.
 *
 * Deliberately free of zustand / React / WebSocket dependencies so it can be
 * exercised directly with the Node built-in test runner (`node --test`).
 */

import type { McpServerState, ReloadMcpResult } from '../client/types.ts';

export interface McpRuntimeServerState {
  attached: boolean;
  includeTools: string[];
  discovered: string[];
  loaded: string[];
}

export interface McpRuntimePatch {
  /** Keyed by server name; merge into (never replace) the existing map. */
  servers: Record<string, McpRuntimeServerState>;
  warnings: string[];
  toolCount: number;
}

/**
 * - ``disabled``: the config flag is off, so nothing is attached by design;
 * - ``connecting``: the attach RPC is in flight (the TUI-style 启动中);
 * - ``attached``: the server's tools are in this session's tool list;
 * - ``unattached``: enabled, runtime state known, but no tools loaded;
 * - ``enabled``: enabled, runtime state not reported yet (older peer / read
 *   failure) — the panel must not claim either way.
 */
export type McpServerPhase =
  | 'disabled'
  | 'connecting'
  | 'attached'
  | 'unattached'
  | 'enabled';

function strings(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string')
    : [];
}

function toRuntimeState(server: McpServerState): McpRuntimeServerState {
  return {
    attached: server.attached === true,
    includeTools: strings(server.include_tools),
    discovered: strings(server.discovered),
    loaded: strings(server.loaded),
  };
}

/** Project one reload result into mergeable per-server runtime state. */
export function mcpRuntimePatch(result: ReloadMcpResult): McpRuntimePatch {
  const servers: Record<string, McpRuntimeServerState> = {};
  for (const server of result.servers ?? []) {
    if (!server || typeof server.name !== 'string' || server.name === '') continue;
    servers[server.name] = toRuntimeState(server);
  }
  return {
    servers,
    warnings: strings(result.warnings),
    toolCount: typeof result.tool_count === 'number' ? result.tool_count : 0,
  };
}

/**
 * One server's phase for the panel.
 *
 * ``connecting`` wins over a stale runtime entry: while the attach RPC is in
 * flight the previous result is no longer known to be current, which is what
 * renders the TUI-style "启动中" state for enabled servers.
 */
export function mcpServerPhase(
  server: { enabled: boolean },
  runtime: McpRuntimeServerState | undefined,
  connecting: boolean,
  known: boolean,
): McpServerPhase {
  if (!server.enabled) return 'disabled';
  if (connecting) return 'connecting';
  if (!known) return 'enabled';
  return runtime?.attached ? 'attached' : 'unattached';
}

/**
 * Footer label.  ``connecting`` mirrors the TUI's startup attach, and the
 * "未连接" suffix is the honest part: configured servers whose tools are not
 * loaded are reported as such instead of counting as running.
 */
export function mcpRuntimeStatusLabel(
  servers: Array<{ name: string; enabled: boolean }>,
  enabled: boolean,
  runtime: Record<string, McpRuntimeServerState>,
  connecting: boolean,
  known: boolean,
): string {
  if (connecting) return '启动中';
  if (!enabled) return 'off';
  const on = servers.filter((server) => server.enabled);
  if (on.length === 0) return '0 on';
  if (!known) return `${on.length} on`;
  const attached = on.filter((server) => runtime[server.name]?.attached).length;
  if (attached === 0) return `${on.length} on · 未连接`;
  if (attached === on.length) return `${on.length} on`;
  return `${on.length} on · ${attached} 已连接`;
}
