import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  Folder20Regular,
} from '@fluentui/react-icons';
import { useShallow } from 'zustand/react/shallow';
import { Portal } from './Portal.tsx';
import { useConsoleStore } from '../stores/useConsoleStore';
import { projectLabel, sessionTitleFrom, getIdenticon } from '../stores/sessionList.ts';
import { sessionKey } from '../stores/sessionViews.ts';
import {
  type HolderStatus,
  type RunningSessionItem,
  type SessionHolderPalette,
  type RecentEndedSession,
  MAX_RECENT_ENDED,
  loadRecentEndedSessions,
  saveRecentEndedSessions,
  recordEndedSession,
  getSessionHolderPalette,
  syncSessionOrder,
  globalSessionOrder,
  setGlobalSessionOrder,
  resetGlobalSessionOrderForTest,
} from './runningSessionHolders.ts';

export type { HolderStatus, RunningSessionItem, SessionHolderPalette, RecentEndedSession };
export { getSessionHolderPalette, syncSessionOrder, resetGlobalSessionOrderForTest };

/**
 * Fluent 2 规范的标准浮层提示 (Informational Flyout / Tooltip)
 * 纯信息展示，禁止假按钮与多余视觉干扰。
 */
const FluentSessionTooltip: React.FC<{
  item: RunningSessionItem;
  targetRect: DOMRect | null;
}> = ({ item, targetRect }) => {
  const [elapsed, setElapsed] = useState<number>(() => {
    if (!item.startedAt) return 0;
    return Math.max(0, Math.floor((Date.now() - item.startedAt) / 1000));
  });

  useEffect(() => {
    if (!item.startedAt || item.status === 'completed') return;
    const interval = setInterval(() => {
      setElapsed(Math.max(0, Math.floor((Date.now() - item.startedAt!) / 1000)));
    }, 1000);
    return () => clearInterval(interval);
  }, [item.startedAt, item.status]);

  if (!targetRect) return null;

  // 定位：紧随书签右侧 8px 处，垂直方向与书签对齐
  const top = Math.max(12, Math.min(window.innerHeight - 180, targetRect.top + targetRect.height / 2 - 40));
  const left = targetRect.right + 8;

  const isApproval = item.status === 'approval';
  const isCompleted = item.status === 'completed';

  return (
    <Portal>
      <div
        role="tooltip"
        style={{ top: `${top}px`, left: `${left}px` }}
        className="fixed z-50 flex w-72 flex-col rounded-card border border-line material-flyout p-3 shadow-flyout pointer-events-none flyout-in select-none"
      >
        {/* 顶部状态与耗时行 */}
        <div className="flex items-center justify-between gap-2 mb-1.5">
          <div className="flex items-center gap-1.5 text-xs font-medium">
            {isApproval ? (
              <>
                <span className="h-2 w-2 rounded-full bg-amber-500 animate-pulse" />
                <span className="text-amber-600">等待审批确认</span>
              </>
            ) : isCompleted ? (
              <>
                <span className="h-2 w-2 rounded-full bg-emerald-500 flex items-center justify-center text-[7px] text-white">
                  ✓
                </span>
                <span className="text-emerald-600">已结束</span>
              </>
            ) : (
              <>
                <span className="relative flex h-2 w-2 items-center justify-center">
                  <span className="absolute inline-flex h-full w-full rounded-full bg-blue-400 opacity-75 animate-ping" aria-hidden="true" />
                  <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-blue-500" />
                </span>
                <span className="text-blue-500 font-medium">正在运行</span>
              </>
            )}
          </div>
          {elapsed > 0 && (
            <span className="text-[11px] font-mono text-gray-500">
              {elapsed}s
            </span>
          )}
        </div>

        {/* 会话标题 */}
        <div className="text-[13px] font-semibold text-gray-900 leading-snug line-clamp-2 mb-1.5">
          {item.title}
        </div>

        {/* 所属项目 */}
        <div className="flex items-center gap-1.5 text-[11px] text-gray-600">
          <Folder20Regular className="h-3.5 w-3.5 shrink-0 text-gray-500" />
          <span className="truncate">{item.projectName}</span>
        </div>

        {/* 当前活动详情（如有） */}
        {item.currentActivity && (
          <div
            className={`mt-2 rounded bg-surface-hover/80 px-2 py-1 text-[11px] font-mono text-gray-700 border-l-2 line-clamp-2 ${
              isApproval
                ? 'border-amber-500'
                : isCompleted
                ? 'border-emerald-500'
                : 'border-blue-500'
            }`}
          >
            {item.currentActivity}
          </div>
        )}
      </div>
    </Portal>
  );
};

export const RunningSessionHolders: React.FC<{ onNavigate?: () => void }> = ({ onNavigate }) => {
  const {
    currentSession,
    runtimeStatus,
    pendingApproval,
    activity,
    backgroundViews,
    sessions,
    projectSessions,
    projects,
    sessionTitle,
    switchProject,
  } = useConsoleStore(
    useShallow((state) => ({
      currentSession: state.currentSession,
      runtimeStatus: state.runtimeStatus,
      pendingApproval: state.pendingApproval,
      activity: state.activity,
      backgroundViews: state.backgroundViews,
      sessions: state.sessions,
      projectSessions: state.projectSessions,
      projects: state.projects,
      sessionTitle: state.sessionTitle,
      switchProject: state.switchProject,
    })),
  );

  const [hoveredKey, setHoveredKey] = useState<string | null>(null);
  const [hoverRect, setHoverRect] = useState<DOMRect | null>(null);
  const [overflowOpen, setOverflowOpen] = useState<boolean>(false);
  const overflowRef = useRef<HTMLDivElement>(null);
  const hoverTimerRef = useRef<number | null>(null);

  // 保留最近结束的最多 5 个会话侧栏图标，方便快速切换
  const [recentEnded, setRecentEnded] = useState<RecentEndedSession[]>(() => loadRecentEndedSessions());
  const activeKeysRef = useRef<Set<string>>(new Set());

  // 初始若无记录且已有会话列表，自动预置当前项目最近的已结束会话（最多 5 个）
  useEffect(() => {
    if (recentEnded.length === 0 && sessions.length > 0 && currentSession.project_id) {
      const seeded: RecentEndedSession[] = sessions
        .filter((s) => s.thread_id !== '')
        .slice(0, MAX_RECENT_ENDED)
        .map((s) => ({
          key: `${currentSession.project_id}:${s.thread_id}`,
          projectId: currentSession.project_id,
          threadId: s.thread_id,
          endedAt: Date.now(),
        }));
      if (seeded.length > 0) {
        setRecentEnded(seeded);
        saveRecentEndedSessions(seeded);
      }
    }
  }, [sessions, currentSession.project_id, recentEnded.length]);

  const runningSessions = useMemo(() => {
    const candidateMap = new Map<string, RunningSessionItem>();
    const currentActiveKeys = new Set<string>();

    const resolveProjectName = (projectId: string): string => {
      const proj = projects.find((p) => p.project_id === projectId);
      return proj ? projectLabel(proj) : projectId.slice(0, 6);
    };

    const resolveTitle = (projectId: string, threadId: string): string => {
      if (
        projectId === currentSession.project_id &&
        threadId === currentSession.thread_id &&
        sessionTitle &&
        sessionTitle.trim() !== ''
      ) {
        return sessionTitle;
      }
      const activeFound = sessionTitleFrom(sessions, threadId);
      if (activeFound) return activeFound;
      const projItems = projectSessions[projectId] ?? [];
      const projFound = sessionTitleFrom(projItems, threadId);
      if (projFound) return projFound;
      return threadId.length > 8 ? threadId.slice(0, 8) : threadId;
    };

    // 1. Current active session
    if (
      currentSession.thread_id !== '' &&
      (runtimeStatus === 'running' || pendingApproval !== null)
    ) {
      const key = sessionKey(currentSession);
      currentActiveKeys.add(key);
      const title = resolveTitle(currentSession.project_id, currentSession.thread_id);
      const projectName = resolveProjectName(currentSession.project_id);
      candidateMap.set(key, {
        key,
        projectId: currentSession.project_id,
        threadId: currentSession.thread_id,
        status: pendingApproval !== null ? 'approval' : 'running',
        isCurrent: true,
        title,
        projectName,
        identicon: getIdenticon(title, projectName),
        currentActivity: activity?.detail || (runtimeStatus === 'running' ? '正在执行任务...' : '等待审批确认'),
        startedAt: activity?.startedAt,
      });
    }

    // 2. Background views
    for (const [key, view] of Object.entries(backgroundViews)) {
      if (currentActiveKeys.has(key)) continue;
      if (view.subscriptionId === null) continue;
      if (view.runtimeStatus !== 'running' && view.pendingApproval === null) continue;

      const sepIndex = key.indexOf(':');
      if (sepIndex === -1) continue;
      const projectId = key.slice(0, sepIndex);
      const threadId = key.slice(sepIndex + 1);

      currentActiveKeys.add(key);
      const title = resolveTitle(projectId, threadId);
      const projectName = resolveProjectName(projectId);
      candidateMap.set(key, {
        key,
        projectId,
        threadId,
        status: view.pendingApproval !== null ? 'approval' : 'running',
        isCurrent: false,
        title,
        projectName,
        identicon: getIdenticon(title, projectName),
        currentActivity: view.activity?.detail || (view.runtimeStatus === 'running' ? '后台执行中...' : '等待审批确认'),
        startedAt: view.activity?.startedAt,
      });
    }

    // 3. 检查刚刚结束的会话，存入最近结束会话（最多保留 5 个）
    let nextRecent = recentEnded;
    let recentChanged = false;
    for (const prevKey of activeKeysRef.current) {
      if (!currentActiveKeys.has(prevKey)) {
        const sepIndex = prevKey.indexOf(':');
        if (sepIndex !== -1) {
          const projectId = prevKey.slice(0, sepIndex);
          const threadId = prevKey.slice(sepIndex + 1);
          nextRecent = recordEndedSession(nextRecent, { projectId, threadId, key: prevKey });
          recentChanged = true;
        }
      }
    }
    activeKeysRef.current = currentActiveKeys;
    if (recentChanged) {
      setRecentEnded(nextRecent);
      saveRecentEndedSessions(nextRecent);
    }

    // 4. 将最近结束的会话加入 candidateMap（不覆盖当前正在运行的同名会话）
    for (const rec of nextRecent) {
      if (!candidateMap.has(rec.key)) {
        const title = resolveTitle(rec.projectId, rec.threadId);
        const projectName = resolveProjectName(rec.projectId);
        const isCurrent =
          rec.projectId === currentSession.project_id &&
          rec.threadId === currentSession.thread_id;
        candidateMap.set(rec.key, {
          key: rec.key,
          projectId: rec.projectId,
          threadId: rec.threadId,
          status: 'completed',
          isCurrent,
          title,
          projectName,
          identicon: getIdenticon(title, projectName),
          currentActivity: '已结束',
        });
      }
    }

    // 按会话原先出现的固定顺序排列，选中（isCurrent）时不改变其在侧栏中的原有位置
    const allActiveKeys = Array.from(candidateMap.keys());
    const nextOrder = syncSessionOrder(globalSessionOrder, allActiveKeys);
    setGlobalSessionOrder(nextOrder);

    const list: RunningSessionItem[] = [];
    for (const key of nextOrder) {
      const item = candidateMap.get(key);
      if (item) {
        list.push(item);
      }
    }

    return list;
  }, [
    currentSession,
    runtimeStatus,
    pendingApproval,
    activity,
    backgroundViews,
    sessions,
    projectSessions,
    projects,
    sessionTitle,
    recentEnded,
  ]);

  const handleMouseEnter = (key: string, el: HTMLElement) => {
    if (hoverTimerRef.current) clearTimeout(hoverTimerRef.current);
    const rect = el.getBoundingClientRect();
    setHoveredKey(key);
    setHoverRect(rect);
  };

  const handleMouseLeave = () => {
    if (hoverTimerRef.current) clearTimeout(hoverTimerRef.current);
    hoverTimerRef.current = window.setTimeout(() => {
      setHoveredKey(null);
      setHoverRect(null);
    }, 100);
  };

  if (runningSessions.length === 0) {
    return null;
  }

  // 最多呈现 7 个活跃书签 Holder（可同时容纳 5 个已结束会话 + 运行中会话），多出的收纳进 +N 溢出胶囊
  const MAX_VISIBLE_HOLDERS = 7;
  const visibleSessions = runningSessions.slice(0, MAX_VISIBLE_HOLDERS);
  const overflowSessions = runningSessions.slice(MAX_VISIBLE_HOLDERS);

  const hoveredItem = runningSessions.find((s) => s.key === hoveredKey);

  return (
    <div className="flex flex-col items-center gap-1.5 w-full my-1.5" role="region" aria-label="运行中会话">
      <div className="w-5 h-px bg-line/80 my-0.5 shrink-0" aria-hidden="true" />

      {visibleSessions.map((item) => {
        const isApproval = item.status === 'approval';
        const isCompleted = item.status === 'completed';
        const isCurrent = item.isCurrent;
        const palette = getSessionHolderPalette(item.key, isCurrent, item.status);
        const identiconText = item.identicon || getIdenticon(item.title, item.projectName);

        return (
          <div
            key={item.key}
            className="relative flex items-center justify-center w-full"
            onMouseEnter={(e) => handleMouseEnter(item.key, e.currentTarget)}
            onMouseLeave={handleMouseLeave}
            onFocus={(e) => handleMouseEnter(item.key, e.currentTarget)}
            onBlur={handleMouseLeave}
          >
            {/* Fluent 2 Navigation Indicator Pill (激活书签贴边竖条) */}
            {isCurrent && (
              <span
                className="absolute left-0 top-1/2 -translate-y-1/2 w-[3px] h-4 rounded-r-full bg-blue-500 pointer-events-none z-10 transition-all duration-150"
                aria-hidden="true"
              />
            )}

            {/*
              实体书签页卡：完全使用主题驱动的语义化背景和边框，
              不同 holder 具备独特色相，且背景色随主题切换自适应（浅色为柔和浅色调，深色为深色高质感底色），
              文字色彩与背景严格适配，保证任何主题下都具备最高对比度与实体书签触感。
            */}
            <button
              type="button"
              onClick={() => {
                if (isCurrent) return;
                onNavigate?.();
                setHoveredKey(null);
                void switchProject(item.projectId, item.threadId);
              }}
              aria-label={`${item.title} (${item.projectName}) - ${
                isApproval ? '等待审批' : isCompleted ? '已结束' : '运行中'
              }`}
              aria-current={isCurrent ? 'page' : undefined}
              className={`relative flex h-8 w-8 items-center justify-center rounded-control text-[11px] font-bold transition-all duration-150 select-none cursor-pointer shadow-xs ${palette.bg} ${palette.border} ${palette.text}`}
            >
              {/* 会话文字标识：最多 2 个汉字或 3 个英文字符，字形清晰饱满 */}
              <span className="truncate max-w-[28px] text-center leading-none tracking-tight">
                {identiconText}
              </span>

              {/* Fluent Presence Badge (右下角状态指示) */}
              <span
                className="absolute -right-0.5 -bottom-0.5 flex h-3 w-3 items-center justify-center rounded-full pointer-events-none ring-2 ring-surface bg-surface"
                aria-hidden="true"
              >
                {isApproval ? (
                  <span className="h-2 w-2 rounded-full bg-amber-500" />
                ) : isCompleted ? (
                  <span className="h-2 w-2 rounded-full bg-emerald-500 flex items-center justify-center text-[7px] text-white">
                    ✓
                  </span>
                ) : (
                  <span className="relative flex h-2 w-2 items-center justify-center">
                    <span className="absolute inline-flex h-full w-full rounded-full bg-blue-400 opacity-75 animate-ping" aria-hidden="true" />
                    <span className={`relative inline-flex h-1.5 w-1.5 rounded-full ${palette.dot}`} />
                  </span>
                )}
              </span>
            </button>
          </div>
        );
      })}

      {/* 超出 4 个时的书签溢出胶囊 */}
      {overflowSessions.length > 0 && (
        <div className="relative flex items-center justify-center w-full" ref={overflowRef}>
          <button
            type="button"
            onClick={() => setOverflowOpen((prev) => !prev)}
            title={`其余 ${overflowSessions.length} 个运行中的会话`}
            aria-label={`展开其余 ${overflowSessions.length} 个运行中的会话`}
            className="flex h-6 w-6 items-center justify-center rounded-full bg-surface border border-line text-[10px] font-bold text-gray-700 hover:border-blue-500 hover:text-blue-500 transition-colors shadow-xs"
          >
            +{overflowSessions.length}
          </button>

          {/* 溢出下拉菜单 */}
          {overflowOpen && (
            <div
              className="absolute left-10 top-0 z-50 w-56 rounded-card border border-line material-flyout p-1 shadow-flyout flyout-in"
              onMouseLeave={() => setOverflowOpen(false)}
            >
              <div className="px-2 py-1 text-[11px] font-semibold text-gray-500">
                运行中会话 ({overflowSessions.length})
              </div>
              <div className="fluent-scrollbar max-h-60 overflow-y-auto space-y-0.5">
                {overflowSessions.map((item) => (
                  <button
                    key={item.key}
                    type="button"
                    onClick={() => {
                      setOverflowOpen(false);
                      onNavigate?.();
                      void switchProject(item.projectId, item.threadId);
                    }}
                    className="ui-menu-item flex items-center gap-2 rounded px-2 py-1.5 text-left text-xs"
                  >
                    <span className={`flex h-5 w-5 shrink-0 items-center justify-center rounded ${getSessionHolderPalette(item.key, item.isCurrent, item.status).bg} ${getSessionHolderPalette(item.key, item.isCurrent, item.status).text} text-[10px] font-bold border border-line`}>
                      {item.identicon || getIdenticon(item.title, item.projectName)}
                    </span>
                    <div className="flex-1 min-w-0">
                      <div className="truncate font-medium text-gray-900">
                        {item.title}
                      </div>
                      <div className="text-[10px] text-gray-500 truncate">
                        {item.projectName}
                      </div>
                    </div>
                    {item.status === 'approval' ? (
                      <span className="h-2 w-2 shrink-0 rounded-full bg-amber-500 animate-pulse" />
                    ) : item.status === 'completed' ? (
                      <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-emerald-500" />
                    ) : (
                      <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-blue-500" />
                    )}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Fluent 悬浮提示 Tooltip */}
      {hoveredItem && (
        <FluentSessionTooltip
          item={hoveredItem}
          targetRect={hoverRect}
        />
      )}

      <div className="w-5 h-px bg-line/80 my-0.5 shrink-0" aria-hidden="true" />
    </div>
  );
};
