/**
 * MCP maintenance section for the settings dialog.
 *
 * Implements:
 * - Individual start/stop toggle per MCP server with live runtime status
 * - Individual selection and filtering of tools/methods provided by each server
 * - Adding new MCP servers (stdio / sse / streamable_http)
 * - Editing existing MCP server configurations (command, args, env, url, headers, etc.)
 * - Deleting MCP servers with confirmation
 * - Global reconnection and diagnostic warnings
 */
import {
  Add16Regular,
  ArrowClockwise20Regular,
  Checkmark16Regular,
  ChevronDown16Regular,
  ChevronUp16Regular,
  Delete16Regular,
  Dismiss20Regular,
  Edit16Regular,
  PuzzlePiece20Regular,
  Search16Regular,
} from '@fluentui/react-icons';
import React, { useCallback, useEffect, useState } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { Portal } from './Portal.tsx';
import type { McpServerDetailView, McpServerListResult } from '../runtime-client/types.ts';
import { useConsoleStore } from '../stores/useConsoleStore.ts';
import { mcpServerPhase } from '../stores/mcpRuntimeView.ts';
import type { McpServerPhase } from '../stores/mcpRuntimeView.ts';

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

interface McpServerFormData {
  name: string;
  transport: 'stdio' | 'sse' | 'streamable_http';
  command: string;
  args: string;
  env: string;
  url: string;
  headers: string;
  toolPrefix: string;
  enabled: boolean;
  timeout: string;
  proxy: string;
}

const EMPTY_FORM: McpServerFormData = {
  name: '',
  transport: 'stdio',
  command: '',
  args: '',
  env: '',
  url: '',
  headers: '',
  toolPrefix: '',
  enabled: true,
  timeout: '12',
  proxy: '',
};

export const McpMaintenanceSection: React.FC = () => {
  const {
    client,
    currentSession,
    mcpConnecting,
    mcpRuntimeKnown,
    mcpRuntime,
    mcpWarnings,
    toggleMcpServer,
    refreshMcpRuntime,
    saveMcpTools,
  } = useConsoleStore(
    useShallow((state) => ({
      client: state.client,
      currentSession: state.currentSession,
      mcpConnecting: state.mcpConnecting,
      mcpRuntimeKnown: state.mcpRuntimeKnown,
      mcpRuntime: state.mcpRuntime,
      mcpWarnings: state.mcpWarnings,
      toggleMcpServer: state.toggleMcpServer,
      refreshMcpRuntime: state.refreshMcpRuntime,
      saveMcpTools: state.saveMcpTools,
    })),
  );

  const [servers, setServers] = useState<McpServerDetailView[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Tools whitelist editing states
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [drafts, setDrafts] = useState<Record<string, string[]>>({});
  const [savingTools, setSavingTools] = useState<Record<string, boolean>>({});
  const [filterQuery, setFilterQuery] = useState<Record<string, string>>({});

  // Modal states for Add / Edit
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [editingOriginalName, setEditingOriginalName] = useState<string | null>(null);
  const [formData, setFormData] = useState<McpServerFormData>(EMPTY_FORM);
  const [modalError, setModalError] = useState<string | null>(null);
  const [isSaving, setIsSaving] = useState(false);

  // Delete confirmation
  const [deleteConfirmName, setDeleteConfirmName] = useState<string | null>(null);
  const [isDeleting, setIsDeleting] = useState(false);

  const loadServers = useCallback(async () => {
    if (!client || client.getState() !== 'connected') return;
    setLoading(true);
    setError(null);
    try {
      const res: McpServerListResult = await client.mcpList(currentSession);
      setServers([...res.servers]);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [client, currentSession]);

  useEffect(() => {
    void loadServers();
  }, [loadServers]);

  // Handle single start/stop toggle
  const handleToggle = async (server: McpServerDetailView) => {
    try {
      await toggleMcpServer(server.name);
      setServers((prev) =>
        prev.map((s) => (s.name === server.name ? { ...s, enabled: !s.enabled } : s))
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  // Get active selection for tools
  const selectionFor = (server: McpServerDetailView): string[] => {
    if (drafts[server.name] !== undefined) {
      return drafts[server.name];
    }
    const runtime = mcpRuntime[server.name];
    const discovered = runtime?.discovered ?? server.discovered;
    const includeTools = runtime?.includeTools ?? server.include_tools;
    return includeTools.length > 0 ? includeTools : discovered;
  };

  const handleToolToggle = (server: McpServerDetailView, tool: string) => {
    const current = selectionFor(server);
    const next = current.includes(tool)
      ? current.filter((t) => t !== tool)
      : [...current, tool];
    setDrafts((prev) => ({ ...prev, [server.name]: next }));
  };

  const handleSelectAllTools = (server: McpServerDetailView, selectAll: boolean) => {
    const runtime = mcpRuntime[server.name];
    const discovered = runtime?.discovered ?? server.discovered;
    setDrafts((prev) => ({
      ...prev,
      [server.name]: selectAll ? [...discovered] : [],
    }));
  };

  const handleSaveTools = async (server: McpServerDetailView) => {
    const selected = selectionFor(server);
    const runtime = mcpRuntime[server.name];
    const discovered = runtime?.discovered ?? server.discovered;
    // Empty array in backend means whitelist cleared -> allow all
    const payload = selected.length === discovered.length ? [] : selected;

    setSavingTools((prev) => ({ ...prev, [server.name]: true }));
    try {
      await saveMcpTools(server.name, payload);
      setDrafts((prev) => {
        const next = { ...prev };
        delete next[server.name];
        return next;
      });
      // Refresh list to update include_tools
      await loadServers();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingTools((prev) => ({ ...prev, [server.name]: false }));
    }
  };

  // Open modal for Create
  const handleOpenAdd = () => {
    setEditingOriginalName(null);
    setFormData(EMPTY_FORM);
    setModalError(null);
    setIsModalOpen(true);
  };

  // Open modal for Edit
  const handleOpenEdit = (server: McpServerDetailView) => {
    setEditingOriginalName(server.name);
    const envStr = Object.entries(server.env || {})
      .map(([k, v]) => `${k}=${v}`)
      .join('\n');
    const headersStr = Object.entries(server.headers || {})
      .map(([k, v]) => `${k}: ${v}`)
      .join('\n');

    setFormData({
      name: server.name,
      transport: (server.transport as 'stdio' | 'sse' | 'streamable_http') || 'stdio',
      command: server.command || '',
      args: (server.args || []).join('\n'),
      env: envStr,
      url: server.url || '',
      headers: headersStr,
      toolPrefix: server.tool_prefix || '',
      enabled: server.enabled,
      timeout: server.timeout ? String(server.timeout) : '12',
      proxy: server.proxy || '',
    });
    setModalError(null);
    setIsModalOpen(true);
  };

  // Save server (Add or Edit)
  const handleSaveServer = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!client || client.getState() !== 'connected') return;

    const trimmedName = formData.name.trim();
    if (!trimmedName) {
      setModalError('请输入服务器名称');
      return;
    }

    // Parse args
    const parsedArgs = formData.args
      .split('\n')
      .map((a) => a.trim())
      .filter(Boolean);

    // Parse env (KEY=VAL)
    const parsedEnv: Record<string, string> = {};
    for (const line of formData.env.split('\n')) {
      const idx = line.indexOf('=');
      if (idx > 0) {
        const k = line.slice(0, idx).trim();
        const v = line.slice(idx + 1).trim();
        if (k) parsedEnv[k] = v;
      }
    }

    // Parse headers (Header: Value)
    const parsedHeaders: Record<string, string> = {};
    for (const line of formData.headers.split('\n')) {
      const idx = line.indexOf(':');
      if (idx > 0) {
        const k = line.slice(0, idx).trim();
        const v = line.slice(idx + 1).trim();
        if (k) parsedHeaders[k] = v;
      }
    }

    const payload: Record<string, unknown> = {
      name: trimmedName,
      transport: formData.transport,
      enabled: formData.enabled,
    };

    if (formData.transport === 'stdio') {
      if (!formData.command.trim()) {
        setModalError('stdio 模式下必须配置执行命令');
        return;
      }
      payload.command = formData.command.trim();
      payload.args = parsedArgs;
      payload.env = parsedEnv;
    } else {
      if (!formData.url.trim()) {
        setModalError('远程流模式下必须配置服务 URL');
        return;
      }
      payload.url = formData.url.trim();
      payload.headers = parsedHeaders;
    }

    if (formData.toolPrefix.trim()) {
      payload.tool_prefix = formData.toolPrefix.trim();
    }
    if (formData.timeout.trim()) {
      const num = Number(formData.timeout.trim());
      if (!isNaN(num) && num > 0) payload.timeout = num;
    }
    if (formData.proxy.trim()) {
      payload.proxy = formData.proxy.trim();
    }

    setIsSaving(true);
    setModalError(null);
    try {
      await client.mcpSave(
        currentSession,
        payload,
        editingOriginalName || undefined
      );
      await loadServers();
      setIsModalOpen(false);
    } catch (err) {
      setModalError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsSaving(false);
    }
  };

  // Delete server
  const handleDeleteServer = async (name: string) => {
    if (!client || client.getState() !== 'connected') return;
    setIsDeleting(true);
    try {
      await client.mcpDelete(currentSession, name);
      setDeleteConfirmName(null);
      await loadServers();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsDeleting(false);
    }
  };

  const totalServers = servers.length;
  const attachedCount = servers.filter(
    (s) => s.enabled && (mcpRuntime[s.name]?.attached ?? s.attached)
  ).length;
  const disabledCount = servers.filter((s) => !s.enabled).length;

  return (
    <div className="space-y-4">
      {/* Header bar */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line pb-3">
        <div>
          <div className="flex items-center gap-2">
            <h3 className="text-base font-semibold text-gray-900">MCP 服务维护</h3>
            <span className="rounded-full bg-blue-50 px-2 py-0.5 text-[11px] font-medium text-blue-700">
              共 {totalServers} 个服务器 · {attachedCount} 已连接 · {disabledCount} 已停用
            </span>
          </div>
          <p className="mt-0.5 text-xs text-gray-500">
            支持针对单个 MCP 服务的独立启停控制、方法（工具）白名单单独勾选以及配置的增删改维护。
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => void refreshMcpRuntime()}
            disabled={mcpConnecting}
            title="重新连接已启用的 MCP 服务并刷新方法列表"
            className="ui-button ui-compact border border-line bg-surface hover:bg-gray-50 text-gray-700"
          >
            <ArrowClockwise20Regular
              className={`h-4 w-4 ${mcpConnecting ? 'animate-spin' : ''}`}
              aria-hidden="true"
            />
            <span>{mcpConnecting ? '正在连接…' : '全部重新连接'}</span>
          </button>
          <button
            type="button"
            onClick={handleOpenAdd}
            className="ui-button ui-compact ui-primary flex items-center gap-1.5"
          >
            <Add16Regular className="h-4 w-4" aria-hidden="true" />
            <span>添加服务器</span>
          </button>
        </div>
      </div>

      {/* Error alert */}
      {error && (
        <div className="flex items-center justify-between rounded-control border border-red-200 bg-red-50 p-2.5 text-xs text-red-700">
          <span>{error}</span>
          <button
            type="button"
            onClick={() => setError(null)}
            className="text-red-500 hover:text-red-800"
          >
            <Dismiss20Regular className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>
      )}

      {/* Global warnings if any */}
      {mcpWarnings.length > 0 && (
        <div className="rounded-control border border-amber-200 bg-amber-50/70 p-2.5 text-xs text-amber-800 space-y-1">
          <span className="font-semibold">连接警告：</span>
          {mcpWarnings.slice(0, 3).map((w) => (
            <p key={w} className="break-all font-mono text-[11px]">
              • {w}
            </p>
          ))}
        </div>
      )}

      {/* Server List */}
      <div className="space-y-3">
        {loading && servers.length === 0 ? (
          <div className="py-8 text-center text-xs text-gray-400">正在加载 MCP 服务器配置…</div>
        ) : servers.length === 0 ? (
          <div className="rounded-control border border-dashed border-line p-8 text-center">
            <PuzzlePiece20Regular className="mx-auto h-8 w-8 text-gray-300" aria-hidden="true" />
            <p className="mt-2 text-sm font-medium text-gray-700">尚未配置任何 MCP 服务器</p>
            <p className="mt-1 text-xs text-gray-400">
              点击上方“添加服务器”按钮，配置 stdio 本地进程或 SSE 远程流协议。
            </p>
          </div>
        ) : (
          servers.map((srv) => {
            const runtime = mcpRuntime[srv.name];
            const phase = mcpServerPhase(srv, runtime, mcpConnecting, mcpRuntimeKnown);
            const discovered = runtime?.discovered ?? srv.discovered ?? [];
            const selectedTools = selectionFor(srv);
            const isOpen = expanded[srv.name] ?? false;
            const isDirty = drafts[srv.name] !== undefined;
            const isSavingThisTools = savingTools[srv.name] ?? false;
            const q = (filterQuery[srv.name] || '').toLowerCase();
            const filteredDiscovered = discovered.filter((t) => t.toLowerCase().includes(q));

            return (
              <div
                key={srv.name}
                className="rounded-control border border-line bg-surface shadow-card transition-all"
              >
                {/* Server Main Row */}
                <div className="flex flex-wrap items-center justify-between gap-2 p-3.5 border-b border-line/40">
                  <div className="flex min-w-0 items-center gap-3">
                    {/* Status Dot */}
                    <span
                      className={`h-2.5 w-2.5 shrink-0 rounded-full ${PHASE_DOT[phase]}`}
                      title={PHASE_LABEL[phase]}
                    />

                    {/* Server Info */}
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="font-semibold text-sm text-gray-900 truncate">
                          {srv.name}
                        </span>
                        <span className="rounded bg-gray-100 px-1.5 py-0.5 font-mono text-[10px] text-gray-600">
                          {srv.transport}
                        </span>
                        <span className={`text-[11px] font-medium ${PHASE_TEXT[phase]}`}>
                          {PHASE_LABEL[phase]}
                        </span>
                      </div>

                      <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-gray-500">
                        {srv.command && (
                          <span className="font-mono truncate max-w-[260px]" title={`${srv.command} ${(srv.args || []).join(' ')}`}>
                            命令: {srv.command} {(srv.args || []).join(' ')}
                          </span>
                        )}
                        {srv.url && (
                          <span className="font-mono truncate max-w-[260px]" title={srv.url}>
                            URL: {srv.url}
                          </span>
                        )}
                        {srv.tool_prefix && (
                          <span>前缀: <code className="font-mono text-[10px]">{srv.tool_prefix}</code></span>
                        )}
                        <span>
                          方法：{selectedTools.length}/{discovered.length} 已选
                        </span>
                      </div>
                    </div>
                  </div>

                  {/* Actions Right */}
                  <div className="flex items-center gap-2">
                    {/* Individual Toggle Switch */}
                    <button
                      type="button"
                      onClick={() => void handleToggle(srv)}
                      title={srv.enabled ? '点击停用此服务器' : '点击启用此服务器'}
                      className="flex items-center gap-1.5 cursor-pointer rounded-full p-1 hover:bg-gray-100"
                    >
                      <span className="text-xs text-gray-600 select-none">
                        {srv.enabled ? '已启用' : '已停用'}
                      </span>
                      <div
                        className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors ${
                          srv.enabled ? 'bg-accent' : 'bg-gray-300'
                        }`}
                        aria-hidden="true"
                      >
                        <span
                          className={`inline-block h-3.5 w-3.5 rounded-full bg-on-accent transition-transform ${
                            srv.enabled ? 'translate-x-4.5' : 'translate-x-0.5'
                          }`}
                        />
                      </div>
                    </button>

                    <div className="h-4 w-px bg-line" />

                    {/* Edit button */}
                    <button
                      type="button"
                      onClick={() => handleOpenEdit(srv)}
                      title="编辑服务器配置"
                      className="ui-icon-button ui-compact text-gray-500 hover:text-gray-800"
                    >
                      <Edit16Regular aria-hidden="true" />
                    </button>

                    {/* Delete button */}
                    <button
                      type="button"
                      onClick={() => setDeleteConfirmName(srv.name)}
                      title="删除服务器"
                      className="ui-icon-button ui-compact text-red-500 hover:text-red-700"
                    >
                      <Delete16Regular aria-hidden="true" />
                    </button>

                    {/* Expand Tools toggle */}
                    <button
                      type="button"
                      onClick={() => setExpanded((prev) => ({ ...prev, [srv.name]: !isOpen }))}
                      className="ui-button ui-compact text-xs flex items-center gap-1 border border-line bg-surface hover:bg-gray-50 text-gray-700"
                      title={isOpen ? '收起提供的方法' : '查看并选择提供的方法'}
                    >
                      <span>选择方法 ({selectedTools.length})</span>
                      {isOpen ? (
                        <ChevronUp16Regular className="h-3.5 w-3.5" aria-hidden="true" />
                      ) : (
                        <ChevronDown16Regular className="h-3.5 w-3.5" aria-hidden="true" />
                      )}
                    </button>
                  </div>
                </div>

                {/* Collapsible Tool Methods Selection Panel */}
                {isOpen && (
                  <div className="bg-gray-50/50 p-3.5 border-t border-line/40 space-y-2.5">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="flex items-center gap-2">
                        <span className="text-xs font-medium text-gray-700">
                          提供的方法/工具列表 ({discovered.length})
                        </span>
                        {discovered.length > 0 && (
                          <div className="flex items-center gap-1">
                            <button
                              type="button"
                              onClick={() => handleSelectAllTools(srv, true)}
                              className="text-[11px] text-blue-600 hover:underline"
                            >
                              全选
                            </button>
                            <span className="text-gray-300">·</span>
                            <button
                              type="button"
                              onClick={() => handleSelectAllTools(srv, false)}
                              className="text-[11px] text-blue-600 hover:underline"
                            >
                              全不选
                            </button>
                          </div>
                        )}
                      </div>

                      {/* Tool filter input */}
                      {discovered.length > 5 && (
                        <div className="relative">
                          <Search16Regular className="absolute left-2 top-2 h-3.5 w-3.5 text-gray-400" />
                          <input
                            type="text"
                            placeholder="筛选方法名称…"
                            value={filterQuery[srv.name] || ''}
                            onChange={(e) =>
                              setFilterQuery((prev) => ({ ...prev, [srv.name]: e.target.value }))
                            }
                            className="ui-field pl-7 py-0.5 text-[11px] w-40"
                          />
                        </div>
                      )}
                    </div>

                    {discovered.length === 0 ? (
                      <div className="rounded border border-dashed border-gray-200 p-4 text-center text-xs text-gray-400">
                        {srv.enabled
                          ? '当前尚未发现该服务器提供的工具方法（请点击右上角“全部重新连接”进行探测）'
                          : '该服务器已停用，启用并连接后将自动发现其支持的工具方法'}
                      </div>
                    ) : (
                      <div className="fluent-scrollbar max-h-52 overflow-y-auto space-y-1 pr-1">
                        {filteredDiscovered.length === 0 ? (
                          <div className="py-2 text-center text-xs text-gray-400">无匹配的方法</div>
                        ) : (
                          filteredDiscovered.map((tool) => {
                            const isChecked = selectedTools.includes(tool);
                            return (
                              <label
                                key={tool}
                                className={`flex cursor-pointer items-center justify-between rounded border p-1.5 text-xs transition-colors ${
                                  isChecked
                                    ? 'border-blue-200 bg-blue-50/50 text-gray-900'
                                    : 'border-transparent bg-surface/70 text-gray-500 hover:bg-surface'
                                }`}
                              >
                                <div className="flex items-center gap-2 truncate">
                                  <input
                                    type="checkbox"
                                    checked={isChecked}
                                    onChange={() => handleToolToggle(srv, tool)}
                                    className="ui-check h-3.5 w-3.5"
                                  />
                                  <span className="font-mono text-[11px] truncate">{tool}</span>
                                </div>
                                <span className="text-[10px] text-gray-400 shrink-0">
                                  {isChecked ? '已激活' : '已过滤'}
                                </span>
                              </label>
                            );
                          })
                        )}
                      </div>
                    )}

                    {/* Save Tools button */}
                    {discovered.length > 0 && (
                      <div className="flex items-center justify-between pt-1 border-t border-gray-200">
                        <span className="text-[11px] text-gray-500">
                          {isDirty
                            ? `存在未保存的方法变更：已选 ${selectedTools.length} / ${discovered.length}`
                            : `当前方法白名单状态：已加载 ${selectedTools.length} 个方法`}
                        </span>
                        <button
                          type="button"
                          onClick={() => void handleSaveTools(srv)}
                          disabled={!isDirty || isSavingThisTools}
                          className={`ui-button ui-compact text-xs flex items-center gap-1 ${
                            isDirty
                              ? 'ui-primary'
                              : 'border border-gray-200 text-gray-300 cursor-not-allowed'
                          }`}
                        >
                          <Checkmark16Regular className="h-3.5 w-3.5" aria-hidden="true" />
                          <span>{isSavingThisTools ? '正在保存…' : '保存方法配置'}</span>
                        </button>
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>

      {/* Add / Edit MCP Server Modal */}
      {isModalOpen && (
        <Portal>
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-xs p-4">
            <div className="w-full max-w-lg rounded-window border border-line bg-surface p-5 shadow-2xl flex flex-col max-h-[90vh]">
              <div className="flex items-center justify-between border-b border-line pb-3">
                <h3 className="text-base font-semibold text-gray-900">
                  {editingOriginalName ? `编辑 MCP 服务器: ${editingOriginalName}` : '添加 MCP 服务器'}
                </h3>
                <button
                  type="button"
                  onClick={() => setIsModalOpen(false)}
                  className="ui-icon-button text-gray-400 hover:text-gray-700"
                >
                  <Dismiss20Regular aria-hidden="true" />
                </button>
              </div>

              {modalError && (
                <div className="mt-3 rounded border border-red-200 bg-red-50 p-2 text-xs text-red-700">
                  {modalError}
                </div>
              )}

              <form onSubmit={(e) => void handleSaveServer(e)} className="mt-3 space-y-3.5 overflow-y-auto pr-1 flex-1">
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="mb-1 block text-xs font-medium text-gray-700">
                      服务器名称 <span className="text-red-500">*</span>
                    </label>
                    <input
                      type="text"
                      value={formData.name}
                      onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                      placeholder="如 fetch, filesystem"
                      className="ui-field w-full text-xs"
                      required
                    />
                  </div>
                  <div>
                    <label className="mb-1 block text-xs font-medium text-gray-700">
                      传输协议 <span className="text-red-500">*</span>
                    </label>
                    <select
                      value={formData.transport}
                      onChange={(e) =>
                        setFormData({
                          ...formData,
                          transport: e.target.value as 'stdio' | 'sse' | 'streamable_http',
                        })
                      }
                      className="ui-field w-full text-xs bg-surface"
                    >
                      <option value="stdio">标准 I/O 进程 (stdio)</option>
                      <option value="sse">Server-Sent Events (sse)</option>
                      <option value="streamable_http">HTTP 流 (streamable_http)</option>
                    </select>
                  </div>
                </div>

                {formData.transport === 'stdio' ? (
                  <>
                    <div>
                      <label className="mb-1 block text-xs font-medium text-gray-700">
                        执行命令 Command <span className="text-red-500">*</span>
                      </label>
                      <input
                        type="text"
                        value={formData.command}
                        onChange={(e) => setFormData({ ...formData, command: e.target.value })}
                        placeholder="如 uvx, python, node, npx"
                        className="ui-field w-full text-xs font-mono"
                        required
                      />
                    </div>
                    <div>
                      <label className="mb-1 block text-xs font-medium text-gray-700">
                        启动参数 Arguments (每行一项)
                      </label>
                      <textarea
                        rows={3}
                        value={formData.args}
                        onChange={(e) => setFormData({ ...formData, args: e.target.value })}
                        placeholder="mcp-server-fetch&#10;--verbose"
                        className="ui-field w-full text-xs font-mono"
                      />
                    </div>
                    <div>
                      <label className="mb-1 block text-xs font-medium text-gray-700">
                        环境变量 Environment (每行 KEY=VAL)
                      </label>
                      <textarea
                        rows={2}
                        value={formData.env}
                        onChange={(e) => setFormData({ ...formData, env: e.target.value })}
                        placeholder="API_KEY=key_123&#10;DEBUG=1"
                        className="ui-field w-full text-xs font-mono"
                      />
                    </div>
                  </>
                ) : (
                  <>
                    <div>
                      <label className="mb-1 block text-xs font-medium text-gray-700">
                        服务地址 URL <span className="text-red-500">*</span>
                      </label>
                      <input
                        type="text"
                        value={formData.url}
                        onChange={(e) => setFormData({ ...formData, url: e.target.value })}
                        placeholder="https://api.example.com/mcp"
                        className="ui-field w-full text-xs font-mono"
                        required
                      />
                    </div>
                    <div>
                      <label className="mb-1 block text-xs font-medium text-gray-700">
                        自定义请求头 Headers (每行 Header: Value)
                      </label>
                      <textarea
                        rows={3}
                        value={formData.headers}
                        onChange={(e) => setFormData({ ...formData, headers: e.target.value })}
                        placeholder="X-API-Key: secret_key&#10;X-Custom: value"
                        className="ui-field w-full text-xs font-mono"
                      />
                    </div>
                  </>
                )}

                <div className="grid grid-cols-2 gap-3 pt-1 border-t border-line">
                  <div>
                    <label className="mb-1 block text-xs font-medium text-gray-700">
                      工具名称前缀 Prefix (可选)
                    </label>
                    <input
                      type="text"
                      value={formData.toolPrefix}
                      onChange={(e) => setFormData({ ...formData, toolPrefix: e.target.value })}
                      placeholder="如 fetch_"
                      className="ui-field w-full text-xs font-mono"
                    />
                  </div>
                  <div>
                    <label className="mb-1 block text-xs font-medium text-gray-700">
                      超时时间 Timeout (秒)
                    </label>
                    <input
                      type="number"
                      value={formData.timeout}
                      onChange={(e) => setFormData({ ...formData, timeout: e.target.value })}
                      placeholder="12"
                      className="ui-field w-full text-xs"
                    />
                  </div>
                </div>

                <div className="flex items-center gap-2 pt-2">
                  <label className="flex cursor-pointer items-center gap-2 text-xs font-medium text-gray-700">
                    <input
                      type="checkbox"
                      checked={formData.enabled}
                      onChange={(e) => setFormData({ ...formData, enabled: e.target.checked })}
                      className="ui-check h-4 w-4"
                    />
                    <span>添加后立即启用该服务</span>
                  </label>
                </div>

                <div className="flex items-center justify-end gap-2 border-t border-line pt-3 mt-4">
                  <button
                    type="button"
                    onClick={() => setIsModalOpen(false)}
                    className="ui-button ui-compact border border-line text-gray-600"
                  >
                    取消
                  </button>
                  <button
                    type="submit"
                    disabled={isSaving}
                    className="ui-button ui-compact ui-primary"
                  >
                    {isSaving ? '正在保存…' : '保存'}
                  </button>
                </div>
              </form>
            </div>
          </div>
        </Portal>
      )}

      {/* Delete Confirmation Modal */}
      {deleteConfirmName && (
        <Portal>
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-xs p-4">
            <div className="w-full max-w-md rounded-window border border-line bg-surface p-5 shadow-2xl">
              <h3 className="text-base font-semibold text-gray-900">确认删除 MCP 服务器</h3>
              <p className="mt-2 text-xs text-gray-600">
                确定要删除服务器 <span className="font-semibold text-red-600 font-mono">{deleteConfirmName}</span> 吗？
                此操作将从配置文件中永久移除该服务器并断开其连接。
              </p>
              <div className="mt-5 flex items-center justify-end gap-2">
                <button
                  type="button"
                  onClick={() => setDeleteConfirmName(null)}
                  className="ui-button ui-compact border border-line text-gray-600"
                >
                  取消
                </button>
                <button
                  type="button"
                  onClick={() => void handleDeleteServer(deleteConfirmName)}
                  disabled={isDeleting}
                  className="ui-button ui-compact ui-danger"
                >
                  {isDeleting ? '正在删除…' : '确认删除'}
                </button>
              </div>
            </div>
          </div>
        </Portal>
      )}
    </div>
  );
};
