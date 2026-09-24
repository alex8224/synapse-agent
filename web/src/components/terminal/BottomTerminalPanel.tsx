/**
 * Central Bottom Integrated Terminal Panel (Fluent 2 Compliant).
 *
 * Implements:
 * - Positioned at the bottom of the central workspace with 100% horizontal width
 * - Draggable top border resizer with double-click reset
 * - Multi-tab terminal management (+ new, x close, switch)
 * - Split terminal view (side-by-side) for concurrent workflows
 * - Maximize/restore full height immersion
 * - Fluent Design 2 Mica/Acrylic surface and subtle shadows
 * - Action toolbar: clear screen, send output to composer, close
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Add16Regular,
  Dismiss16Regular,
  SquareMultiple16Regular,
  Sparkle16Regular,
  WindowConsole20Regular,
} from '@fluentui/react-icons';
import { useShallow } from 'zustand/react/shallow';
import { useTerminalStore } from '../../stores/useTerminalStore.ts';
import { useConsoleStore } from '../../stores/useConsoleStore.ts';
import { XtermView, terminalInstances, getTerminalContext } from './XtermView.tsx';
import { insertTextAtCaret } from '../composer/composerSelection.ts';

export const BottomTerminalPanel: React.FC = () => {
  const {
    open,
    height,
    isMaximized,
    activeSessionId,
    sessions,
    setOpen,
    setHeight,
    toggleMaximize,
    setActiveSession,
    createSession,
    closeSession,
  } = useTerminalStore(
    useShallow((s) => ({
      open: s.open,
      height: s.height,
      isMaximized: s.isMaximized,
      activeSessionId: s.activeSessionId,
      sessions: s.sessions,
      setOpen: s.setOpen,
      setHeight: s.setHeight,
      toggleMaximize: s.toggleMaximize,
      setActiveSession: s.setActiveSession,
      createSession: s.createSession,
      closeSession: s.closeSession,
    })),
  );

  const activeProject = useConsoleStore((s) =>
    s.projects.find((p) => p.project_id === s.activeProjectId),
  );
  const activeWorkspace = activeProject?.workspace_path;

  const [isDragging, setIsDragging] = useState(false);
  const startYRef = useRef(0);
  const startHRef = useRef(0);

  // Auto-create initial session if terminal is opened with 0 sessions
  useEffect(() => {
    if (open && sessions.length === 0) {
      void createSession(activeWorkspace);
    }
  }, [open, sessions.length, activeWorkspace, createSession]);

  // Handle top border drag-to-resize
  const handleMouseDown = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      setIsDragging(true);
      startYRef.current = e.clientY;
      startHRef.current = height;
    },
    [height],
  );

  useEffect(() => {
    if (!isDragging) return;

    const onMouseMove = (e: MouseEvent) => {
      const deltaY = startYRef.current - e.clientY;
      setHeight(startHRef.current + deltaY);
    };

    const onMouseUp = () => {
      setIsDragging(false);
    };

    window.addEventListener('mousemove', onMouseMove);
    window.addEventListener('mouseup', onMouseUp);
    return () => {
      window.removeEventListener('mousemove', onMouseMove);
      window.removeEventListener('mouseup', onMouseUp);
    };
  }, [isDragging, setHeight]);

  const activeSession = useMemo(
    () => sessions.find((s) => s.id === activeSessionId) ?? sessions[0],
    [sessions, activeSessionId],
  );

  // Send terminal action to composer
  const handleSendToComposer = useCallback(() => {
    const composer = document.getElementById('console-composer') as HTMLElement | null;
    if (!composer) return;

    const term = activeSession ? terminalInstances.get(activeSession.id) : null;
    const context = term ? getTerminalContext(term) : { text: '', hasSelection: false };

    let prompt = '';
    if (context.text) {
      const heading = context.hasSelection
        ? '请分析以下终端选中的内容并给出解决建议：'
        : '请分析以下终端命令的执行输出与报错信息并给出解决建议：';
      prompt = `${heading}\n\`\`\`terminal\n${context.text}\n\`\`\`\n`;
    } else {
      prompt = '请根据当前工作区终端环境与执行输出进行分析与处理：\n';
    }

    composer.focus();
    if (composer instanceof HTMLTextAreaElement || composer instanceof HTMLInputElement) {
      const current = composer.value;
      const pad = current && !current.endsWith('\n') ? '\n\n' : '';
      composer.value = `${current}${pad}${prompt}`;
      composer.dispatchEvent(new Event('input', { bubbles: true }));
    } else {
      const currentText = composer.textContent || '';
      const pad = currentText && !currentText.endsWith('\n') ? '\n\n' : '';
      insertTextAtCaret(composer, `${pad}${prompt}`);
      composer.dispatchEvent(new Event('input', { bubbles: true }));
    }
  }, [activeSession]);

  const panelHeight = isMaximized ? 'calc(100% - 42px)' : `${height}px`;

  return (
    <div
      inert={!open}
      style={{ height: open ? panelHeight : '0px' }}
      className={`material-chrome relative w-full shrink-0 flex flex-col overflow-hidden select-none border-t border-line shadow-panel backdrop-blur-md ${
        open ? 'opacity-100' : 'opacity-0 pointer-events-none border-t-0'
      } ${isDragging ? '' : 'transition-[height] duration-250 ease-[cubic-bezier(0,0,0,1)]'}`}
    >
      {/* Draggable Resizer Bar */}
      <div
        onMouseDown={handleMouseDown}
        onDoubleClick={() => setHeight(300)}
        title="按住上下拖拽调节终端高度 (双击复位)"
        className={`group absolute top-0 left-0 right-0 z-50 h-2 cursor-row-resize transition-colors ${
          isDragging ? 'bg-accent/80' : 'bg-transparent hover:bg-accent/30'
        }`}
      />

      {/* Terminal Header with Multi-Tabs and Complex Action Bar */}
      <div className="flex h-9 items-center justify-between border-b border-line/70 bg-surface/90 px-2.5 pt-0.5 select-none shrink-0 backdrop-blur-sm">
        {/* Left: Session Tabs */}
        <div className="flex items-center gap-1 overflow-x-auto fluent-scrollbar min-w-0 pr-2">
          {sessions.map((session) => {
            const isActive = session.id === activeSession?.id;
            return (
              <div
                key={session.id}
                onClick={() => setActiveSession(session.id)}
                className={`group flex items-center gap-1.5 rounded-control px-2.5 py-1 text-xs font-mono font-medium transition-colors cursor-pointer shrink-0 border ${
                  isActive
                    ? 'bg-surface text-gray-900 border-line/60 shadow-xs'
                    : 'text-gray-500 hover:text-gray-900 hover:bg-surface-hover/70 border-transparent'
                }`}
              >
                <WindowConsole20Regular className={`text-xs ${isActive ? 'text-accent' : 'text-gray-400'}`} style={{ fontSize: '14px' }} />
                <span className="truncate max-w-[130px]">{session.title}</span>
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    void closeSession(session.id);
                  }}
                  title="关闭终端"
                  className="rounded p-0.5 text-gray-400 opacity-60 hover:opacity-100 hover:bg-red-500/20 hover:text-red-600 transition-all"
                >
                  <Dismiss16Regular className="text-[10px]" />
                </button>
              </div>
            );
          })}

          {/* New Terminal Tab Button */}
          <button
            type="button"
            onClick={() => void createSession(activeWorkspace)}
            title="新建终端会话"
            className="flex h-6 w-6 items-center justify-center rounded-control text-gray-500 hover:text-gray-900 hover:bg-surface-hover transition-colors shrink-0"
          >
            <Add16Regular className="text-xs" />
          </button>
        </div>

        {/* Right: Complex Actions Toolbar */}
        <div className="flex items-center gap-1 shrink-0">
          {/* Send to Composer / Agent */}
          <button
            type="button"
            onClick={handleSendToComposer}
            title="将终端诊断发送给 Agent"
            className="flex items-center gap-1 rounded-control px-2 py-1 text-[11px] text-gray-500 hover:text-gray-900 hover:bg-surface-hover transition-colors"
          >
            <Sparkle16Regular className="text-xs text-purple-600" />
            <span className="hidden sm:inline">向 Agent 提问</span>
          </button>

          {/* Maximize / Restore */}
          <button
            type="button"
            onClick={toggleMaximize}
            title={isMaximized ? '还原面板尺寸' : '最大化终端面板'}
            className={`ui-icon-button ui-compact text-gray-500 hover:text-gray-900 ${
              isMaximized ? 'text-accent' : ''
            }`}
          >
            <SquareMultiple16Regular className="text-xs" />
          </button>

          {/* Close Panel */}
          <button
            type="button"
            onClick={() => setOpen(false)}
            title="收起终端面板 (Ctrl+`)"
            className="ui-icon-button ui-compact text-gray-400 hover:text-gray-800"
          >
            <Dismiss16Regular />
          </button>
        </div>
      </div>

      {/* Terminal View Body: Keep-Alive all sessions in DOM */}
      <div className="flex-1 flex overflow-hidden bg-canvas relative">
        {sessions.length > 0 ? (
          sessions.map((session) => {
            const isActive = session.id === activeSession?.id;
            return (
              <div
                key={session.id}
                className={`absolute inset-0 h-full w-full overflow-hidden ${
                  isActive ? 'block z-10' : 'hidden z-0'
                }`}
              >
                <XtermView session={session} isActive={isActive} />
              </div>
            );
          })
        ) : (
          <div className="flex-1 flex items-center justify-center text-xs text-gray-400">
            暂无活动终端会话，点击上方 “+” 新建
          </div>
        )}
      </div>
    </div>
  );
};
