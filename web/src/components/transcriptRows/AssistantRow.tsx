import {
  Add16Regular,
  BranchFork16Regular,
  Checkmark16Regular,
  Copy16Regular,
  Send16Regular,
  WindowConsole20Regular,
} from '@fluentui/react-icons';
import React, { useEffect, useRef, useState } from 'react';
import { Markdown } from '../Markdown.tsx';
import type { RowRenderProps } from './context.ts';
import { useCopyFlag } from './useCopyFlag.ts';
import { useTerminalStore } from '../../stores/useTerminalStore.ts';
import { useConsoleStore } from '../../stores/useConsoleStore.ts';
import { buildSessionContextHandoffPrompt } from '../terminal/XtermView.tsx';

/**
 * The assistant's answer: body typography on the left, with copy, fork, and
 * direct dispatch-to-terminal-agent actions.
 *
 * It hugs the left edge and is capped at 80% of the reading column -- the pair the
 * user row's 20% inset completes (`transcriptLayoutGuard.test.ts`).
 */
export const AssistantRow = React.memo(function AssistantRow({ message, actions }: RowRenderProps) {
  const [copied, copy] = useCopyFlag();
  const [dispatchMenuOpen, setDispatchMenuOpen] = useState(false);
  const [sentNotice, setSentNotice] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  const sessions = useTerminalStore((s) => s.sessions);
  const activeProjectId = useConsoleStore((s) => s.activeProjectId);
  const projects = useConsoleStore((s) => s.projects);

  useEffect(() => {
    if (!dispatchMenuOpen) return;
    const onPointerDown = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setDispatchMenuOpen(false);
      }
    };
    document.addEventListener('mousedown', onPointerDown);
    return () => document.removeEventListener('mousedown', onPointerDown);
  }, [dispatchMenuOpen]);

  const workspacePath = projects.find((p) => p.project_id === activeProjectId)?.workspace_path;

  const markSent = () => {
    setDispatchMenuOpen(false);
    setSentNotice(true);
    window.setTimeout(() => setSentNotice(false), 1500);
  };

  const handleDispatchToSession = (sessionId: string) => {
    const content = (message.content ?? '').trim();
    if (!content) return;
    const handoff = buildSessionContextHandoffPrompt(`请根据以下主会话方案执行: ${content}`);
    void useTerminalStore.getState().dispatchToSession(sessionId, handoff);
    markSent();
  };

  const handleSpawnWithPlan = (agentId: string) => {
    const content = (message.content ?? '').trim();
    if (!content) return;
    const handoff = buildSessionContextHandoffPrompt(`请根据以下主会话方案执行: ${content}`);
    void useTerminalStore.getState().spawnAndDispatch(workspacePath, agentId, handoff);
    markSent();
  };

  const handleBroadcastPlan = () => {
    const content = (message.content ?? '').trim();
    if (!content) return;
    const handoff = buildSessionContextHandoffPrompt(`请根据以下主会话方案执行: ${content}`);
    void useTerminalStore.getState().broadcastInput(handoff);
    markSent();
  };

  return (
    <div className="flex max-w-[80%] flex-col items-start gap-1.5 w-full min-w-0">
      <div className="text-base leading-relaxed font-sans text-gray-900 w-full min-w-0">
        <Markdown text={message.content ?? ''} />
      </div>
      <div className="flex items-center gap-1.5 pt-0.5">
        <button
          type="button"
          onClick={() => copy(message.content ?? '')}
          title={copied ? '已复制' : '复制回答'}
          aria-label="复制回答"
          className="p-1 rounded text-gray-400 hover:text-gray-700 hover:bg-surface-hover cursor-pointer transition-colors"
        >
          {copied ? <Checkmark16Regular aria-hidden="true" className="text-accent-fg" /> : <Copy16Regular aria-hidden="true" />}
        </button>
        <button
          type="button"
          onClick={() => actions.onFork(message.turnId ?? '')}
          title="从此处分叉新会话"
          aria-label="从此处分叉新会话"
          className="p-1 rounded text-gray-400 hover:text-gray-700 hover:bg-surface-hover cursor-pointer transition-colors"
        >
          <BranchFork16Regular aria-hidden="true" />
        </button>

        {/* Dispatch Plan/Response to Terminal Agent */}
        <div ref={menuRef} className="relative">
          <button
            type="button"
            onClick={() => setDispatchMenuOpen((prev) => !prev)}
            title="将本条回答/方案派发给终端 Agent 本体执行"
            className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] text-gray-400 hover:text-accent-fg hover:bg-surface-hover cursor-pointer transition-colors"
          >
            <WindowConsole20Regular className="h-3.5 w-3.5" />
            <span>{sentNotice ? '已派发至终端' : '派发给终端 Agent'}</span>
          </button>

          {dispatchMenuOpen && (
            <div className="material-flyout flyout-in absolute left-0 bottom-7 z-50 w-60 rounded-control border border-line bg-surface p-1.5 shadow-card">
              {sessions.length > 0 && (
                <>
                  <div className="px-2 py-0.5 text-[10px] font-semibold text-gray-400">
                    投递到已开启终端
                  </div>
                  {sessions.map((s) => (
                    <button
                      key={s.id}
                      type="button"
                      onClick={() => handleDispatchToSession(s.id)}
                      className="flex w-full items-center gap-1.5 rounded-control px-2 py-1 text-left text-xs text-gray-800 hover:bg-surface-hover"
                    >
                      <WindowConsole20Regular className="h-3.5 w-3.5 text-accent-fg shrink-0" />
                      <span className="truncate">发往 {s.title}</span>
                    </button>
                  ))}
                  {sessions.length > 1 && (
                    <button
                      type="button"
                      onClick={handleBroadcastPlan}
                      className="flex w-full items-center gap-1.5 rounded-control px-2 py-1 text-left text-xs text-accent-fg hover:bg-surface-hover"
                    >
                      <Send16Regular className="h-3.5 w-3.5 shrink-0" />
                      <span className="truncate">广播给所有已勾选终端</span>
                    </button>
                  )}
                  <div className="my-1 border-t border-line/60" />
                </>
              )}
              <div className="px-2 py-0.5 text-[10px] font-semibold text-gray-400">
                自动新建 Agent 终端并派发方案
              </div>
              {(
                [
                  { id: 'agy', label: '新建 agy CLI 并执行此方案' },
                  { id: 'claude', label: '新建 Claude Code 并执行此方案' },
                  { id: 'codex', label: '新建 Codex CLI 并执行此方案' },
                ] as const
              ).map((item) => (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => handleSpawnWithPlan(item.id)}
                  className="flex w-full items-center gap-1.5 rounded-control px-2 py-1 text-left text-xs text-gray-800 hover:bg-surface-hover"
                >
                  <Add16Regular className="h-3.5 w-3.5 text-accent-fg shrink-0" />
                  <span className="truncate">{item.label}</span>
                </button>
              ))}
            </div>
          )}
        </div>

        <span className="font-mono text-[10px] text-gray-400">{message.timestamp}</span>
      </div>
    </div>
  );
});
