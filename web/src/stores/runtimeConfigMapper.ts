/**
 * Pure mapper between the read-only `runtime.config.get` DTO and the console
 * state, plus the capability/read-only helpers.
 *
 * Deliberately free of zustand / React / WebSocket dependencies so it can be
 * exercised directly with the Node built-in test runner (`node --test`) and
 * reused from `useConsoleStore`.
 *
 * The mapper never fabricates state: `attached` is only ever filled from the
 * MCP reload result (never from this projection), `activeGoal` is never part
 * of the config surface, and `modelName` is preserved when `preserveModel` is
 * set so a session-scoped model can never be over-written by a project-level
 * default during a refresh.
 */

import type { RuntimeConfigResult, McpServerView } from '../client/types.ts';

/** Shown next to read-only controls (thinking level, global MCP toggle). */
export const RUNTIME_CONFIG_READ_ONLY_NOTICE = '当前为只读状态，暂不支持修改';

export interface MappedMcpServer {
  name: string;
  transport: string;
  enabled: boolean;
  toolPrefix?: string | null;
  /** Only present after an MCP reload result; never fabricated by the mapper. */
  attached?: boolean;
}

export interface RuntimeConfigStatePatch {
  modelName?: string;
  availableModels?: string[];
  thinkingLevel?: string | null;
  thinkingLevels?: string[];
  mcpServers?: MappedMcpServer[];
  mcpEnabled?: boolean;
  mcpStatus?: string;
  canSetThinking?: boolean;
  canToggleMcpGlobal?: boolean;
  /** The project's own default level (never the session's rebind). */
  projectThinkingLevel?: string | null;
  canSetProjectThinking?: boolean;
  /** Selected model's input context size; `null` when its profile has none. */
  contextWindow?: number | null;
}

/** Human label for the MCP footer, derived only from real server states. */
export function mcpStatusLabel(servers: MappedMcpServer[], enabled: boolean): string {
  if (!enabled) return 'off';
  const on = servers.filter((server) => server.enabled).length;
  return `${on} on`;
}

function toMappedMcpServer(server: McpServerView): MappedMcpServer {
  return {
    name: server.name,
    transport: server.transport,
    enabled: server.enabled,
    toolPrefix: server.tool_prefix,
    // No `attached` here: the config view has no attachment information and
    // the mapper must never pretend otherwise.
  };
}

/**
 * Map a `runtime.config.get` result to console state.
 *
 * `preserveModel` must be true for refreshes of an attached (already opened)
 * session whose model was resolved from `open.view` — the mapper then never
 * touches `modelName`, so a project default can never clobber the session
 * model.
 */
export function mapRuntimeConfig(
  view: RuntimeConfigResult,
  opts: { preserveModel?: boolean } = {},
): RuntimeConfigStatePatch {
  const servers = view.mcp_servers.map(toMappedMcpServer);
  const patch: RuntimeConfigStatePatch = {
    availableModels: [...view.available_models],
    thinkingLevel: view.thinking_level,
    thinkingLevels: [...view.thinking_levels],
    mcpServers: servers,
    mcpEnabled: view.mcp_enabled,
    mcpStatus: mcpStatusLabel(servers, view.mcp_enabled),
    canSetThinking: view.can_set_thinking,
    canToggleMcpGlobal: view.can_toggle_mcp_global,
    // A peer that predates the project-scoped port simply omits both fields;
    // the console then shows the project default as unknown and read-only.
    projectThinkingLevel: view.project_thinking_level ?? null,
    canSetProjectThinking: view.can_set_project_thinking ?? false,
    // A peer that predates the field omits it: the console then renders the bare
    // context count instead of a fraction, never a guessed window.
    contextWindow: view.context_window ?? null,
  };
  if (!opts.preserveModel) {
    patch.modelName = view.current_model;
  }
  return patch;
}
