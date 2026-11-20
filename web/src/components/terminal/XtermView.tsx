/**
 * Xterm Native Terminal View.
 *
 * Implements:
 * - Direct WebGL/Canvas/DOM terminal rendering via @xterm/xterm
 * - Responsive auto-fitting via @xterm/addon-fit
 * - Dynamic Fluent 2 Light/Dark theme switching
 * - Bidirectional Tauri PTY IPC data streaming
 * - Rex OSC 7501 status protocol + non-intrusive output tail & approval detector
 * - Floating "[💬 引用选区到会话]" chip when text is selected inside the terminal
 */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Chat16Regular } from '@fluentui/react-icons';
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import '@xterm/xterm/css/xterm.css';
import {
  writeTerminal,
  resizeTerminal,
  onTerminalData,
  onTerminalAgentState,
  onTerminalAgentTitle,
  onTerminalExit,
} from '../../client/tauriTerminal.ts';
import type { TerminalTabSession } from '../../stores/useTerminalStore.ts';
import {
  reportTerminalDimensions,
  requestInsertTerminalPill,
  useTerminalStore,
} from '../../stores/useTerminalStore.ts';
import { useConsoleStore } from '../../stores/useConsoleStore.ts';
import { useAppearanceStore } from '../../stores/appearance.ts';
import { TerminalInputQueue } from './terminalInputQueue.ts';

interface XtermViewProps {
  session: TerminalTabSession;
  isActive?: boolean;
}

export const terminalInstances = new Map<string, Terminal>();

const APPROVAL_PROMPT_PATTERN =
  /(\[y\/n[^\]]*\]|\(y\/n[^)]*\)|Allow\b.*\?|Approve\b|Confirm\b|Do you want to\b|Would you like to\b|Press Enter to continue)/i;

/**
 * Patterns indicating that an interactive CLI Agent TUI (`agy`, `claude`, `codex`, `synapse`)
 * or shell (`pwsh`, `bash`) has finished rendering its initial startup banner and
 * mounted its interactive input box.
 */
const TUI_INPUT_READY_PATTERN =
  /(\?\s+for\s+shortcuts|Auto mode:|shift\+tab to cycle|Type a message|Send a message|^>\s|^❯\s|agy\s*>|claude\s*>|codex\s*>|^PS\s+[^>]+>)/i;

/**
 * Indicators that an interactive CLI Agent (`agy`, `claude`, `codex`) is still
 * actively thinking or executing tools in the middle of a turn.
 */
const TUI_STILL_BUSY_PATTERN =
  /(esc to cancel|ctrl\+c to interrupt|Thinking\b|Working\b|[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]|running\s+[a-z0-9_.-]+…)/i;

function isTuiStillBusy(term: Terminal): boolean {
  const buffer = term.buffer.active;
  let checked = 0;
  for (let i = buffer.length - 1; i >= 0 && checked < 10; i--) {
    const line = buffer.getLine(i)?.translateToString(true).trim();
    if (!line) continue;
    checked++;
    if (TUI_STILL_BUSY_PATTERN.test(line)) {
      return true;
    }
  }
  return false;
}

function extractCleanTaskOutput(term: Terminal): string {
  const raw = getTerminalContext(term).text;
  if (!raw) return '';
  const cleaned = raw
    .split(/\r?\n/)
    .map((line) => line.trimEnd())
    .filter((line) => {
      const t = line.trim();
      if (!t) return false;
      if (TUI_CHROME_NOISE_PATTERN.test(t)) return false;
      if (/^[─━═\-_]{5,}$/.test(t)) return false;
      return true;
    });
  return cleaned.slice(-30).join('\n').trim();
}

function isTerminalInputBoxReady(
  term: Terminal,
  isAgent: boolean,
  elapsedMs: number,
): boolean {
  const buffer = term.buffer.active;
  let nonEmptyLines = 0;
  for (let i = buffer.length - 1; i >= 0 && nonEmptyLines < 16; i--) {
    const line = buffer.getLine(i)?.translateToString(true).trim();
    if (!line) continue;
    nonEmptyLines++;
    if (TUI_INPUT_READY_PATTERN.test(line)) {
      return true;
    }
  }
  // Plain shell fallback once prompt has settled
  if (!isAgent && nonEmptyLines >= 1 && elapsedMs >= 900) {
    return true;
  }
  // Custom unknown Agent CLI fallback after 7s of boot time
  if (isAgent && nonEmptyLines >= 3 && elapsedMs >= 7000) {
    return true;
  }
  return false;
}

const TUI_CHROME_NOISE_PATTERN =
  /^(\?\s+for\s+shortcuts|>\s*Auto mode:|>\s*$|Auto mode:|\s*auto\s+·\s+Gemini|Antigravity CLI\s+\d|Press up to edit queued messages)/i;

function readTailSnapshot(term: Terminal): { approvalLine?: string; tailLines: string[] } {
  const buffer = term.buffer.active;
  const tailLines: string[] = [];
  let approvalLine: string | undefined;

  for (let i = buffer.length - 1; i >= 0 && tailLines.length < 4; i--) {
    const line = buffer.getLine(i)?.translateToString(true).trim();
    if (!line) continue;
    if (!approvalLine && APPROVAL_PROMPT_PATTERN.test(line)) {
      approvalLine = line.slice(0, 90);
    }
    if (TUI_CHROME_NOISE_PATTERN.test(line) || /^[─━═\-_]{5,}$/.test(line)) {
      continue;
    }
    tailLines.unshift(line.slice(0, 120));
  }
  return { approvalLine, tailLines: tailLines.slice(-3) };
}

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

/**
 * Build a self-contained handoff prompt carrying the current main chat session's
 * context (session title, changed files from the latest turn, recent user request
 * and assistant conclusion) so a newly spawned or targeted Terminal CLI Agent
 * (`agy`, `claude`, `codex`) has full context to act immediately.
 */
export function buildSessionContextHandoffPrompt(userInstruction: string): string {
  const cs = useConsoleStore.getState();
  const parts: string[] = [];

  if (cs.sessionTitle && cs.sessionTitle.trim()) {
    parts.push(`当前会话主题: "${cs.sessionTitle.trim()}"`);
  }

  // Extract latest changed files from the transcript's `changes` rows
  const changeRows = cs.messages.filter(
    (m) => m.type === 'changes' && Array.isArray(m.changes) && m.changes.length > 0,
  );
  if (changeRows.length > 0) {
    const latestChanges = changeRows[changeRows.length - 1].changes ?? [];
    const activeFiles = latestChanges
      .filter((c) => !c.reverted)
      .slice(0, 12)
      .map((c) =>
        c.binary ? c.path : `${c.path} (+${c.insertions} -${c.deletions})`,
      );
    if (activeFiles.length > 0) {
      parts.push(`本轮已修改文件 (${activeFiles.length}个): ${activeFiles.join(', ')}`);
    }
  }

  // Extract the most recent user prompt and assistant response summary
  const userMsgs = cs.messages.filter((m) => m.type === 'user' && m.content?.trim());
  const assistantMsgs = cs.messages.filter(
    (m) => m.type === 'assistant' && m.content?.trim(),
  );

  if (userMsgs.length > 0) {
    const lastUser = (userMsgs[userMsgs.length - 1].content ?? '')
      .replace(/\s+/g, ' ')
      .trim()
      .slice(0, 220);
    if (lastUser) {
      parts.push(`最近用户诉求: "${lastUser}"`);
    }
  }

  if (assistantMsgs.length > 0) {
    const lastAssistant = (assistantMsgs[assistantMsgs.length - 1].content ?? '')
      .replace(/```[\s\S]*?```/g, '[代码块]')
      .replace(/\s+/g, ' ')
      .trim()
      .slice(0, 360);
    if (lastAssistant) {
      parts.push(`主会话最新分析/改动摘要: "${lastAssistant}"`);
    }
  }

  const cleanInstruction = userInstruction.trim() || '请基于上述主会话上下文与已修改文件进行审查与处理';
  if (parts.length === 0) {
    return cleanInstruction;
  }

  return `[主会话上下文同步 | ${parts.join(' | ')}] ===> 请执行任务: ${cleanInstruction}`;
}

/**
 * Intercept `@spawn:<agent>` and `@send:<seq|all>` directives before main chat submission:
 * - If the prompt is a dispatch directive to a terminal Agent (`@spawn:agy ...` or `@send:1 ...`),
 *   it packages the current main session's context + any referenced `@term` outputs,
 *   dispatches directly to the terminal Agent in the cockpit, and returns `{ intercepted: true }`
 *   so the main chat session does NOT run a redundant LLM turn or spawn an internal subagent.
 * - Otherwise, expands `@term:<seq>`, `@term:all`, and `@term:quote:<id>` tokens for the
 *   main chat session and returns `{ intercepted: false, promptText }`.
 */
export function processTerminalMentionsAndDirectives(
  rawPrompt: string,
  workspace?: string,
): { intercepted: boolean; promptText: string } {
  const state = useTerminalStore.getState();
  let promptText = rawPrompt;
  const appendedContexts: string[] = [];

  // 1. Expand `@term:quote:<quoteId>` tokens
  promptText = promptText.replace(/@term:quote:([a-zA-Z0-9_-]+)/g, (full, quoteId: string) => {
    const quote = state.terminalQuotes[quoteId];
    if (!quote) return full;
    appendedContexts.push(
      `<terminal_selection terminal="${quote.title}" lines="${quote.lineCount}">\n${quote.text}\n</terminal_selection>`,
    );
    return `[终端 ${quote.title} 选区 (${quote.lineCount}行)]`;
  });

  // 2. Check for `@spawn:<agentId>` or `@send:<seq|all>` terminal dispatch directives
  const spawnMatches = [...promptText.matchAll(/@spawn:([a-zA-Z0-9_-]+)/g)];
  const sendMatches = [...promptText.matchAll(/@send:(\d+|all)\b/gi)];

  if (spawnMatches.length > 0 || sendMatches.length > 0) {
    const userInstruction = promptText
      .replace(/@spawn:[a-zA-Z0-9_-]+\s*/g, '')
      .replace(/@send:(?:\d+|all)\s*/gi, '')
      .trim();

    const handoffBase = buildSessionContextHandoffPrompt(userInstruction);
    const fullHandoff =
      appendedContexts.length > 0
        ? `${handoffBase} | 附加终端选区: ${appendedContexts.join(' ')}`
        : handoffBase;

    for (const m of spawnMatches) {
      const agentId = m[1];
      void state.spawnAndDispatch(workspace, agentId, fullHandoff);
    }

    for (const m of sendMatches) {
      const targetToken = m[1].toLowerCase();
      if (targetToken === 'all') {
        void state.broadcastInput(fullHandoff);
      } else {
        const seq = Number(targetToken);
        const targetSession = state.sessions.find((s) => s.seq === seq);
        if (targetSession) {
          void state.dispatchToSession(targetSession.id, fullHandoff);
        }
      }
    }

    return { intercepted: true, promptText: '' };
  }

  // 3. Check for `@term:all` (referencing terminal output into main chat)
  if (/@term:all\b/i.test(promptText)) {
    for (const s of state.sessions) {
      const term = terminalInstances.get(s.id);
      const ctx = term ? getTerminalContext(term).text : s.tailLines.join('\n');
      if (ctx) {
        appendedContexts.push(
          `<terminal_context id="${s.seq}" title="${s.title}" agent="${s.agentId}" status="${s.activity}">\n${ctx}\n</terminal_context>`,
        );
      }
    }
  }

  // 4. Check for `@term:<seq>` (referencing terminal output into main chat)
  const termSeqMatches = [...promptText.matchAll(/@term:(\d+)\b/g)];
  const seenSeqs = new Set<number>();
  for (const m of termSeqMatches) {
    const seq = Number(m[1]);
    if (seenSeqs.has(seq)) continue;
    seenSeqs.add(seq);
    const s = state.sessions.find((item) => item.seq === seq);
    if (!s) continue;
    const term = terminalInstances.get(s.id);
    const ctx = term ? getTerminalContext(term).text : s.tailLines.join('\n');
    if (ctx) {
      appendedContexts.push(
        `<terminal_context id="${s.seq}" title="${s.title}" agent="${s.agentId}" status="${s.activity}">\n${ctx}\n</terminal_context>`,
      );
    }
  }

  if (appendedContexts.length === 0) {
    return { intercepted: false, promptText };
  }

  return {
    intercepted: false,
    promptText: `${promptText.trim()}\n\n${appendedContexts.join('\n\n')}`,
  };
}

export const XtermView: React.FC<XtermViewProps> = ({ session, isActive = true }) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const xtermRef = useRef<Terminal | null>(null);
  const fitAddonRef = useRef<FitAddon | null>(null);
  const lastHookRef = useRef<{ ptyId: number; seq: number } | null>(null);
  const appearance = useAppearanceStore((s) => s.appearance);
  const doFitRef = useRef<(() => void) | null>(null);

  const [selectedText, setSelectedText] = useState('');

  // Quote current terminal selection as an atomic pill in the main chat composer
  const handleQuoteSelectionToChat = useCallback(() => {
    if (!selectedText.trim()) return;
    const quote = useTerminalStore
      .getState()
      .registerSelectionQuote(session.id, selectedText);
    if (!quote) return;

    const store = useTerminalStore.getState();
    if (store.displayMode === 'stage') {
      store.setDisplayMode('split');
    }

    requestInsertTerminalPill({
      token: `@term:quote:${quote.quoteId}`,
      label: `终端 #${quote.seq} 选区 (${quote.lineCount}行)`,
      detail: `${quote.title}: ${quote.text.slice(0, 120)}`,
    });

    xtermRef.current?.clearSelection();
    setSelectedText('');
  }, [selectedText, session.id]);

  // Tab activation observer: re-fit & focus when switching back
  useEffect(() => {
    if (!isActive) return;
    const frame = requestAnimationFrame(() => {
      try {
        doFitRef.current?.();
        xtermRef.current?.focus();
      } catch {}
    });
    return () => cancelAnimationFrame(frame);
  }, [isActive]);

  const { id: sessionId, ptyId: titlePtyId } = session;
  useEffect(() => {
    if (!titlePtyId) return;
    let disposed = false;
    let unlisten: (() => void) | undefined;
    void onTerminalAgentTitle(titlePtyId, (payload) => {
      if (disposed) return;
      useTerminalStore.getState().setAgentConversationTitle(sessionId, titlePtyId, payload.title);
    }).then((stop) => {
      if (disposed) stop();
      else unlisten = stop;
    }).catch(() => {});
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [sessionId, titlePtyId]);

  useEffect(() => {
    if (!containerRef.current) return;

    const handleSessionExit = useTerminalStore.getState().handleSessionExit;

    const themeAttr = document.documentElement.dataset.theme;
    const isDark =
      appearance === 'dark' ||
      (appearance === 'system' && themeAttr !== 'fluent-light' && themeAttr !== 'light') ||
      themeAttr === 'fluent-dark';

    const isWindows =
      typeof navigator !== 'undefined' && /windows/i.test(navigator.userAgent);

    const term = new Terminal({
      allowProposedApi: true,
      allowTransparency: true,
      cursorBlink: true,
      cursorStyle: 'bar',
      fontSize: 13,
      letterSpacing: 0,
      lineHeight: 1.35,
      windowsPty: isWindows ? { backend: 'conpty' } : undefined,
      fontFamily:
        "'CaskaydiaCove Nerd Font', 'CaskaydiaCove NF', 'Cascadia Code NF', 'Cascadia Mono NF', 'JetBrainsMono Nerd Font', 'MesloLGS NF', 'FiraCode Nerd Font', 'Caskaydia Cove Nerd Font', 'Cascadia Code', Consolas, 'Segoe UI Symbol', monospace",
      theme: {
        background: 'rgba(0, 0, 0, 0)',
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

    const ptyId = session.ptyId;
    // Keep authority across xterm recreation (e.g. theme changes), but not across PTY restarts.
    const hookAuthoritative = () => ptyId !== null && lastHookRef.current?.ptyId === ptyId;

    // Native support for Rex's OSC 7501 agent status reporting sequence
    const oscDisposable = term.parser.registerOscHandler(7501, (data) => {
      if (!hookAuthoritative()) {
        useTerminalStore.getState().updateActivityFromOsc(session.id, data);
      }
      return true;
    });

    // Selection listener for the floating "[💬 引用选区到会话]" chip
    const selectionSub = term.onSelectionChange(() => {
      setSelectedText(term.getSelection().trim());
    });

    let idleTimer: ReturnType<typeof setTimeout> | null = null;
    let dispatchTimer: ReturnType<typeof setTimeout> | null = null;
    const mountedAt = Date.now();
    let lastDataAt = Date.now();
    const stageReflowForUserDecision = (
      current: TerminalTabSession,
      cleanMarkdown: string,
    ) => {
      if (!cleanMarkdown.trim()) return;
      useTerminalStore.getState().setReflowResult(current.id, cleanMarkdown.trim());
    };

    const schedulePendingDispatchCheck = () => {
      if (disposed || hookAuthoritative()) return;
      if (!useTerminalStore.getState().pendingDispatches[session.id]) return;
      if (dispatchTimer) clearTimeout(dispatchTimer);
      dispatchTimer = setTimeout(() => {
        if (disposed || hookAuthoritative()) return;
        if (!useTerminalStore.getState().pendingDispatches[session.id]) return;
        const now = Date.now();
        const elapsed = now - mountedAt;
        const quietMs = now - lastDataAt;
        if (quietMs >= 650 && isTerminalInputBoxReady(term, session.isAgent, elapsed)) {
          try {
            term.focus();
          } catch {}
          void useTerminalStore.getState().consumePendingDispatch(session.id);
        } else {
          schedulePendingDispatchCheck();
        }
      }, 450);
    };

    if (useTerminalStore.getState().pendingDispatches[session.id]) {
      schedulePendingDispatchCheck();
    }

    const scheduleIdleInspection = () => {
      if (hookAuthoritative()) return;
      if (idleTimer) clearTimeout(idleTimer);
      idleTimer = setTimeout(() => {
        if (disposed || hookAuthoritative()) return;
        const current = useTerminalStore.getState().sessions.find((s) => s.id === session.id);
        if (!current || current.status !== 'running') return;
        const { approvalLine, tailLines } = readTailSnapshot(term);
        if (approvalLine) {
          useTerminalStore
            .getState()
            .setSessionActivity(session.id, 'waiting_input', approvalLine, tailLines);
          return;
        }

        const now = Date.now();
        const taskAge = current.taskStartedAt ? now - current.taskStartedAt : 9999;
        const quietMs = now - lastDataAt;
        if (current.hasActiveTask) {
          if (taskAge < 2800 || quietMs < 2100 || isTuiStillBusy(term)) {
            scheduleIdleInspection();
            return;
          }
          const shouldReflow = useTerminalStore
            .getState()
            .completeActiveTask(session.id, tailLines);
          if (shouldReflow) {
            const cleanOutput = extractCleanTaskOutput(term) || tailLines.join('\n');
            stageReflowForUserDecision(current, cleanOutput);
          }
        } else if (current.activity === 'working') {
          useTerminalStore
            .getState()
            .setSessionActivity(session.id, 'idle', undefined, tailLines);
        }
      }, 2200);
    };

    term.attachCustomKeyEventHandler((e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && (e.key === '`' || e.key === '~' || e.code === 'Backquote')) {
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

    let disposed = false;
    const reportedErrors = new Set<string>();
    const reportError = (message: string): void => {
      if (disposed || reportedErrors.has(message)) return;
      reportedErrors.add(message);
      term.write(`\r\n\x1b[31m${message}\x1b[0m\r\n`);
    };
    let lastCols = -1;
    let lastRows = -1;
    let resizeTimer: ReturnType<typeof setTimeout> | null = null;

    const flushResize = () => {
      const ptyId = session.ptyId;
      if (!ptyId || disposed || term.cols <= 0 || term.rows <= 0) return;
      if (term.cols === lastCols && term.rows === lastRows) return;
      lastCols = term.cols;
      lastRows = term.rows;
      void resizeTerminal(ptyId, term.cols, term.rows).catch(() => {
        if (disposed) return;
        lastCols = -1;
        lastRows = -1;
        reportError('终端尺寸同步失败，请重新调整面板大小');
      });
    };

    const syncSize = () => {
      const ptyId = session.ptyId;
      if (!ptyId || disposed || term.cols <= 0 || term.rows <= 0) return;
      if (term.cols === lastCols && term.rows === lastRows) return;
      if (resizeTimer) clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        resizeTimer = null;
        flushResize();
      }, 60);
    };

    const doFit = () => {
      if (disposed) return;
      try {
        if (
          containerRef.current &&
          containerRef.current.clientWidth > 0 &&
          containerRef.current.clientHeight > 0
        ) {
          fitAddon.fit();
          reportTerminalDimensions(session.id, term.cols, term.rows);
          syncSize();
        }
      } catch {}
    };
    doFitRef.current = doFit;

    doFit();
    if (typeof document !== 'undefined' && 'fonts' in document) {
      document.fonts.ready
        .then(() => {
          doFit();
        })
        .catch(() => {});
    }
    const initialFrame = requestAnimationFrame(() => doFit());

    xtermRef.current = term;
    terminalInstances.set(session.id, term);
    fitAddonRef.current = fitAddon;

    const inputQueue = ptyId
      ? new TerminalInputQueue((data) => writeTerminal(ptyId, data), {
          onError: () => reportError('终端输入发送失败，请重试'),
        })
      : null;

    const dataSub = term.onData((data) => {
      if (data === '\r' || data === '\n') {
        // User manually pressed Enter inside the terminal: mark an active task if autoReflow is on
        const cur = useTerminalStore.getState().sessions.find((s) => s.id === session.id);
        if (cur && cur.isAgent && !cur.hasActiveTask) {
          useTerminalStore.getState().markTaskStarted(session.id);
        }
      }
      inputQueue?.enqueue(data);
    });

    let cleanupDataStream: (() => void) | null = null;
    let cleanupAgentStateStream: (() => void) | null = null;
    let cleanupExitStream: (() => void) | null = null;

    if (ptyId) {
      // Subscribe to deterministic lifecycle hook state events (`SessionStart`, `PreInvocation`, `PostToolUse`, `Stop`)
      void onTerminalAgentState(ptyId, (hookEvt) => {
        if (disposed || hookEvt.ptyId !== ptyId) return;
        const previous = lastHookRef.current;
        if (previous?.ptyId === ptyId && hookEvt.seq <= previous.seq) return;
        const hook = { ptyId, seq: hookEvt.seq };
        lastHookRef.current = hook;
        if (idleTimer) clearTimeout(idleTimer);
        if (dispatchTimer) clearTimeout(dispatchTimer);
        const { tailLines } = readTailSnapshot(term);

        const stopped = hookEvt.event === 'Stop';
        const done = stopped && hookEvt.fullyIdle !== false;
        if ((stopped && !done) || (!stopped && hookEvt.state === 'working')) {
          useTerminalStore.getState().markTaskStarted(session.id);
          useTerminalStore
            .getState()
            .setSessionActivity(session.id, 'working', stopped ? '正在收尾...' : hookEvt.detail, tailLines);
        } else if (!stopped && hookEvt.state === 'idle') {
          useTerminalStore
            .getState()
            .setSessionActivity(session.id, 'idle', hookEvt.detail, tailLines);
          if (useTerminalStore.getState().pendingDispatches[session.id]) {
            setTimeout(() => {
              if (disposed || lastHookRef.current !== hook) return;
              try {
                term.focus();
              } catch {}
              void useTerminalStore.getState().consumePendingDispatch(session.id);
            }, 300);
          }
        } else if (done) {
          const current = useTerminalStore.getState().sessions.find((s) => s.id === session.id);
          const cleanMarkdown =
            (hookEvt.lastAssistantMarkdown && hookEvt.lastAssistantMarkdown.trim()) ||
            extractCleanTaskOutput(term) ||
            tailLines.join('\n');
          const shouldReflow = useTerminalStore
            .getState()
            .completeActiveTask(session.id, tailLines, cleanMarkdown, true);
          if (shouldReflow && current) {
            stageReflowForUserDecision(current, cleanMarkdown);
          }
          // If another prompt was queued while the previous turn was running, flush it now
          if (useTerminalStore.getState().pendingDispatches[session.id]) {
            setTimeout(() => {
              if (disposed || lastHookRef.current !== hook) return;
              try {
                term.focus();
              } catch {}
              void useTerminalStore.getState().consumePendingDispatch(session.id);
            }, 400);
          }
        }
      })
        .then((unlisten) => {
          if (disposed) {
            unlisten();
            return;
          }
          cleanupAgentStateStream = unlisten;
        })
        .catch(() => {});

      void onTerminalData(ptyId, (data) => {
        if (disposed) return;
        lastDataAt = Date.now();
        term.write(data, () => {
          if (disposed) return;
          lastDataAt = Date.now();
          if (useTerminalStore.getState().pendingDispatches[session.id]) {
            schedulePendingDispatchCheck();
          }
          const { approvalLine, tailLines } = readTailSnapshot(term);
          if (hookAuthoritative()) {
            // Update live tail preview lines without overriding deterministic hook state
            const cur = useTerminalStore.getState().sessions.find((s) => s.id === session.id);
            if (cur) {
              useTerminalStore
                .getState()
                .setSessionActivity(session.id, cur.activity, cur.activityDetail, tailLines);
            }
          } else if (approvalLine) {
            useTerminalStore
              .getState()
              .setSessionActivity(session.id, 'waiting_input', approvalLine, tailLines);
          } else {
            useTerminalStore
              .getState()
              .setSessionActivity(session.id, 'working', undefined, tailLines);
            scheduleIdleInspection();
          }
        });
      })
        .then((unlisten) => {
          if (disposed) {
            unlisten();
            return;
          }
          cleanupDataStream = unlisten;
        })
        .catch(() => reportError('终端输出连接失败，请重新打开终端'));

      void onTerminalExit(ptyId, () => {
        if (disposed) return;
        handleSessionExit(ptyId);
      })
        .then((unlisten) => {
          if (disposed) {
            unlisten();
            return;
          }
          cleanupExitStream = unlisten;
        })
        .catch(() => reportError('终端退出监听失败，请重新打开终端'));

      syncSize();
    } else if (session.status === 'error') {
      term.writeln(
        `\r\n\x1b[31m启动终端失败: ${session.errorMessage || '无法连接原生 PTY'}\x1b[0m\r\n`,
      );
    }

    const resizeObserver = new ResizeObserver(() => {
      doFit();
    });

    resizeObserver.observe(containerRef.current);

    return () => {
      disposed = true;
      if (idleTimer) clearTimeout(idleTimer);
      if (dispatchTimer) clearTimeout(dispatchTimer);
      if (resizeTimer) clearTimeout(resizeTimer);
      oscDisposable.dispose();
      selectionSub.dispose();
      cancelAnimationFrame(initialFrame);
      if (doFitRef.current === doFit) doFitRef.current = null;
      terminalInstances.delete(session.id);
      dataSub.dispose();
      resizeObserver.disconnect();
      inputQueue?.dispose();
      if (cleanupDataStream) cleanupDataStream();
      if (cleanupAgentStateStream) cleanupAgentStateStream();
      if (cleanupExitStream) cleanupExitStream();
      xtermRef.current = null;
      fitAddonRef.current = null;
      term.dispose();
    };
  }, [session.ptyId, session.status, session.errorMessage, session.id, appearance]);

  const selectedLineCount = selectedText ? selectedText.split(/\r?\n/).length : 0;

  return (
    <div className="relative h-full w-full overflow-hidden px-3 py-1.5">
      <div
        ref={containerRef}
        className="h-full w-full overflow-hidden select-text"
      />
      {selectedLineCount > 0 && (
        <button
          type="button"
          onClick={handleQuoteSelectionToChat}
          title="将当前选中的终端输出作为紧凑胶囊插入主对话输入框"
          className=" absolute top-2 right-3 z-30 flex items-center gap-1 rounded-full bg-accent px-2.5 py-1 text-[11px] font-medium text-on-accent shadow-card transition-transform hover:scale-105"
        >
          <Chat16Regular className="h-3.5 w-3.5" />
          <span>引用选区到会话 ({selectedLineCount}行)</span>
        </button>
      )}
    </div>
  );
};
