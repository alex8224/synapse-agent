import React, { useState } from 'react';
import { Dismiss16Regular, ChevronUp16Regular, ChevronDown16Regular } from '@fluentui/react-icons';
import { useShallow } from 'zustand/react/shallow';
import { useConsoleStore } from '../stores/useConsoleStore';
import { RUNTIME_CONFIG_READ_ONLY_NOTICE } from '../stores/runtimeConfigMapper.ts';
import { mcpServerPhase } from '../stores/mcpRuntimeView.ts';
import type { McpServerPhase } from '../stores/mcpRuntimeView.ts';

/**
 * MCP panel: configured servers *and* what the live session actually has.
 *
 * Mirrors the TUI panel (F5): every server row shows its real phase (启动中 /
 * 已连接 / 未连接 / 已停用), the discovered tools can be checked one by one and
 * saved to the same `include_tools` whitelist the TUI writes, and the daemon's
 * warnings (connection failures) are shown instead of being swallowed.
 */

/** Per-phase dot + label, mirroring the TUI MCP panel states. */
const PHASE_DOT: Record<McpServerPhase, string> = {
  disabled: 'bg-gray-300',
  connecting: 'bg-amber-400 animate-pulse',
  attached: 'bg-green-500',
  unattached: 'bg-red-400',
  enabled: 'bg-blue-400',
};

const PHASE_LABEL: Record<McpServerPhase, string> = {
  disabled: '已停用',
  connecting: '启动中…',
  attached: '已连接',
  unattached: '未连接',
  enabled: '已启用',
};

const PHASE_TEXT: Record<McpServerPhase, string> = {
  disabled: 'text-gray-400',
  connecting: 'text-amber-600',
  attached: 'text-green-600',
  unattached: 'text-red-500',
  enabled: 'text-gray-500',
};

export const McpPanel: React.FC<{ onClose: () => void }> = ({ onClose }) => {
  const {
    mcpServers,
    mcpEnabled,
    mcpRuntime,
    mcpWarnings,
    mcpConnecting,
    mcpRuntimeKnown,
    canToggleMcpGlobal,
    runtimeStatus,
    toggleMcpServer,
    toggleMcpGlobal,
    refreshMcpRuntime,
    saveMcpTools,
  } = useConsoleStore(
    // Only the fields this panel paints: a reasoning delta must not re-render it.
    useShallow((state) => ({
      mcpServers: state.mcpServers,
      mcpEnabled: state.mcpEnabled,
      mcpRuntime: state.mcpRuntime,
      mcpWarnings: state.mcpWarnings,
      mcpConnecting: state.mcpConnecting,
      mcpRuntimeKnown: state.mcpRuntimeKnown,
      canToggleMcpGlobal: state.canToggleMcpGlobal,
      runtimeStatus: state.runtimeStatus,
      toggleMcpServer: state.toggleMcpServer,
      toggleMcpGlobal: state.toggleMcpGlobal,
      refreshMcpRuntime: state.refreshMcpRuntime,
      saveMcpTools: state.saveMcpTools,
    })),
  );

  // Unsaved checkbox edits, keyed by server. An absent entry means "show the
  // persisted selection", so a refresh can never clobber an in-progress edit.
  const [draft, setDraft] = useState<Record<string, string[]>>({});
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  /** The persisted selection: an empty include_tools means "every tool". */
  const persistedSelection = (name: string): string[] => {
    const runtime = mcpRuntime[name];
    const discovered = runtime?.discovered ?? [];
    const includeTools = runtime?.includeTools ?? [];
    return includeTools.length > 0 ? includeTools : discovered;
  };

  const selectionFor = (name: string): string[] => draft[name] ?? persistedSelection(name);

  const toggleTool = (name: string, tool: string) => {
    const current = selectionFor(name);
    const next = current.includes(tool)
      ? current.filter((item) => item !== tool)
      : [...current, tool];
    setDraft((prev) => ({ ...prev, [name]: next }));
  };

  const save = async (name: string) => {
    const tools = selectionFor(name);
    // Saving "everything selected" clears the whitelist, exactly like the TUI.
    const discovered = mcpRuntime[name]?.discovered ?? [];
    const payload = tools.length === discovered.length ? [] : tools;
    try {
      await saveMcpTools(name, payload);
      setDraft((prev) => {
        const next = { ...prev };
        delete next[name];
        return next;
      });
    } catch {
      // The store already published the reason through `mcpWarnings`.
    }
  };

  return (
    <div className="absolute bottom-8 left-0 z-50 w-96 max-w-[calc(100vw-2rem)] space-y-2.5 rounded-card border border-line/80 material-flyout flyout-in p-3.5 shadow-flyout">
      <div className="flex items-center justify-between border-b border-line/60 pb-2.5">
        <span className="text-xs font-bold text-gray-900">MCP 工具与服务器 (F5)</span>
        <div className="flex items-center gap-1.5">
          <button
            onClick={() => void refreshMcpRuntime()}
            title="重新连接已启用的 MCP 服务器并刷新工具列表"
            className="ui-button ui-compact border border-line/60 bg-surface/50 text-gray-700 hover:bg-surface text-[11px]"
          >
            {mcpConnecting ? '连接中…' : '重新连接'}
          </button>
          <span
            aria-disabled={!canToggleMcpGlobal}
            title={canToggleMcpGlobal ? undefined : RUNTIME_CONFIG_READ_ONLY_NOTICE}
            className={`inline-flex items-center gap-1 rounded-control px-2 py-0.5 font-mono text-[10px] border transition-colors ${
              canToggleMcpGlobal
                ? 'cursor-pointer'
                : 'cursor-not-allowed border-dashed border-gray-300 text-gray-400'
            } ${mcpEnabled ? 'border-emerald-300/80 bg-emerald-50 text-emerald-800' : 'border-line/70 bg-gray-100 text-gray-600'}`}
            onClick={() => {
              if (canToggleMcpGlobal) void toggleMcpGlobal();
            }}
          >
            <span className={`h-1.5 w-1.5 rounded-full ${mcpEnabled ? 'bg-emerald-500' : 'bg-gray-400'}`} />
            <span>{mcpEnabled ? '全局启用' : '全局停用'}</span>
          </span>
          <button
            onClick={onClose}
            title="关闭 (Esc)"
            className="ui-icon-button ui-compact text-gray-400 hover:text-gray-700"
          >
            <Dismiss16Regular aria-hidden="true" />
          </button>
        </div>
      </div>

      {runtimeStatus === 'running' && mcpConnecting && (
        <p className="font-sans text-[10px] leading-relaxed text-amber-600">
          正在连接 MCP；切换结果从下一轮开始生效。
        </p>
      )}

      <div className="fluent-scrollbar max-h-64 space-y-1.5 overflow-y-auto pr-1">
        {mcpServers.length === 0 ? (
          <div className="py-2 text-center font-sans text-xs text-gray-400">
            未配置任何 MCP 服务器
          </div>
        ) : (
          mcpServers.map((srv) => {
            const runtime = mcpRuntime[srv.name];
            const phase = mcpServerPhase(srv, runtime, mcpConnecting, mcpRuntimeKnown);
            const discovered = runtime?.discovered ?? [];
            const selection = selectionFor(srv.name);
            const dirty = draft[srv.name] !== undefined;
            const open = expanded[srv.name] ?? false;
            return (
              <div
                key={srv.name}
                className="rounded-control border border-line/60 bg-surface/50 p-2 text-xs transition-all hover:border-line hover:bg-surface/80 shadow-card"
              >
                <div
                  className="flex cursor-pointer items-center justify-between"
                  onClick={() => {
                    void toggleMcpServer(srv.name);
                  }}
                  title={srv.enabled ? '点击停用该服务器' : '点击启用该服务器'}
                >
                  <div className="flex min-w-0 items-center space-x-2 truncate">
                    <span className={`h-2 w-2 shrink-0 rounded-full ${PHASE_DOT[phase]}`} />
                    <span className="truncate font-medium text-gray-800">{srv.name}</span>
                    <span className="font-mono text-[10px] text-gray-400">
                      ({srv.transport})
                    </span>
                    <span className={`shrink-0 text-[10px] ${PHASE_TEXT[phase]}`}>
                      {PHASE_LABEL[phase]}
                      {phase === 'attached' && discovered.length > 0
                        ? ` ${selection.length}/${discovered.length} 工具`
                        : ''}
                    </span>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    <div
                      className={`relative inline-flex h-4 w-7 shrink-0 items-center rounded-full transition-colors ${
                        srv.enabled ? 'bg-accent' : 'bg-gray-300'
                      }`}
                      aria-hidden="true"
                    >
                      <span
                        className={`inline-block h-3 w-3 rounded-full bg-on-accent transition-transform ${
                          srv.enabled ? 'translate-x-3.5' : 'translate-x-0.5'
                        }`}
                      />
                    </div>
                    {srv.enabled && discovered.length > 0 && (
                      <button
                        onClick={(event) => {
                          event.stopPropagation();
                          setExpanded((prev) => ({ ...prev, [srv.name]: !open }));
                        }}
                        title={open ? '收起工具列表' : '展开工具列表'}
                        className="ui-icon-button ui-compact text-gray-400 hover:text-gray-700"
                      >
                        {open ? <ChevronUp16Regular aria-hidden="true" /> : <ChevronDown16Regular aria-hidden="true" />}
                      </button>
                    )}
                  </div>
                </div>

                {srv.enabled && phase !== 'connecting' && discovered.length === 0 && (
                  <div className="mt-1 pl-4 font-sans text-[10px] text-gray-400">
                    （未连接：点击「重新连接」后可选择工具）
                  </div>
                )}

                {srv.enabled && open && discovered.length > 0 && (
                  <div className="mt-1 space-y-0.5 border-t border-gray-50 pt-1">
                    {discovered.map((tool) => (
                      <label
                        key={tool}
                        className="flex cursor-pointer items-center gap-1.5 pl-4 font-mono text-[11px] text-gray-700"
                      >
                        <input
                          id={`mcp-tool-${srv.name}-${tool}`}
                          name={`mcp-tool-${srv.name}`}
                          type="checkbox"
                          checked={selection.includes(tool)}
                          onChange={() => toggleTool(srv.name, tool)}
                        />
                        <span className="truncate">{tool}</span>
                      </label>
                    ))}
                    <div className="flex items-center justify-between pt-1 pl-4">
                      <span className="font-sans text-[10px] text-gray-400">
                        {selection.length}/{discovered.length} 已选
                      </span>
                      <button
                        onClick={() => void save(srv.name)}
                        disabled={!dirty}
                        className={`rounded px-2 py-0.5 font-mono text-[10px] ${
                          dirty
                            ? 'cursor-pointer bg-blue-50 text-blue-600 hover:bg-blue-100'
                            : 'cursor-not-allowed text-gray-300'
                        }`}
                      >
                        保存工具选择
                      </button>
                    </div>
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>

      {mcpWarnings.length > 0 && (
        <div className="space-y-0.5 border-t border-gray-100 pt-1.5">
          {mcpWarnings.slice(0, 4).map((warning) => (
            <p
              key={warning}
              className="break-all font-sans text-[10px] leading-relaxed text-amber-700"
            >
              mcp: {warning}
            </p>
          ))}
        </div>
      )}
    </div>
  );
};
