import { Bot20Regular, BrainCircuit20Regular, ChevronDown20Regular, LockClosed20Regular } from '@fluentui/react-icons';
import React, { useEffect, useRef, useState } from 'react';
import { useShallow } from 'zustand/react/shallow';
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
  } = useConsoleStore(
    // Only the fields these controls paint: a reasoning delta must not re-render
    // them.
    useShallow((state) => ({
      modelName: state.modelName,
      availableModels: state.availableModels,
      setModel: state.setModel,
      thinkingLevel: state.thinkingLevel,
      thinkingLevels: state.thinkingLevels,
      canSetThinking: state.canSetThinking,
      setThinkingLevel: state.setThinkingLevel,
      thinkingLevelError: state.thinkingLevelError,
    })),
  );

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
      <div className="relative min-w-0 max-w-full" ref={modelRef}>
        <button
          type="button"
          onClick={() => {
            const next = !showModelPicker;
            closeOthers();
            setShowModelPicker(next);
          }}
          title="切换模型 (F2)"
          aria-expanded={showModelPicker}
          aria-controls="model-picker"
          className="ui-button ui-model-trigger"
        >
          <Bot20Regular aria-hidden="true" />
          <span className="max-w-[14rem] truncate">{modelName || '-'}</span>
          <ChevronDown20Regular aria-hidden="true" />
        </button>
        {showModelPicker && (
          <div id="model-picker" role="group" aria-label="选择模型" className="absolute bottom-full right-0 mb-2 z-50 flex max-h-80 w-72 max-w-[calc(100vw-4rem)] flex-col rounded-control border border-line material-flyout flyout-in p-2 shadow-flyout">
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
              aria-label="过滤模型名称"
              className="ui-field my-2 w-full"
            />
            <div className="max-h-60 flex-1 space-y-0.5 overflow-y-auto pr-1">
              {availableModels
                .filter((m) => m.toLowerCase().includes(modelSearch.toLowerCase()))
                .map((m) => (
                  <button
                    key={m}
                    type="button"
                    aria-pressed={m === modelName}
                    onClick={() => {
                      setModel(m);
                      setShowModelPicker(false);
                      setModelSearch('');
                    }}
                    className="ui-menu-item truncate text-gray-700"
                  >
                    {m}
                  </button>
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
          aria-expanded={showThinkingPicker}
          aria-controls="thinking-picker"
          className="ui-button ui-model-trigger"
        >
          <BrainCircuit20Regular aria-hidden="true" />
          <span>{thinkingLevel === null ? '-' : thinkingLevel}</span>
          {canSetThinking ? (
            <ChevronDown20Regular aria-hidden="true" />
          ) : (
            <LockClosed20Regular aria-hidden="true" />
          )}
        </button>
        {showThinkingPicker && (
          <div id="thinking-picker" role="group" aria-label="推理等级" className="absolute bottom-full right-0 mb-2 z-50 w-40 space-y-1 rounded-control border border-line material-flyout flyout-in p-1 shadow-flyout">
            <div className="border-b border-gray-100 px-2 py-0.5 text-[10px] font-semibold text-gray-400">
              推理等级
            </div>
            {!canSetThinking && (
              <div className="px-2 py-1 text-[10px] leading-relaxed text-gray-500">
                当前只读：该会话未开放推理等级写端口，等级由服务端设置决定。
              </div>
            )}
            {thinkingLevels.map((lvl) => (
              <button
                key={lvl}
                type="button"
                disabled={!canSetThinking}
                aria-pressed={lvl === thinkingLevel}
                onClick={() => {
                  if (!canSetThinking) return;
                  // Keep the popover open on failure so the reason below the list
                  // stays readable instead of flashing away.
                  void setThinkingLevel(lvl).then((ok) => {
                    if (ok) setShowThinkingPicker(false);
                  });
                }}
                className="ui-menu-item text-gray-700"
              >
                {lvl}
              </button>
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
