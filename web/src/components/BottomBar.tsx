import React, { useState, useEffect } from 'react';
import { useConsoleStore } from '../stores/useConsoleStore';
import { RUNTIME_CONFIG_READ_ONLY_NOTICE } from '../stores/runtimeConfigMapper';

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

  return (
    <>
      <footer className="bg-white text-gray-500 font-mono fixed bottom-0 left-0 w-full z-40 border-t border-[#e5e7eb] flex justify-between items-center h-7 px-3 shrink-0 select-none text-[11px]">
        <div className="flex items-center space-x-2.5">
          {/* Model Selector Dropdown */}
          <div className="relative">
            <div
              onClick={() => { setShowModelPicker((v) => !v); setShowThinkingPicker(false); }}
              className="flex items-center space-x-1 hover:text-gray-900 transition-colors cursor-pointer"
            >
              <span className="text-[13px]">🤖</span>
              <span className="text-gray-700">{modelName}</span>
              <span className="material-symbols-outlined text-[13px] text-gray-400">expand_more</span>
            </div>
            {showModelPicker && (
              <div className="absolute bottom-8 left-0 w-72 bg-white border border-gray-200 rounded-md shadow-xl p-2 z-50 flex flex-col max-h-80">
                <div className="text-[11px] text-gray-500 pb-1.5 border-b border-gray-100 font-semibold flex justify-between items-center">
                  <span>选择模型 ({availableModels.length} 个可用)</span>
                  <span className="text-[10px] text-gray-400 font-mono">F2</span>
                </div>
                <input
                  type="text"
                  value={modelSearch}
                  onChange={(e) => setModelSearch(e.target.value)}
                  placeholder="过滤模型名称..."
                  className="my-1.5 px-2 py-1 bg-gray-50 border border-gray-200 rounded text-xs focus:outline-none focus:border-blue-500"
                />
                <div className="overflow-y-auto flex-1 space-y-0.5 max-h-60 pr-1">
                  {availableModels.filter(m => m.toLowerCase().includes(modelSearch.toLowerCase())).map((m) => (
                    <div
                      key={m}
                      onClick={() => { setModel(m); setShowModelPicker(false); setModelSearch(''); }}
                      className={`px-2 py-1 rounded cursor-pointer transition-colors text-xs truncate ${
                        m === modelName ? 'bg-blue-50 text-blue-600 font-medium' : 'hover:bg-gray-100 text-gray-700'
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

          {/* Thinking Level Dropdown (read-only view) */}
          <div className="relative">
            <div
              onClick={() => { setShowThinkingPicker((v) => !v); setShowModelPicker(false); }}
              className="flex items-center space-x-1 hover:text-gray-900 transition-colors cursor-pointer"
              title={canSetThinking ? undefined : RUNTIME_CONFIG_READ_ONLY_NOTICE}
            >
              <span className="text-[13px]">🧠</span>
              <span className="text-gray-700">
                thinking: {thinkingLevel === null ? '-' : thinkingLevel}
              </span>
              {!canSetThinking && (
                <span className="text-[10px] text-gray-400 px-1 rounded bg-gray-100">只读</span>
              )}
              <span className="material-symbols-outlined text-[13px] text-gray-400">expand_more</span>
            </div>
            {showThinkingPicker && (
              <div className="absolute bottom-8 left-0 w-36 bg-white border border-gray-200 rounded-md shadow-lg p-1 space-y-1 z-50">
                <div className="text-[10px] text-gray-400 px-2 py-0.5 border-b border-gray-100 font-semibold">
                  推理等级
                </div>
                {!canSetThinking && (
                  <div className="text-[10px] text-gray-500 px-2 py-1">
                    当前只读：推理等级修改暂未开放
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
                    className={`px-2 py-1 rounded text-xs ${
                      !canSetThinking ? 'cursor-not-allowed text-gray-400' : 'cursor-pointer transition-colors'
                    } ${
                      lvl === thinkingLevel ? 'bg-purple-50 text-purple-600 font-medium' : 'hover:bg-gray-100 text-gray-700'
                    }`}
                  >
                    {lvl}
                  </div>
                ))}
              </div>
            )}
          </div>

          <span className="text-gray-200">|</span>

          {/* MCP Status */}
          <div className="relative">
            <div
              onClick={() => { setShowMcpPanel((v) => !v); setShowModelPicker(false); setShowThinkingPicker(false); }}
              className="flex items-center space-x-1 hover:text-gray-900 transition-colors cursor-pointer"
              title="点击管理 MCP 服务器 (F5)"
            >
              <span className={`material-symbols-outlined text-[14px] ${mcpStatus.includes('0') || mcpStatus === 'off' ? 'text-gray-400' : 'text-green-600'}`}>
                bolt
              </span>
              <span className="text-gray-700">mcp: {mcpStatus}</span>
              <span className="material-symbols-outlined text-[13px] text-gray-400">expand_more</span>
            </div>
            {showMcpPanel && (
              <div className="absolute bottom-8 left-0 w-80 bg-white border border-gray-200 rounded-md shadow-xl p-3 z-50 space-y-2">
                <div className="flex justify-between items-center pb-2 border-b border-gray-100">
                  <span className="font-bold text-gray-900 text-xs">MCP 工具与服务器 (F5)</span>
                  <button
                    onClick={toggleMcpGlobal}
                    disabled={!canToggleMcpGlobal}
                    title={canToggleMcpGlobal ? undefined : RUNTIME_CONFIG_READ_ONLY_NOTICE}
                    className={`px-2 py-0.5 rounded text-[10px] font-mono ${
                      canToggleMcpGlobal ? 'cursor-pointer' : 'cursor-not-allowed opacity-60'
                    } ${
                      mcpEnabled ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-600'
                    }`}
                  >
                    {mcpEnabled ? '全局启用' : '全局停用'} · 只读
                  </button>
                </div>
                {!canToggleMcpGlobal && (
                  <div className="text-[10px] text-gray-500 px-1">
                    当前只读：全局 MCP 开关暂未开放，可单独切换已配置服务器
                  </div>
                )}
                <div className="space-y-1.5 max-h-56 overflow-y-auto">
                  {mcpServers.length === 0 ? (
                    <div className="text-gray-400 text-center py-2 text-xs">未配置任何 MCP 服务器</div>
                  ) : (
                    mcpServers.map((srv: { name: string; transport: string; enabled: boolean }) => (
                      <div
                        key={srv.name}
                        onClick={() => toggleMcpServer(srv.name)}
                        className="flex items-center justify-between p-1.5 rounded hover:bg-gray-50 border border-gray-100 cursor-pointer text-xs"
                      >
                        <div className="flex items-center space-x-2 truncate">
                          <span className={`w-2 h-2 rounded-full ${srv.enabled ? 'bg-green-500' : 'bg-gray-300'}`} />
                          <span className="font-medium text-gray-800 truncate">{srv.name}</span>
                          <span className="text-[10px] text-gray-400 font-mono">({srv.transport})</span>
                        </div>
                        <span className={`text-[10px] font-mono px-1.5 py-0.5 rounded ${
                          srv.enabled ? 'bg-blue-50 text-blue-600' : 'text-gray-400'
                        }`}>
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

        {/* Center Running Status */}
        <div className={`flex items-center space-x-1.5 font-medium ${runtimeStatus === 'running' ? 'text-blue-600' : 'text-gray-500'}`}>
          <span className={`w-1.5 h-1.5 rounded-full ${runtimeStatus === 'running' ? 'bg-blue-600 animate-pulse' : 'bg-gray-400'}`}></span>
          <span>{runtimeStatus === 'running' ? '运行中' : '空闲'}</span>
        </div>

        {/* Right Target & Shortcut Hints */}
        <div className="flex items-center space-x-3 text-gray-500">
          <div className="flex items-center space-x-2 text-gray-400 font-mono text-[10px]">
            <span onClick={() => setShowHelp((v) => !v)} className="cursor-pointer hover:text-gray-700">
              <kbd className="px-1 bg-gray-50 border border-gray-200 rounded text-[10px]">F1</kbd> 帮助
            </span>
            <span onClick={() => setShowModelPicker((v) => !v)} className="cursor-pointer hover:text-gray-700">
              <kbd className="px-1 bg-gray-50 border border-gray-200 rounded text-[10px]">F2</kbd> 切换模型
            </span>
            <span onClick={() => setShowMcpPanel((v) => !v)} className="cursor-pointer hover:text-gray-700">
              <kbd className="px-1 bg-gray-50 border border-gray-200 rounded text-[10px]">F5</kbd> MCP
            </span>
            <span><kbd className="px-1 bg-gray-50 border border-gray-200 rounded text-[10px]">Ctrl+C</kbd> 中止</span>
          </div>
        </div>
      </footer>

      {/* Help Modal */}
      {showHelp && (
        <div className="fixed inset-0 bg-black/20 flex items-center justify-center z-50 p-4" onClick={() => setShowHelp(false)}>
          <div className="bg-white rounded-lg shadow-xl border border-gray-200 p-5 max-w-md w-full font-sans" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between border-b pb-2 mb-3">
              <span className="font-bold text-gray-900 text-sm">快捷键与使用帮助</span>
              <button onClick={() => setShowHelp(false)} className="text-gray-400 hover:text-gray-600 text-xs cursor-pointer">✕</button>
            </div>
            <div className="space-y-2 text-xs text-gray-600 font-mono">
              <div className="flex justify-between py-1 border-b border-gray-50"><span>Ctrl + B</span><span>展开 / 收起侧边栏</span></div>
              <div className="flex justify-between py-1 border-b border-gray-50"><span>Ctrl + C</span><span>中止当前运行中的轮次</span></div>
              <div className="flex justify-between py-1 border-b border-gray-50"><span>F1</span><span>打开快捷键帮助</span></div>
              <div className="flex justify-between py-1 border-b border-gray-50"><span>F2</span><span>切换大语言模型</span></div>
              <div className="flex justify-between py-1 border-b border-gray-50"><span>Enter</span><span>发送指令 / 运行态下排队插话</span></div>
            </div>
          </div>
        </div>
      )}
    </>
  );
};