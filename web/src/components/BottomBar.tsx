import React, { useState, useEffect } from 'react';
import { useConsoleStore } from '../stores/useConsoleStore';
import { RUNTIME_CONFIG_READ_ONLY_NOTICE } from '../stores/runtimeConfigMapper';
import { turnStatSegments, usageSegments, usageTooltip } from '../stores/usageView.ts';

/** Full shortcut list for the F1 dialog. */
const HELP_ROWS: Array<{ keys: string; label: string }> = [
  { keys: 'Enter', label: '发送指令 / 运行态下排队插话' },
  { keys: 'Ctrl + C', label: '中止当前运行中的轮次' },
  { keys: 'Ctrl + B', label: '展开 / 收起侧边栏' },
  { keys: 'Ctrl + N', label: '新建会话' },
  { keys: 'Ctrl + K', label: '搜索会话' },
  { keys: 'F1', label: '打开快捷键帮助' },
  { keys: 'F2', label: '切换大语言模型' },
  { keys: 'F5', label: 'MCP 服务器' },
];

export const BottomBar: React.FC = () => {
  const {
    modelName,
    availableModels,
    setModel,
    thinkingLevel,
    thinkingLevels,
    setThinkingLevel,
    mcpStatus,
    mcpServers,
    mcpEnabled,
    canSetThinking,
    canToggleMcpGlobal,
    toggleMcpServer,
    toggleMcpGlobal,
    runtimeStatus,
    usage,
  } = useConsoleStore();

  const [showModelPicker, setShowModelPicker] = useState(false);
  const [showThinkingPicker, setShowThinkingPicker] = useState(false);
  const [showMcpPanel, setShowMcpPanel] = useState(false);
  const [modelSearch, setModelSearch] = useState('');
  const [showHelp, setShowHelp] = useState(false);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'F1') {
        e.preventDefault();
        setShowHelp((v) => !v);
      } else if (e.key === 'F2') {
        e.preventDefault();
        setShowModelPicker((v) => !v);
      } else if (e.key === 'F5') {
        e.preventDefault();
        setShowMcpPanel((v) => !v);
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, []);

  const busy = runtimeStatus === 'running';
  // Every usage metric now lives here: the token totals and the speed/latency/
  // step telemetry of the current turn.
  const telemetry = [...usageSegments(usage), ...turnStatSegments(usage)];
  const closeOthers = () => {
    setShowModelPicker(false);
    setShowThinkingPicker(false);
    setShowMcpPanel(false);
  };

  return (
    <>
      <footer className="fixed bottom-0 left-0 z-40 grid h-7 w-full grid-cols-[1fr_auto_1fr] items-center gap-4 border-t border-[#e5e7eb] bg-white px-3 font-mono text-[11px] text-gray-500 shrink-0 select-none">
        {/* Left: activity + configuration */}
        <div className="flex min-w-0 items-center gap-2.5">
          <span
            className={`flex items-center gap-1.5 font-sans text-[11px] font-medium ${
              busy ? 'text-blue-600' : 'text-gray-500'
            }`}
          >
            <span
              className={`h-1.5 w-1.5 rounded-full ${busy ? 'animate-pulse bg-blue-600' : 'bg-gray-400'}`}
            />
            {busy ? '运行中' : '空闲'}
          </span>

          <span className="text-gray-200">|</span>

          {/* Model */}
          <div className="relative">
            <button
              type="button"
              onClick={() => {
                const next = !showModelPicker;
                closeOthers();
                setShowModelPicker(next);
              }}
              title="切换模型 (F2)"
              className="flex items-center gap-1 cursor-pointer transition-colors hover:text-gray-900"
            >
              <span className="material-symbols-outlined text-[14px] text-gray-500">smart_toy</span>
              <span className="max-w-[14rem] truncate text-gray-700">{modelName || '-'}</span>
              <span className="material-symbols-outlined text-[14px] text-gray-400">expand_more</span>
            </button>
            {showModelPicker && (
              <div className="absolute bottom-8 left-0 z-50 flex max-h-80 w-72 flex-col rounded-md border border-gray-200 bg-white p-2 shadow-xl">
                <div className="flex items-center justify-between border-b border-gray-100 pb-1.5 text-[11px] font-semibold text-gray-500">
                  <span>选择模型 ({availableModels.length} 个可用)</span>
                  <span className="font-mono text-[10px] text-gray-400">F2</span>
                </div>
                <input
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

          <span className="text-gray-200">|</span>

          {/* Thinking level (read-only today) */}
          <div className="relative">
            <button
              type="button"
              onClick={() => {
                const next = !showThinkingPicker;
                closeOthers();
                setShowThinkingPicker(next);
              }}
              title={canSetThinking ? '推理等级' : RUNTIME_CONFIG_READ_ONLY_NOTICE}
              className="flex items-center gap-1 cursor-pointer transition-colors hover:text-gray-900"
            >
              <span className="material-symbols-outlined text-[14px] text-gray-500">psychology</span>
              <span className="text-gray-700">{thinkingLevel === null ? '-' : thinkingLevel}</span>
              {canSetThinking ? (
                <span className="material-symbols-outlined text-[14px] text-gray-400">
                  expand_more
                </span>
              ) : (
                <span className="material-symbols-outlined text-[13px] text-gray-300">lock</span>
              )}
            </button>
            {showThinkingPicker && (
              <div className="absolute bottom-8 left-0 z-50 w-40 space-y-1 rounded-md border border-gray-200 bg-white p-1 shadow-lg">
                <div className="border-b border-gray-100 px-2 py-0.5 text-[10px] font-semibold text-gray-400">
                  推理等级
                </div>
                {!canSetThinking && (
                  <div className="px-2 py-1 text-[10px] leading-relaxed text-gray-500">
                    当前只读：运行时配置面没有写端口，等级由服务端设置决定。
                  </div>
                )}
                {thinkingLevels.map((lvl) => (
                  <div
                    key={lvl}
                    onClick={() => {
                      if (!canSetThinking) return;
                      setThinkingLevel(lvl);
                      setShowThinkingPicker(false);
                    }}
                    className={`rounded px-2 py-1 text-xs ${
                      canSetThinking
                        ? 'cursor-pointer transition-colors hover:bg-gray-100 text-gray-700'
                        : 'cursor-not-allowed text-gray-400'
                    } ${lvl === thinkingLevel ? 'bg-purple-50 font-medium text-purple-600' : ''}`}
                  >
                    {lvl}
                  </div>
                ))}
              </div>
            )}
          </div>

          <span className="text-gray-200">|</span>

          {/* MCP */}
          <div className="relative">
            <button
              type="button"
              onClick={() => {
                const next = !showMcpPanel;
                closeOthers();
                setShowMcpPanel(next);
              }}
              title="管理 MCP 服务器 (F5)"
              className="flex items-center gap-1 cursor-pointer transition-colors hover:text-gray-900"
            >
              <span
                className={`material-symbols-outlined text-[14px] ${
                  mcpServers.some((s) => s.enabled) ? 'text-green-600' : 'text-gray-400'
                }`}
              >
                bolt
              </span>
              <span className="text-gray-700">mcp: {mcpStatus}</span>
              <span className="material-symbols-outlined text-[14px] text-gray-400">expand_more</span>
            </button>
            {showMcpPanel && (
              <div className="absolute bottom-8 left-0 z-50 w-80 space-y-2 rounded-md border border-gray-200 bg-white p-3 shadow-xl">
                <div className="flex items-center justify-between border-b border-gray-100 pb-2">
                  <span className="text-xs font-bold text-gray-900">MCP 工具与服务器 (F5)</span>
                  <span
                    aria-disabled={!canToggleMcpGlobal}
                    title={canToggleMcpGlobal ? undefined : RUNTIME_CONFIG_READ_ONLY_NOTICE}
                    className={`rounded px-2 py-0.5 font-mono text-[10px] ${
                      canToggleMcpGlobal
                        ? 'cursor-pointer'
                        : 'cursor-not-allowed border border-dashed border-gray-300 text-gray-400'
                    } ${mcpEnabled ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-600'}`}
                    onClick={() => {
                      if (canToggleMcpGlobal) void toggleMcpGlobal();
                    }}
                  >
                    {mcpEnabled ? '全局启用' : '全局停用'} · 只读
                  </span>
                </div>
                <div className="max-h-56 space-y-1.5 overflow-y-auto">
                  {mcpServers.length === 0 ? (
                    <div className="py-2 text-center font-sans text-xs text-gray-400">
                      未配置任何 MCP 服务器
                    </div>
                  ) : (
                    mcpServers.map((srv) => (
                      <div
                        key={srv.name}
                        onClick={() => {
                          void toggleMcpServer(srv.name);
                        }}
                        className="flex cursor-pointer items-center justify-between rounded border border-gray-100 p-1.5 text-xs hover:bg-gray-50"
                      >
                        <div className="flex items-center space-x-2 truncate">
                          <span
                            className={`h-2 w-2 rounded-full ${srv.enabled ? 'bg-green-500' : 'bg-gray-300'}`}
                          />
                          <span className="truncate font-medium text-gray-800">{srv.name}</span>
                          <span className="font-mono text-[10px] text-gray-400">
                            ({srv.transport})
                          </span>
                        </div>
                        <span
                          className={`rounded px-1.5 py-0.5 font-mono text-[10px] ${
                            srv.enabled ? 'bg-blue-50 text-blue-600' : 'text-gray-400'
                          }`}
                        >
                          {srv.enabled ? 'ON' : 'OFF'}
                        </span>
                      </div>
                    ))
                  )}
                </div>
              </div>
            )}
          </div>
        </div>

        {/* Centre: all turn telemetry (tokens + speed / latency / steps) */}
        <div
          className="flex shrink-0 items-center gap-2 tabular-nums"
          title={telemetry.length > 0 ? usageTooltip(usage) : undefined}
        >
          {telemetry.length === 0 ? (
            <span className="font-sans text-[11px] text-gray-300">尚无本轮指标</span>
          ) : (
            telemetry.map((segment, index) => (
              <span key={segment.key} className="flex items-center gap-2">
                {index > 0 && <span className="text-gray-200">|</span>}
                <span className="flex items-baseline gap-1">
                  {segment.label !== '' && (
                    <span className="font-sans text-[10px] text-gray-400">{segment.label}</span>
                  )}
                  <span
                    className={segment.emphasis ? 'font-medium text-gray-800' : 'text-gray-600'}
                  >
                    {segment.value}
                  </span>
                </span>
              </span>
            ))
          )}
        </div>

        {/* Right slot intentionally empty: F1 opens the full shortcut list. */}
        <div />
      </footer>

      {showHelp && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/20 p-4"
          onClick={() => setShowHelp(false)}
        >
          <div
            className="w-full max-w-md rounded-lg border border-gray-200 bg-white p-5 font-sans shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="mb-3 flex items-center justify-between border-b pb-2">
              <span className="text-sm font-bold text-gray-900">快捷键与使用帮助</span>
              <button
                onClick={() => setShowHelp(false)}
                title="关闭 (Esc)"
                className="material-symbols-outlined cursor-pointer text-[18px] text-gray-400 hover:text-gray-600"
              >
                close
              </button>
            </div>
            <div className="space-y-1.5 font-mono text-xs text-gray-600">
              {HELP_ROWS.map((row) => (
                <div
                  key={row.keys}
                  className="flex items-center justify-between border-b border-gray-50 py-1 last:border-b-0"
                >
                  <kbd className="rounded border border-gray-200 bg-gray-50 px-1.5 py-0.5 text-[10px] text-gray-500">
                    {row.keys}
                  </kbd>
                  <span className="font-sans">{row.label}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </>
  );
};
