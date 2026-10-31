import React from 'react';
import { useShallow } from 'zustand/react/shallow';
import { useConsoleStore } from '../stores/useConsoleStore';
import { ConsolePanel, ConsolePanelRow } from './consolePanel.tsx';

/**
 * What the console knows about the session it is attached to.
 *
 * It opens from the sidebar's actions row, next to the other context panels, and is
 * anchored to that row.  It used to open from the session title in the header, which
 * is the window's caption drag handle in an installed window: a reader aiming to
 * move the window got a panel instead.  The trigger is at the bottom of the window,
 * so the panel opens *above* it (the `ConsolePanel` default) and lines up with the
 * row's start edge, like the diagnostics panel beside it.
 */
export const SessionInfoPanel: React.FC<{
  anchor: HTMLElement | null;
  onClose: () => void;
}> = ({ anchor, onClose }) => {
  const {
    workspacePath,
    gitBranch,
    sessionTitle,
    currentSession,
    modelName,
    connectionState,
    usage,
    metricsLabel,
  } = useConsoleStore(
    // Only the fields this panel paints: a reasoning delta must not re-render it.
    useShallow((state) => ({
      workspacePath: state.workspacePath,
      gitBranch: state.gitBranch,
      sessionTitle: state.sessionTitle,
      currentSession: state.currentSession,
      modelName: state.modelName,
      connectionState: state.connectionState,
      usage: state.usage,
      metricsLabel: state.metricsLabel,
    })),
  );

  return (
    <ConsolePanel title="会话信息" anchor={anchor} onClose={onClose} side="top" align="start">
      <ConsolePanelRow label="工作区" value={workspacePath || '-'} />
      <ConsolePanelRow label="项目" value={currentSession.project_id || '-'} />
      <ConsolePanelRow label="分支" value={gitBranch || '-'} />
      <ConsolePanelRow label="会话" value={sessionTitle || '-'} />
      <ConsolePanelRow label="thread_id" value={currentSession.thread_id || '-'} />
      <ConsolePanelRow label="模型" value={modelName || '-'} />
      <ConsolePanelRow label="连接" value={connectionState} />
      <ConsolePanelRow label="用量" value={metricsLabel || '-'} />
      <ConsolePanelRow
        label="上下文"
        value={usage === null || usage.contextSize === null ? '-' : usage.contextSize}
      />
    </ConsolePanel>
  );
};
