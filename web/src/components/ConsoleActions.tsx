import React, { useEffect, useState } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { useConsoleStore } from '../stores/useConsoleStore';
import { ArtifactsPanel } from './ArtifactsPanel.tsx';

/**
 * The console's context actions: session info, the read-only workspace file
 * browser, runtime diagnostics and logout.
 *
 * They live at the sidebar's settings row rather than in the header, so the
 * header keeps only identity (workspace, branch, session) and the actions sit
 * with the other app-level entry point.  `orientation` picks the layout: a row
 * for the expanded sidebar's footer, a column for the collapsed 44px rail.
 *
 * Each panel opens *above* its trigger (`bottom-full`) because the triggers are
 * at the bottom of the window; Escape closes the open one.
 */
export const ConsoleActions: React.FC<{ orientation?: 'row' | 'column' }> = ({
  orientation = 'row',
}) => {
  const {
    workspacePath,
    gitBranch,
    sessionTitle,
    currentSession,
    modelName,
    connectionState,
    usage,
    metricsLabel,
    logoutConsole,
    runtimeDiagnostics,
    loadRuntimeDiagnostics,
  } = useConsoleStore(
    // Only the fields these actions paint: a reasoning delta must not re-render
    // them.
    useShallow((state) => ({
      workspacePath: state.workspacePath,
      gitBranch: state.gitBranch,
      sessionTitle: state.sessionTitle,
      currentSession: state.currentSession,
      modelName: state.modelName,
      connectionState: state.connectionState,
      usage: state.usage,
      metricsLabel: state.metricsLabel,
      logoutConsole: state.logoutConsole,
      runtimeDiagnostics: state.runtimeDiagnostics,
      loadRuntimeDiagnostics: state.loadRuntimeDiagnostics,
    })),
  );

  const [openPanel, setOpenPanel] = useState<'info' | 'diagnostics' | 'artifacts' | null>(null);

  useEffect(() => {
    if (openPanel === null) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpenPanel(null);
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [openPanel]);

  const diagnostics = runtimeDiagnostics;
  const trigger = 'material-symbols-outlined cursor-pointer text-[18px] text-gray-500 transition-colors hover:text-gray-900';

  return (
    <div className="relative shrink-0">
      <div className={orientation === 'row' ? 'flex items-center gap-1' : 'flex flex-col items-center gap-1'}>
        <button
          onClick={() => setOpenPanel((v) => (v === 'info' ? null : 'info'))}
          title="会话信息"
          className={trigger}
        >
          layers
        </button>
        <button
          onClick={() => setOpenPanel((v) => (v === 'artifacts' ? null : 'artifacts'))}
          title="工作区文件（只读，按块读取）"
          className={trigger}
        >
          folder_open
        </button>
        <button
          onClick={() => {
            setOpenPanel((v) => (v === 'diagnostics' ? null : 'diagnostics'));
            void loadRuntimeDiagnostics({ trigger: 'manual', force: true });
          }}
          title="运行时诊断（读取宿主只读端点）"
          className={trigger}
        >
          terminal
        </button>
        <button
          onClick={() => {
            void logoutConsole();
          }}
          title="退出配对（作废控制台会话，回到配对界面）"
          className={trigger}
        >
          logout
        </button>
      </div>

      {openPanel === 'info' && (
        <Panel title="会话信息" onClose={() => setOpenPanel(null)}>
          <Row label="工作区" value={workspacePath || '-'} />
          <Row label="项目" value={currentSession.project_id || '-'} />
          <Row label="分支" value={gitBranch || '-'} />
          <Row label="会话" value={sessionTitle || '-'} />
          <Row label="thread_id" value={currentSession.thread_id || '-'} />
          <Row label="模型" value={modelName || '-'} />
          <Row label="连接" value={connectionState} />
          <Row label="用量" value={metricsLabel || '-'} />
          <Row
            label="上下文"
            value={usage === null || usage.contextSize === null ? '-' : usage.contextSize}
          />
        </Panel>
      )}

      {openPanel === 'diagnostics' && (
        <Panel title="运行时诊断" onClose={() => setOpenPanel(null)}>
          <Row label="状态" value={diagnostics.status} />
          <Row
            label="daemon"
            value={
              diagnostics.view?.endpoint
                ? `${diagnostics.view.endpoint.host}:${diagnostics.view.endpoint.port}`
                : 'unknown'
            }
          />
          <Row label="state dir" value={diagnostics.view?.state_dir ?? '-'} />
          <Row label="hint" value={diagnostics.view?.hint ?? '-'} />
          {diagnostics.reason !== null && <Row label="失败原因" value={diagnostics.reason} />}
          <p className="pt-1 text-[10px] leading-relaxed text-gray-500">
            取自宿主只读端点 GET /api/runtime-status（endpoint / state_dir / hint），不含任何凭据。
          </p>
        </Panel>
      )}

      {openPanel === 'artifacts' && <ArtifactsPanel onClose={() => setOpenPanel(null)} />}
    </div>
  );
};

function Panel({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: React.ReactNode;
}) {
  return (
    <div
      role="dialog"
      aria-label={title}
      className="absolute bottom-full left-0 z-50 mb-1 w-80 rounded-control border border-gray-200 material-flyout p-3 text-left shadow-flyout"
    >
      <div className="mb-2 flex items-center justify-between border-b border-gray-100 pb-1.5">
        <span className="text-xs font-semibold text-gray-900">{title}</span>
        <button
          onClick={onClose}
          title="关闭 (Esc)"
          className="material-symbols-outlined cursor-pointer text-[16px] text-gray-400 hover:text-gray-700"
        >
          close
        </button>
      </div>
      <div className="space-y-1">{children}</div>
    </div>
  );
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-3 border-b border-gray-50 py-1 last:border-b-0">
      <span className="shrink-0 font-mono text-[10px] text-gray-400">{label}</span>
      <span className="break-all text-right text-[11px] text-gray-800">{value}</span>
    </div>
  );
}
