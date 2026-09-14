import React, { useState } from 'react';
import { useConsoleStore } from '../stores/useConsoleStore';
import { latestTodos, todoPanelLabel, type TodoKind } from '../stores/todoView.ts';

/** Mark + colour per todo kind, mirroring the runtime's `✓`/`●`/`○` checklist. */
const KIND_STYLE: Record<TodoKind, { mark: string; className: string }> = {
  done: { mark: '✓', className: 'text-gray-400 line-through' },
  active: { mark: '●', className: 'text-blue-700 font-medium' },
  pending: { mark: '○', className: 'text-gray-600' },
};

/**
 * Floating progress panel for the session's todo list.
 *
 * The runtime already normalises a `write_todos` call into a checklist on the
 * tool item's `preview`, so the panel reads the newest one from the transcript
 * and renders it — no extra wire surface.  It is collapsed/expanded by hand and
 * disappears entirely when the session never wrote a todo list.
 */
export const TodoPanel: React.FC = () => {
  const messages = useConsoleStore((state) => state.messages);
  const [collapsed, setCollapsed] = useState(false);
  const view = latestTodos(messages);
  if (view === null) return null;

  return (
    <div className="absolute right-2 top-2 z-20 w-72 max-w-[calc(100%-1rem)] select-none">
      <div className="overflow-hidden rounded-lg border border-gray-200 bg-surface/95 shadow-sm backdrop-blur">
        <button
          type="button"
          onClick={() => setCollapsed((v) => !v)}
          title={collapsed ? '展开 todo' : '收起 todo'}
          className="flex w-full cursor-pointer items-center gap-2 px-2.5 py-1.5 text-left transition-colors hover:bg-gray-50"
        >
          <span className="material-symbols-outlined text-[15px] text-gray-500">checklist</span>
          <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-gray-700">
            {todoPanelLabel(view)}
          </span>
          {view.omitted > 0 && (
            <span className="shrink-0 font-mono text-[10px] text-gray-400">+{view.omitted}</span>
          )}
          <span className="material-symbols-outlined text-[16px] text-gray-400">
            {collapsed ? 'expand_more' : 'expand_less'}
          </span>
        </button>
        {!collapsed && (
          <ul className="max-h-64 space-y-0.5 overflow-y-auto border-t border-gray-100 px-2.5 py-1.5">
            {view.items.map((item, index) => (
              <li
                key={`${index}-${item.content}`}
                className="flex items-start gap-1.5 font-mono text-[11px] leading-relaxed"
              >
                <span className={`shrink-0 ${KIND_STYLE[item.kind].className}`}>
                  {KIND_STYLE[item.kind].mark}
                </span>
                <span className={`min-w-0 break-words ${KIND_STYLE[item.kind].className}`}>
                  {item.content}
                </span>
              </li>
            ))}
            {view.omitted > 0 && (
              <li className="pt-0.5 font-mono text-[10px] text-gray-400">
                … 另有 {view.omitted} 项未包含在预览中
              </li>
            )}
          </ul>
        )}
      </div>
    </div>
  );
};
