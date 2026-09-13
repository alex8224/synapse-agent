import React, { useEffect, useRef, useState } from 'react';
import { useConsoleStore } from '../stores/useConsoleStore';
import { RUNTIME_CONFIG_READ_ONLY_NOTICE } from '../stores/runtimeConfigMapper';

/**
 * The model and reasoning-level pickers, rendered inside the composer's control
 * row rather than in the status bar: they configure the *next* turn, so they
 * belong next to the input that starts it.
 *
 * Both popovers keep the bar's dismissal contract — each trigger+panel sits in
 * its own ref'd wrapper, and they close on a click outside, on Escape, or when
 * the other one opens (two overlapping popovers must never be open at once).
 * `F2` still toggles the model picker, so the advertised shortcut keeps working
 * from wherever the composer is.
 */
export const ModelControls: React.FC = () => {
  const {
    modelName,
    availableModels,
    setModel,
    thinkingLevel,
    thinkingLevels,
    canSetThinking,
    setThinkingLevel,
    thinkingLevelError,
  } = useConsoleStore();

  const [showModelPicker, setShowModelPicker] = useState(false);
  const [showThinkingPicker, setShowThinkingPicker] = useState(false);
  const [modelSearch, setModelSearch] = useState('');
  const modelRef = useRef<HTMLDivElement | null>(null);
  const thinkingRef = useRef<HTMLDivElement | null>(null);
  const popoverOpen = showModelPicker || showThinkingPicker;

  const closeOthers = () => {
    setShowModelPicker(false);
    setShowThinkingPicker(false);
  };

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'F2') {
        event.preventDefault();
        closeOthers();
        setShowModelPicker((v) => !v);
      } else if (event.key === 'Escape') {
        closeOthers();
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, []);

  useEffect(() => {
    if (!popoverOpen) return;
    const onPointerDown = (event: MouseEvent) => {
      const target = event.target as Node | null;
      if (target === null) return;
      const inside = [modelRef, thinkingRef].some((ref) => ref.current?.contains(target));
      if (!inside) closeOthers();
    };
    document.addEventListener('mousedown', onPointerDown);
    return () => document.removeEventListener('mousedown', onPointerDown);
  }, [popoverOpen]);

  return (
    <>
      <div className="relative" ref={modelRef}>
        <button
          type="button"
          onClick={() => {
            const next = !showModelPicker;
            closeOthers();
            setShowModelPicker(next);
          }}
          title="切换模型 (F2)"
          className="flex cursor-pointer items-center gap-1 font-mono text-[11px] text-gray-600 transition-colors hover:text-gray-900"
        >
          <span className="material-symbols-outlined text-[15px] text-gray-500">smart_toy</span>
          <span className="max-w-[14rem] truncate">{modelName || '-'}</span>
          <span className="material-symbols-outlined text-[15px] text-gray-400">expand_more</span>
        </button>
        {showModelPicker && (
          <div className="absolute bottom-8 right-0 z-50 flex max-h-80 w-72 flex-col rounded-md border border-gray-200 bg-white p-2 shadow-xl">
            <div className="flex items-center justify-between border-b border-gray-100 pb-1.5 text-[11px] font-semibold text-gray-500">
              <span>选择模型 ({availableModels.length} 个可用)</span>
              <span className="font-mono text-[10px] text-gray-400">F2</span>
            </div>
            <input
              id="model-filter"
              name="model-filter"
              type="text"
              value={modelSearch}
              onChange={(e) => setModelSearch(e.target.value)}
              placeholder="过滤模型名称..."
              className="my-1.5 rounded border border-gray-200 bg-gray-50 px-2 py-1 font-sans text-xs focus:border-blue-500 focus:outline-none"
            />
            <div className="max-h-60 flex-1 space-y-0.5 overflow-y-auto pr-1">
              {availableModels
                .filter((m) => m.toLowerCase().includes(modelSearch.toLowerCase()))
                .map((m) => (
                  <div
                    key={m}
                    onClick={() => {
                      setModel(m);
                      setShowModelPicker(false);
                      setModelSearch('');
                    }}
                    className={`cursor-pointer truncate rounded px-2 py-1 text-xs transition-colors ${
                      m === modelName
                        ? 'bg-blue-50 font-medium text-blue-600'
                        : 'text-gray-700 hover:bg-gray-100'
                    }`}
                  >
                    {m}
                  </div>
                ))}
            </div>
          </div>
        )}
      </div>

      <div className="relative" ref={thinkingRef}>
        <button
          type="button"
          onClick={() => {
            const next = !showThinkingPicker;
            closeOthers();
            setShowThinkingPicker(next);
          }}
          title={canSetThinking ? '推理等级' : RUNTIME_CONFIG_READ_ONLY_NOTICE}
          className="flex cursor-pointer items-center gap-1 font-mono text-[11px] text-gray-600 transition-colors hover:text-gray-900"
        >
          <span className="material-symbols-outlined text-[15px] text-gray-500">psychology</span>
          <span>{thinkingLevel === null ? '-' : thinkingLevel}</span>
          {canSetThinking ? (
            <span className="material-symbols-outlined text-[15px] text-gray-400">expand_more</span>
          ) : (
            <span className="material-symbols-outlined text-[14px] text-gray-300">lock</span>
          )}
        </button>
        {showThinkingPicker && (
          <div className="absolute bottom-8 right-0 z-50 w-40 space-y-1 rounded-md border border-gray-200 bg-white p-1 shadow-lg">
            <div className="border-b border-gray-100 px-2 py-0.5 text-[10px] font-semibold text-gray-400">
              推理等级
            </div>
            {!canSetThinking && (
              <div className="px-2 py-1 text-[10px] leading-relaxed text-gray-500">
                当前只读：该会话未开放推理等级写端口，等级由服务端设置决定。
              </div>
            )}
            {thinkingLevels.map((lvl) => (
              <div
                key={lvl}
                onClick={() => {
                  if (!canSetThinking) return;
                  // Keep the popover open on failure so the reason below the list
                  // stays readable instead of flashing away.
                  void setThinkingLevel(lvl).then((ok) => {
                    if (ok) setShowThinkingPicker(false);
                  });
                }}
                className={`rounded px-2 py-1 text-xs ${
                  canSetThinking
                    ? 'cursor-pointer text-gray-700 transition-colors hover:bg-gray-100'
                    : 'cursor-not-allowed text-gray-400'
                } ${lvl === thinkingLevel ? 'bg-purple-50 font-medium text-purple-600' : ''}`}
              >
                {lvl}
              </div>
            ))}
            {thinkingLevelError !== null && (
              <div className="border-t border-gray-100 px-2 py-1 text-[10px] leading-relaxed text-red-600">
                切换失败：{thinkingLevelError}
              </div>
            )}
          </div>
        )}
      </div>
    </>
  );
};
