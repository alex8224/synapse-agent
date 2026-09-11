import React from 'react';
import { useConsoleStore } from '../stores/useConsoleStore';

export const TopBar: React.FC = () => {
  const {
    toggleSidebar,
    workspacePath,
    gitBranch,
    sessionTitle,
    metricsLabel,
    logoutConsole,
  } = useConsoleStore();

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

      <div className="flex items-center space-x-3 text-gray-600 text-xs">
        {/* No demo metrics: nothing is rendered until the runtime reports real ones. */}
        {metricsLabel !== '' && <span className="text-gray-800">{metricsLabel}</span>}
        <div className="flex items-center space-x-2 ml-2 text-gray-600">
          <span className="material-symbols-outlined text-[18px] cursor-pointer hover:text-gray-900 transition-colors">layers</span>
          <span className="material-symbols-outlined text-[18px] cursor-pointer hover:text-gray-900 transition-colors">terminal</span>
          <span
            onClick={() => { void logoutConsole(); }}
            className="material-symbols-outlined text-[18px] cursor-pointer hover:text-gray-900 transition-colors"
            title="退出配对（作废控制台会话，回到配对界面）"
          >logout</span>
        </div>
      </div>
    </header>
  );
};
