/**
 * Right Auxiliary Dock Host Component (inspired by Codex & ZCode).
 *
 * Implements:
 * - Fluid draggable horizontal resize splitter with bounds clamping and double-click reset
 * - Dynamic tab bar derived purely from the extensible RIGHT_DOCK_MANIFEST
 * - Dynamic badges (diff counters, goal progress, active flags)
 * - Clean responsive collapse/expand with transition
 * - Out-of-flow over the workspace: opening the dock never re-flows the chat
 *   (the panel floats, the reading column keeps its width, see `.dock-overlay`
 *   in `src/index.css`)
 */
import { Dismiss16Regular } from '@fluentui/react-icons';
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { useConsoleStore } from '../../stores/useConsoleStore.ts';
import { useRightDockStore } from '../../stores/useRightDockStore.ts';
import {
  availabilityGate,
  resolveRightDockTabs,
  tabById,
  type RightDockContext,
  type RightDockVisibilityContext,
} from './contract.ts';
import { RIGHT_DOCK_MANIFEST } from './manifest.tsx';

export const RightDock: React.FC = () => {
  const {
    open,
    width,
    activeTabId,
    setOpen,
    setWidth,
    resetWidth,
    setActiveTab,
  } = useRightDockStore(
    useShallow((s) => ({
      open: s.open,
      width: s.width,
      activeTabId: s.activeTabId,
      setOpen: s.setOpen,
      setWidth: s.setWidth,
      resetWidth: s.resetWidth,
      setActiveTab: s.setActiveTab,
    })),
  );

  const currentThreadId = useConsoleStore((s) => s.currentSession.thread_id);
  const currentProjectId = useConsoleStore((s) => s.currentSession.project_id);
  const gitStatus = useConsoleStore((s) => s.gitStatus);

  const [isDragging, setIsDragging] = useState(false);
  const dockRef = useRef<HTMLDivElement>(null);

  // Dynamic availability gate for all registered tabs
  const gate = useMemo(() => availabilityGate(RIGHT_DOCK_MANIFEST), []);
  const availableTabs = gate.getSnapshot();

  const visibilityContext: RightDockVisibilityContext = useMemo(
    () => ({
      sessionOpen: currentThreadId !== '',
      compact: false,
      activeProjectId: currentProjectId,
      hasUncommittedChanges: (gitStatus?.files?.length ?? 0) > 0,
    }),
    [currentThreadId, currentProjectId, gitStatus],
  );

  const visibleTabs = useMemo(
    () => resolveRightDockTabs(availableTabs, visibilityContext),
    [availableTabs, visibilityContext],
  );

  // Ensure active tab points to a valid visible tab
  useEffect(() => {
    if (visibleTabs.length > 0 && !visibleTabs.some((t) => t.id === activeTabId)) {
      setActiveTab(visibleTabs[0].id);
    }
  }, [visibleTabs, activeTabId, setActiveTab]);

  const activeTab = useMemo(
    () => tabById(visibleTabs, activeTabId) ?? visibleTabs[0],
    [visibleTabs, activeTabId],
  );

  // Responsive label visibility: hide labels and show only icons when dock width is tight
  const showLabels = width >= 435;

  // Drag-to-resize logic
  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    setIsDragging(true);
  }, []);

  useEffect(() => {
    if (!isDragging) return;
    const onMouseMove = (e: MouseEvent) => {
      const newWidth = window.innerWidth - e.clientX;
      setWidth(newWidth);
    };
    const onMouseUp = () => {
      setIsDragging(false);
    };
    window.addEventListener('mousemove', onMouseMove);
    window.addEventListener('mouseup', onMouseUp);
    return () => {
      window.removeEventListener('mousemove', onMouseMove);
      window.removeEventListener('mouseup', onMouseUp);
    };
  }, [isDragging, setWidth]);

  const dockContext: RightDockContext = useMemo(
    () => ({
      ...visibilityContext,
      activeTabId,
      setActiveTab,
      closeDock: () => setOpen(false),
    }),
    [visibilityContext, activeTabId, setActiveTab, setOpen],
  );

  return (
    <>
      {/* Dock Container with Fluent Design Acrylic/Chrome and Left-Sidebar parity animation */}
      <aside
        ref={dockRef}
        inert={!open}
        style={{ width: open ? `${width}px` : '0px' }}
        // `absolute` in the workspace row (the row is the positioning context, see
        // `App.tsx`): the dock is a panel over the workspace, not a third column,
        // so opening it leaves the chat column -- and the composer on it -- where
        // they were.  The width still animates, anchored to the right edge.
        className={`material-chrome absolute inset-y-0 right-0 z-40 flex flex-col select-none text-gray-900 ${
          open ? 'border-l border-line dock-overlay' : 'border-l-0 pointer-events-none'
        } ${isDragging ? '' : 'transition-[width] duration-300 ease-[cubic-bezier(0,0,0,1)]'}`}
      >
        {/* Draggable Resizer Separator - overlay on the left border without in-flow gap */}
        {open && (
          <div
            onMouseDown={handleMouseDown}
            onDoubleClick={resetWidth}
            title="按住左右拖拽调节右侧栏宽度 (双击复位)"
            className={`group absolute -left-1.5 inset-y-0 z-30 w-3 cursor-col-resize select-none transition-colors ${
              isDragging ? 'bg-accent/40' : 'hover:bg-accent/20'
            }`}
          >
            <div className="absolute inset-y-0 left-1.5 w-[1px] bg-line/80 group-hover:bg-accent/80 transition-colors" />
          </div>
        )}
        <div className="flex h-full w-full flex-col min-w-[320px] overflow-hidden">
        {/* Dock Header with Tabs */}
        <div className="flex h-chrome items-center justify-between border-b border-line px-2 gap-1 overflow-hidden shrink-0">
          {/* Tab buttons */}
          <div className="flex items-center gap-1 min-w-0 overflow-x-auto no-scrollbar py-1">
            {visibleTabs.map((tab) => {
              const isActive = tab.id === activeTab?.id;
              const Icon = tab.Icon;
              const badgeValue = tab.badge ? tab.badge(visibilityContext) : null;
              const tooltip = badgeValue !== null && badgeValue !== undefined ? `${tab.label} (${badgeValue})` : tab.label;

              return (
                <button
                  key={tab.id}
                  type="button"
                  onClick={() => setActiveTab(tab.id)}
                  title={tooltip}
                  aria-label={tab.label}
                  aria-selected={isActive}
                  className={`flex h-7 items-center gap-1.5 rounded-control border text-xs transition-colors duration-150 whitespace-nowrap shrink-0 ${
                    showLabels ? 'px-2.5' : 'px-2'
                  } ${
                    isActive
                      ? 'bg-surface text-gray-900 font-semibold shadow-xs border-line/60'
                      : 'border-transparent text-gray-700 hover:text-gray-900 hover:bg-surface-hover'
                  }`}
                >
                  {Icon && <Icon className={`shrink-0 text-sm ${isActive ? 'text-accent' : 'text-gray-400'}`} />}
                  {showLabels && <span className="whitespace-nowrap">{tab.label}</span>}
                  {badgeValue !== null && badgeValue !== undefined && (
                    <span
                      className={`rounded-full px-1.5 py-0.5 font-mono text-[9.5px] font-bold leading-none text-on-accent shadow-xs ${
                        tab.badgeVariant === 'diff'
                          ? 'bg-amber-600'
                          : tab.badgeVariant === 'goal'
                          ? 'bg-emerald-600'
                          : tab.badgeVariant === 'trace'
                          ? 'bg-purple-600'
                          : 'bg-accent'
                      }`}
                    >
                      {badgeValue}
                    </span>
                  )}
                </button>
              );
            })}
          </div>

          {/* Action buttons (HeaderExtra + Close) */}
          <div className="flex items-center gap-1 shrink-0 ml-1">
            {activeTab?.HeaderExtra && <activeTab.HeaderExtra context={dockContext} />}
            <button
              type="button"
              onClick={() => setOpen(false)}
              title="收起右侧栏 (Ctrl+J)"
              aria-label="收起右侧栏"
              className="ui-icon-button ui-compact text-gray-400 hover:text-gray-900 hover:bg-surface-hover"
            >
              <Dismiss16Regular aria-hidden="true" />
            </button>
          </div>
        </div>

        {/* Tab Body Content */}
        <div className="flex-1 overflow-hidden">
          {activeTab && <activeTab.Content context={dockContext} />}
        </div>
        </div>
      </aside>
    </>
  );
};
