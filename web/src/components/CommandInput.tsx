import React, { useState } from 'react';
import { useConsoleStore } from '../stores/useConsoleStore';

export const CommandInput: React.FC = () => {
  const [text, setText] = useState('');
  const { steerQueueCount, runtimeStatus, submitPrompt } = useConsoleStore();

  const handleSubmit = (e?: React.FormEvent) => {
    if (e) e.preventDefault();
    if (!text.trim()) return;
    submitPrompt(text.trim());
    setText('');
  };

  return (
    <div className="absolute bottom-10 left-0 w-full px-8 pointer-events-none flex justify-center z-30">
      <div className="w-full max-w-3xl pointer-events-auto bg-white border border-[#e5e7eb] rounded-lg shadow-sm flex flex-col focus-within:border-blue-500 transition-colors">
        <div className="px-3.5 py-1.5 border-b border-[#f1f2f4] bg-[#fbfcfd] rounded-t-lg">
          <span className="font-mono text-gray-500 text-xs">{runtimeStatus === 'running' ? `Steer queue: ${steerQueueCount} queued` : 'Ready'}</span>
        </div>
        <form onSubmit={handleSubmit} className="flex items-center px-3 py-2.5">
          <span className="text-gray-400 mr-2 text-xs font-mono font-semibold">›</span>
          <input
            type="text"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Build anything (/ for commands, @ for files)"
            className="flex-1 bg-transparent border-none text-gray-900 text-xs placeholder:text-gray-400 focus:outline-none font-sans"
          />
          <button
            type="submit"
            disabled={!text.trim()}
            className="w-7 h-7 rounded-full bg-[#2563eb] text-white flex items-center justify-center hover:bg-blue-700 transition-colors ml-2 shrink-0 disabled:opacity-40 cursor-pointer"
            title="Send (Enter)"
          >
            <span className="material-symbols-outlined text-[16px]">arrow_upward</span>
          </button>
        </form>
      </div>
    </div>
  );
};
