/**
 * Xterm Native Terminal View.
 *
 * Implements:
 * - Direct WebGL/Canvas/DOM terminal rendering via @xterm/xterm
 * - Responsive auto-fitting via @xterm/addon-fit
 * - Dynamic Fluent 2 Light/Dark theme switching
 * - Bidirectional Tauri PTY IPC data streaming
 */
import React, { useEffect, useRef } from 'react';
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import '@xterm/xterm/css/xterm.css';
import {
  writeTerminal,
  resizeTerminal,
  onTerminalData,
  onTerminalExit,
} from '../../client/tauriTerminal.ts';
import type { TerminalTabSession } from '../../stores/useTerminalStore.ts';
import { useTerminalStore } from '../../stores/useTerminalStore.ts';
import { useAppearanceStore } from '../../stores/appearance.ts';

interface XtermViewProps {
  session: TerminalTabSession;
  isActive?: boolean;
}

export const terminalInstances = new Map<string, Terminal>();

export function getTerminalContext(term: Terminal): { text: string; hasSelection: boolean } {
  const selection = term.getSelection().trim();
  if (selection) {
    return { text: selection, hasSelection: true };
  }

  const buffer = term.buffer.active;
  const lines: string[] = [];
  const total = buffer.length;
  let lastNonEmpty = total - 1;
  while (lastNonEmpty >= 0) {
    const line = buffer.getLine(lastNonEmpty);
    if (line && line.translateToString(true).trim().length > 0) {
      break;
    }
    lastNonEmpty--;
  }

  if (lastNonEmpty < 0) {
    return { text: '', hasSelection: false };
  }

  const start = Math.max(0, lastNonEmpty - 35);
  for (let i = start; i <= lastNonEmpty; i++) {
    const line = buffer.getLine(i);
    if (line) {
      lines.push(line.translateToString(true));
    }
  }
  return { text: lines.join('\n').trim(), hasSelection: false };
}

export const XtermView: React.FC<XtermViewProps> = ({ session, isActive = true }) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const xtermRef = useRef<Terminal | null>(null);
  const fitAddonRef = useRef<FitAddon | null>(null);
  const appearance = useAppearanceStore((s) => s.appearance);
  const doFitRef = useRef<(() => void) | null>(null);

  // Tab activation observer: re-fit & focus when switching back
  useEffect(() => {
    if (isActive) {
      requestAnimationFrame(() => {
        try {
          doFitRef.current?.();
          xtermRef.current?.focus();
        } catch {}
      });
    }
  }, [isActive]);

  useEffect(() => {
    if (!containerRef.current) return;

    const handleSessionExit = useTerminalStore.getState().handleSessionExit;

    const themeAttr = document.documentElement.dataset.theme;
    const isDark = appearance === 'dark' || (appearance === 'system' && themeAttr !== 'fluent-light' && themeAttr !== 'light') || themeAttr === 'fluent-dark';

    const term = new Terminal({
      allowProposedApi: true,
      cursorBlink: true,
      cursorStyle: 'bar',
      fontSize: 13,
      letterSpacing: 0,
      lineHeight: 1.35,
      fontFamily:
        "'CaskaydiaCove Nerd Font', 'CaskaydiaCove NF', 'Cascadia Code NF', 'Cascadia Mono NF', 'JetBrainsMono Nerd Font', 'MesloLGS NF', 'FiraCode Nerd Font', 'Caskaydia Cove Nerd Font', 'Cascadia Code', Consolas, 'Segoe UI Symbol', monospace",
      theme: {
        background: isDark ? '#181818' : '#fafafa',
        foreground: isDark ? '#d4d4d4' : '#1f1f1f',
        cursor: '#0078d4',
        cursorAccent: '#ffffff',
        selectionBackground: 'rgba(0, 120, 212, 0.35)',
        black: '#000000',
        red: '#cd3131',
        green: '#0dbc79',
        yellow: '#e5e510',
        blue: '#2472c8',
        magenta: '#bc3fbc',
        cyan: '#11a8cd',
        white: '#e5e5e5',
        brightBlack: '#666666',
        brightRed: '#f14c4c',
        brightGreen: '#23d18b',
        brightYellow: '#f5f543',
        brightBlue: '#3b8eea',
        brightMagenta: '#d670d6',
        brightCyan: '#29b8db',
        brightWhite: '#ffffff',
      },
    });

    const fitAddon = new FitAddon();
    term.loadAddon(fitAddon);
    term.open(containerRef.current);
    term.attachCustomKeyEventHandler((e: KeyboardEvent) => {
      // Allow global shortcuts to pass through to window (e.g. Ctrl+` to toggle terminal)
      if ((e.ctrlKey || e.metaKey) && (e.key === '`' || e.code === 'Backquote')) {
        return false;
      }
      if ((e.ctrlKey || e.metaKey) && (e.key.toLowerCase() === 'j' || e.code === 'KeyJ')) {
        return false;
      }
      if ((e.ctrlKey || e.metaKey) && (e.key.toLowerCase() === 'b' || e.code === 'KeyB')) {
        return false;
      }
      return true;
    });

    const doFit = () => {
      try {
        if (containerRef.current && containerRef.current.clientWidth > 0 && containerRef.current.clientHeight > 0) {
          fitAddon.fit();
          if (session.ptyId && term.cols > 0 && term.rows > 0) {
            void resizeTerminal(session.ptyId, term.cols, term.rows);
          }
        }
      } catch {}
    };
    doFitRef.current = doFit;

    // Immediate fit and deferred fit once web fonts are fully rendered
    doFit();
    if (typeof document !== 'undefined' && 'fonts' in document) {
      document.fonts.ready.then(() => {
        doFit();
      });
    }
    requestAnimationFrame(() => doFit());

    xtermRef.current = term;
    terminalInstances.set(session.id, term);
    fitAddonRef.current = fitAddon;

    // Stream user input from xterm to native PTY
    const dataSub = term.onData((data) => {
      if (session.ptyId) {
        void writeTerminal(session.ptyId, data);
      }
    });

    let cleanupDataStream: (() => void) | null = null;
    let cleanupExitStream: (() => void) | null = null;

    if (session.ptyId) {
      void onTerminalData(session.ptyId, (data) => {
        term.write(data);
      }).then((unlisten) => {
        cleanupDataStream = unlisten;
      });

      void onTerminalExit(session.ptyId, () => {
        if (session.ptyId) {
          handleSessionExit(session.ptyId);
        }
      }).then((unlisten) => {
        cleanupExitStream = unlisten;
      });

      // Synchronize initial dimensions
      void resizeTerminal(session.ptyId, term.cols, term.rows);
    } else if (session.status === 'error') {
      term.writeln(`\r\n\x1b[31m启动终端失败: ${session.errorMessage || '无法连接原生 PTY'}\x1b[0m\r\n`);
    }

    // Auto-fit on resize observer
    const resizeObserver = new ResizeObserver(() => {
      doFit();
    });

    resizeObserver.observe(containerRef.current);

    return () => {
      terminalInstances.delete(session.id);
      dataSub.dispose();
      resizeObserver.disconnect();
      if (cleanupDataStream) cleanupDataStream();
      if (cleanupExitStream) cleanupExitStream();
      term.dispose();
    };
  }, [session.ptyId, session.status, session.errorMessage, session.id, appearance]);

  return (
    <div
      ref={containerRef}
      className="h-full w-full overflow-hidden px-3 py-1.5 select-text"
      style={{
        backgroundColor: document.documentElement.dataset.theme === 'fluent-light' ? '#fafafa' : '#181818',
      }}
    />
  );
};
