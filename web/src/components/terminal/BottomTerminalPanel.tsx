/**
 * Central Bottom / Side-by-Side / Full-Stage Integrated Terminal & Tiling Multi-Agent CLI Cockpit.
 *
 * Implements:
 * - Three Workspace Display Modes (`displayMode`):
 *   - `dock`: Bottom drawer with top draggable height resizer (`↕`)
 *   - `split`: Side-by-Side Split Workspace (Left Chat + Right Tiling Terminal Cockpit)
 *     with left draggable width resizer (`↔`)
 *   - `stage`: Full workspace stage (100% terminal canvas)
 * - Tiling Window Manager layouts (`auto`, `master-stack`, `columns`, `rows`, `monocle`)
 * - Interactive drag-to-resize on every internal pane border (`↔`, `↕`, `↘`)
 * - Per-pane header bar with `[🔔 完成后回流 ☑]`, `[广播已勾选 ☑]`, `[抓取输出 → 转发]`,
 *   `[↻ 重启]`, `[⤢ 放大聚焦]`, and `[× 关闭]`
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Add16Regular,
  Alert16Regular,
  ArrowClockwise16Regular,
  ArrowExpand16Regular,
  ArrowMinimize16Regular,
  Chat16Regular,
  Checkmark16Regular,
  ChevronDown16Regular,
  Dismiss16Regular,
  Grid16Regular,
  PanelBottom20Regular,
  PanelLeft16Regular,
  Send16Regular,
  Sparkle16Regular,
  SplitHorizontal16Regular,
  SplitVertical16Regular,
  Square16Regular,
  SquareMultiple16Regular,
  WindowConsole20Regular,
} from '@fluentui/react-icons';
import { useShallow } from 'zustand/react/shallow';
import {
  AGENT_CLI_PRESETS,
  type AgentActivityState,
  type TerminalDisplayMode,
  type TerminalGridLayout,
  type TerminalTabSession,
  formatPtySubmission,
  useTerminalStore,
} from '../../stores/useTerminalStore.ts';
import { useConsoleStore } from '../../stores/useConsoleStore.ts';
import { writeTerminal } from '../../client/tauriTerminal.ts';
import { XtermView, terminalInstances, getTerminalContext } from './XtermView.tsx';
import { TERMINAL_MAXIMIZE_CHORD } from '../consoleShortcuts.ts';
import { insertTextAtCaret } from '../composer/composerSelection.ts';

function TerminalPaneTitle({ session }: { session: TerminalTabSession }) {
  const renameSession = useTerminalStore((s) => s.renameSession);
  const [draft, setDraft] = useState<string | null>(null);
  const restoreFocus = useRef(false);
  const focusInput = useCallback((input: HTMLInputElement | null) => {
    input?.focus();
    input?.select();
  }, []);
  const focusTitle = useCallback((button: HTMLButtonElement | null) => {
    if (button && restoreFocus.current) {
      restoreFocus.current = false;
      button.focus();
    }
  }, []);
  const startEditing = () => setDraft(session.alias ?? '');

  if (draft !== null) {
    return (
      <input
        ref={focusInput}
        aria-label={`终端 ${session.seq} 的别名`}
        placeholder="终端别名"
        value={draft}
        maxLength={80}
        onChange={(e) => setDraft(e.currentTarget.value)}
        onBlur={() => setDraft(null)}
        onKeyDown={(e) => {
          if (e.nativeEvent.isComposing || (e.key !== 'Enter' && e.key !== 'Escape')) return;
          e.preventDefault();
          e.stopPropagation();
          if (e.key === 'Enter') renameSession(session.id, draft);
          restoreFocus.current = true;
          setDraft(null);
        }}
        className="w-28 min-w-0 rounded border border-accent-fg bg-surface px-1.5 text-inherit"
      />
    );
  }

  return (
    <button
      ref={focusTitle}
      type="button"
      className="min-w-0 truncate text-left font-medium text-gray-900"
      title={`${session.title} — 双击编辑别名`}
      onDoubleClick={(e) => {
        e.stopPropagation();
        startEditing();
      }}
      onKeyDown={(e) => {
        if (e.nativeEvent.isComposing || e.key !== 'Enter') return;
        e.preventDefault();
        e.stopPropagation();
        startEditing();
      }}
      onClick={(e) => {
        if (e.detail === 0) startEditing();
      }}
    >
      {session.title}
    </button>
  );
}

function activityDotClass(activity: AgentActivityState): string {
  switch (activity) {
    case 'working':
      return 'bg-emerald-500 animate-pulse';
    case 'waiting_input':
      return 'bg-amber-500 animate-pulse';
    case 'exited':
      return 'bg-red-500';
    default:
      return 'bg-gray-400';
  }
}

function activityLabel(activity: AgentActivityState): string {
  switch (activity) {
    case 'working':
      return '运行中';
    case 'waiting_input':
      return '等待确认';
    case 'exited':
      return '已退出';
    default:
      return '就绪';
  }
}

export type HorizontalDragSpec =
  | {
      kind: 'master';
      initialRatio: number;
    }
  | {
      kind: 'col-pair';
      leftId: string;
      rightId: string;
      leftWeight: number;
      rightWeight: number;
      leftPct: number;
      combinedPct: number;
    };

export interface VerticalDragSpec {
  kind: 'row-pair';
  topKey: string;
  bottomKey: string;
  topWeight: number;
  bottomWeight: number;
  topPct: number;
  combinedPct: number;
}

export interface TileRect {
  leftPct: number;
  topPct: number;
  widthPct: number;
  heightPct: number;
  rightDrag?: HorizontalDragSpec;
  bottomDrag?: VerticalDragSpec;
}

export function computeTilingRects(
  layout: TerminalGridLayout,
  visibleSessions: TerminalTabSession[],
  colWeights: Record<string, number>,
  rowWeights: Record<string, number>,
  masterRatio: number,
): Map<string, TileRect> {
  const result = new Map<string, TileRect>();
  const total = visibleSessions.length;
  if (total === 0) return result;

  if (total === 1 || layout === 'monocle') {
    result.set(visibleSessions[0].id, {
      leftPct: 0,
      topPct: 0,
      widthPct: 100,
      heightPct: 100,
    });
    return result;
  }

  if (layout === 'columns') {
    const weights = visibleSessions.map((s) => Math.max(0.15, colWeights[s.id] ?? 1));
    const sumW = weights.reduce((a, b) => a + b, 0);
    let accLeft = 0;
    for (let i = 0; i < total; i++) {
      const s = visibleSessions[i];
      const wPct = (weights[i] / sumW) * 100;
      const nextS = i < total - 1 ? visibleSessions[i + 1] : undefined;
      const nextW = i < total - 1 ? weights[i + 1] : 0;
      const nextWPct = i < total - 1 ? (nextW / sumW) * 100 : 0;
      result.set(s.id, {
        leftPct: accLeft,
        topPct: 0,
        widthPct: wPct,
        heightPct: 100,
        rightDrag: nextS
          ? {
              kind: 'col-pair',
              leftId: s.id,
              rightId: nextS.id,
              leftWeight: weights[i],
              rightWeight: nextW,
              leftPct: wPct,
              combinedPct: wPct + nextWPct,
            }
          : undefined,
      });
      accLeft += wPct;
    }
    return result;
  }

  if (layout === 'rows') {
    const weights = visibleSessions.map((s) => Math.max(0.15, rowWeights[s.id] ?? 1));
    const sumH = weights.reduce((a, b) => a + b, 0);
    let accTop = 0;
    for (let i = 0; i < total; i++) {
      const s = visibleSessions[i];
      const hPct = (weights[i] / sumH) * 100;
      const nextS = i < total - 1 ? visibleSessions[i + 1] : undefined;
      const nextH = i < total - 1 ? weights[i + 1] : 0;
      const nextHPct = i < total - 1 ? (nextH / sumH) * 100 : 0;
      result.set(s.id, {
        leftPct: 0,
        topPct: accTop,
        widthPct: 100,
        heightPct: hPct,
        bottomDrag: nextS
          ? {
              kind: 'row-pair',
              topKey: s.id,
              bottomKey: nextS.id,
              topWeight: weights[i],
              bottomWeight: nextH,
              topPct: hPct,
              combinedPct: hPct + nextHPct,
            }
          : undefined,
      });
      accTop += hPct;
    }
    return result;
  }

  if (layout === 'master-stack') {
    const master = visibleSessions[0];
    const masterWPct = Math.max(18, Math.min(82, masterRatio * 100));
    const stackWPct = 100 - masterWPct;

    result.set(master.id, {
      leftPct: 0,
      topPct: 0,
      widthPct: masterWPct,
      heightPct: 100,
      rightDrag: {
        kind: 'master',
        initialRatio: masterWPct / 100,
      },
    });

    const stackSessions = visibleSessions.slice(1);
    const stackWeights = stackSessions.map((s) => Math.max(0.15, rowWeights[s.id] ?? 1));
    const sumH = stackWeights.reduce((a, b) => a + b, 0);
    let accTop = 0;

    for (let i = 0; i < stackSessions.length; i++) {
      const s = stackSessions[i];
      const hPct = (stackWeights[i] / sumH) * 100;
      const nextS = i < stackSessions.length - 1 ? stackSessions[i + 1] : undefined;
      const nextH = i < stackSessions.length - 1 ? stackWeights[i + 1] : 0;
      const nextHPct = i < stackSessions.length - 1 ? (nextH / sumH) * 100 : 0;

      result.set(s.id, {
        leftPct: masterWPct,
        topPct: accTop,
        widthPct: stackWPct,
        heightPct: hPct,
        bottomDrag: nextS
          ? {
              kind: 'row-pair',
              topKey: s.id,
              bottomKey: nextS.id,
              topWeight: stackWeights[i],
              bottomWeight: nextH,
              topPct: hPct,
              combinedPct: hPct + nextHPct,
            }
          : undefined,
      });
      accTop += hPct;
    }
    return result;
  }

  // `auto`: Smart N-pane grid
  const maxCols = total <= 4 ? 2 : 3;
  const rows: TerminalTabSession[][] = [];
  for (let i = 0; i < total; i += maxCols) {
    rows.push(visibleSessions.slice(i, i + maxCols));
  }

  const numRows = rows.length;
  const rKeys = rows.map((_, rIdx) => `auto-row-${rIdx}`);
  const rWeights = rKeys.map((k) => Math.max(0.15, rowWeights[k] ?? 1));
  const sumRowW = rWeights.reduce((a, b) => a + b, 0);

  let accTop = 0;
  for (let r = 0; r < numRows; r++) {
    const rowSessions = rows[r];
    const rowHPct = (rWeights[r] / sumRowW) * 100;
    const nextRowKey = r < numRows - 1 ? rKeys[r + 1] : undefined;
    const nextRowW = r < numRows - 1 ? rWeights[r + 1] : 0;
    const nextRowHPct = r < numRows - 1 ? (nextRowW / sumRowW) * 100 : 0;

    const cWeights = rowSessions.map((s) => Math.max(0.15, colWeights[s.id] ?? 1));
    const sumColW = cWeights.reduce((a, b) => a + b, 0);

    let accLeft = 0;
    for (let c = 0; c < rowSessions.length; c++) {
      const s = rowSessions[c];
      const colWPct = (cWeights[c] / sumColW) * 100;
      const nextColS = c < rowSessions.length - 1 ? rowSessions[c + 1] : undefined;
      const nextColW = c < rowSessions.length - 1 ? cWeights[c + 1] : 0;
      const nextColWPct = c < rowSessions.length - 1 ? (nextColW / sumColW) * 100 : 0;

      result.set(s.id, {
        leftPct: accLeft,
        topPct: accTop,
        widthPct: colWPct,
        heightPct: rowHPct,
        rightDrag: nextColS
          ? {
              kind: 'col-pair',
              leftId: s.id,
              rightId: nextColS.id,
              leftWeight: cWeights[c],
              rightWeight: nextColW,
              leftPct: colWPct,
              combinedPct: colWPct + nextColWPct,
            }
          : undefined,
        bottomDrag: nextRowKey
          ? {
              kind: 'row-pair',
              topKey: rKeys[r],
              bottomKey: nextRowKey,
              topWeight: rWeights[r],
              bottomWeight: nextRowW,
              topPct: rowHPct,
              combinedPct: rowHPct + nextRowHPct,
            }
          : undefined,
      });
      accLeft += colWPct;
    }
    accTop += rowHPct;
  }

  return result;
}

export const BottomTerminalPanel: React.FC = () => {
  const {
    open,
    height,
    isMaximized,
    displayMode,
    splitWorkspaceRatio,
    gridLayout,
    zoomedSessionId,
    colWeights,
    rowWeights,
    masterRatio,
    activeSessionId,
    sessions,
    pendingDispatches,
    setOpen,
    setHeight,
    toggleMaximize,
    setDisplayMode,
    setSplitWorkspaceRatio,
    setGridLayout,
    toggleZoomSession,
    promoteToMaster,
    setMasterRatio,
    setColPairWeights,
    setRowPairWeights,
    resetTilingWeights,
    setActiveSession,
    createSession,
    consumePendingDispatch,
    restartSession,
    closeSession,
    toggleBroadcastTarget,
    setAllBroadcastTargets,
    toggleAutoReflow,
    broadcastInput,
    sendQuickResponse,
    setSessionActivity,
  } = useTerminalStore(
    useShallow((s) => ({
      open: s.open,
      height: s.height,
      isMaximized: s.isMaximized,
      displayMode: s.displayMode,
      splitWorkspaceRatio: s.splitWorkspaceRatio,
      gridLayout: s.gridLayout,
      zoomedSessionId: s.zoomedSessionId,
      colWeights: s.colWeights,
      rowWeights: s.rowWeights,
      masterRatio: s.masterRatio,
      activeSessionId: s.activeSessionId,
      sessions: s.sessions,
      pendingDispatches: s.pendingDispatches,
      setOpen: s.setOpen,
      setHeight: s.setHeight,
      toggleMaximize: s.toggleMaximize,
      setDisplayMode: s.setDisplayMode,
      setSplitWorkspaceRatio: s.setSplitWorkspaceRatio,
      setGridLayout: s.setGridLayout,
      toggleZoomSession: s.toggleZoomSession,
      promoteToMaster: s.promoteToMaster,
      setMasterRatio: s.setMasterRatio,
      setColPairWeights: s.setColPairWeights,
      setRowPairWeights: s.setRowPairWeights,
      resetTilingWeights: s.resetTilingWeights,
      setActiveSession: s.setActiveSession,
      createSession: s.createSession,
      consumePendingDispatch: s.consumePendingDispatch,
      restartSession: s.restartSession,
      closeSession: s.closeSession,
      toggleBroadcastTarget: s.toggleBroadcastTarget,
      setAllBroadcastTargets: s.setAllBroadcastTargets,
      toggleAutoReflow: s.toggleAutoReflow,
      broadcastInput: s.broadcastInput,
      sendQuickResponse: s.sendQuickResponse,
      setSessionActivity: s.setSessionActivity,
    })),
  );

  const activeProject = useConsoleStore((s) =>
    s.projects.find((p) => p.project_id === s.activeProjectId),
  );
  const activeWorkspace = activeProject?.workspace_path;

  const [isDragging, setIsDragging] = useState(false);
  const [isSplitWorkspaceDragging, setIsSplitWorkspaceDragging] = useState(false);
  const [launcherOpen, setLauncherOpen] = useState(false);
  const [customCmd, setCustomCmd] = useState('');
  const [broadcastText, setBroadcastText] = useState('');
  const [handoffMenuForId, setHandoffMenuForId] = useState<string | null>(null);

  const startYRef = useRef(0);
  const startHRef = useRef(0);
  const splitStartXRef = useRef(0);
  const splitStartRatioRef = useRef(0.48);
  const launcherPopoverRef = useRef<HTMLDivElement>(null);
  const launcherButtonRef = useRef<HTMLButtonElement>(null);
  const focusLauncher = useCallback((el: HTMLDivElement | null) => {
    launcherPopoverRef.current = el;
    el?.querySelector('button')?.focus();
  }, []);
  const canvasRef = useRef<HTMLDivElement>(null);
  const panelRootRef = useRef<HTMLDivElement>(null);

  const [paneDragState, setPaneDragState] = useState<{
    startX: number;
    startY: number;
    rightDrag?: HorizontalDragSpec;
    bottomDrag?: VerticalDragSpec;
  } | null>(null);

  useEffect(() => {
    if (open && sessions.length === 0) {
      void createSession(activeWorkspace);
    }
  }, [open, sessions.length, activeWorkspace, createSession]);

  useEffect(() => {
    if (!launcherOpen && !handoffMenuForId) return;
    const onPointerDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (
        launcherOpen &&
        !launcherButtonRef.current?.contains(target) &&
        !launcherPopoverRef.current?.contains(target)
      ) {
        setLauncherOpen(false);
      }
      if (handoffMenuForId && !(target as HTMLElement).closest?.('[data-handoff-menu]')) {
        setHandoffMenuForId(null);
      }
    };
    document.addEventListener('mousedown', onPointerDown);
    return () => document.removeEventListener('mousedown', onPointerDown);
  }, [launcherOpen, handoffMenuForId]);

  // Handle top border drag-to-resize (dock mode)
  const handleMouseDown = useCallback(
    (e: React.MouseEvent) => {
      if (displayMode !== 'dock') return;
      e.preventDefault();
      setIsDragging(true);
      startYRef.current = e.clientY;
      startHRef.current = height;
    },
    [height, displayMode],
  );

  useEffect(() => {
    if (!isDragging) return;
    const onMouseMove = (e: MouseEvent) => {
      const deltaY = startYRef.current - e.clientY;
      setHeight(startHRef.current + deltaY);
    };
    const onMouseUp = () => setIsDragging(false);
    window.addEventListener('mousemove', onMouseMove);
    window.addEventListener('mouseup', onMouseUp);
    return () => {
      window.removeEventListener('mousemove', onMouseMove);
      window.removeEventListener('mouseup', onMouseUp);
    };
  }, [isDragging, setHeight]);

  // Handle left border drag-to-resize (side-by-side `split` workspace mode)
  const handleSplitWorkspaceMouseDown = useCallback(
    (e: React.MouseEvent) => {
      if (displayMode !== 'split') return;
      e.preventDefault();
      setIsSplitWorkspaceDragging(true);
      splitStartXRef.current = e.clientX;
      splitStartRatioRef.current = splitWorkspaceRatio;
    },
    [displayMode, splitWorkspaceRatio],
  );

  useEffect(() => {
    if (!isSplitWorkspaceDragging) return;
    const onMouseMove = (e: MouseEvent) => {
      const parentBox = panelRootRef.current?.parentElement?.getBoundingClientRect();
      const totalWidth = parentBox?.width || window.innerWidth;
      if (totalWidth <= 0) return;
      const deltaRatio = (e.clientX - splitStartXRef.current) / totalWidth;
      setSplitWorkspaceRatio(splitStartRatioRef.current + deltaRatio);
    };
    const onMouseUp = () => setIsSplitWorkspaceDragging(false);
    window.addEventListener('mousemove', onMouseMove);
    window.addEventListener('mouseup', onMouseUp);
    return () => {
      window.removeEventListener('mousemove', onMouseMove);
      window.removeEventListener('mouseup', onMouseUp);
    };
  }, [isSplitWorkspaceDragging, setSplitWorkspaceRatio]);

  // Interactive internal pane border drag-to-resize
  useEffect(() => {
    if (!paneDragState) return;
    const onMouseMove = (e: MouseEvent) => {
      const box = canvasRef.current?.getBoundingClientRect();
      if (!box || box.width <= 0 || box.height <= 0) return;

      const deltaXPct = ((e.clientX - paneDragState.startX) / box.width) * 100;
      const deltaYPct = ((e.clientY - paneDragState.startY) / box.height) * 100;

      if (paneDragState.rightDrag) {
        const rd = paneDragState.rightDrag;
        if (rd.kind === 'master') {
          setMasterRatio(rd.initialRatio + deltaXPct / 100);
        } else {
          const minPct = Math.min(12, rd.combinedPct * 0.25);
          const nextLeftPct = Math.max(
            minPct,
            Math.min(rd.combinedPct - minPct, rd.leftPct + deltaXPct),
          );
          const totalWeight = rd.leftWeight + rd.rightWeight;
          const nextLeftW = (nextLeftPct / rd.combinedPct) * totalWeight;
          const nextRightW = totalWeight - nextLeftW;
          setColPairWeights(rd.leftId, rd.rightId, nextLeftW, nextRightW);
        }
      }

      if (paneDragState.bottomDrag) {
        const bd = paneDragState.bottomDrag;
        const minPct = Math.min(12, bd.combinedPct * 0.25);
        const nextTopPct = Math.max(
          minPct,
          Math.min(bd.combinedPct - minPct, bd.topPct + deltaYPct),
        );
        const totalWeight = bd.topWeight + bd.bottomWeight;
        const nextTopW = (nextTopPct / bd.combinedPct) * totalWeight;
        const nextBottomW = totalWeight - nextTopW;
        setRowPairWeights(bd.topKey, bd.bottomKey, nextTopW, nextBottomW);
      }
    };

    const onMouseUp = () => setPaneDragState(null);
    window.addEventListener('mousemove', onMouseMove);
    window.addEventListener('mouseup', onMouseUp);
    return () => {
      window.removeEventListener('mousemove', onMouseMove);
      window.removeEventListener('mouseup', onMouseUp);
    };
  }, [paneDragState, setMasterRatio, setColPairWeights, setRowPairWeights]);

  const activeSession = useMemo(
    () => sessions.find((s) => s.id === activeSessionId) ?? sessions[0],
    [sessions, activeSessionId],
  );

  const handleSendToComposer = useCallback(
    (sourceSession?: TerminalTabSession) => {
      const targetSession = sourceSession ?? activeSession;
      if (displayMode === 'stage') {
        setDisplayMode('split');
      }
      const composer = document.getElementById('console-composer') as HTMLElement | null;
      if (!composer) return;

      const term = targetSession ? terminalInstances.get(targetSession.id) : null;
      const context = term ? getTerminalContext(term) : { text: '', hasSelection: false };

      let prompt = '';
      if (context.text) {
        const heading = context.hasSelection
          ? `请分析以下来自终端 [${targetSession?.title ?? 'terminal'}] 选中的内容并给出解决建议：`
          : `请分析以下来自终端 [${targetSession?.title ?? 'terminal'}] 的执行输出与状态并给出解决建议：`;
        prompt = `${heading}\n\`\`\`terminal\n${context.text}\n\`\`\`\n`;
      } else {
        prompt = '请根据当前工作区终端环境与执行输出进行分析与处理：\n';
      }

      composer.focus();
      if (composer instanceof HTMLTextAreaElement || composer instanceof HTMLInputElement) {
        const current = composer.value;
        const pad = current && !current.endsWith('\n') ? '\n\n' : '';
        composer.value = `${current}${pad}${prompt}`;
        composer.dispatchEvent(new Event('input', { bubbles: true }));
      } else {
        const currentText = composer.textContent || '';
        const pad = currentText && !currentText.endsWith('\n') ? '\n\n' : '';
        insertTextAtCaret(composer, `${pad}${prompt}`);
        composer.dispatchEvent(new Event('input', { bubbles: true }));
      }
      setHandoffMenuForId(null);
    },
    [activeSession, displayMode, setDisplayMode],
  );

  const handleForwardToPeerTerminal = useCallback(
    async (sourceSession: TerminalTabSession, targetSession: TerminalTabSession) => {
      if (!targetSession.ptyId || targetSession.status !== 'running') return;
      const sourceTerm = terminalInstances.get(sourceSession.id);
      const context = sourceTerm
        ? getTerminalContext(sourceTerm)
        : { text: '', hasSelection: false };
      if (!context.text) return;

      const wrapped = `[来自 ${sourceSession.title} 的终端上下文]:\n${context.text}`;
      setSessionActivity(targetSession.id, 'working');
      await writeTerminal(targetSession.ptyId, formatPtySubmission(wrapped));
      setActiveSession(targetSession.id);
      setHandoffMenuForId(null);
    },
    [setActiveSession, setSessionActivity],
  );

  const handleLaunchPreset = useCallback(
    (preset: { id: string; label: string; command: string; isAgent: boolean }) => {
      setLauncherOpen(false);
      void createSession(activeWorkspace, undefined, {
        agentId: preset.id,
        label: preset.label,
        command: preset.command,
        isAgent: preset.isAgent,
      });
    },
    [activeWorkspace, createSession],
  );

  const handleLaunchCustomCommand = useCallback(
    (e: React.FormEvent) => {
      e.preventDefault();
      const cmd = customCmd.trim();
      if (!cmd) return;
      setCustomCmd('');
      setLauncherOpen(false);
      const token = cmd.split(/\s+/)[0] || 'agent';
      void createSession(activeWorkspace, undefined, {
        agentId: token,
        label: cmd.length > 18 ? `${token}…` : cmd,
        command: cmd,
        isAgent: true,
      });
    },
    [customCmd, activeWorkspace, createSession],
  );

  const handleBroadcastSubmit = useCallback(
    async (e?: React.FormEvent) => {
      if (e) e.preventDefault();
      const text = broadcastText.trim();
      if (!text) return;
      const count = await broadcastInput(text);
      if (count > 0) {
        setBroadcastText('');
      }
    },
    [broadcastText, broadcastInput],
  );

  const singleFocusedId =
    zoomedSessionId ?? (gridLayout === 'monocle' ? (activeSession?.id ?? null) : null);

  const visibleSessions = useMemo(() => {
    if (singleFocusedId) {
      return sessions.filter((s) => s.id === singleFocusedId);
    }
    return sessions;
  }, [sessions, singleFocusedId]);

  const tileRectMap = useMemo(
    () =>
      computeTilingRects(
        singleFocusedId ? 'monocle' : gridLayout,
        visibleSessions,
        colWeights,
        rowWeights,
        masterRatio,
      ),
    [singleFocusedId, gridLayout, visibleSessions, colWeights, rowWeights, masterRatio],
  );

  const broadcastTargets = useMemo(
    () => sessions.filter((s) => s.broadcastEnabled && s.status === 'running'),
    [sessions],
  );

  const isStage = displayMode === 'stage';
  const isSplitWorkspace = displayMode === 'split';
  const isFullHeight = isStage || isSplitWorkspace;
  const isMultiPane = visibleSessions.length > 1;
  const showHerdBar = isFullHeight || sessions.length > 1;

  const panelHeight = isFullHeight
    ? '100%'
    : isMaximized
      ? 'calc(100% - 42px)'
      : `${height}px`;

  return (
    <div
      ref={panelRootRef}
      inert={!open}
      style={{
        height: open ? panelHeight : '0px',
        width: open && isSplitWorkspace ? `${(1 - splitWorkspaceRatio) * 100}%` : undefined,
      }}
      className={`material-pane terminal-pane relative shrink-0 flex flex-col overflow-hidden select-none shadow-card ${
        isSplitWorkspace
          ? 'border-l border-line h-full'
          : isStage
            ? 'w-full flex-1 border-t border-line'
            : 'w-full border-t border-line'
      } ${open ? 'opacity-100' : 'opacity-0 pointer-events-none border-0'} ${
        isDragging || isSplitWorkspaceDragging || paneDragState
          ? ''
          : 'transition-[height] duration-250 ease-[cubic-bezier(0,0,0,1)]'
      }`}
    >
      {/* Top Draggable Resizer Bar (Dock mode only) */}
      {displayMode === 'dock' && (
        <div
          onMouseDown={handleMouseDown}
          onDoubleClick={() => setHeight(300)}
          title="按住上下拖拽调节终端高度 (双击复位)"
          className={`group absolute top-0 left-0 right-0 z-50 h-2 cursor-row-resize transition-colors ${
            isDragging ? 'bg-accent/80' : 'bg-transparent hover:bg-accent/30'
          }`}
        />
      )}

      {/* Left Draggable Resizer Bar (Side-by-Side Split Workspace mode only) */}
      {isSplitWorkspace && (
        <div
          onMouseDown={handleSplitWorkspaceMouseDown}
          onDoubleClick={() => setSplitWorkspaceRatio(0.48)}
          title="左右拖拽调整会话区与终端画布比例 (双击恢复均分)"
          className={`group absolute top-0 bottom-0 left-0 z-50 w-2 cursor-col-resize transition-colors ${
            isSplitWorkspaceDragging ? 'bg-accent/80' : 'bg-transparent hover:bg-accent/40'
          }`}
        />
      )}

      {/* Top Header Bar: Pill Tabs + Agent CLI Launcher + Tiling & Workspace Mode Switchers */}
      <div
        onDoubleClick={(e) => {
          if ((e.target as HTMLElement).closest('button, input, [data-no-maximize]')) return;
          toggleMaximize();
        }}
        className="flex h-9 items-center justify-between border-b border-line/70 bg-surface/40 px-2.5 select-none shrink-0 gap-2"
      >
        {/* Left: Pill Session Tabs + Agent Launcher */}
        <div className="flex items-center gap-1.5 overflow-x-auto fluent-scrollbar min-w-0 pr-1">
          {sessions.map((session) => {
            const isActive = session.id === activeSession?.id;
            const isVisibleInTile = tileRectMap.has(session.id);
            const isWaiting = session.activity === 'waiting_input';
            return (
              <div
                key={session.id}
                onClick={() => setActiveSession(session.id)}
                onDoubleClick={(e) => {
                  e.stopPropagation();
                  toggleZoomSession(session.id);
                }}
                title={`${session.title} — ${activityLabel(session.activity)}${
                  session.activityDetail ? `: ${session.activityDetail}` : ''
                } (双击单独放大/还原)`}
                className={`group flex h-[26px] items-center gap-1.5 rounded-full px-2.5 text-xs font-medium transition-colors cursor-pointer shrink-0 ${
                  isWaiting
                    ? 'bg-amber-500/15 text-amber-600'
                    : isActive
                      ? 'bg-surface/90 text-gray-900'
                      : isVisibleInTile
                        ? 'bg-surface/45 text-gray-700 hover:bg-surface-hover/70'
                        : 'text-gray-500 hover:text-gray-900 hover:bg-surface-hover/60'
                }`}
              >
                <span
                  className={`inline-block h-2 w-2 rounded-full shrink-0 ${activityDotClass(
                    session.activity,
                  )}`}
                />
                <WindowConsole20Regular
                  className={`h-3.5 w-3.5 shrink-0 ${
                    isWaiting
                      ? 'text-amber-500'
                      : isActive
                        ? 'text-accent-fg'
                        : 'text-gray-400'
                  }`}
                />
                <span className="truncate max-w-[130px] leading-none">{session.title}</span>
                {isWaiting && (
                  <span className="rounded-full bg-amber-500/20 px-1.5 py-0.5 text-[10px] leading-none text-amber-600">
                    待确认
                  </span>
                )}
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    void closeSession(session.id);
                  }}
                  title="关闭该终端"
                  className={`flex h-4 w-4 shrink-0 items-center justify-center rounded-full text-gray-400 transition-all hover:bg-red-500/20 hover:text-red-500 ${
                    isActive || isWaiting
                      ? 'opacity-70 hover:opacity-100'
                      : 'opacity-40 group-hover:opacity-80 hover:!opacity-100'
                  }`}
                >
                  <Dismiss16Regular className="h-3 w-3" />
                </button>
              </div>
            );
          })}

          <div data-no-maximize className="relative flex items-center shrink-0">
            <button
              ref={launcherButtonRef}
              type="button"
              onClick={() => setLauncherOpen((prev) => !prev)}
              aria-label="新建终端"
              aria-expanded={launcherOpen}
              aria-controls={launcherOpen ? 'terminal-launcher-options' : undefined}
              title="选择要启动的本地 Agent CLI 或 Shell"
              className="flex h-[26px] items-center gap-1 rounded-full bg-surface/60 px-2.5 text-[11px] font-medium text-gray-700 hover:bg-surface-hover hover:text-gray-900"
            >
              <Add16Regular className="h-3.5 w-3.5 text-accent-fg" />
              <span className="hidden sm:inline">新建终端</span>
              <ChevronDown16Regular className="h-3 w-3" />
            </button>
          </div>
        </div>

        {/* Right: Tiling Layout Switcher + 3-Mode Workspace Switcher + Actions */}
        <div className="flex items-center gap-1 shrink-0">
          {/* Tiling Layout Mode Switcher */}
          <div
            data-no-maximize
            className="hidden sm:flex items-center gap-0.5 rounded-full bg-surface/60 p-0.5"
          >
            {(
              [
                {
                  id: 'auto',
                  label: '智能网格平铺 (自动适配所有已开终端并补位)',
                  Icon: Grid16Regular,
                },
                {
                  id: 'master-stack',
                  label: '左主右从平铺 (左侧主 Agent + 右侧垂直堆叠)',
                  Icon: PanelLeft16Regular,
                },
                {
                  id: 'columns',
                  label: '左右并排平铺 (所有终端水平排列)',
                  Icon: SplitVertical16Regular,
                },
                {
                  id: 'rows',
                  label: '上下堆叠平铺 (所有终端垂直排列)',
                  Icon: SplitHorizontal16Regular,
                },
                {
                  id: 'monocle',
                  label: '单窗聚焦模式 (仅放大当前激活终端)',
                  Icon: Square16Regular,
                },
              ] as const
            ).map(({ id, label, Icon }) => {
              const selected = gridLayout === id && !zoomedSessionId;
              return (
                <button
                  key={id}
                  type="button"
                  onClick={() => setGridLayout(id as TerminalGridLayout)}
                  title={label}
                  className={`flex h-6 w-6 items-center justify-center rounded-full transition-colors ${
                    selected
                      ? 'bg-accent text-on-accent'
                      : 'text-gray-500 hover:text-gray-900 hover:bg-surface-hover'
                  }`}
                >
                  <Icon className="h-3.5 w-3.5" />
                </button>
              );
            })}
          </div>

          {/* 3-State Workspace View Mode Switcher: [底部面板] [会话+终端联屏] [全屏画布] */}
          <div
            data-no-maximize
            className="flex items-center gap-0.5 rounded-full bg-surface/60 p-0.5"
          >
            {(
              [
                {
                  mode: 'dock',
                  label: '底部面板',
                  title: '底部抽屉模式（上对话 + 下终端）',
                  Icon: PanelBottom20Regular,
                },
                {
                  mode: 'split',
                  label: '左右联屏',
                  title: '会话 + 终端左右并排联屏（同屏联动编排）',
                  Icon: SplitVertical16Regular,
                },
                {
                  mode: 'stage',
                  label: '全屏画布',
                  title: '全屏多 Agent 平铺画布 (Center Stage)',
                  Icon: Chat16Regular,
                },
              ] as const
            ).map(({ mode, label, title, Icon }) => {
              const active = displayMode === mode;
              return (
                <button
                  key={mode}
                  type="button"
                  onClick={() => setDisplayMode(mode as TerminalDisplayMode)}
                  title={title}
                  className={`flex h-6 items-center gap-1 rounded-full px-2 text-[11px] font-medium transition-colors ${
                    active
                      ? 'bg-accent text-on-accent'
                      : 'text-gray-600 hover:text-gray-900 hover:bg-surface-hover'
                  }`}
                >
                  <Icon className="h-3.5 w-3.5" />
                  <span className="hidden lg:inline">{label}</span>
                </button>
              );
            })}
          </div>

          {/* Send to Composer / Agent */}
          <button
            type="button"
            onClick={() => handleSendToComposer()}
            title="将当前终端诊断发送给主会话 Agent"
            className="flex h-[26px] items-center gap-1 rounded-full px-2 text-[11px] text-gray-500 hover:text-gray-900 hover:bg-surface-hover transition-colors"
          >
            <Sparkle16Regular className="h-3.5 w-3.5 text-purple-600" />
            <span className="hidden xl:inline">向 Agent 提问</span>
          </button>

          {/* Maximize / Restore (Dock mode) */}
          {displayMode === 'dock' && (
            <button
              type="button"
              onClick={toggleMaximize}
              title={
                isMaximized
                  ? `还原面板尺寸 (${TERMINAL_MAXIMIZE_CHORD})`
                  : `最大化终端面板 (${TERMINAL_MAXIMIZE_CHORD})`
              }
              className={`ui-icon-button ui-compact text-gray-500 hover:text-gray-900 ${
                isMaximized ? 'text-accent-fg' : ''
              }`}
            >
              <SquareMultiple16Regular className="h-3.5 w-3.5" />
            </button>
          )}

          {/* Close Panel */}
          <button
            type="button"
            onClick={() => setOpen(false)}
            title="收起终端面板 (Ctrl+`)"
            className="ui-icon-button ui-compact text-gray-400 hover:text-gray-800"
          >
            <Dismiss16Regular />
          </button>
        </div>
      </div>

      {/* Agent CLI Launcher Dropdown Popover */}
      {launcherOpen && (
        <div
          id="terminal-launcher-options"
          ref={focusLauncher}
          data-no-maximize
          onKeyDown={(e) => {
            if (e.key !== 'Escape' || e.nativeEvent.isComposing) return;
            e.stopPropagation();
            setLauncherOpen(false);
            launcherButtonRef.current?.focus();
          }}
          className="flyout-in absolute top-10 left-3 z-50 max-h-[calc(100%_-_2.75rem)] w-72 max-w-[calc(100%_-_1.5rem)] overflow-y-auto rounded-control border border-line bg-surface p-2 shadow-card"
        >
          <div className="px-2 py-1 text-[11px] font-semibold text-gray-500">
            点击启动本地 CLI 或 Shell
          </div>
          <div className="flex flex-col gap-0.5">
            {AGENT_CLI_PRESETS.map((preset) => (
              <button
                key={preset.id}
                type="button"
                onClick={() => handleLaunchPreset(preset)}
                className="flex items-center justify-between rounded-control px-2.5 py-1.5 text-left text-xs hover:bg-surface-hover transition-colors"
              >
                <div className="min-w-0">
                  <div className="font-medium text-gray-900 flex items-center gap-1.5">
                    <span>{preset.label}</span>
                    {preset.command && (
                      <span className="rounded bg-sunken px-1 py-0.5 font-mono text-[10px] text-gray-500">
                        {preset.command}
                      </span>
                    )}
                  </div>
                  <div className="truncate text-[11px] text-gray-500">{preset.description}</div>
                </div>
                <Add16Regular className="h-3.5 w-3.5 shrink-0 text-accent-fg" />
              </button>
            ))}
          </div>
          <form
            onSubmit={handleLaunchCustomCommand}
            className="mt-2 flex items-center gap-1 border-t border-line/60 pt-2"
          >
            <input
              type="text"
              aria-label="自定义本地启动命令"
              value={customCmd}
              onChange={(e) => setCustomCmd(e.target.value)}
              placeholder="自定义本地命令（可带参数）"
              className="min-w-0 flex-1 rounded-control border border-line bg-sunken px-2 py-1 text-xs text-gray-900 placeholder:text-gray-400"
            />
            <button
              type="submit"
              disabled={!customCmd.trim()}
              className="rounded-control bg-accent px-2 py-1 text-xs font-medium text-on-accent disabled:opacity-50"
            >
              运行命令
            </button>
          </form>
        </div>
      )}

      {/* Tiling Window Manager Body: Single flat DOM container keeps all Xterm instances alive */}
      <div
        ref={canvasRef}
        className="flex-1 min-h-0 w-full overflow-hidden relative"
      >
        {sessions.length > 0 ? (
          sessions.map((session, idx) => {
            const isActive = session.id === activeSession?.id;
            const rect = tileRectMap.get(session.id);
            const isVisible = rect !== undefined;
            const isWaiting = session.activity === 'waiting_input';
            const isZoomed = zoomedSessionId === session.id;
            const peerSessions = sessions.filter(
              (s) => s.id !== session.id && s.status === 'running',
            );

            const padPx = isMultiPane || isFullHeight ? 3 : 0;
            const tileStyle: React.CSSProperties | undefined = rect
              ? {
                  position: 'absolute',
                  left: `calc(${rect.leftPct}% + ${padPx}px)`,
                  top: `calc(${rect.topPct}% + ${padPx}px)`,
                  width: `calc(${rect.widthPct}% - ${padPx * 2}px)`,
                  height: `calc(${rect.heightPct}% - ${padPx * 2}px)`,
                }
              : undefined;

            return (
              <div
                key={session.id}
                style={tileStyle}
                onMouseDown={() => {
                  if (!isActive) setActiveSession(session.id);
                }}
                className={
                  isVisible
                    ? `flex min-h-0 min-w-0 flex-col overflow-hidden rounded-control border transition-colors ${
                        isWaiting
                          ? 'border-amber-500/80'
                          : isActive
                            ? 'border-accent-fg/70'
                            : 'border-line/70'
                      }`
                    : 'hidden'
                }
              >
                {/* Per-Agent Pane Title Bar */}
                <div
                  onDoubleClick={(e) => {
                    if ((e.target as HTMLElement).closest('button, input')) return;
                    toggleZoomSession(session.id);
                  }}
                  title="双击标题栏可临时放大聚焦该终端 / 再次双击还原平铺"
                  className="flex h-7 items-center justify-between border-b border-line/60 bg-surface/60 px-2.5 text-[11px] shrink-0 gap-2"
                >
                  <div className="flex items-center gap-1.5 min-w-0">
                    <span
                      className={`inline-block h-2 w-2 rounded-full shrink-0 ${activityDotClass(
                        session.activity,
                      )}`}
                    />
                    <TerminalPaneTitle session={session} />
                    {gridLayout === 'master-stack' && idx === 0 && isMultiPane && (
                      <span className="rounded bg-accent/15 px-1.5 py-0.5 text-[10px] font-medium text-accent-fg shrink-0">
                        主窗
                      </span>
                    )}
                    {session.workspace && (
                      <span className="hidden xl:inline truncate text-gray-400 max-w-[160px]">
                        — {session.workspace}
                      </span>
                    )}
                    {isWaiting && (
                      <span className="rounded-full bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-medium text-amber-600 shrink-0">
                        等待确认
                      </span>
                    )}
                    {pendingDispatches[session.id] && (
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          void consumePendingDispatch(session.id);
                        }}
                        title="正在等待终端输入区就绪后自动注入指令（点击可立即强制注入）"
                        className="flex items-center gap-1 rounded-full bg-accent/15 px-2 py-0.5 text-[10px] font-medium text-accent-fg animate-pulse shrink-0"
                      >
                        <span>待自动注入指令 (点击立即注入 ⏎)</span>
                      </button>
                    )}
                  </div>

                  {/* Right Controls on Every Agent Pane Title Bar */}
                  <div className="flex items-center gap-1 shrink-0">
                    {/* Promote to Master in Master-Stack layout */}
                    {gridLayout === 'master-stack' && idx !== 0 && isMultiPane && (
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          promoteToMaster(session.id);
                        }}
                        title="将该 Agent 移至左侧主窗"
                        className="rounded-full px-2 py-0.5 text-[10px] text-gray-500 hover:text-gray-900 hover:bg-surface-hover transition-colors"
                      >
                        设为主窗
                      </button>
                    )}

                    {/* Auto-Reflow to Chat Watch Toggle */}
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        toggleAutoReflow(session.id);
                      }}
                      title="开启后，当该终端完成输出或需要确认时自动在主会话提醒回流"
                      className={`flex items-center gap-0.5 rounded-full px-2 py-0.5 text-[10px] font-medium transition-colors ${
                        session.autoReflow
                          ? 'bg-accent/15 text-accent-fg'
                          : 'bg-surface text-gray-400 hover:text-gray-700'
                      }`}
                    >
                      <Alert16Regular className="h-3 w-3" />
                      <span className="hidden sm:inline">
                        {session.autoReflow ? '完成后回流 ☑' : '回流 ☐'}
                      </span>
                    </button>

                    {/* Broadcast Participation Checkbox Chip */}
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        toggleBroadcastTarget(session.id);
                      }}
                      title="切换是否接收底部 Herd 广播群发指令"
                      className={`flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium transition-colors ${
                        session.broadcastEnabled
                          ? 'bg-accent/15 text-accent-fg'
                          : 'bg-surface text-gray-400 hover:text-gray-700'
                      }`}
                    >
                      <span>{session.broadcastEnabled ? '广播 ☑' : '广播 ☐'}</span>
                    </button>

                    {/* Cross-Terminal Context Handoff Menu Trigger */}
                    <div data-handoff-menu className="relative">
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          setHandoffMenuForId((prev) =>
                            prev === session.id ? null : session.id,
                          );
                        }}
                        title="抓取当前终端选中内容或最近输出，转发给其他 Agent 或主会话"
                        className="rounded-full px-2 py-0.5 text-[10px] text-gray-500 hover:text-gray-900 hover:bg-surface-hover transition-colors"
                      >
                        抓取输出 → 转发
                      </button>
                      {handoffMenuForId === session.id && (
                        <div className="material-flyout flyout-in absolute right-0 top-6 z-50 w-56 rounded-control border border-line bg-surface p-1.5 shadow-card">
                          <button
                            type="button"
                            onClick={() => handleSendToComposer(session)}
                            className="flex w-full items-center gap-1.5 rounded-control px-2 py-1.5 text-left text-xs text-gray-800 hover:bg-surface-hover"
                          >
                            <Sparkle16Regular className="h-3.5 w-3.5 text-purple-600 shrink-0" />
                            <span className="truncate">转发至主对话输入框</span>
                          </button>
                          {peerSessions.map((peer) => (
                            <button
                              key={peer.id}
                              type="button"
                              onClick={() => void handleForwardToPeerTerminal(session, peer)}
                              className="flex w-full items-center gap-1.5 rounded-control px-2 py-1.5 text-left text-xs text-gray-800 hover:bg-surface-hover"
                            >
                              <WindowConsole20Regular className="h-3.5 w-3.5 text-accent-fg shrink-0" />
                              <span className="truncate">投递给 {peer.title}</span>
                            </button>
                          ))}
                        </div>
                      )}
                    </div>

                    {/* Restart button */}
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        void restartSession(session.id);
                      }}
                      title="重新启动该 Agent 终端进程"
                      className="flex h-5 w-5 items-center justify-center rounded-full text-gray-400 hover:bg-surface-hover hover:text-accent-fg transition-colors"
                    >
                      <ArrowClockwise16Regular className="h-3 w-3" />
                    </button>

                    {/* Temporary Zoom / Restore Single Pane */}
                    {sessions.length > 1 && (
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          toggleZoomSession(session.id);
                        }}
                        title={isZoomed ? '还原多窗平铺布局' : '临时全屏聚焦该 Agent 分屏'}
                        className={`flex h-5 w-5 items-center justify-center rounded-full transition-colors ${
                          isZoomed
                            ? 'bg-accent text-on-accent'
                            : 'text-gray-400 hover:bg-surface-hover hover:text-gray-900'
                        }`}
                      >
                        {isZoomed ? (
                          <ArrowMinimize16Regular className="h-3 w-3" />
                        ) : (
                          <ArrowExpand16Regular className="h-3 w-3" />
                        )}
                      </button>
                    )}

                    {/* Direct Close Button on Agent Pane Header */}
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        void closeSession(session.id);
                      }}
                      title="关闭该 Agent 终端（其余分屏自动补位）"
                      className="flex h-5 w-5 items-center justify-center rounded-full text-gray-400 hover:bg-red-500/20 hover:text-red-500 transition-colors"
                    >
                      <Dismiss16Regular className="h-3.5 w-3.5" />
                    </button>
                  </div>
                </div>

                {/* Xterm Viewport */}
                <div className="relative flex-1 min-h-0 w-full overflow-hidden">
                  <XtermView session={session} isActive={isActive && isVisible} />

                  {/* Floating Quick Approval Chips when Agent is waiting for confirmation */}
                  {isWaiting && (
                    <div className="absolute right-3 bottom-2.5 z-30 flex items-center gap-1.5 rounded-full border border-amber-500/40 bg-surface/95 px-2.5 py-1 shadow-card">
                      <span className="text-[11px] font-medium text-amber-600">等待确认:</span>
                      <button
                        type="button"
                        onClick={() => void sendQuickResponse(session.id, 'y\r')}
                        className="flex items-center gap-0.5 rounded-full bg-emerald-500/15 px-2 py-0.5 text-[11px] font-medium text-emerald-600 hover:bg-emerald-500/25 transition-colors"
                      >
                        <Checkmark16Regular className="h-3 w-3" />
                        <span>一键批准 (y)</span>
                      </button>
                      <button
                        type="button"
                        onClick={() => void sendQuickResponse(session.id, 'n\r')}
                        className="flex items-center gap-0.5 rounded-full bg-red-500/15 px-2 py-0.5 text-[11px] font-medium text-red-500 hover:bg-red-500/25 transition-colors"
                      >
                        <Dismiss16Regular className="h-3 w-3" />
                        <span>拒绝 (n)</span>
                      </button>
                      <button
                        type="button"
                        onClick={() => void sendQuickResponse(session.id, '\r')}
                        className="rounded-full bg-surface-hover px-2 py-0.5 text-[11px] text-gray-700 hover:text-gray-900"
                      >
                        回车 ⏎
                      </button>
                    </div>
                  )}
                </div>

                {/* Interactive Right-Edge Split Resizer Handle (↔) */}
                {rect?.rightDrag && (
                  <div
                    onMouseDown={(e) => {
                      e.preventDefault();
                      e.stopPropagation();
                      setPaneDragState({
                        startX: e.clientX,
                        startY: e.clientY,
                        rightDrag: rect.rightDrag,
                      });
                    }}
                    onDoubleClick={(e) => {
                      e.stopPropagation();
                      resetTilingWeights();
                    }}
                    title="左右拖拽调整相邻 Agent 窗口宽度 (双击恢复均分)"
                    className="absolute top-0 right-0 bottom-0 z-40 w-2 cursor-col-resize bg-transparent hover:bg-accent/50 active:bg-accent transition-colors"
                  />
                )}

                {/* Interactive Bottom-Edge Split Resizer Handle (↕) */}
                {rect?.bottomDrag && (
                  <div
                    onMouseDown={(e) => {
                      e.preventDefault();
                      e.stopPropagation();
                      setPaneDragState({
                        startX: e.clientX,
                        startY: e.clientY,
                        bottomDrag: rect.bottomDrag,
                      });
                    }}
                    onDoubleClick={(e) => {
                      e.stopPropagation();
                      resetTilingWeights();
                    }}
                    title="上下拖拽调整相邻 Agent 窗口高度 (双击恢复均分)"
                    className="absolute left-0 right-0 bottom-0 z-40 h-2 cursor-row-resize bg-transparent hover:bg-accent/50 active:bg-accent transition-colors"
                  />
                )}

                {/* Interactive Bottom-Right Corner Resizer Handle (↘) */}
                {rect?.rightDrag && rect?.bottomDrag && (
                  <div
                    onMouseDown={(e) => {
                      e.preventDefault();
                      e.stopPropagation();
                      setPaneDragState({
                        startX: e.clientX,
                        startY: e.clientY,
                        rightDrag: rect.rightDrag,
                        bottomDrag: rect.bottomDrag,
                      });
                    }}
                    onDoubleClick={(e) => {
                      e.stopPropagation();
                      resetTilingWeights();
                    }}
                    title="拖拽同时调整当前 Agent 窗口宽高 (双击恢复均分)"
                    className="absolute right-0 bottom-0 z-50 h-3.5 w-3.5 cursor-nwse-resize rounded-tl bg-transparent hover:bg-accent/70 active:bg-accent transition-colors"
                  />
                )}
              </div>
            );
          })
        ) : (
          <div className="flex-1 flex items-center justify-center text-xs text-gray-400">
            暂无活动终端会话，点击上方 “+ 启动 Agent 本体” 新建
          </div>
        )}
      </div>

      {/* Bottom Herd Unified Broadcast & Command Bar */}
      {showHerdBar && sessions.length > 0 && (
        <form
          onSubmit={(e) => void handleBroadcastSubmit(e)}
          className="flex flex-wrap items-center gap-2 border-t border-line/70 bg-surface/50 px-3 py-1.5 shrink-0"
        >
          {/* Target Quick Pills */}
          <div className="flex items-center gap-1 text-[11px] shrink-0 overflow-x-auto fluent-scrollbar">
            <span className="rounded-full bg-accent/15 px-2 py-0.5 font-medium text-accent-fg">
              群发目标: 已选 {broadcastTargets.length} 个
            </span>
            {sessions.map((s) => (
              <button
                key={s.id}
                type="button"
                onClick={() => toggleBroadcastTarget(s.id)}
                title={`点击切换 ${s.title} 是否参与群发（也可在输入框用 @${s.seq} 或 @${s.agentId} 定向发送）`}
                className={`rounded-full px-2 py-0.5 text-[11px] transition-colors ${
                  s.broadcastEnabled
                    ? 'bg-surface text-gray-900 font-medium'
                    : 'text-gray-400 hover:text-gray-700'
                }`}
              >
                @{s.title}
              </button>
            ))}
            <button
              type="button"
              onClick={() =>
                setAllBroadcastTargets(broadcastTargets.length < sessions.length)
              }
              className="rounded-full px-1.5 py-0.5 text-[10px] text-gray-500 hover:text-gray-900 hover:bg-surface-hover"
            >
              {broadcastTargets.length < sessions.length ? '全选' : '取消全选'}
            </button>
          </div>

          {/* Unified Broadcast / @agent Prompt Input */}
          <div className="flex min-w-[220px] flex-1 items-center gap-1.5">
            <input
              type="text"
              value={broadcastText}
              onChange={(e) => setBroadcastText(e.target.value)}
              placeholder="向已选 Agent 终端广播指令，或输入 @1 / @agy / @claude 定向投递 Prompt..."
              className="min-w-0 flex-1 rounded-full border border-line/70 bg-surface px-3 py-1 text-xs text-gray-900 placeholder:text-gray-400"
            />
            <button
              type="submit"
              disabled={!broadcastText.trim()}
              className="flex h-7 items-center gap-1 rounded-full bg-accent px-3 text-xs font-medium text-on-accent disabled:opacity-40 transition-opacity shrink-0"
            >
              <Send16Regular className="h-3.5 w-3.5" />
              <span>发送 / 广播</span>
            </button>
          </div>
        </form>
      )}
    </div>
  );
};
