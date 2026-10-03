/**
 * Quick Switcher & Activity Dashboard (Fluent Design 2)
 *
 * A keyboard-first overlay for quickly searching, locating, and switching sessions,
 * while inspecting live running activities and summaries across all projects.
 *
 * Key bindings:
 *  - `Ctrl+P` / `Ctrl+K`: Toggle Quick Switcher
 *  - `ArrowDown` / `ArrowUp`: Navigate list
 *  - `Enter`: Switch to selected session and close
 *  - `Y` / `N`: In-situ approval / rejection for pending security gates
 *  - `Escape`: Close switcher
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Search20Regular,
  Dismiss20Regular,
  Play20Regular,
  Warning20Regular,
  Checkmark20Regular,
  Folder20Regular,
  Chat20Regular,
  Wrench20Regular,
  ArrowRight16Regular,
} from '@fluentui/react-icons';
import { Portal } from './Portal.tsx';
import { useConsoleStore } from '../stores/useConsoleStore.ts';
import { projectLabel } from '../stores/sessionList.ts';
import { sessionKey } from '../stores/sessionViews.ts';
import type { SessionItem, TranscriptMessage } from '../stores/historyMapper.ts';
import type { ActivityView, PendingApproval } from '../stores/liveEventReducer.ts';

export interface QuickSwitcherProps {
  isOpen: boolean;
  onClose: () => void;
}

export interface SwitcherSessionItem {
  thread_id: string;
  project_id: string;
  title: string;
  updated_at: string;
  status: 'running' | 'approval' | 'idle';
  activity: ActivityView | null;
  pendingApproval: PendingApproval | null;
  lastSummary: string;
  isCurrent: boolean;
}

/**
 * Strips raw markdown tokens (# headers, ** bold, code fences) so the executive summary
 * reads cleanly in the detail card rather than looking like an unformatted debug dump.
 */
function cleanSummaryText(raw: string): string {
  if (!raw) return '';
  return raw
    .replace(/```[\s\S]*?```/g, '[代码块]')
    .replace(/^#+\s+/gm, '')
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/\*([^*]+)\*/g, '$1')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/^>\s+/gm, '')
    .replace(/^[-*+]\s+/gm, '• ')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

export const QuickSwitcher: React.FC<QuickSwitcherProps> = ({ isOpen, onClose }) => {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listContainerRef = useRef<HTMLDivElement | null>(null);
  const [query, setQuery] = useState('');
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [onlyCurrentProject, setOnlyCurrentProject] = useState(false);

  // Store bindings
  const currentSession = useConsoleStore((s) => s.currentSession);
  const currentSessionTitle = useConsoleStore((s) => s.sessionTitle);
  const currentRuntimeStatus = useConsoleStore((s) => s.runtimeStatus);
  const currentPendingApproval = useConsoleStore((s) => s.pendingApproval);
  const currentActivity = useConsoleStore((s) => s.activity);
  const currentMessages = useConsoleStore((s) => s.messages);
  const sessions = useConsoleStore((s) => s.sessions);
  const projectSessions = useConsoleStore((s) => s.projectSessions);
  const projects = useConsoleStore((s) => s.projects);
  const backgroundViews = useConsoleStore((s) => s.backgroundViews);
  const switchSession = useConsoleStore((s) => s.switchSession);
  const switchProject = useConsoleStore((s) => s.switchProject);
  const toggleProjectExpanded = useConsoleStore((s) => s.toggleProjectExpanded);
  const resolveApproval = useConsoleStore((s) => s.resolveApproval);

  // Safe helper to resolve project display name
  const resolveProjectLabel = useCallback(
    (projId: string) => {
      const p = projects.find((entry) => entry.project_id === projId);
      return p ? projectLabel(p) : projId;
    },
    [projects],
  );

  // Helper to extract a summary from messages
  const extractSummary = useCallback((messages: TranscriptMessage[]): string => {
    if (!messages || messages.length === 0) return '';
    for (let i = messages.length - 1; i >= 0; i--) {
      const msg = messages[i];
      if (msg.type === 'assistant' && typeof msg.content === 'string' && msg.content.trim()) {
        const cleaned = cleanSummaryText(msg.content);
        return cleaned.length > 280 ? cleaned.slice(0, 280) + '...' : cleaned;
      }
    }
    return '';
  }, []);

  // Preload sessions for registered projects when switcher opens so search is instantly comprehensive
  useEffect(() => {
    if (isOpen) {
      projects.forEach((p) => {
        if (p.project_id !== currentSession.project_id && !projectSessions[p.project_id]) {
          void toggleProjectExpanded(p.project_id);
        }
      });
    }
  }, [isOpen, projects, currentSession.project_id, projectSessions, toggleProjectExpanded]);

  // Aggregate all sessions across active project and cached background projects
  const allItems = useMemo<SwitcherSessionItem[]>(() => {
    const map = new Map<string, SwitcherSessionItem>();

    // 1. Current active session
    if (currentSession.thread_id) {
      const key = sessionKey(currentSession);
      const status =
        currentPendingApproval !== null
          ? 'approval'
          : currentRuntimeStatus === 'running'
            ? 'running'
            : 'idle';

      map.set(key, {
        thread_id: currentSession.thread_id,
        project_id: currentSession.project_id,
        title: currentSessionTitle || '当前会话',
        updated_at: new Date().toISOString(),
        status,
        activity: currentActivity,
        pendingApproval: currentPendingApproval,
        lastSummary: extractSummary(currentMessages),
        isCurrent: true,
      });
    }

    // 2. Active project sessions
    sessions.forEach((s: SessionItem) => {
      const key = sessionKey({ project_id: currentSession.project_id, thread_id: s.thread_id });
      if (!map.has(key)) {
        const bgView = backgroundViews[key];
        const status =
          bgView?.pendingApproval != null
            ? 'approval'
            : bgView?.runtimeStatus === 'running'
              ? 'running'
              : 'idle';

        map.set(key, {
          thread_id: s.thread_id,
          project_id: currentSession.project_id,
          title: s.title || '无标题会话',
          updated_at: s.updated_at,
          status,
          activity: bgView?.activity ?? null,
          pendingApproval: bgView?.pendingApproval ?? null,
          lastSummary: bgView ? extractSummary(bgView.messages) : '',
          isCurrent: s.thread_id === currentSession.thread_id,
        });
      }
    });

    // 3. Other cached projects sessions
    Object.entries(projectSessions).forEach(([projId, list]) => {
      list.forEach((s: SessionItem) => {
        const key = sessionKey({ project_id: projId, thread_id: s.thread_id });
        if (!map.has(key)) {
          const bgView = backgroundViews[key];
          const status =
            bgView?.pendingApproval != null
              ? 'approval'
              : bgView?.runtimeStatus === 'running'
                ? 'running'
                : 'idle';

          map.set(key, {
            thread_id: s.thread_id,
            project_id: projId,
            title: s.title || '无标题会话',
            updated_at: s.updated_at,
            status,
            activity: bgView?.activity ?? null,
            pendingApproval: bgView?.pendingApproval ?? null,
            lastSummary: bgView ? extractSummary(bgView.messages) : '',
            isCurrent: false,
          });
        }
      });
    });

    const list = Array.from(map.values());

    // Sort: running & approval first, then active session, then recent updated_at
    return list.sort((a, b) => {
      const score = (item: SwitcherSessionItem) => {
        if (item.status === 'approval') return 4;
        if (item.status === 'running') return 3;
        if (item.isCurrent) return 2;
        return 1;
      };
      const diff = score(b) - score(a);
      if (diff !== 0) return diff;
      return new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime();
    });
  }, [
    currentSession,
    currentSessionTitle,
    currentRuntimeStatus,
    currentPendingApproval,
    currentActivity,
    currentMessages,
    sessions,
    projectSessions,
    backgroundViews,
    extractSummary,
  ]);

  // Filter by project scope toggle
  const scopedItems = useMemo(() => {
    if (!onlyCurrentProject) return allItems;
    return allItems.filter((item) => item.project_id === currentSession.project_id);
  }, [allItems, onlyCurrentProject, currentSession.project_id]);

  // Filter items by search query, with strict default capping to prevent clutter
  const filteredItems = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) {
      const approval = scopedItems.filter((i) => i.status === 'approval');
      const running = scopedItems.filter((i) => i.status === 'running');
      const currentProjRecent = scopedItems.filter((i) => i.status === 'idle' && i.project_id === currentSession.project_id).slice(0, 5);
      const otherProjRecent = scopedItems.filter((i) => i.status === 'idle' && i.project_id !== currentSession.project_id).slice(0, 3);
      return [...approval, ...running, ...currentProjRecent, ...otherProjRecent];
    }
    return scopedItems.filter((item) => {
      const pLabel = resolveProjectLabel(item.project_id).toLowerCase();
      const titleMatch = item.title.toLowerCase().includes(q);
      const projectMatch = pLabel.includes(q) || item.project_id.toLowerCase().includes(q);
      const activityMatch = item.activity?.detail?.toLowerCase().includes(q) ?? false;
      const summaryMatch = item.lastSummary.toLowerCase().includes(q);
      return titleMatch || projectMatch || activityMatch || summaryMatch;
    }).slice(0, 12);
  }, [scopedItems, query, currentSession.project_id, resolveProjectLabel]);

  // Categorize filtered items into clear visual sections
  const categorizedGroups = useMemo(() => {
    const q = query.trim();
    if (q) {
      return [{ key: 'search-results', label: `搜索匹配结果 (${filteredItems.length})`, count: filteredItems.length, items: filteredItems }];
    }

    const activeTasks = filteredItems.filter((i) => i.status === 'running' || i.status === 'approval');
    const currentProjectSessions = filteredItems.filter((i) => i.status === 'idle' && i.project_id === currentSession.project_id);
    const otherProjectSessions = filteredItems.filter((i) => i.status === 'idle' && i.project_id !== currentSession.project_id);

    const groups: Array<{ key: string; label: string; count: number; items: SwitcherSessionItem[] }> = [];
    if (activeTasks.length > 0) {
      groups.push({ key: 'active', label: '进行中的任务', count: activeTasks.length, items: activeTasks });
    }
    if (currentProjectSessions.length > 0) {
      const currentLabel = resolveProjectLabel(currentSession.project_id);
      groups.push({ key: 'current-project', label: `当前项目 (${currentLabel})`, count: currentProjectSessions.length, items: currentProjectSessions });
    }
    if (otherProjectSessions.length > 0) {
      groups.push({ key: 'other-projects', label: '其他项目常用会话', count: otherProjectSessions.length, items: otherProjectSessions });
    }
    if (groups.length === 0 && filteredItems.length > 0) {
      groups.push({ key: 'all', label: '会话列表', count: filteredItems.length, items: filteredItems });
    }
    return groups;
  }, [filteredItems, query, currentSession.project_id, resolveProjectLabel]);

  // Ensure selectedIndex is always within range
  useEffect(() => {
    if (selectedIndex >= filteredItems.length) {
      setSelectedIndex(Math.max(0, filteredItems.length - 1));
    }
  }, [filteredItems.length, selectedIndex]);

  // Reset search and focus input whenever switcher opens
  useEffect(() => {
    if (isOpen) {
      setQuery('');
      setSelectedIndex(0);
      requestAnimationFrame(() => {
        inputRef.current?.focus();
        inputRef.current?.select();
      });
    }
  }, [isOpen]);

  // Scroll active item into view
  useEffect(() => {
    if (!isOpen || !listContainerRef.current) return;
    const activeEl = listContainerRef.current.querySelector<HTMLElement>(
      `[data-qs-index="${selectedIndex}"]`,
    );
    if (activeEl) {
      activeEl.scrollIntoView({ block: 'nearest' });
    }
  }, [selectedIndex, isOpen]);

  // Action: Switch to session
  const handleSelectSession = useCallback(
    async (item: SwitcherSessionItem) => {
      onClose();
      if (item.project_id === currentSession.project_id) {
        if (item.thread_id !== currentSession.thread_id) {
          await switchSession(item.thread_id, item.title);
        }
      } else {
        await switchProject(item.project_id, item.thread_id);
      }
    },
    [currentSession, switchSession, switchProject, onClose],
  );

  // Keyboard navigation inside Quick Switcher
  const handleKeyDown = useCallback(
    async (e: React.KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onClose();
        return;
      }

      // Tab: Toggle scope between All Projects and Current Project
      if (e.key === 'Tab') {
        e.preventDefault();
        setOnlyCurrentProject((prev) => !prev);
        setSelectedIndex(0);
        return;
      }

      if (e.key === 'ArrowDown') {
        e.preventDefault();
        if (filteredItems.length > 0) {
          setSelectedIndex((prev) => (prev + 1) % filteredItems.length);
        }
        return;
      }

      if (e.key === 'ArrowUp') {
        e.preventDefault();
        if (filteredItems.length > 0) {
          setSelectedIndex((prev) => (prev - 1 + filteredItems.length) % filteredItems.length);
        }
        return;
      }

      if (e.key === 'Enter') {
        e.preventDefault();
        const selected = filteredItems[selectedIndex];
        if (selected) {
          await handleSelectSession(selected);
        }
        return;
      }

      // In-situ approval shortcuts (Y/N)
      const selected = filteredItems[selectedIndex];
      if (selected && selected.status === 'approval' && selected.isCurrent) {
        if ((e.key === 'y' || e.key === 'Y') && query === '') {
          e.preventDefault();
          await resolveApproval('allow_once');
          return;
        }
        if ((e.key === 'n' || e.key === 'N') && query === '') {
          e.preventDefault();
          await resolveApproval('reject_once');
          return;
        }
      }
    },
    [filteredItems, selectedIndex, handleSelectSession, resolveApproval, query, onClose],
  );

  if (!isOpen) return null;

  const currentSelected = filteredItems[selectedIndex] ?? null;

  // Compute running index across grouped lists
  let itemCounter = 0;

  return (
    <Portal>
      <div
        className="fixed inset-0 z-50 flex items-start justify-center bg-black/40 backdrop-blur-sm p-4 pt-[10vh] scrim-in"
        onClick={onClose}
      >
        <div
          role="dialog"
          aria-modal="true"
          aria-label="快速定位与切换会话"
          tabIndex={-1}
          onKeyDown={handleKeyDown}
          className="w-full max-w-4xl rounded-card border border-line-strong/80 material-flyout flyout-in flex flex-col overflow-hidden font-sans shadow-flyout h-[540px]"
          onClick={(e) => e.stopPropagation()}
        >
          {/* Header with Search Input */}
          <div className="flex items-center gap-3 border-b border-line px-4 py-3 bg-surface/60">
            <Search20Regular className="text-gray-400 shrink-0" />
            <button
              onClick={() => setOnlyCurrentProject((prev) => !prev)}
              className="px-2 py-0.5 rounded text-[11px] font-mono bg-surface-sunken border border-line text-gray-400 hover:text-gray-200 transition-colors flex items-center gap-1 shrink-0"
              title="按 Tab 切换工作区范围"
            >
              <span>{onlyCurrentProject ? `仅当前项目` : '全部工作区'}</span>
              <span className="text-[9px] opacity-70">▾</span>
            </button>
            <input
              ref={inputRef}
              type="text"
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                setSelectedIndex(0);
              }}
              placeholder="搜索会话标题、执行工具、或项目名... (按 Tab 切换范围)"
              className="flex-1 bg-transparent text-sm text-gray-900 dark:text-gray-100 placeholder-gray-400 outline-none"
            />
            {query ? (
              <button
                onClick={() => {
                  setQuery('');
                  setSelectedIndex(0);
                  inputRef.current?.focus();
                }}
                className="text-gray-400 hover:text-gray-600 dark:hover:text-gray-200"
                title="清空"
              >
                <Dismiss20Regular />
              </button>
            ) : (
              <span className="rounded border border-line px-1.5 py-0.5 font-mono text-[10px] text-gray-500">
                Esc 退出
              </span>
            )}
          </div>

          {/* Master-Detail Columns */}
          <div className="flex flex-1 min-h-0 overflow-hidden">
            {/* Left Column: Session List */}
            <div
              ref={listContainerRef}
              className="w-[45%] border-r border-line overflow-y-auto p-2 flex flex-col gap-1 bg-surface-canvas/60 no-scrollbar select-none"
            >
              {filteredItems.length === 0 ? (
                <div className="py-12 text-center text-xs text-gray-400">
                  没有找到匹配的会话
                </div>
              ) : (
                categorizedGroups.map((group) => (
                  <div key={group.key} className="flex flex-col gap-0.5 mb-1.5">
                    <div className="flex items-center justify-between px-2.5 py-1 text-[11px] font-semibold text-gray-400 tracking-wider">
                      <span>{group.label}</span>
                      <span className="text-[10px] font-mono px-1.5 py-0.2 rounded-full bg-surface-sunken text-gray-500">
                        {group.count}
                      </span>
                    </div>
                    {group.items.map((item) => {
                      const idx = itemCounter++;
                  const isSelected = idx === selectedIndex;
                  return (
                    <div
                      key={`${item.project_id}:${item.thread_id}`}
                      data-qs-index={idx}
                      onClick={() => {
                        setSelectedIndex(idx);
                      }}
                      onDoubleClick={() => handleSelectSession(item)}
                      className={`relative flex items-center gap-3 rounded-control px-3 py-2.5 cursor-pointer transition-all duration-fast ${
                        isSelected
                          ? 'bg-blue-600/15 dark:bg-blue-500/20 text-gray-900 dark:text-white font-medium shadow-sm border border-blue-500/35 dark:border-blue-400/35'
                          : 'hover:bg-control-hover/70 text-gray-600 dark:text-gray-400 border border-transparent'
                      }`}
                    >
                      {/* Fluent Selection Indicator: 3.5px accent bar on left */}
                      {isSelected && (
                        <div className="absolute left-0 top-1/2 -translate-y-1/2 h-5 w-[3.5px] rounded-full bg-blue-600 dark:bg-blue-400 shadow-sm" />
                      )}

                      {/* Status icon / dot */}
                      {item.status === 'running' && (
                        <span className="relative flex h-2 w-2 shrink-0">
                          <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-blue-400 opacity-75" />
                          <span className="relative inline-flex rounded-full h-2 w-2 bg-blue-500" />
                        </span>
                      )}
                      {item.status === 'approval' && (
                        <span className="h-2 w-2 shrink-0 rounded-full bg-amber-500 ring-2 ring-amber-500/20" />
                      )}
                      {item.status === 'idle' && (
                        <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-gray-400 dark:bg-gray-500 opacity-75" />
                      )}

                      {/* Session title & subtitle */}
                      <div className="flex-1 min-w-0 flex flex-col">
                        <div className="flex items-center gap-1.5">
                          <span
                            className={`truncate text-xs ${
                              isSelected ? 'font-semibold text-gray-900 dark:text-white' : 'font-medium text-gray-700 dark:text-gray-300'
                            }`}
                          >
                            {item.title}
                          </span>
                          {item.isCurrent && (
                            <span className="text-[10px] bg-accent/15 text-accent px-1 rounded font-mono shrink-0">
                              当前
                            </span>
                          )}
                        </div>
                        <div className="truncate text-[11px] font-mono text-gray-500 dark:text-gray-400">
                          {item.status === 'approval'
                            ? '▲ 等待安全审批'
                            : item.activity?.detail ||
                              resolveProjectLabel(item.project_id)}
                        </div>
                      </div>

                      {/* Project badge with cross-project indicator */}
                      <div className="flex items-center gap-1 shrink-0 font-mono text-[10px]">
                        {item.project_id !== currentSession.project_id && (
                          <span className="text-blue-500 bg-blue-500/10 border border-blue-500/20 px-1 rounded">
                            跨项目
                          </span>
                        )}
                        <span className="text-gray-400">
                          {resolveProjectLabel(item.project_id)}
                        </span>
                      </div>
                    </div>
                      );
                    })}
                  </div>
                ))
              )}
              {query === '' && (
                <div className="py-2.5 px-3 text-[11px] text-gray-400 text-center border-t border-line-subtle font-sans mt-auto">
                  已精简收拢历史会话 · 键入关键字可搜索全部会话
                </div>
              )}
            </div>

            {/* Right Column: Live Activity & Summary Details */}
            <div className="w-[55%] p-5 overflow-y-auto flex flex-col gap-3.5 bg-surface/50 select-text">
              {currentSelected ? (
                <>
                  {/* Top metadata row */}
                  <div className="flex items-center justify-between gap-2 border-b border-line/60 pb-3">
                    <div className="flex items-center gap-1.5 text-xs text-gray-500 font-mono">
                      <Folder20Regular className="shrink-0 text-gray-400" />
                      <span>{resolveProjectLabel(currentSelected.project_id)}</span>
                    </div>

                    {currentSelected.status === 'running' && (
                      <span className="inline-flex items-center gap-1 rounded-full bg-blue-500/10 px-2.5 py-0.5 text-[11px] font-medium text-blue-600 dark:text-blue-400 border border-blue-500/20">
                        <Play20Regular className="text-xs" />
                        运行中
                      </span>
                    )}
                    {currentSelected.status === 'approval' && (
                      <span className="inline-flex items-center gap-1 rounded-full bg-amber-500/15 px-2 py-0.5 text-[11px] font-medium text-amber-600 dark:text-amber-400 border border-amber-500/30">
                        <Warning20Regular className="text-xs" />
                        等待安全审批
                      </span>
                    )}
                    {currentSelected.status === 'idle' && (
                      <span className="inline-flex items-center gap-1 rounded-full bg-gray-500/10 px-2.5 py-0.5 text-[11px] font-medium text-gray-500 border border-gray-500/20">
                        <Checkmark20Regular className="text-xs" />
                        已就绪
                      </span>
                    )}
                  </div>

                  {/* Cross-project Notice */}
                  {currentSelected.project_id !== currentSession.project_id && (
                    <div className="rounded-control bg-blue-500/10 border border-blue-500/25 px-2.5 py-1.5 text-xs text-blue-600 dark:text-blue-400 flex items-center justify-between font-mono">
                      <span>跨项目会话</span>
                      <span className="text-[10px] opacity-80">Enter 将自动载入该项目工作区</span>
                    </div>
                  )}

                  {/* Title */}
                  <div className="text-base font-semibold text-gray-900 dark:text-gray-100 flex items-start gap-2 leading-snug">
                    <Chat20Regular className="text-accent shrink-0 mt-0.5" />
                    <span>{currentSelected.title}</span>
                  </div>

                  {/* Live Activity Section */}
                  {currentSelected.activity && (
                    <div className="rounded-card bg-surface-sunken p-3 border border-line flex flex-col gap-2 shadow-sm">
                      <div className="flex items-center justify-between text-[11px] text-gray-400 font-semibold">
                        <span className="flex items-center gap-1.5 text-accent">
                          <Wrench20Regular />
                          当前执行工具 ({currentSelected.activity.phase})
                        </span>
                      </div>
                      <div className="font-mono text-xs text-gray-900 dark:text-gray-100 break-all bg-surface/80 p-2 rounded border border-line-subtle">
                        {currentSelected.activity.detail || '处理中...'}
                      </div>
                      <div className="h-1 w-full bg-surface rounded-full overflow-hidden">
                        <div className="h-full bg-accent rounded-full animate-pulse w-3/4" />
                      </div>
                    </div>
                  )}

                  {/* Pending Approval Section */}
                  {currentSelected.status === 'approval' && currentSelected.pendingApproval && (
                    <div className="rounded-control bg-amber-500/10 p-3 border border-amber-500/30 flex flex-col gap-2">
                      <div className="text-xs font-semibold text-amber-700 dark:text-amber-400 flex items-center gap-1.5">
                        <Warning20Regular />
                        <span>安全网关保护：等待授权</span>
                      </div>
                      <div className="font-mono text-[11px] text-gray-800 dark:text-gray-200 bg-surface/70 p-2 rounded border border-amber-500/20">
                        {currentSelected.pendingApproval.actions
                          .map((a) => a.description || a.name)
                          .join(', ') || '请求执行系统操作'}
                      </div>
                      {currentSelected.isCurrent && (
                        <div className="flex items-center gap-2 pt-1">
                          <button
                            onClick={() => resolveApproval('allow_once')}
                            className="ui-button ui-compact ui-primary text-xs"
                          >
                            Y 允许执行
                          </button>
                          <button
                            onClick={() => resolveApproval('reject_once')}
                            className="ui-button ui-compact ui-secondary text-xs"
                          >
                            N 拒绝
                          </button>
                        </div>
                      )}
                    </div>
                  )}

                  {/* Recent Summary / Transcript Context */}
                  <div className="flex-1 flex flex-col gap-1.5 min-h-[90px]">
                    <span className="text-[11px] font-semibold text-gray-400">
                      最新摘要 / 执行结果:
                    </span>
                    <div className="flex-1 rounded-card bg-surface-sunken p-3 border border-line text-xs text-gray-700 dark:text-gray-200 leading-relaxed overflow-y-auto font-sans no-scrollbar">
                      {currentSelected.lastSummary ? (
                        <p className="whitespace-pre-wrap">{currentSelected.lastSummary}</p>
                      ) : (
                        <span className="text-gray-400 italic">暂无上下文摘要</span>
                      )}
                    </div>
                  </div>

                  {/* Quick Action Button */}
                  <div className="pt-2 flex items-center justify-between border-t border-line/60">
                    <span className="text-[11px] text-gray-400 font-mono">
                      {currentSelected.thread_id.slice(0, 12)}
                    </span>
                    <button
                      onClick={() => handleSelectSession(currentSelected)}
                      className="ui-button ui-compact ui-primary text-xs flex items-center gap-1.5"
                    >
                      <span>
                        {currentSelected.project_id === currentSession.project_id
                          ? '切换到此会话'
                          : `切换至 ${resolveProjectLabel(currentSelected.project_id)} 并接入`}
                      </span>
                      <ArrowRight16Regular />
                    </button>
                  </div>
                </>
              ) : (
                <div className="py-24 text-center text-xs text-gray-400 font-sans">
                  请选择左侧会话查看执行状态与动态摘要
                </div>
              )}
            </div>
          </div>

          {/* Footer Guide Strip */}
          <div className="flex items-center justify-between border-t border-line px-4 py-2 bg-surface/90 text-[11px] text-gray-400 font-mono select-none">
            <div className="flex items-center gap-3">
              <span>
                <kbd className="rounded border border-line bg-surface-sunken px-1.5 py-0.5">↑</kbd>{' '}
                <kbd className="rounded border border-line bg-surface-sunken px-1.5 py-0.5">↓</kbd>{' '}
                漫游
              </span>
              <span>
                <kbd className="rounded border border-line bg-surface-sunken px-1.5 py-0.5">Tab</kbd>{' '}
                切换范围
              </span>
              <span>
                <kbd className="rounded border border-line bg-surface-sunken px-1.5 py-0.5">Enter</kbd>{' '}
                切换
              </span>
              <span>
                <kbd className="rounded border border-line bg-surface-sunken px-1.5 py-0.5">Y</kbd> /{' '}
                <kbd className="rounded border border-line bg-surface-sunken px-1.5 py-0.5">N</kbd>{' '}
                就地审批
              </span>
            </div>
            <div>
              <span>共 {filteredItems.length} 个会话</span>
            </div>
          </div>
        </div>
      </div>
    </Portal>
  );
};
