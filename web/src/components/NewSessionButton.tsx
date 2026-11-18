import { Add20Regular, Bot20Regular } from '@fluentui/react-icons';
import React, { useEffect, useRef, useState } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { useConsoleStore } from '../stores/useConsoleStore.ts';

/**
 * The sidebar's per-project "+" action.
 *
 * With no external agent configured this is the plain new-session button it has
 * always been. When the daemon reports local agents it becomes a menu, because
 * the backend is chosen once, at creation: a session never switches between the
 * native runtime and an external agent afterwards.
 */
export const NewSessionButton: React.FC<{
  projectId: string;
  label: string;
  onNavigate?: () => void;
  className: string;
}> = ({ projectId, label, onNavigate, className }) => {
  const { externalAgents, createSessionInProject } = useConsoleStore(
    useShallow((state) => ({
      externalAgents: state.externalAgents,
      createSessionInProject: state.createSessionInProject,
    })),
  );
  const [open, setOpen] = useState(false);
  const wrapper = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      const target = event.target as Node | null;
      if (target !== null && !wrapper.current?.contains(target)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    window.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      window.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  const start = (agentId?: string) => {
    setOpen(false);
    onNavigate?.();
    void createSessionInProject(projectId, agentId);
  };

  return (
    <div className="relative" ref={wrapper}>
      <button
        type="button"
        onClick={() => (externalAgents.length === 0 ? start() : setOpen((v) => !v))}
        title={`在 ${label} 新建会话`}
        aria-label={`在 ${label} 新建会话`}
        aria-expanded={externalAgents.length === 0 ? undefined : open}
        className={className}
      >
        <Add20Regular aria-hidden="true" />
      </button>
      {open && (
        <div
          role="group"
          aria-label={`在 ${label} 新建会话`}
          className="absolute right-0 z-50 mt-1 w-56 rounded-card border border-line/80 material-flyout flyout-in p-1.5 shadow-flyout"
        >
          <button
            type="button"
            onClick={() => start()}
            className="ui-menu-item flex w-full items-center gap-2 rounded-control px-2.5 py-1.5 text-left text-xs text-gray-700 hover:bg-gray-100/70"
          >
            Synapse（内置）
          </button>
          <div className="my-1 border-t border-line/60" />
          <div className="px-2.5 pb-1 text-[10px] uppercase tracking-wide text-gray-400">
            本机外部 Agent
          </div>
          {externalAgents.map((agent) => (
            <button
              key={agent.id}
              type="button"
              onClick={() => start(agent.id)}
              className="ui-menu-item flex w-full items-center gap-2 rounded-control px-2.5 py-1.5 text-left text-xs text-gray-700 hover:bg-gray-100/70"
            >
              <Bot20Regular aria-hidden="true" className="shrink-0" />
              <span className="truncate">{agent.name}</span>
              {agent.permission_mode === 'full_access' && (
                <span className="ml-auto shrink-0 text-[10px] font-medium text-amber-700">完全权限</span>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  );
};
