import React, { useEffect, useState } from 'react';
import { CONSOLE_VERSION } from '../consoleInfo.ts';
import { RUNTIME_CONFIG_READ_ONLY_NOTICE } from '../stores/runtimeConfigMapper';
import { useConsoleStore } from '../stores/useConsoleStore';
import { cacheHitRate, formatSessionUsage } from '../stores/usageView.ts';

export interface SettingsDialogProps {
  onClose: () => void;
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-gray-100 py-1.5 last:border-b-0">
      <span className="shrink-0 font-mono text-[11px] text-gray-400">{label}</span>
      <span className="break-all text-right text-xs text-gray-800">{value}</span>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mt-4">
      <h2 className="mb-1 font-mono text-[11px] font-semibold uppercase tracking-wider text-gray-400">
        {title}
      </h2>
      {children}
    </section>
  );
}

/**
 * Read-only settings surface for the console.
 *
 * Every value shown here comes from data the console already holds: the
 * authenticated project context, the read-only runtime configuration
 * (`runtime.config.get`) and the latest `usage_updated` metrics.  Nothing on
 * this panel writes server state except the two paths the runtime actually
 * exposes — model rebind and per-server MCP reload — plus console logout.
 */
export const SettingsDialog: React.FC<SettingsDialogProps> = ({ onClose }) => {
  const {
    workspacePath,
    gitBranch,
    gitDirty,
    currentSession,
    sessionTitle,
    connectionState,
    pairingState,
    sessionsTotal,
    modelName,
    availableModels,
    setModel,
    thinkingLevel,
    thinkingLevels,
    canSetThinking,
    projectThinkingLevel,
    canSetProjectThinking,
    projectThinkingError,
    setProjectThinkingLevel,
    mcpServers,
    mcpEnabled,
    mcpRuntime,
    mcpConnecting,
    mcpRuntimeKnown,
    toggleMcpServer,
    canToggleMcpGlobal,
    usage,
    sessionUsage,
    logoutConsole,
  } = useConsoleStore();

  // Success is reported explicitly: the stored value changes in the row above,
  // but a write that succeeds must say so instead of leaving the user to guess
  // whether the default was persisted.
  const [projectNotice, setProjectNotice] = useState<string | null>(null);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/20 p-4"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-label="设置"
        className="max-h-[80vh] w-full max-w-lg overflow-y-auto rounded-lg border border-gray-200 bg-white p-5 font-sans shadow-xl"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-gray-100 pb-2">
          <span className="text-sm font-bold text-gray-900">设置</span>
          <button
            onClick={onClose}
            title="关闭 (Esc)"
            className="material-symbols-outlined cursor-pointer text-[18px] text-gray-400 hover:text-gray-700"
          >
            close
          </button>
        </div>

        <Section title="控制台">
          <Row label="版本" value={CONSOLE_VERSION} />
          <Row label="配对状态" value={pairingState} />
          <Row label="连接状态" value={connectionState} />
          <Row label="会话总数" value={sessionsTotal} />
        </Section>

        <Section title="工作区">
          <Row label="路径" value={workspacePath || '-'} />
          <Row label="项目" value={currentSession.project_id || '-'} />
          <Row label="分支" value={gitBranch ? `${gitBranch}${gitDirty ? ' (dirty)' : ''}` : '-'} />
          <Row label="会话" value={sessionTitle || currentSession.thread_id || '-'} />
          <Row label="thread_id" value={currentSession.thread_id || '-'} />
        </Section>

        <Section title="模型与推理">
          <Row
            label="模型"
            value={
              availableModels.length > 0 ? (
                <select
                  value={modelName}
                  onChange={(event) => {
                    void setModel(event.target.value);
                  }}
                  className="max-w-[16rem] rounded border border-gray-200 bg-white px-1 py-0.5 text-xs text-gray-800 focus:border-blue-500 focus:outline-none"
                >
                  {availableModels.map((name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
                </select>
              ) : (
                modelName || '-'
              )
            }
          />
          <Row
            label="推理级别"
            value={
              <span title={canSetThinking ? undefined : RUNTIME_CONFIG_READ_ONLY_NOTICE}>
                {thinkingLevel ?? '-'}
                {thinkingLevels.length > 0 ? (
                  <span className="text-gray-400">（可选：{thinkingLevels.join(' / ')}）</span>
                ) : null}
                {!canSetThinking && <span className="ml-1 text-gray-400">只读</span>}
              </span>
            }
          />
          <Row
            label="项目默认"
            value={
              <span
                className="flex items-center justify-end gap-2"
                title={
                  canSetProjectThinking
                    ? '项目默认只影响此后新建的会话'
                    : RUNTIME_CONFIG_READ_ONLY_NOTICE
                }
              >
                <span>{projectThinkingLevel ?? '未知'}</span>
                <select
                  value=""
                  disabled={!canSetProjectThinking || thinkingLevels.length === 0}
                  onChange={(event) => {
                    const next = event.target.value;
                    if (!next) return;
                    void setProjectThinkingLevel(next).then((ok) => {
                      setProjectNotice(
                        ok ? `已写入项目默认：${next}（此后新建会话生效）` : null,
                      );
                    });
                  }}
                  title="设为项目默认"
                  className="max-w-[10rem] rounded border border-gray-200 bg-white px-1 py-0.5 text-xs text-gray-800 focus:border-blue-500 focus:outline-none disabled:cursor-not-allowed disabled:text-gray-400"
                >
                  <option value="">设为项目默认…</option>
                  {thinkingLevels.map((level) => (
                    <option key={level} value={level}>
                      {level}
                    </option>
                  ))}
                </select>
              </span>
            }
          />
          {projectNotice !== null && projectThinkingError === null && (
            <p className="mt-1 text-[11px] leading-relaxed text-green-700">{projectNotice}</p>
          )}
          {projectThinkingError !== null && (
            <p className="mt-1 text-[11px] leading-relaxed text-red-600">
              设置项目默认失败：{projectThinkingError}
            </p>
          )}
          <p className="mt-1 text-[11px] leading-relaxed text-gray-500">
            项目默认写入项目设置层（daemon 重启后仍生效），只作用于此后新建的会话；上方「推理级别」与底栏显示的始终是
            <span className="font-mono"> 当前会话 </span>的实际值。
            {!canSetProjectThinking && ' 该运行时未提供项目级写端口，此项只读。'}
          </p>
          {!canSetThinking && (
            <p className="mt-1 text-[11px] leading-relaxed text-gray-500">
              运行时配置面是只读的（无写端口），推理级别由服务端设置决定，控制台无法修改。
            </p>
          )}
        </Section>

        <Section title="MCP">
          <Row label="全局" value={mcpEnabled ? '已启用' : '已停用'} />
          <Row
            label="会话连接"
            value={
              mcpConnecting
                ? '启动中…'
                : mcpRuntimeKnown
                  ? `${mcpServers.filter((server) => server.enabled && mcpRuntime[server.name]?.attached).length} / ${mcpServers.filter((server) => server.enabled).length} 已连接`
                  : '未上报'
            }
          />
          <div className="mt-1 space-y-1">
            {mcpServers.length === 0 ? (
              <div className="text-[11px] text-gray-400">未配置任何 MCP 服务器</div>
            ) : (
              mcpServers.map((server) => {
                const runtime = mcpRuntime[server.name];
                const attached = runtime?.attached === true;
                return (
                  <div
                    key={server.name}
                    onClick={() => {
                      void toggleMcpServer(server.name);
                    }}
                    className="flex cursor-pointer items-center justify-between rounded border border-gray-100 px-2 py-1 text-xs hover:bg-gray-50"
                  >
                    <span className="truncate text-gray-800">{server.name}</span>
                    <span className="font-mono text-[10px] text-gray-500">
                      {server.transport} · {server.enabled ? 'ON' : 'OFF'}
                      {server.enabled && mcpRuntimeKnown
                        ? attached
                          ? ` · 已连接 ${runtime?.loaded.length ?? 0} 工具`
                          : ' · 未连接'
                        : ''}
                    </span>
                  </div>
                );
              })
            )}
          </div>
          {!canToggleMcpGlobal && (
            <p className="mt-1 text-[11px] text-gray-500">
              全局 MCP 开关只读；可单独切换已配置的服务器。
            </p>
          )}
        </Section>

        <Section title="用量">
          {/* The session's own totals, not `metricsLabel`: that label is this
              turn's telemetry, so the row used to contradict its own name. */}
          <Row label="本会话累计" value={formatSessionUsage(sessionUsage)} />
          <Row
            label="最近一次调用"
            value={usage ? `in ${usage.lastInput} / out ${usage.lastOutput}` : '-'}
          />
          <Row
            label="上下文"
            value={usage?.contextSize === null || usage === null ? '-' : usage.contextSize}
          />
          <Row label="缓存命中率" value={cacheHitRate(sessionUsage) ?? '-'} />
        </Section>

        <div className="mt-5 flex justify-end border-t border-gray-100 pt-3">
          <button
            onClick={() => {
              void logoutConsole();
              onClose();
            }}
            className="rounded border border-red-200 bg-red-50 px-3 py-1 text-xs text-red-700 transition-colors hover:bg-red-100"
          >
            退出配对
          </button>
        </div>
      </div>
    </div>
  );
};
