import React from 'react';
import { isTauri, tauriClose, tauriMinimize, tauriToggleMaximize } from '../client/tauri';

export const TauriWindowControls: React.FC = () => {
  if (!isTauri()) {
    return null;
  }

  return (
    <div
      data-tauri-drag-region="false"
      className="wco-caption-controls flex items-center justify-end gap-1 shrink-0 ml-auto select-none"
      style={{ WebkitAppRegion: 'no-drag' } as React.CSSProperties}
    >
      <button
        type="button"
        data-tauri-drag-region="false"
        title="最小化"
        aria-label="最小化"
        onClick={(e) => {
          e.stopPropagation();
          void tauriMinimize();
        }}
        className="w-7 h-6 flex items-center justify-center rounded text-muted hover:text-foreground hover:bg-surface-elevated transition-colors cursor-pointer"
      >
        <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <line x1="5" y1="12" x2="19" y2="12" />
        </svg>
      </button>
      <button
        type="button"
        data-tauri-drag-region="false"
        title="最大化 / 还原"
        aria-label="最大化 / 还原"
        onClick={(e) => {
          e.stopPropagation();
          void tauriToggleMaximize();
        }}
        className="w-7 h-6 flex items-center justify-center rounded text-muted hover:text-foreground hover:bg-surface-elevated transition-colors cursor-pointer"
      >
        <svg className="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <rect x="3" y="3" width="18" height="18" rx="2" />
        </svg>
      </button>
      <button
        type="button"
        data-tauri-drag-region="false"
        title="关闭到托盘"
        aria-label="关闭到托盘"
        onClick={(e) => {
          e.stopPropagation();
          void tauriClose();
        }}
        className="w-7 h-6 flex items-center justify-center rounded text-muted hover:text-red-100 hover:bg-red-600 transition-colors cursor-pointer"
      >
        <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <line x1="6" y1="6" x2="18" y2="18" />
          <line x1="6" y1="18" x2="18" y2="6" />
        </svg>
      </button>
    </div>
  );
};
