import React, { useEffect, useState } from 'react';
import { useConsoleStore } from '../stores/useConsoleStore';
import { ArtifactsPanel } from './ArtifactsPanel.tsx';

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
      className="absolute right-0 top-9 z-50 w-80 rounded-md border border-gray-200 bg-white p-3 text-left shadow-xl"
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

export const TopBar: React.FC = () => {
  const {
    toggleSidebar,
    workspacePath,
    gitBranch,
    sessionTitle,
    metricsLabel,
    logoutConsole,
    currentSession,
    modelName,
    connectionState,
    usage,
    runtimeDiagnostics,
    loadRuntimeDiagnostics,
  } = useConsoleStore();

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

  return (
    <header className="bg-white border-b border-[#e5e7eb] flex justify-between items-center h-10 px-4 w-full shrink-0 z-20 select-none text-xs font-mono">
      <div className="flex items-center space-x-3">
        <button
          onClick={toggleSidebar}
          className="text-gray-700 hover:bg-gray-100 p-1 rounded transition-colors flex items-center"
          title="Toggle Sidebar"
        >
          <span className="material-symbols-outlined text-[18px]">dock_to_left</span>
        </button>

        <div className="flex items-center space-x-1.5 text-gray-800">
          <span className="material-symbols-outlined text-[16px] text-gray-600">folder</span>
          <span>{workspacePath}</span>
        </div>

        <span className="text-gray-300 mx-1">|</span>

        <div className="flex items-center space-x-1.5 text-gray-800">
          <span className="w-2 h-2 rounded-full bg-emerald-500 inline-block"></span>
          <span className="material-symbols-outlined text-[15px] text-gray-600">fork_right</span>
          <span>{gitBranch}</span>
        </div>

        <span className="text-gray-300 mx-1">|</span>

        <div className="text-blue-600 font-medium">{sessionTitle}</div>
      </div>

      <div className="flex items-center text-gray-600 text-xs">
        {/* Telemetry lives in the bottom status bar; the header keeps context + panels. */}
        <div className="relative flex items-center space-x-2 text-gray-600">
          <button
            onClick={() => setOpenPanel((v) => (v === 'info' ? null : 'info'))}
            title="会话信息"
            className="material-symbols-outlined text-[18px] cursor-pointer hover:text-gray-900 transition-colors"
          >
            layers
          </button>
          <button
            onClick={() => setOpenPanel((v) => (v === 'artifacts' ? null : 'artifacts'))}
            title="工作区文件（只读，按块读取）"
            className="material-symbols-outlined text-[18px] cursor-pointer hover:text-gray-900 transition-colors"
          >
            folder_open
          </button>
          <button
            onClick={() => {
              setOpenPanel((v) => (v === 'diagnostics' ? null : 'diagnostics'));
              void loadRuntimeDiagnostics({ trigger: 'manual', force: true });
            }}
            title="运行时诊断（读取宿主只读端点）"
            className="material-symbols-outlined text-[18px] cursor-pointer hover:text-gray-900 transition-colors"
          >
            terminal
          </button>
          <button
            onClick={() => {
              void logoutConsole();
            }}
            className="material-symbols-outlined text-[18px] cursor-pointer hover:text-gray-900 transition-colors"
            title="退出配对（作废控制台会话，回到配对界面）"
          >
            logout
          </button>

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
      </div>
    </header>
  );
};
