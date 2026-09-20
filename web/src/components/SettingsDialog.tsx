import { Dismiss20Regular } from '@fluentui/react-icons';
import React, { useEffect, useRef, useState } from 'react';
import { CONSOLE_VERSION } from '../consoleInfo.ts';
import { APPEARANCE_OPTIONS, useAppearanceStore } from '../stores/appearance.ts';
import { Portal } from './Portal.tsx';
import { SpeechEngineSection } from './SpeechEngineSection.tsx';
import { useDialogKeyboardNav } from './keyboardNav.ts';
import { RUNTIME_CONFIG_READ_ONLY_NOTICE } from '../stores/runtimeConfigMapper';
import { useConsoleStore } from '../stores/useConsoleStore';
import { useShallow } from 'zustand/react/shallow';
import { cacheHitRate, formatSessionUsage } from '../stores/usageView.ts';
import {
  notificationPermission,
  permissionLabel,
  requestNotificationPermission,
} from '../stores/backgroundAlerts';
import type { NotificationPermissionState } from '../stores/backgroundAlerts';

export interface SettingsDialogProps {
  onClose: () => void;
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-line py-2.5 last:border-b-0">
      <span className="shrink-0 text-sm text-gray-600">{label}</span>
      <span className="min-w-0 break-all text-right text-sm text-gray-900">{value}</span>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mt-4">
      <h2 className="ui-section-label mb-2">
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
  // The appearance is the one preference that is not the runtime's: it lives in
  // the browser, so it is read from its own small store rather than the console's.
  const appearance = useAppearanceStore((state) => state.appearance);
  const setAppearance = useAppearanceStore((state) => state.setAppearance);
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
  } = useConsoleStore(
    // Only the fields this dialog paints: a reasoning delta must not re-render it.
    useShallow((state) => ({
      workspacePath: state.workspacePath,
      gitBranch: state.gitBranch,
      gitDirty: state.gitDirty,
      currentSession: state.currentSession,
      sessionTitle: state.sessionTitle,
      connectionState: state.connectionState,
      pairingState: state.pairingState,
      sessionsTotal: state.sessionsTotal,
      modelName: state.modelName,
      availableModels: state.availableModels,
      setModel: state.setModel,
      thinkingLevel: state.thinkingLevel,
      thinkingLevels: state.thinkingLevels,
      canSetThinking: state.canSetThinking,
      projectThinkingLevel: state.projectThinkingLevel,
      canSetProjectThinking: state.canSetProjectThinking,
      projectThinkingError: state.projectThinkingError,
      setProjectThinkingLevel: state.setProjectThinkingLevel,
      mcpServers: state.mcpServers,
      mcpEnabled: state.mcpEnabled,
      mcpRuntime: state.mcpRuntime,
      mcpConnecting: state.mcpConnecting,
      mcpRuntimeKnown: state.mcpRuntimeKnown,
      toggleMcpServer: state.toggleMcpServer,
      canToggleMcpGlobal: state.canToggleMcpGlobal,
      usage: state.usage,
      sessionUsage: state.sessionUsage,
      logoutConsole: state.logoutConsole,
    })),
  );

  // Success is reported explicitly: the stored value changes in the row above,
  // but a write that succeeds must say so instead of leaving the user to guess
  // whether the default was persisted.
  const [projectNotice, setProjectNotice] = useState<string | null>(null);
  // Browser-side notification permission: read once (never prompting on open),
  // then refreshed only by the button below, which is the user gesture the
  // browser requires before it will show the prompt at all.
  const [notificationState, setNotificationState] = useState<NotificationPermissionState>(() =>
    notificationPermission(),
  );
  const dialogRef = useRef<HTMLDivElement | null>(null);

  // The sidebar's settings button keeps the focus, and the dialog is portalled
  // to the body, so without this the arrows and Tab would walk the console
  // behind it before ever reaching a control inside.
  const onKeyDown = useDialogKeyboardNav(dialogRef, true);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [onClose]);

  return (
    // `Portal`: a dialog belongs to the window, not to the sidebar it is opened
    // from (an acrylic ancestor would otherwise anchor its `fixed` box to the rail).
    <Portal>
      <div
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/25 backdrop-blur-sm p-4 scrim-in"
        onClick={onClose}
      >
        <div
          role="dialog"
          aria-label="设置"
          ref={dialogRef}
          tabIndex={-1}
          onKeyDown={onKeyDown}
          // No visible scrollbar: a dialog is a window of its own, and the wheel /
          // keyboard still move it (the console hides its other scrollers too).
          className="no-scrollbar max-h-[85vh] w-full max-w-lg overflow-y-auto rounded-card border border-line/70 material-flyout flyout-in p-5 font-sans shadow-flyout"
          onClick={(event) => event.stopPropagation()}
        >
        <div className="flex items-center justify-between border-b border-gray-100 pb-2">
          <span className="ui-settings-title text-gray-900">设置</span>
          <button
            onClick={onClose}
            title="关闭 (Esc)"
            aria-label="关闭设置"
            className="ui-icon-button"
          >
            <Dismiss20Regular aria-hidden="true" />
          </button>
        </div>

        <Section title="控制台">
          <Row label="版本" value={CONSOLE_VERSION} />
          <Row label="配对状态" value={pairingState} />
          <Row label="连接状态" value={connectionState} />
          <Row label="会话总数" value={sessionsTotal} />
        </Section>

        <Section title="后台通知">
          <Row label="浏览器权限" value={permissionLabel(notificationState)} />
          <div className="mt-1 flex items-start justify-between gap-3">
            <p className="text-[11px] leading-relaxed text-gray-500">
              窗口不在前台时，等待审批与本轮结束会弹出系统通知，数量显示在已安装应用的角标上；
              回到窗口即视为已读，角标清零。通知只能由运行中的控制台页面发出——页面关闭后不会
              推送（本地宿主不使用 Web Push），服务端仍是唯一事实来源。
            </p>
            <button
              type="button"
              onClick={() => {
                void requestNotificationPermission().then(setNotificationState);
              }}
              disabled={notificationState === 'granted' || notificationState === 'unsupported'}
              className="ui-button shrink-0 border border-line bg-surface"
            >
              {notificationState === 'granted'
                ? '已启用'
                : notificationState === 'unsupported'
                  ? '不支持'
                  : '启用通知'}
            </button>
          </div>
        </Section>

        {/* The speech engine is the one preference here that the *runtime* owns,
            because it decides who transcribes the audio.  The section writes it
            and signals the composer to re-read, so the switch is live. */}
        <SpeechEngineSection />

        <Section title="外观">
          {/* The theme is CSS (`src/index.css`); this only chooses which one the
              document carries.  "跟随系统" keeps following the OS while it is
              selected, so a machine that switches at sunset switches the console. */}
          <Row
            label="主题"
            value={
              <span className="flex flex-wrap items-center justify-end gap-1" role="group" aria-label="主题">
                {APPEARANCE_OPTIONS.map((option) => (
                  <button
                    key={option.value}
                    type="button"
                    onClick={() => setAppearance(option.value)}
                    aria-pressed={appearance === option.value}
                    className={`ui-button border ${
                      appearance === option.value
                        ? 'ui-primary border-accent'
                        : 'border-line bg-surface'
                    }`}
                  >
                    {option.label}
                  </button>
                ))}
              </span>
            }
          />
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
                  aria-label="模型"
                  className="ui-field max-w-full w-64"
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
                  aria-label="设为项目默认"
                  className="ui-field max-w-full w-40"
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
    </Portal>
  );
};
