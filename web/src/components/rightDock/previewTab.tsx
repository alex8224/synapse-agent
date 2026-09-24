/**
 * In-App Preview & Browser Tab for the Right Auxiliary Dock (inspired by Codex & Bolt).
 *
 * Implements:
 * - Built-in URL preview toolbar with refresh and external browser launch
 * - Localhost dev server detection presets (:5173, :3000, :8080)
 * - Safe iframe webview container for instant frontend and interactive artifact rendering
 */
import {
  Play16Regular,
  ArrowClockwise16Regular,
  Open16Regular,
  Globe16Regular,
} from '@fluentui/react-icons';
import React, { useState } from 'react';
import type { RightDockContext, RightDockTabDefinition } from './contract.ts';

const PRESET_URLS = [
  'http://localhost:5173',
  'http://localhost:3000',
  'http://localhost:8080',
];

export const PreviewContent: React.FC<{ context: RightDockContext }> = () => {
  const [url, setUrl] = useState('http://localhost:5173');
  const [inputUrl, setInputUrl] = useState('http://localhost:5173');
  const [iframeKey, setIframeKey] = useState(0);

  const handleApplyUrl = (e: React.FormEvent) => {
    e.preventDefault();
    if (!inputUrl.trim()) return;
    let target = inputUrl.trim();
    if (!target.startsWith('http://') && !target.startsWith('https://')) {
      target = `http://${target}`;
    }
    setUrl(target);
    setInputUrl(target);
  };

  const handleRefresh = () => {
    setIframeKey((k) => k + 1);
  };

  const handleOpenExternal = () => {
    window.open(url, '_blank', 'noopener,noreferrer');
  };

  return (
    <div className="flex h-full flex-col overflow-hidden text-xs font-sans">
      {/* Mini browser address bar */}
      <form
        onSubmit={handleApplyUrl}
        className="flex items-center gap-1.5 border-b border-line/80 px-2.5 py-1.5 shrink-0"
      >
        <button
          type="button"
          onClick={handleRefresh}
          title="刷新页面"
          className="ui-icon-button ui-compact text-gray-400 hover:text-gray-900"
        >
          <ArrowClockwise16Regular />
        </button>

        <div className="relative flex flex-1 items-center">
          <Globe16Regular className="pointer-events-none absolute left-2 text-gray-400 text-xs shrink-0" />
          <input
            type="text"
            value={inputUrl}
            onChange={(e) => setInputUrl(e.target.value)}
            placeholder="输入预览地址 (例如 http://localhost:5173)..."
            className="ui-field ui-compact w-full pl-7 pr-2 font-mono text-[11px] text-gray-900 placeholder:text-gray-400"
          />
        </div>

        <button
          type="button"
          onClick={handleOpenExternal}
          title="在系统独立浏览器中打开"
          className="ui-icon-button ui-compact text-gray-400 hover:text-gray-900"
        >
          <Open16Regular />
        </button>
      </form>

      {/* Quick port chips */}
      <div className="flex items-center gap-1 border-b border-line/60 bg-surface/30 px-3 py-1 font-mono text-[10.5px] text-gray-500 overflow-x-auto shrink-0 no-scrollbar">
        <span className="text-[10px] text-gray-400 font-sans">快捷端口:</span>
        {PRESET_URLS.map((preset) => (
          <button
            key={preset}
            type="button"
            onClick={() => {
              setUrl(preset);
              setInputUrl(preset);
            }}
            className={`rounded-full px-2 py-0.5 border transition-colors ${
              url === preset ? 'bg-accent/15 text-accent border-accent/30 font-semibold' : 'border-line/60 bg-surface/50 text-gray-600 hover:bg-surface-hover hover:text-gray-900'
            }`}
          >
            {preset.replace('http://localhost', '')}
          </button>
        ))}
      </div>

      {/* Embedded Iframe Preview */}
      <div className="relative flex-1 bg-surface">
        <iframe
          key={iframeKey}
          src={url}
          title="Synapse Embedded Web Preview"
          sandbox="allow-scripts allow-same-origin allow-forms allow-popups"
          className="h-full w-full border-none bg-surface"
        />
      </div>
    </div>
  );
};

export const previewTab: RightDockTabDefinition = {
  id: 'browser',
  label: '预览',
  order: 40,
  Icon: Play16Regular,
  Content: PreviewContent,
};
