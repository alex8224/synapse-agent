import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  Add16Regular,
  ChevronDown16Regular,
  Play16Regular,
  WindowConsole20Regular,
} from '@fluentui/react-icons';
import { highlight, type HighlightKind } from '../markdown/highlight.ts';
import { useTerminalStore } from '../stores/useTerminalStore.ts';
import { useConsoleStore } from '../stores/useConsoleStore.ts';

/** Above this size highlighting is skipped so a huge paste cannot stall a frame. */
const HIGHLIGHT_MAX_CHARS = 40_000;

const TOKEN_CLASS: Record<HighlightKind, string> = {
  plain: 'text-gray-800',
  comment: 'text-gray-400',
  string: 'text-emerald-700',
  number: 'text-amber-700',
  keyword: 'text-purple-600',
  added: 'text-emerald-700',
  removed: 'text-red-700',
  meta: 'text-blue-500',
};

export interface CodeBlockProps {
  lang: string;
  code: string;
  /** True while a streamed fence has not been closed yet. */
  streaming?: boolean;
  /**
   * Optional short caveat rendered in the header (e.g. a language the console
   * has no renderer for).  Kept as plain text so it can never inject markup.
   */
  note?: string;
  /**
   * Fill the parent's height instead of capping the body at a fixed height.
   * Used by the file viewer, whose code must track the (resizable) window.
   */
  fill?: boolean;
}

/**
 * One fenced code block: language label, terminal dispatch menu, copy button,
 * and a monospace body that scrolls instead of wrapping.
 */
export const CodeBlock: React.FC<CodeBlockProps> = ({
  lang,
  code,
  streaming = false,
  note,
  fill = false,
}) => {
  const [copied, setCopied] = useState(false);
  const [dispatched, setDispatched] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  const sessions = useTerminalStore((s) => s.sessions);
  const activeSessionId = useTerminalStore((s) => s.activeSessionId);

  useEffect(() => {
    if (!menuOpen) return;
    const onPointerDown = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    };
    document.addEventListener('mousedown', onPointerDown);
    return () => document.removeEventListener('mousedown', onPointerDown);
  }, [menuOpen]);

  const tokens = useMemo(
    () =>
      code.length > HIGHLIGHT_MAX_CHARS
        ? [{ text: code, kind: 'plain' as HighlightKind }]
        : highlight(code, lang),
    [code, lang],
  );

  const copy = (): void => {
    const clipboard = typeof navigator === 'undefined' ? undefined : navigator.clipboard;
    if (!clipboard) return;
    void clipboard
      .writeText(code)
      .then(() => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1200);
      })
      .catch(() => undefined);
  };

  const getWorkspace = (): string | undefined => {
    const cs = useConsoleStore.getState();
    return cs.projects.find((p) => p.project_id === cs.activeProjectId)?.workspace_path;
  };

  const markDispatched = () => {
    setDispatched(true);
    setMenuOpen(false);
    window.setTimeout(() => setDispatched(false), 1400);
  };

  const handleDispatchDefault = () => {
    const store = useTerminalStore.getState();
    const target =
      store.sessions.find((s) => s.id === activeSessionId && s.status === 'running') ??
      store.sessions.find((s) => s.status === 'running');
    if (target) {
      void store.dispatchToSession(target.id, code);
    } else {
      const isShellCode = /^(bash|sh|pwsh|powershell|cmd|shell|console|zsh)$/i.test(lang);
      void store.spawnAndDispatch(getWorkspace(), isShellCode ? 'pwsh' : 'agy', code);
    }
    markDispatched();
  };

  const handleDispatchToSession = (sessionId: string) => {
    void useTerminalStore.getState().dispatchToSession(sessionId, code);
    markDispatched();
  };

  const handleSpawnAndDispatch = (agentId: string) => {
    void useTerminalStore.getState().spawnAndDispatch(getWorkspace(), agentId, code);
    markDispatched();
  };

  return (
    <div
      className={`code-block my-2 overflow-hidden rounded-control border border-line/60 bg-canvas transition-colors ${
        fill ? 'flex min-h-0 flex-1 flex-col' : ''
      }`}
    >
      <div className="code-block-header flex items-center justify-between border-b border-line/60 bg-sunken px-2.5 py-1 transition-colors">
        <span className="font-mono text-[10px] uppercase tracking-wide text-gray-500">
          {lang || 'text'}
          {streaming ? ' · streaming' : ''}
        </span>
        <div className="flex items-center gap-2">
          {note !== undefined && (
            <span
              title={note}
              className="max-w-[24rem] truncate font-sans text-[10px] text-amber-600"
            >
              {note}
            </span>
          )}

          {/* Split Button: [▶ 投递到终端 ▾] */}
          <div ref={menuRef} className="relative flex items-center">
            <div className="flex items-center rounded bg-surface/80 border border-line/60">
              <button
                type="button"
                onClick={handleDispatchDefault}
                title="将此代码/指令投递到激活终端（若无终端则自动新建并执行）"
                className="flex items-center gap-1 px-1.5 py-0.5 font-sans text-[10px] text-gray-600 hover:text-accent-fg transition-colors cursor-pointer"
              >
                <Play16Regular className="h-2.5 w-2.5 text-accent-fg" />
                <span>{dispatched ? '已投递' : '投递到终端'}</span>
              </button>
              <button
                type="button"
                onClick={() => setMenuOpen((prev) => !prev)}
                title="选择目标 Agent 终端或新建终端执行"
                className="flex items-center justify-center border-l border-line/60 px-1 py-0.5 text-gray-500 hover:text-gray-900 cursor-pointer"
              >
                <ChevronDown16Regular className="h-2.5 w-2.5" />
              </button>
            </div>

            {menuOpen && (
              <div className="material-flyout flyout-in absolute right-0 top-6 z-50 w-56 rounded-control border border-line bg-surface p-1.5 shadow-card font-sans">
                {sessions.length > 0 && (
                  <>
                    <div className="px-2 py-0.5 text-[10px] font-semibold text-gray-400">
                      发往已开启终端
                    </div>
                    {sessions.map((s) => (
                      <button
                        key={s.id}
                        type="button"
                        onClick={() => handleDispatchToSession(s.id)}
                        className="flex w-full items-center gap-1.5 rounded-control px-2 py-1 text-left text-xs text-gray-800 hover:bg-surface-hover"
                      >
                        <WindowConsole20Regular className="h-3.5 w-3.5 text-accent-fg shrink-0" />
                        <span className="truncate">发往 {s.title}</span>
                      </button>
                    ))}
                    <div className="my-1 border-t border-line/60" />
                  </>
                )}
                <div className="px-2 py-0.5 text-[10px] font-semibold text-gray-400">
                  自动新建终端并投递
                </div>
                {(
                  [
                    { id: 'agy', label: '新建 agy CLI 并投递' },
                    { id: 'claude', label: '新建 Claude Code 并投递' },
                    { id: 'codex', label: '新建 Codex CLI 并投递' },
                    { id: 'pwsh', label: '新建 PowerShell 并执行' },
                  ] as const
                ).map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() => handleSpawnAndDispatch(item.id)}
                    className="flex w-full items-center gap-1.5 rounded-control px-2 py-1 text-left text-xs text-gray-800 hover:bg-surface-hover"
                  >
                    <Add16Regular className="h-3.5 w-3.5 text-accent-fg shrink-0" />
                    <span className="truncate">{item.label}</span>
                  </button>
                ))}
              </div>
            )}
          </div>

          <button
            type="button"
            onClick={copy}
            title="复制代码"
            className="font-mono text-[10px] text-gray-500 hover:text-gray-900 transition-colors cursor-pointer"
          >
            {copied ? '已复制' : '复制'}
          </button>
        </div>
      </div>
      <pre
        className={`fluent-scrollbar overflow-auto px-3 py-2 font-mono text-[12px] leading-5 ${
          fill ? 'min-h-0 flex-1' : 'max-h-[28rem]'
        }`}
      >
        <code className="whitespace-pre">
          {tokens.map((token, index) => (
            <span key={index} className={TOKEN_CLASS[token.kind]}>
              {token.text}
            </span>
          ))}
        </code>
      </pre>
    </div>
  );
};
