/**
 * Inline Live Terminal Orchestration Card in the Main Chat Column.
 *
 * Implements:
 * - Real-time status overview of running CLI Agent terminals (`🟢 运行中`, `🟡 等待确认`)
 * - Live 3-line terminal tail preview right inside the chat view
 * - Zero-switch remote approval (`[✓ 远程批准 (y)]` `[✗ 拒绝 (n)]` `[回车 ⏎]`)
 * - Auto-reflow completion notification (`🔔 已执行完毕 — [一键送入主会话审查]`)
 * - Quick jump to focus any terminal pane or switch to Side-by-Side Split Workspace
 */
import React, { useState } from 'react';
import {
  Alert16Regular,
  ArrowExpand16Regular,
  Chat16Regular,
  Checkmark16Regular,
  ChevronDown16Regular,
  ChevronUp16Regular,
  Dismiss16Regular,
  SplitVertical16Regular,
  WindowConsole20Regular,
} from '@fluentui/react-icons';
import { useShallow } from 'zustand/react/shallow';
import {
  type TerminalTabSession,
  requestInsertTerminalPill,
  useTerminalStore,
} from '../../stores/useTerminalStore.ts';
import { useConsoleStore } from '../../stores/useConsoleStore.ts';

function statusDot(session: TerminalTabSession): string {
  if (session.status === 'connecting') return 'bg-blue-500 animate-pulse';
  if (session.status === 'error') return 'bg-red-500';
  switch (session.activity) {
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

function statusWord(session: TerminalTabSession): string {
  if (session.status === 'connecting') return '正在拉起';
  if (session.status === 'error') return '启动失败';
  switch (session.activity) {
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

export const TerminalLinkageCard: React.FC = () => {
  const {
    sessions,
    activeSessionId,
    displayMode,
    reflowReadySessionIds,
    setActiveSession,
    setOpen,
    setDisplayMode,
    toggleAutoReflow,
    dismissReflowNotice,
    sendQuickResponse,
  } = useTerminalStore(
    useShallow((s) => ({
      sessions: s.sessions,
      activeSessionId: s.activeSessionId,
      displayMode: s.displayMode,
      reflowReadySessionIds: s.reflowReadySessionIds,
      setActiveSession: s.setActiveSession,
      setOpen: s.setOpen,
      setDisplayMode: s.setDisplayMode,
      toggleAutoReflow: s.toggleAutoReflow,
      dismissReflowNotice: s.dismissReflowNotice,
      sendQuickResponse: s.sendQuickResponse,
    })),
  );

  const [collapsed, setCollapsed] = useState(false);

  if (sessions.length === 0) return null;

  const waitingSession = sessions.find((s) => s.activity === 'waiting_input');
  const reflowSession = sessions.find((s) => reflowReadySessionIds.includes(s.id));
  const focusedSession: TerminalTabSession =
    waitingSession ??
    reflowSession ??
    sessions.find((s) => s.id === activeSessionId) ??
    sessions[0];

  const handleAdoptAndSubmit = (target: TerminalTabSession) => {
    const content = target.reflowResult?.trim() || target.tailLines.join('\n').trim();
    dismissReflowNotice(target.id);
    if (!content) return;
    const cs = useConsoleStore.getState();
    const prompt = `[采纳终端结果: ${target.title}]\n请基于以下终端 Agent 的输出进行审查、总结或继续下一步：\n<terminal_result id="${target.seq}" title="${target.title}" agent="${target.agentId}">\n${content}\n</terminal_result>`;
    void cs.submitPrompt(prompt);
  };

  const handleReviewInChat = (target: TerminalTabSession) => {
    dismissReflowNotice(target.id);
    requestInsertTerminalPill({
      token: `@term:${target.seq}`,
      label: `${target.title} 输出`,
      detail: `引用终端 ${target.title} 的最新执行结果`,
      followUpText: '请审查该终端的最新执行结果并给出下一步建议',
    });
  };

  const handleLocatePane = (target: TerminalTabSession) => {
    setActiveSession(target.id);
    setOpen(true);
    if (displayMode === 'dock') {
      setDisplayMode('split');
    }
  };

  const isWaiting = focusedSession.activity === 'waiting_input';
  const isReflowReady = reflowReadySessionIds.includes(focusedSession.id);

  return (
    <div className="console-column ui-composer pointer-events-auto relative isolate">
      <div
        className={`relative rounded-card border bg-surface/95 px-3 py-2 shadow-card transition-colors ${
          isWaiting
            ? 'border-amber-500/70'
            : isReflowReady
              ? 'border-accent-fg/70'
              : 'border-line/70'
        }`}
      >
        {/* Header Row: Session Pills + Reflow Toggle + Locate / Collapse */}
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-1.5 overflow-x-auto fluent-scrollbar min-w-0">
            <WindowConsole20Regular className="h-4 w-4 shrink-0 text-accent-fg" />
            <span className="text-xs font-semibold text-gray-800 shrink-0">
              终端联动监控
            </span>
            {sessions.map((s) => {
              const selected = s.id === focusedSession.id;
              const sWaiting = s.activity === 'waiting_input';
              return (
                <button
                  key={s.id}
                  type="button"
                  onClick={() => {
                    setActiveSession(s.id);
                    setCollapsed(false);
                  }}
                  className={`flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium transition-colors shrink-0 ${
                    sWaiting
                      ? 'bg-amber-500/15 text-amber-600'
                      : selected
                        ? 'bg-accent/15 text-accent-fg'
                        : 'bg-sunken text-gray-600 hover:text-gray-900'
                  }`}
                >
                  <span className={`inline-block h-1.5 w-1.5 rounded-full ${statusDot(s)}`} />
                  <span className="truncate max-w-[110px]">{s.title}</span>
                  <span className="text-[10px] opacity-75">
                    ({statusWord(s)}
                    {s.activityDetail && s.activity === 'working' ? ` · ${s.activityDetail}` : ''})
                  </span>
                </button>
              );
            })}
          </div>

            <div className="flex items-center gap-1.5 shrink-0">
              <button
                type="button"
                onClick={() => toggleAutoReflow(focusedSession.id)}
                title="开启后，当该终端完成输出或需要确认时自动在主会话提醒回流"
                className={`flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium transition-colors ${
                  focusedSession.autoReflow
                    ? 'bg-accent/15 text-accent-fg'
                    : 'bg-sunken text-gray-500 hover:text-gray-800'
                }`}
              >
                <Alert16Regular className="h-3 w-3" />
                <span>{focusedSession.autoReflow ? '完成后自动回流 ☑' : '自动回流 ☐'}</span>
              </button>

              {displayMode !== 'split' && (
                <button
                  type="button"
                  onClick={() => setDisplayMode('split')}
                  title="开启左会话 + 右终端并排联屏视图"
                  className="flex items-center gap-1 rounded-full bg-sunken px-2 py-0.5 text-[10px] text-gray-600 hover:text-gray-900 hover:bg-surface-hover"
                >
                  <SplitVertical16Regular className="h-3 w-3" />
                  <span>左右联屏</span>
                </button>
              )}

              <button
                type="button"
                onClick={() => handleLocatePane(focusedSession)}
                title="在终端画布中定位并聚焦该分屏"
                className="flex items-center gap-0.5 rounded-full bg-sunken px-2 py-0.5 text-[10px] text-gray-600 hover:text-gray-900 hover:bg-surface-hover"
              >
                <ArrowExpand16Regular className="h-3 w-3" />
                <span>定位分屏</span>
              </button>

              <button
                type="button"
                onClick={() => setCollapsed((prev) => !prev)}
                title={collapsed ? '展开实时终端预览' : '折叠实时终端预览'}
                className="flex h-5 w-5 items-center justify-center rounded-full text-gray-400 hover:bg-surface-hover hover:text-gray-800"
              >
                {collapsed ? (
                  <ChevronUp16Regular className="h-3.5 w-3.5" />
                ) : (
                  <ChevronDown16Regular className="h-3.5 w-3.5" />
                )}
              </button>
            </div>
          </div>

          {/* Expanded Body: Live 3-Line Terminal Preview / Captured Reflow Result + User Adoption Controls */}
          {!collapsed && (
            <div className="mt-1.5 flex flex-col gap-1.5">
              {(isReflowReady && focusedSession.reflowResult) ? (
                <div className="max-h-28 overflow-y-auto fluent-scrollbar rounded-control border border-accent-fg/40 bg-sunken px-2.5 py-1.5 font-sans text-xs leading-relaxed text-gray-800 whitespace-pre-wrap">
                  {focusedSession.reflowResult}
                </div>
              ) : focusedSession.tailLines.length > 0 ? (
                <div className="rounded-control border border-line/50 bg-sunken px-2.5 py-1.5 font-mono text-[11px] leading-4 text-gray-700 overflow-x-auto">
                  {focusedSession.tailLines.map((line, i) => (
                    <div key={i} className="truncate">
                      {line}
                    </div>
                  ))}
                </div>
              ) : null}

              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-1.5 text-[11px]">
                  {isWaiting ? (
                    <span className="font-medium text-amber-600">
                      终端正在等待确认，可直接在此远程审批：
                    </span>
                  ) : isReflowReady ? (
                    <span className="font-medium text-accent-fg">
                      {focusedSession.title} 已完成并回流结果，是否采纳并注入本轮对话？
                    </span>
                  ) : (
                    <span className="text-gray-500">
                      可在下方输入框用 <code className="font-mono text-accent-fg">@term:{focusedSession.seq}</code> 引用该终端，或点击右侧抓取结果
                    </span>
                  )}
                </div>

                <div className="flex items-center gap-1.5">
                  {isWaiting && (
                    <>
                      <button
                        type="button"
                        onClick={() => void sendQuickResponse(focusedSession.id, 'y\r')}
                        className="flex items-center gap-0.5 rounded-full bg-emerald-500/15 px-2.5 py-0.5 text-[11px] font-medium text-emerald-600 hover:bg-emerald-500/25 transition-colors"
                      >
                        <Checkmark16Regular className="h-3 w-3" />
                        <span>远程批准 (y)</span>
                      </button>
                      <button
                        type="button"
                        onClick={() => void sendQuickResponse(focusedSession.id, 'n\r')}
                        className="flex items-center gap-0.5 rounded-full bg-red-500/15 px-2.5 py-0.5 text-[11px] font-medium text-red-500 hover:bg-red-500/25 transition-colors"
                      >
                        <Dismiss16Regular className="h-3 w-3" />
                        <span>拒绝 (n)</span>
                      </button>
                    </>
                  )}
                  {isReflowReady ? (
                    <>
                      <button
                        type="button"
                        onClick={() => handleAdoptAndSubmit(focusedSession)}
                        className="flex items-center gap-1 rounded-full bg-accent px-2.5 py-0.5 text-[11px] font-medium text-on-accent hover:opacity-90 transition-opacity"
                      >
                        <Checkmark16Regular className="h-3 w-3" />
                        <span>采纳并注入 Turn</span>
                      </button>
                      <button
                        type="button"
                        onClick={() => handleReviewInChat(focusedSession)}
                        className="flex items-center gap-1 rounded-full bg-accent/15 px-2.5 py-0.5 text-[11px] font-medium text-accent-fg hover:bg-accent/25 transition-colors"
                      >
                        <Chat16Regular className="h-3 w-3" />
                        <span>插入输入框</span>
                      </button>
                      <button
                        type="button"
                        onClick={() => dismissReflowNotice(focusedSession.id)}
                        className="rounded-full bg-sunken px-2 py-0.5 text-[11px] text-gray-500 hover:text-gray-800"
                      >
                        忽略
                      </button>
                    </>
                  ) : (
                    <button
                      type="button"
                      onClick={() => handleReviewInChat(focusedSession)}
                      className="flex items-center gap-1 rounded-full bg-accent/15 px-2.5 py-0.5 text-[11px] font-medium text-accent-fg hover:bg-accent/25 transition-colors"
                    >
                      <Chat16Regular className="h-3 w-3" />
                      <span>抓取结果让主会话审查</span>
                    </button>
                  )}
                </div>
              </div>
            </div>
          )}
      </div>
    </div>
  );
};
