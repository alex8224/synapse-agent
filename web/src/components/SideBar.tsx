import React from 'react';
import { useConsoleStore } from '../stores/useConsoleStore';

export const SideBar: React.FC = () => {
  const {
    isSidebarCollapsed,
    sessions,
    createNewSession,
    currentSession,
    switchSession,
  } = useConsoleStore();

  if (isSidebarCollapsed) {
    return null;
  }

  return (
    <nav className="bg-[#f8f9fa] border-r border-[#e5e7eb] h-full w-[220px] flex flex-col py-3 shrink-0 select-none text-xs font-sans">
      <div className="flex-1 overflow-y-auto px-2">
        <div className="mt-1 px-2">
          <div className="flex items-center justify-between mb-2">
            <span className="text-[11px] font-semibold text-gray-400 uppercase tracking-wider font-mono">RECENT SESSIONS</span>
            <button
              onClick={() => createNewSession()}
              className="flex items-center justify-center w-5 h-5 rounded hover:bg-gray-200 text-gray-500 hover:text-gray-900 transition-colors cursor-pointer"
              title="新建会话 (Ctrl+N)"
            >
              <span className="material-symbols-outlined text-[15px]">add</span>
            </button>
          </div>

          <ul className="space-y-1 text-gray-600 text-xs">
            {sessions.map((sess) => {
              const isSelected = sess.thread_id === currentSession.thread_id;
              return (
                <li
                  key={sess.thread_id}
                  onClick={() => switchSession(sess.thread_id, sess.title)}
                  title={sess.title}
                  className={`cursor-pointer px-1.5 py-1 rounded hover:bg-gray-200/60 transition-colors truncate ${
                    isSelected ? 'font-medium text-gray-900 bg-gray-200/80 shadow-2xs' : 'text-gray-600'
                  }`}
                >
                  {sess.title}
                </li>
              );
            })}
          </ul>
        </div>
      </div>

      <div className="mt-auto px-3 pt-3 border-t border-[#e5e7eb]">
        <a className="flex items-center space-x-2 text-gray-600 hover:bg-gray-200/60 p-1.5 rounded transition-colors text-xs" href="#">
          <span className="material-symbols-outlined text-[17px] text-gray-500">settings</span>
          <span className="flex-1">设置</span>
          <span className="text-gray-400 font-mono text-[11px]">Synapse v0.1.44</span>
        </a>
      </div>
    </nav>
  );
};
