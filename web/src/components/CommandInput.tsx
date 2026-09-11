import React, { useState } from 'react';
import { useConsoleStore } from '../stores/useConsoleStore';

/**
 * Floating command card.
 *
 * The single round button is the primary action for the current state: `↑`
 * sends when idle, and becomes an enabled `■` stop button while a turn is
 * running (the previous behaviour left it looking disabled because the input
 * was empty, with no way to interrupt from the UI).  Typing while busy still
 * queues a steer — that path is Enter, not the button.
 */
export const CommandInput: React.FC = () => {
  const [text, setText] = useState('');
  const { steerQueueCount, runtimeStatus, submitPrompt, cancelActiveTurn } = useConsoleStore();

  const busy = runtimeStatus === 'running';
  const ready = text.trim() !== '';

  const handleSubmit = (e?: React.FormEvent) => {
    if (e) e.preventDefault();
    if (!ready) return;
    submitPrompt(text.trim());
    setText('');
  };

  return (
    <div className="absolute bottom-10 left-0 w-full px-8 pointer-events-none flex justify-center z-30">
      <div className="w-full max-w-3xl pointer-events-auto bg-white border border-[#e5e7eb] rounded-lg shadow-sm flex flex-col focus-within:border-blue-500 transition-colors">
        <div className="flex items-center gap-2 rounded-t-lg border-b border-[#f1f2f4] bg-[#fbfcfd] px-3.5 py-1.5">
          {busy ? (
            <>
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-blue-600" />
              <span className="font-mono text-xs text-gray-600">
                运行中 · Steer 队列 {steerQueueCount}
              </span>
            </>
          ) : (
            <span className="font-mono text-xs text-gray-500">Ready</span>
          )}
        </div>
        <form onSubmit={handleSubmit} className="flex items-center px-3 py-2.5">
          <span className="text-gray-400 mr-2 text-xs font-mono font-semibold">›</span>
          <input
            type="text"
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                handleSubmit();
              }
            }}
            placeholder={
              busy ? '运行中：输入内容回车可插话排队' : 'Build anything (/ for commands, @ for files)'
            }
            className="flex-1 bg-transparent border-none text-gray-900 text-xs placeholder:text-gray-400 focus:outline-none font-sans"
          />
          {busy ? (
            <button
              type="button"
              onClick={() => {
                void cancelActiveTurn();
              }}
              title="停止当前轮次 (Ctrl+C)"
              className="ml-2 flex h-7 w-7 shrink-0 cursor-pointer items-center justify-center rounded-full bg-[#dc2626] text-white transition-colors hover:bg-red-700"
            >
              <span className="material-symbols-outlined text-[16px]">stop</span>
            </button>
          ) : (
            <button
              type="submit"
              disabled={!ready}
              title="Send (Enter)"
              className="ml-2 flex h-7 w-7 shrink-0 cursor-pointer items-center justify-center rounded-full bg-[#2563eb] text-white transition-colors hover:bg-blue-700 disabled:opacity-40"
            >
              <span className="material-symbols-outlined text-[16px]">arrow_upward</span>
            </button>
          )}
        </form>
      </div>
    </div>
  );
};
