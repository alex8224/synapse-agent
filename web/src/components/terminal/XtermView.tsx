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
import { useAppearanceStore } from '../../stores/appearance.ts';

interface XtermViewProps {
  session: TerminalTabSession;
}

export const XtermView: React.FC<XtermViewProps> = ({ session }) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const xtermRef = useRef<Terminal | null>(null);
  const fitAddonRef = useRef<FitAddon | null>(null);
  const appearance = useAppearanceStore((s) => s.appearance);

  useEffect(() => {
    if (!containerRef.current) return;

    const themeAttr = document.documentElement.dataset.theme;
    const isDark = appearance === 'dark' || (appearance === 'system' && themeAttr !== 'fluent-light' && themeAttr !== 'light') || themeAttr === 'fluent-dark';

    const term = new Terminal({
      cursorBlink: true,
      cursorStyle: 'bar',
      fontSize: 12.5,
      lineHeight: 1.35,
      fontFamily: 'Cascadia Code, Consolas, ui-monospace, Menlo, Monaco, monospace',
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
    fitAddon.fit();

    xtermRef.current = term;
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
        term.writeln('\r\n\x1b[33m[终端进程已退出]\x1b[0m');
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
      try {
        fitAddon.fit();
        if (session.ptyId && term.cols > 0 && term.rows > 0) {
          void resizeTerminal(session.ptyId, term.cols, term.rows);
        }
      } catch {}
    });

    resizeObserver.observe(containerRef.current);

    return () => {
      dataSub.dispose();
      resizeObserver.disconnect();
      if (cleanupDataStream) cleanupDataStream();
      if (cleanupExitStream) cleanupExitStream();
      term.dispose();
    };
  }, [session.ptyId, session.status, session.errorMessage, appearance]);

  return (
    <div
      ref={containerRef}
      className="h-full w-full overflow-hidden p-2 select-text"
      style={{
        backgroundColor: document.documentElement.dataset.theme === 'fluent-light' ? '#fafafa' : '#181818',
      }}
    />
  );
};
