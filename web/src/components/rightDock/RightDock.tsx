/**
 * Right Auxiliary Dock Host Component (inspired by Codex & ZCode).
 *
 * Implements:
 * - Fluid draggable horizontal resize splitter with bounds clamping and double-click reset
 * - Dynamic tab bar derived purely from the extensible RIGHT_DOCK_MANIFEST
 * - Dynamic badges (diff counters, goal progress, active flags)
 * - Clean responsive collapse/expand with transition
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
      {/* Draggable Resizer Separator - visible only when dock is open */}
      {open && (
        <div
          onMouseDown={handleMouseDown}
          onDoubleClick={resetWidth}
          title="按住左右拖拽调节右侧栏宽度 (双击复位)"
          className={`group relative z-20 w-1.5 shrink-0 cursor-col-resize select-none transition-colors ${
            isDragging ? 'bg-accent' : 'bg-transparent hover:bg-accent/40'
          }`}
        >
          <div className="absolute inset-y-0 left-0.5 w-[1px] bg-line group-hover:bg-accent/60" />
        </div>
      )}

      {/* Dock Container with Fluent Design Acrylic/Chrome and Left-Sidebar parity animation */}
      <aside
        ref={dockRef}
        inert={!open}
        style={{ width: open ? `${width}px` : '0px' }}
        className={`material-chrome relative flex h-full shrink-0 flex-col overflow-hidden select-none text-gray-900 shadow-card backdrop-blur-md ${
          open ? 'border-l border-line' : 'border-l-0 pointer-events-none'
        } ${isDragging ? '' : 'transition-[width] duration-300 ease-[cubic-bezier(0,0,0,1)]'}`}
      >
        <div className="flex h-full w-full flex-col min-w-[320px] overflow-hidden">
        {/* Dock Header with Tabs */}
        <div className="flex h-chrome items-center justify-between border-b border-line bg-surface/70 backdrop-blur-sm px-2 gap-1 overflow-hidden shrink-0">
          {/* Tab buttons */}
          <div className="flex items-center gap-0.5 rounded-control bg-sunken/70 p-0.5 min-w-0 overflow-x-auto fluent-scrollbar">
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
                  className={`flex items-center gap-1.5 rounded-control py-1 text-xs font-medium transition-colors whitespace-nowrap shrink-0 ${
                    showLabels ? 'px-2.5' : 'px-2'
                  } ${
                    isActive
                      ? 'bg-surface text-gray-900 font-semibold shadow-xs border border-line/40'
                      : 'text-gray-500 hover:text-gray-900 hover:bg-surface-hover/80'
                  }`}
                >
                  {Icon && <Icon className="shrink-0 text-sm" />}
                  {showLabels && <span className="whitespace-nowrap">{tab.label}</span>}
                  {badgeValue !== null && badgeValue !== undefined && (
                    <span
                      className={`rounded-full px-1.5 py-0.2 font-mono text-[9px] font-semibold ${
                        tab.badgeVariant === 'diff'
                          ? 'bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300'
                          : tab.badgeVariant === 'goal'
                          ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300'
                          : tab.badgeVariant === 'trace'
                          ? 'bg-purple-100 text-purple-700 dark:bg-purple-950 dark:text-purple-300'
                          : 'bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300'
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
          <div className="flex items-center gap-1 shrink-0">
            {activeTab?.HeaderExtra && <activeTab.HeaderExtra context={dockContext} />}
            <button
              type="button"
              onClick={() => setOpen(false)}
              title="收起右侧栏 (Ctrl+J)"
              aria-label="收起右侧栏"
              className="ui-icon-button ui-compact text-gray-400 hover:text-gray-700"
            >
              <Dismiss16Regular />
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
