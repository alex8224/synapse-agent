import React from 'react';
import {
  TasksApp20Regular,
  Dismiss16Regular,
  CheckmarkCircle16Filled,
  Circle16Regular,
  ArrowCircleRight16Filled,
} from '@fluentui/react-icons';
import { useConsoleStore } from '../stores/useConsoleStore';
import { latestTodos, todoPanelLabel } from '../stores/todoView.ts';

/**
 * Floating progress panel for the session's todo list.
 *
 * The runtime already normalises a `write_todos` call into a checklist on the
 * tool item's `preview`, so the panel reads the newest one from the transcript
 * and renders it — no extra wire surface.  It is collapsed/expanded by hand and
 * disappears entirely when the session never wrote a todo list.
 */
export const TodoPanel: React.FC<{ onClose?: () => void }> = ({ onClose }) => {
  const messages = useConsoleStore((state) => state.messages);
  const view = latestTodos(messages);
  if (view === null) return null;

  return (
    <div className="select-none space-y-2">
      <div className="flex items-center justify-between border-b border-line/60 pb-2">
        <div className="flex min-w-0 items-center gap-2">
          <TasksApp20Regular aria-hidden="true" className="shrink-0 text-accent" style={{ fontSize: '16px' }} />
          <span className="min-w-0 truncate font-sans text-xs font-semibold text-gray-800">
            {todoPanelLabel(view)}
          </span>
          {view.omitted > 0 && (
            <span className="shrink-0 font-mono text-[10px] text-gray-400">+{view.omitted}</span>
          )}
        </div>
        {onClose && (
          <button
            type="button"
            onClick={onClose}
            title="关闭 (Esc)"
            className="ui-icon-button ui-compact shrink-0 text-gray-400 hover:text-gray-700"
          >
            <Dismiss16Regular aria-hidden="true" />
          </button>
        )}
      </div>

      <ul className="fluent-scrollbar max-h-72 space-y-1.5 overflow-y-auto pr-1">
            {view.items.map((item, index) => (
              <li
                key={`${index}-${item.content}`}
                className="flex items-start gap-2 text-xs leading-relaxed"
              >
                {item.kind === 'done' ? (
                  <CheckmarkCircle16Filled aria-hidden="true" className="mt-0.5 shrink-0 text-emerald-600" />
                ) : item.kind === 'active' ? (
                  <ArrowCircleRight16Filled aria-hidden="true" className="mt-0.5 shrink-0 text-accent animate-pulse" />
                ) : (
                  <Circle16Regular aria-hidden="true" className="mt-0.5 shrink-0 text-gray-400" />
                )}
                <span
                  className={`min-w-0 break-words font-sans ${
                    item.kind === 'done'
                      ? 'text-gray-400 line-through decoration-gray-300'
                      : item.kind === 'active'
                        ? 'font-medium text-gray-900'
                        : 'text-gray-600'
                  }`}
                >
                  {item.content}
                </span>
              </li>
            ))}
            {view.omitted > 0 && (
              <li className="pt-1 font-mono text-[10px] text-gray-400">
                … 另有 {view.omitted} 项未包含在预览中
              </li>
            )}
          </ul>
    </div>
  );
};
