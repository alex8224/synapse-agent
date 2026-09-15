import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  BrainCircuit20Regular,
  Sparkle20Regular,
  Wrench20Regular,
  SpinnerIos20Regular,
  Warning20Regular,
  Info20Regular,
  ArrowSort20Regular,
  DismissCircle20Regular,
  Shield20Regular,
  Copy16Regular,
  Checkmark16Regular,
  Edit16Regular,
  ChevronRight16Regular,
  ChevronDown16Regular,
  WindowConsole20Regular,
  Bot20Regular,
} from '@fluentui/react-icons';
import { useShallow } from 'zustand/react/shallow';
import { useConsoleStore } from '../stores/useConsoleStore';
import {
  expandHint,
  formatToolArgs,
  groupToolsForView,
  isTerminalTool,
  thoughtLabel,
  toolGroupLabel,
  toolPreviewLanguage,
  toolStatusLabel,
  type SubagentToolGroup,
  type ToolRenderNode,
} from '../stores/transcriptLabels.ts';
import { CodeBlock } from './CodeBlock.tsx';
import { TerminalOutput } from './TerminalOutput.tsx';
import { Markdown } from './Markdown.tsx';
import { AttachmentThumb } from './AttachmentThumb.tsx';
import { TurnRail } from './TurnRail.tsx';
import { TodoPanel } from './TodoPanel.tsx';
import type { ActivityView } from '../stores/liveEventReducer.ts';
import type { ToolItemView, TranscriptMessage } from '../stores/historyMapper.ts';
import {
  formatWorkDuration,
  getGroupIntentStatus,
  workGroups,
  workSeconds,
  type GroupIntentStatus,
  type WorkGroup,
} from '../stores/turnWork.ts';

/**
 * How close to the bottom the view still counts as "following the stream", in
 * pixels: a little slack keeps a sub-pixel scroll position (or a wrapped line
 * landing mid-frame) from silently stopping the follow.
 */
const PINNED_TO_BOTTOM_PX = 32;

/**
 * How long after the last wheel / touch event a scroll gesture counts as over.
 *
 * Long enough that a continuous gesture keeps the follow latch live, short enough
 * that the next streamed update is not yanked back while the reader is still
 * looking elsewhere.
 */
const USER_SCROLL_SETTLE_MS = 150;

/**
 * How close to the top of the transcript counts as "reached the top", in pixels.
 *
 * Reaching it loads the next page of earlier history, so the button above the
 * oldest loaded turn is a fallback rather than the only way in.
 */
const EARLIER_HISTORY_TRIGGER_PX = 48;

/**
 * One transcript row.
 *
 * Memoized on the message object: folding streamed text rebuilds the transcript
 * array but leaves every row that did not grow identical, so a long conversation
 * does not re-render -- and re-parse the Markdown of -- every row on every chunk.
 * `handleToggleExpand` is the transcript's stable fold handler, so it does not
 * invalidate the memo.
 */
const TranscriptRow = React.memo(function TranscriptRow({
  message: m,
  handleToggleExpand,
  processMeta,
}: {
  message: TranscriptMessage;
  handleToggleExpand: (id: string) => void;
  processMeta?: {
    isFirst: boolean;
    isExpanded: boolean;
    totalDurationText: string;
    groupStatus: GroupIntentStatus | null;
    onToggleExpand: () => void;
  };
}) {
  const updateSpotlight = (e: React.MouseEvent<HTMLElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    e.currentTarget.style.setProperty('--mouse-x', `${e.clientX - rect.left}px`);
    e.currentTarget.style.setProperty('--mouse-y', `${e.clientY - rect.top}px`);
  };
  const submitPrompt = useConsoleStore((state) => state.submitPrompt);
  const [isEditing, setIsEditing] = useState(false);
  const [editText, setEditText] = useState(m.content ?? '');
  const [copied, setCopied] = useState(false);
  /**
   * Which subagent cards are open, keyed by the call that started them.
   *
   * A subagent's steps are a fold of their own inside the tool batch, so opening one
   * must not be a store write: the transcript's fold flags live on the message and
   * flipping one re-creates the messages array (and re-parses the Markdown of every
   * row).  A card is closed by default -- the batch it lives in already says the
   * subagent ran, and the steps are there for a reader who asks for them -- and
   * only a deliberate expansion is remembered.
   */
  const [expandedSubagents, setExpandedSubagents] = useState<Record<string, boolean>>({});
  const toggleSubagent = (id: string) =>
    setExpandedSubagents((prev) => ({ ...prev, [id]: !(prev[id] ?? false) }));

  const handleCopy = (text: string) => {
    const clipboard = typeof navigator === 'undefined' ? undefined : navigator.clipboard;
    if (!clipboard || !text) return;
    void clipboard
      .writeText(text)
      .then(() => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1200);
      })
      .catch(() => undefined);
  };

  if (m.type === 'user') {
    return (
      // Chat layout: the user's turn sits on the right, the assistant's on
      // the left, and the side it is on is the role — so no "User" /
      // "Assistant" heading is needed.
      // The 20% right inset shares the assistant body's right edge: that block is
      // capped at 80% of the reading column, so the user's turn is held back by
      // whatever is left. The two numbers must keep summing to 100% (pinned by
      // `transcriptLayoutGuard.test.ts`), which is what keeps the bubble from
      // hanging past the answer it belongs to.
      // `data-turn-id` is the anchor the turn rail scrolls to.
      <div key={m.id} data-turn-id={m.id} className="flex justify-end">
        <div className="mr-[20%] flex max-w-[80%] flex-col items-end gap-1.5 group">
          {isEditing ? (
            <div className="w-full flex flex-col gap-2 rounded-card border border-accent bg-surface p-2.5 shadow-card">
              <textarea
                value={editText}
                onChange={(e) => setEditText(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    const trimmed = editText.trim();
                    if (trimmed) {
                      void submitPrompt(trimmed);
                      setIsEditing(false);
                    }
                  } else if (e.key === "Escape") {
                    setIsEditing(false);
                  }
                }}
                className="w-full resize-none bg-transparent text-sm text-gray-900 focus:outline-none font-sans"
                rows={Math.min(8, Math.max(2, editText.split('\n').length))}
                autoFocus
              />
              <div className="flex items-center justify-end gap-2 text-xs">
                <button
                  type="button"
                  onClick={() => setIsEditing(false)}
                  className="rounded px-2.5 py-1 text-gray-500 hover:bg-surface-hover cursor-pointer"
                >
                  取消
                </button>
                <button
                  type="button"
                  onClick={() => {
                    const trimmed = editText.trim();
                    if (trimmed) {
                      void submitPrompt(trimmed);
                      setIsEditing(false);
                    }
                  }}
                  disabled={!editText.trim()}
                  className="rounded bg-accent px-3 py-1 text-on-accent font-medium hover:bg-blue-700 cursor-pointer disabled:opacity-50"
                >
                  发送
                </button>
              </div>
            </div>
          ) : (
            <>
              {m.content !== "" && (
                <div
                  onMouseMove={updateSpotlight}
                  className="ui-user-bubble fluent-spotlight whitespace-pre-wrap break-words text-base leading-relaxed text-gray-900"
                >
                  {m.content}
                </div>
              )}
              {m.attachments !== undefined && m.attachments.length > 0 && (
                <div className="flex flex-wrap justify-end gap-2">
                  {m.attachments.map((attachment) => (
                    <AttachmentThumb key={attachment.attachmentId} attachment={attachment} />
                  ))}
                </div>
              )}
              <div className="flex items-center gap-1">
                <button
                  type="button"
                  onClick={() => handleCopy(m.content ?? '')}
                  title={copied ? "已复制" : "复制消息"}
                  aria-label="复制消息"
                  className="p-1 rounded text-gray-400 hover:text-gray-700 hover:bg-surface-hover cursor-pointer transition-colors"
                >
                  {copied ? <Checkmark16Regular aria-hidden="true" className="text-accent" /> : <Copy16Regular aria-hidden="true" />}
                </button>
                <button
                  type="button"
                  onClick={() => { setIsEditing(true); setEditText(m.content ?? ''); }}
                  title="编辑并重新发送"
                  aria-label="编辑并重新发送"
                  className="p-1 rounded text-gray-400 hover:text-gray-700 hover:bg-surface-hover cursor-pointer transition-colors"
                >
                  <Edit16Regular aria-hidden="true" />
                </button>
                <span className="font-mono text-[10px] text-gray-400 ml-1">{m.timestamp}</span>
              </div>
            </>
          )}
        </div>
      </div>
    );
  }
  if (m.type === 'thought') {
    if (processMeta && !processMeta.isExpanded && !processMeta.isFirst) {
      return null;
    }
    return (
      <div key={m.id} className="max-w-[85%]">
        {processMeta && !processMeta.isExpanded ? (
          <div className="transcript-fold-header">
            <div className="flex items-center gap-2 py-1 min-w-0">
              <button
                type="button"
                onClick={processMeta.onToggleExpand}
                className="flex items-center gap-1 text-xs text-gray-500 hover:text-gray-700 py-1 cursor-pointer select-none font-sans transition-colors"
              >
                <span>已工作 {processMeta.totalDurationText}</span>
                <ChevronRight16Regular aria-hidden="true" style={{ fontSize: '13px' }} />
              </button>
              {processMeta.groupStatus && <FoldStatusPill status={processMeta.groupStatus} />}
            </div>
            <div className="border-b border-line/60 my-2.5" />
          </div>
        ) : (
          <>
            {processMeta && processMeta.isFirst && (
              <div className="transcript-fold-header">
                <div className="flex items-center gap-2 py-1 min-w-0">
                  <button
                    type="button"
                    onClick={processMeta.onToggleExpand}
                    className="flex items-center gap-1 text-xs text-gray-500 hover:text-gray-700 py-1 cursor-pointer select-none font-sans transition-colors"
                  >
                    <span>已工作 {processMeta.totalDurationText}</span>
                    <ChevronDown16Regular aria-hidden="true" style={{ fontSize: '13px' }} />
                  </button>
                  {processMeta.groupStatus && <FoldStatusPill status={processMeta.groupStatus} />}
                </div>
                {/* The rule belongs to the header, in both folds: the steps below it
                    are the fold's content, so the block must not draw a second rule
                    at their end.  Header and rule stay in flow with the steps they
                    name, so nothing of the fold is masked while the column scrolls. */}
                <div className="border-b border-line/60 my-2.5" />
              </div>
            )}
            <div
              onClick={() => handleToggleExpand(m.id)}
              onMouseMove={updateSpotlight}
              className="inline-flex cursor-pointer select-none items-center gap-1.5 rounded-control border border-line bg-surface px-2.5 py-1 font-mono text-xs text-gray-600 transition-colors hover:bg-surface-hover hover:text-gray-900 active:bg-surface-pressed fluent-spotlight"
            >
              {m.duration === 'streaming' ? (
                <Sparkle20Regular aria-hidden="true" className="shrink-0 animate-pulse text-accent" style={{ fontSize: '14px' }} />
              ) : (
                <BrainCircuit20Regular aria-hidden="true" className="shrink-0 text-gray-500" style={{ fontSize: '14px' }} />
              )}
              <span>{thoughtLabel(m.duration)}</span>
              <span className="text-gray-400">{expandHint(m.expanded === true)}</span>
            </div>
            <div className="fluent-accordion" data-expanded={m.expanded === true}>
              <div className="fluent-accordion-content pt-1.5">
                <div className="material-card rounded-card border border-line p-3 text-sm text-gray-700 shadow-card">
                  <Markdown text={m.content ?? ''} />
                </div>
              </div>
            </div>
          </>
        )}
      </div>
    );
  }
  if (m.type === 'tool_group') {
    const toolList = m.tools || [];
    if (toolList.length === 0) {
      return null;
    }
    const failed = toolList.filter((t) => t.error || t.status === 'failed').length;
    const running = toolList.filter(
      (t) => t.status === 'running' || t.status === 'pending',
    ).length;
    const expanded = m.expanded === true;
    if (processMeta && !processMeta.isExpanded && !processMeta.isFirst) {
      return null;
    }
    // The batch is flat on the wire; this is where a subagent's own steps are put
    // back under the call that started it, so they can be painted as one card
    // instead of as N more rows of the main agent's list.
    const toolNodes = groupToolsForView(toolList);
    /**
     * One tool row: the call's name, the intent the model gave it, its bounded
     * arguments and its body.  Shared by a main-agent row and by a subagent step,
     * so a nested call reads exactly like a top-level one.
     */
    const renderToolRow = (t: ToolItemView) => {
      const argsLine = formatToolArgs(t.args);
      // A run tool returns a program's terminal output, so it is painted
      // (escapes and all) rather than tokenized as source code.
      const terminal = isTerminalTool(t.name);
      // A read / edit body is a file (or a patch), so it gets the same
      // highlighter the markdown fences use; anything else stays plain.
      const previewLang = t.preview && !terminal
        ? toolPreviewLanguage(t.name, t.path, t.preview)
        : '';
      return (
        <div
          key={t.id}
          onMouseMove={updateSpotlight}
          className={"rounded-control border px-2.5 py-1.5 font-mono text-xs fluent-spotlight " + (t.error ? "border-red-200 bg-red-50" : "border-line bg-surface")}
        >
          <div className="flex items-center space-x-2">
            {t.name === 'execute' ? (
              <WindowConsole20Regular aria-hidden="true" className="shrink-0 text-gray-500" style={{ fontSize: '13px' }} />
            ) : (
              <Wrench20Regular aria-hidden="true" className="shrink-0 text-gray-500" style={{ fontSize: '13px' }} />
            )}
            {/* The tool's own name is never replaced: it is what the call
                *was*, while the intent beside it is what the model said it
                was for.  The icon carries the kind, so no word repeats it. */}
            <span className="shrink-0 font-medium text-gray-900">{t.name}</span>
            {t.label && t.label !== t.name && (
              <span className="truncate text-gray-600" title={t.label}>
                {t.label}
              </span>
            )}
            {t.sub && (
              <span className="rounded-control bg-sunken px-1 text-[10px] text-gray-500">
                sub
              </span>
            )}
            {t.subagentName && (
              <span className="text-[10px] text-gray-400">@{t.subagentName}</span>
            )}
            {t.path && <span className="truncate text-gray-500">{t.path}</span>}
            <span
              className={"ml-auto shrink-0 rounded-control px-1 text-[10px] " + (t.error ? "bg-red-100 text-red-700" : t.status === "completed" ? "bg-green-100 text-green-700" : "bg-blue-50 text-blue-500")}
            >
              {t.subagentStatus
                ? (toolStatusLabel(t.status) + " · " + t.subagentStatus)
                : toolStatusLabel(t.status)}
            </span>
          </div>
          {/* The call's own arguments, bounded by `formatToolArgs`: an
              argument can be a whole command or file, so it is one
              collapsed line rather than a payload. */}
          {argsLine !== '' && (
            <div className="mt-1 break-all text-gray-500" title={argsLine}>
              {argsLine}
            </div>
          )}
          {terminal && t.preview ? (
            <TerminalOutput text={t.preview} />
          ) : previewLang !== '' && t.preview ? (
            <CodeBlock lang={previewLang} code={t.preview} />
          ) : (
            t.preview && (
              <div className="mt-1 whitespace-pre-wrap break-all text-gray-600">
                {t.preview}
              </div>
            )
          )}
        </div>
      );
    };
    /**
     * One subagent: the call that started it, the goal it was given, and its own
     * steps behind a rail.  The rail is what makes the nesting readable at a
     * glance -- a step hangs off the card, not off the main agent's list.
     */
    const renderSubagentCard = (node: SubagentToolGroup, key: string) => {
      const subExpanded = expandedSubagents[key] === true;
      const subRunning = node.parent.status === 'running'
        || node.tools.some((s) => s.status === 'running' || s.status === 'pending');
      // Only the task call itself decides the card's outcome: a subagent that
      // recovered from a failed step still completed, and its own tool errors are
      // counted on the step line instead of painting the whole card red.
      const parentFailed = node.parent.error || node.parent.status === 'failed';
      const failedSteps = node.tools.filter((s) => s.error || s.status === 'failed').length;
      return (
        <div key={key} className="rounded-control border border-line bg-raised">
          {/* The raised fill equals the dark palette's hover step, so the header
              takes the pressed step to stay visible when it is hovered. */}
          <button
            type="button"
            onClick={() => toggleSubagent(key)}
            aria-expanded={subExpanded}
            title={subExpanded ? '收起子代理步骤' : '展开子代理步骤'}
            className="flex w-full cursor-pointer select-none items-center gap-2 px-2.5 py-1.5 text-left transition-colors hover:bg-surface-pressed"
          >
            {/* The subagent's own mark: purple is the identity accent, and the card
                surface stays a neutral layer like every other in-page card. */}
            <span className="flex h-[22px] w-[22px] shrink-0 items-center justify-center rounded-control border border-purple-200/60 bg-purple-50 text-purple-700">
              <Bot20Regular aria-hidden="true" style={{ fontSize: '13px' }} />
            </span>
            <span className="shrink-0 rounded-control bg-purple-50 text-purple-700 border border-purple-200/60 font-mono text-[10px] px-1">
              @{node.subagentName}
            </span>
            <span className="truncate text-xs text-gray-900" title={node.subagentGoal}>
              {node.subagentGoal}
            </span>
            <span className="ml-auto shrink-0 font-mono text-[10px] text-gray-400">
              {failedSteps > 0 ? `${node.tools.length} 步骤 (${failedSteps} 失败)` : `${node.tools.length} 步骤`}
            </span>
            <span
              className={"flex shrink-0 items-center gap-1 rounded-control px-1 font-mono text-[10px] " + (parentFailed ? "bg-red-100 text-red-700" : subRunning ? "bg-blue-50 text-blue-500" : "bg-green-100 text-green-700")}
            >
              {parentFailed ? (
                <DismissCircle20Regular aria-hidden="true" style={{ fontSize: '11px' }} />
              ) : subRunning ? (
                <SpinnerIos20Regular aria-hidden="true" className="animate-spin" style={{ fontSize: '11px' }} />
              ) : (
                <Checkmark16Regular aria-hidden="true" style={{ fontSize: '11px' }} />
              )}
              {parentFailed ? '失败' : subRunning ? '运行中' : '完成'}
            </span>
            {subExpanded ? (
              <ChevronDown16Regular aria-hidden="true" className="shrink-0 text-gray-400" style={{ fontSize: '13px' }} />
            ) : (
              <ChevronRight16Regular aria-hidden="true" className="shrink-0 text-gray-400" style={{ fontSize: '13px' }} />
            )}
          </button>
          <div className="fluent-accordion" data-expanded={subExpanded}>
            <div className="fluent-accordion-content">
              {/* The guide rail: one line the steps hang off, with a state circle
                  per step sitting on it.  The circle is offset by the rail's own
                  inset, so it stays centred on the line at any text size. */}
              <div className="border-l border-line ml-3.5 pl-3 space-y-2 pb-2 pr-2.5">
                {node.tools.map((t) => {
                  // The step's own outcome, so the circle can carry it at a glance;
                  // the row's badge still prints the word beside it.
                  const stepRunning = t.status === 'running' || t.status === 'pending';
                  const stepFailed = t.error || t.status === 'failed';
                  return (
                    <div key={t.id} className="relative">
                      <span
                        aria-hidden="true"
                        className={"absolute -left-[19px] top-1.5 flex h-3.5 w-3.5 items-center justify-center rounded-full " + (stepFailed ? "bg-red-100 text-red-700" : stepRunning ? "bg-blue-50 text-blue-500" : "bg-green-100 text-green-700")}
                      >
                        {stepFailed ? (
                          <DismissCircle20Regular aria-hidden="true" style={{ fontSize: '10px' }} />
                        ) : stepRunning ? (
                          <SpinnerIos20Regular aria-hidden="true" className="animate-spin" style={{ fontSize: '10px' }} />
                        ) : (
                          <Checkmark16Regular aria-hidden="true" style={{ fontSize: '10px' }} />
                        )}
                      </span>
                      {renderToolRow(t)}
                    </div>
                  );
                })}
                {node.tools.length === 0 && (
                  <div className="font-mono text-[10px] text-gray-400">等待子代理步骤…</div>
                )}
              </div>
            </div>
          </div>
        </div>
      );
    };
    return (
      <div key={m.id} className="max-w-[85%] py-1">
        {processMeta && !processMeta.isExpanded ? (
          <div className="transcript-fold-header">
            <div className="flex items-center gap-2 py-1 min-w-0">
              <button
                type="button"
                onClick={processMeta.onToggleExpand}
                className="flex items-center gap-1 text-xs text-gray-500 hover:text-gray-700 py-1 cursor-pointer select-none font-sans transition-colors"
              >
                <span>已工作 {processMeta.totalDurationText}</span>
                <ChevronRight16Regular aria-hidden="true" style={{ fontSize: '13px' }} />
              </button>
              {processMeta.groupStatus && <FoldStatusPill status={processMeta.groupStatus} />}
            </div>
            <div className="border-b border-line/60 my-2.5" />
          </div>
        ) : (
          <>
            {processMeta && processMeta.isFirst && (
              <div className="transcript-fold-header">
                <div className="flex items-center gap-2 py-1 min-w-0">
                  <button
                    type="button"
                    onClick={processMeta.onToggleExpand}
                    className="flex items-center gap-1 text-xs text-gray-500 hover:text-gray-700 py-1 cursor-pointer select-none font-sans transition-colors"
                  >
                    <span>已工作 {processMeta.totalDurationText}</span>
                    <ChevronDown16Regular aria-hidden="true" style={{ fontSize: '13px' }} />
                  </button>
                  {processMeta.groupStatus && <FoldStatusPill status={processMeta.groupStatus} />}
                </div>
                {/* Same header rule as the thought fold: the tool rows hang below it,
                    and the block ends without a second rule.  The header stays in flow
                    with the tool rows it names. */}
                <div className="border-b border-line/60 my-2.5" />
              </div>
            )}
            <div
              onClick={() => handleToggleExpand(m.id)}
              title={expanded ? '收起工具详情' : '展开工具详情'}
              onMouseMove={updateSpotlight}
              className="inline-flex cursor-pointer select-none items-center gap-1.5 rounded-control border border-line bg-surface px-2.5 py-1 font-mono text-xs text-gray-600 transition-colors hover:bg-surface-hover hover:text-gray-900 active:bg-surface-pressed fluent-spotlight"
            >
              {failed > 0 ? (
                <DismissCircle20Regular aria-hidden="true" className="shrink-0 text-red-500" style={{ fontSize: '14px' }} />
              ) : running > 0 ? (
                <SpinnerIos20Regular aria-hidden="true" className="shrink-0 animate-spin text-blue-500" style={{ fontSize: '14px' }} />
              ) : (
                <Wrench20Regular aria-hidden="true" className="shrink-0 text-gray-500" style={{ fontSize: '14px' }} />
              )}
              <span>{toolGroupLabel(toolList.length, m.parallel === true)}</span>
              {running > 0 && (
                <span className="font-medium text-blue-500">{running} running</span>
              )}
              {failed > 0 && <span className="font-medium text-red-600">{failed} failed</span>}
              {!expanded && toolList.length > 0 && (
                <span className="truncate text-gray-400">
                  {toolList.slice(0, 4).map((t) => t.name).join(' · ')}
                  {toolList.length > 4 ? ' +' + (toolList.length - 4) : ''}
                </span>
              )}
              <span className="text-gray-400">{expandHint(expanded)}</span>
            </div>
            <div className="fluent-accordion" data-expanded={expanded}>
              <div className="fluent-accordion-content pt-1.5 space-y-1.5">
                {toolNodes.map((node: ToolRenderNode, index) =>
                  node.type === 'subagent' ? (
                    renderSubagentCard(
                      node,
                      node.parent.id || node.parent.callId || `${node.subagentName}-${index}`,
                    )
                  ) : (
                    renderToolRow(node.tool)
                  ),
                )}
              </div>
            </div>
          </>
        )}
      </div>
    );
  }
  if (m.type === 'assistant') {
    return (
      <div key={m.id} className="flex max-w-[80%] flex-col items-start gap-1.5">
        <div className="text-base leading-relaxed font-sans text-gray-900">
          <Markdown text={m.content ?? ''} />
        </div>
        <div className="flex items-center gap-1.5 pt-0.5">
          <button
            type="button"
            onClick={() => handleCopy(m.content ?? '')}
            title={copied ? '已复制' : '复制回答'}
            aria-label="复制回答"
            className="p-1 rounded text-gray-400 hover:text-gray-700 hover:bg-surface-hover cursor-pointer transition-colors"
          >
            {copied ? <Checkmark16Regular aria-hidden="true" className="text-accent" /> : <Copy16Regular aria-hidden="true" />}
          </button>
          <span className="font-mono text-[10px] text-gray-400">{m.timestamp}</span>
        </div>
      </div>
    );
  }
  if (m.type === 'info') {
    const warning = m.infoLevel === 'warning';
    return (
      <div
        key={m.id}
        className={`flex max-w-[85%] items-start gap-1.5 rounded-control border px-2.5 py-1.5 font-mono text-xs leading-relaxed ${
          warning
            ? 'border-amber-200 bg-amber-50 text-amber-800'
            : 'border-line bg-surface text-gray-600'
        }`}
      >
        {warning ? (
          <Warning20Regular aria-hidden="true" className="shrink-0 text-amber-600" style={{ fontSize: '14px' }} />
        ) : (
          <Info20Regular aria-hidden="true" className="shrink-0 text-blue-500" style={{ fontSize: '14px' }} />
        )}
        <span className="whitespace-pre-wrap break-all">{m.content}</span>
      </div>
    );
  }
  return null;
});

/**
 * What the runtime reports it is doing right now (`activity_*` events).
 *
 * Only an *active* status is painted: `activity_stopped` keeps the last phase so the
 * next one can inherit its timer, and a finished turn must not keep claiming it is
 * still working.  It is the line the transcript holds under a running turn, and the
 * only thing the pending header can report once the reader opens it -- a turn that
 * has not produced a step yet has nothing else to show.
 */
function ActivityLine({ activity }: { activity: ActivityView | null }) {
  if (activity === null || !activity.active) return null;
  return (
    <div className="flex select-none items-center gap-1.5 font-mono text-xs text-gray-500">
      <span className="h-1.5 w-1.5 rounded-full bg-blue-600 animate-pulse" />
      <span>{activity.phase}</span>
      {activity.detail && <span className="text-gray-400">{activity.detail}</span>}
    </div>
  );
}

/**
 * What a fold group is doing, printed beside the header chevron.
 *
 * The fold header is the only line of a collapsed group, so this is where the
 * reader learns whether the turn is reasoning, running a tool or done -- without
 * opening the fold.  The icon and the tint carry the state (running / completed /
 * failed); the label carries the tool and its intent, truncated so one long intent
 * cannot push the elapsed time out of the header.
 */
function FoldStatusPill({ status }: { status: GroupIntentStatus }) {
  const badge = 'inline-flex shrink-0 items-center gap-1 rounded-control border px-1.5 py-0.5 font-mono text-xs';
  if (status.kind === 'thinking') {
    return status.state === 'running' ? (
      <span className={`${badge} border-blue-200/50 bg-blue-50/80 text-blue-600`}>
        <Sparkle20Regular aria-hidden="true" className="shrink-0 animate-pulse" style={{ fontSize: '12px' }} />
        <span>{status.text}</span>
      </span>
    ) : (
      <span className={`${badge} border-line bg-sunken/60 text-gray-500`}>
        <BrainCircuit20Regular aria-hidden="true" className="shrink-0" style={{ fontSize: '12px' }} />
        <span>{status.text}</span>
      </span>
    );
  }
  if (status.state === 'running') {
    return (
      <span className={`${badge} max-w-[24rem] border-blue-200 bg-blue-50/90 text-blue-700`}>
        <SpinnerIos20Regular aria-hidden="true" className="shrink-0 animate-spin text-blue-600" style={{ fontSize: '12px' }} />
        <span className="truncate" title={status.text}>{status.text}</span>
      </span>
    );
  }
  if (status.state === 'failed') {
    return (
      <span className={`${badge} max-w-[24rem] border-red-200/60 bg-red-50/80 text-red-600`}>
        <DismissCircle20Regular aria-hidden="true" className="shrink-0 text-red-500" style={{ fontSize: '12px' }} />
        <span className="truncate" title={status.text}>{status.text}</span>
      </span>
    );
  }
  return (
    <span className={`${badge} max-w-[24rem] border-line bg-sunken/60 text-gray-600`}>
      <Checkmark16Regular aria-hidden="true" className="shrink-0 text-green-600" style={{ fontSize: '12px' }} />
      <span className="truncate" title={status.text}>{status.text}</span>
    </span>
  );
}

/**
 * The "已工作" header of a turn whose own rows have not landed yet.
 *
 * A turn paints that header -- and the rule under it -- from its first thought /
 * tool row, so between the submit and that first row the left column would be
 * empty.  This row stands in with the same header and the same rule, so the
 * elapsed time is on screen from the moment the message is sent; it stays for as
 * long as the turn has no process row (a plain question and answer never grows
 * one), which is what keeps the final elapsed time visible after the turn ends.
 *
 * The rule under the header is the stopwatch's own separator, so it is drawn
 * directly under the button in both folds; what the reader opens (the running
 * status, once there is one to report) hangs *below* that rule.  Button and rule
 * are one strip (`transcript-fold-header`), left in flow with the status it
 * reports: the row is a line of the reading column, never a bar over it.
 */
function PendingTurnRow({
  turnKey,
  text,
  expanded,
  group,
  activity,
  onToggleExpand,
}: {
  turnKey: string;
  text: string;
  expanded: boolean;
  group: WorkGroup;
  activity: ActivityView | null;
  onToggleExpand: (turnKey: string) => void;
}) {
  const pendingStatus = getGroupIntentStatus(group, activity);
  return (
    // The process rows bound the assistant column to 85% of its width; the same
    // bound is a width here, so the rule under this header is exactly as long as the
    // one the first thought row draws.
    <div className="w-[85%]">
      <div className="transcript-fold-header">
        <div className="flex items-center gap-2 py-1 min-w-0">
          <button
            type="button"
            onClick={() => onToggleExpand(turnKey)}
            className="flex items-center gap-1 text-xs text-gray-500 hover:text-gray-700 py-1 cursor-pointer select-none font-sans transition-colors"
          >
            <span>已工作 {text}</span>
            {expanded ? (
              <ChevronDown16Regular aria-hidden="true" style={{ fontSize: '13px' }} />
            ) : (
              <ChevronRight16Regular aria-hidden="true" style={{ fontSize: '13px' }} />
            )}
          </button>
          {pendingStatus && <FoldStatusPill status={pendingStatus} />}
        </div>
        <div className="border-b border-line/60 my-2.5" />
      </div>
      {expanded && <ActivityLine activity={activity} />}
    </div>
  );
}

export const Transcript: React.FC = () => {
  const {
    messages,
    activity,
    runtimeStatus,
    activeTurnId,
    toggleWorkExpand,
    toggleMessageExpand,
    pendingApproval,
    resolveApproval,
    historyLoading,
    historyHasMore,
    historyAvailable,
    historyError,
    loadEarlierHistory,
  } = useConsoleStore(
    // Only the fields this column paints: an activity tick or a usage update must
    // not re-render the transcript.
    useShallow((state) => ({
      messages: state.messages,
      activity: state.activity,
      runtimeStatus: state.runtimeStatus,
      toggleMessageExpand: state.toggleMessageExpand,
      activeTurnId: state.activeTurnId,
      toggleWorkExpand: state.toggleWorkExpand,
      pendingApproval: state.pendingApproval,
      resolveApproval: state.resolveApproval,
      historyLoading: state.historyLoading,
      historyHasMore: state.historyHasMore,
      historyAvailable: state.historyAvailable,
      historyError: state.historyError,
      loadEarlierHistory: state.loadEarlierHistory,
    })),
  );
  const scrollerRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const skipAutoScroll = useRef(false);

  const toggleTurnExpanded = useCallback((turnKey: string) => {
    skipAutoScroll.current = true;
    toggleWorkExpand(turnKey);
  }, [toggleWorkExpand]);
  const groups = useMemo(
    () => workGroups(messages, activeTurnId, runtimeStatus === 'running'),
    [messages, activeTurnId, runtimeStatus],
  );
  const runningTurnKey = groups.find((group) => group.running)?.key ?? null;
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (runningTurnKey === null) return;
    const read = () => setNow(Date.now());
    read();
    const timer = window.setInterval(read, 1000);
    return () => window.clearInterval(timer);
  }, [runningTurnKey]);

  const pendingTurns = useMemo(() => new Map(
    groups.filter((g) => g.rows.length === 0 && (g.anchor.work || g.running))
      .map((g) => [g.anchor.id, g]),
  ), [groups]);
  const processMetaMap = useMemo(() => {
    const map = new Map<string, {
      isFirst: boolean;
      isExpanded: boolean;
      totalDurationText: string;
      turnKey: string;
      groupStatus: GroupIntentStatus | null;
    }>();
    for (const group of groups) {
      const groupStatus = getGroupIntentStatus(group, activity);
      group.rows.forEach((row, i) => {
        map.set(row.id, {
          isFirst: i === 0,
          isExpanded: group.anchor.workExpanded === true,
          totalDurationText: formatWorkDuration(workSeconds(group, now)),
          turnKey: group.anchor.id,
          groupStatus,
        });
      });
    }
    return map;
  }, [groups, now, activity]);
  /**
   * Whether the view is following the newest content.
   *
   * Following is the reader's choice: a stream that scrolls on every update makes
   * reading back through a running turn impossible, so only a view that is
   * already at the bottom follows.  Submitting a prompt is an explicit intent, so
   * that always follows.
   */
  const pinnedToBottom = useRef(true);
  /**
   * True while a wheel / touch gesture is driving the scroller.
   *
   * Only the reader may end the follow.  Our own `scrollIntoView` and the
   * browser's scroll anchoring also fire `scroll`, and a layout change *above* the
   * viewport (a streamed thought settling to its final height, a tool row
   * appearing) moves the scroll position on its own: reading the latch from every
   * scroll event ended the follow for the rest of the turn, so the reasoning
   * streamed into view but the tool call after it did not.
   */
  const userScrolling = useRef(false);

  // Stable identity: the scroll listener below triggers the same guarded path the
  // button uses, and a changing callback would re-attach that listener.
  const handleLoadEarlier = useCallback(() => {
    skipAutoScroll.current = true;
    loadEarlierHistory();
  }, [loadEarlierHistory]);

  useEffect(() => {
    const scroller = scrollerRef.current;
    if (scroller === null) return;
    const distance = (): number =>
      scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
    const track = () => {
      if (!userScrolling.current) return;
      pinnedToBottom.current = distance() <= PINNED_TO_BOTTOM_PX;
    };
    let settle: ReturnType<typeof setTimeout> | null = null;
    const beginUserScroll = () => {
      userScrolling.current = true;
      if (settle !== null) clearTimeout(settle);
      // A gesture is over shortly after its last event; the latch is then final
      // until the next one.  A gesture that ended at the bottom re-arms the
      // follow, which is how scrolling back down resumes it.
      settle = setTimeout(() => {
        settle = null;
        pinnedToBottom.current = distance() <= PINNED_TO_BOTTOM_PX;
        userScrolling.current = false;
      }, USER_SCROLL_SETTLE_MS);
    };
    /**
     * Load the next page of earlier history once the reader reaches the top.
     *
     * The gates are read from the store rather than closed over, so the listener
     * never acts on a stale `historyHasMore` / `historyLoading`; the store refuses
     * a load that is already running or exhausted anyway.
     */
    const loadEarlierAtTop = () => {
      if (scroller.scrollTop > EARLIER_HISTORY_TRIGGER_PX) return;
      const { historyHasMore, historyLoading } = useConsoleStore.getState();
      if (!historyHasMore || historyLoading) return;
      handleLoadEarlier();
    };
    track();
    scroller.addEventListener('wheel', beginUserScroll, { passive: true });
    scroller.addEventListener('touchstart', beginUserScroll, { passive: true });
    scroller.addEventListener('touchmove', beginUserScroll, { passive: true });
    scroller.addEventListener('scroll', track, { passive: true });
    scroller.addEventListener('scroll', loadEarlierAtTop, { passive: true });
    return () => {
      if (settle !== null) clearTimeout(settle);
      scroller.removeEventListener('wheel', beginUserScroll);
      scroller.removeEventListener('touchstart', beginUserScroll);
      scroller.removeEventListener('touchmove', beginUserScroll);
      scroller.removeEventListener('scroll', track);
      scroller.removeEventListener('scroll', loadEarlierAtTop);
    };
  }, [handleLoadEarlier]);

  useEffect(() => {
    if (skipAutoScroll.current) {
      // Prepending an earlier history page must not yank the view back to bottom.
      skipAutoScroll.current = false;
      return;
    }
    const newest = messages[messages.length - 1];
    if (!pinnedToBottom.current && newest?.type !== 'user') return;
    // Instant rather than smooth: while a turn streams, the target moves every
    // few milliseconds, so a smooth animation is restarted (and never finishes)
    // hundreds of times over a single thought.
    bottomRef.current?.scrollIntoView({ block: 'end' });
    // The view is at the bottom now, whatever moved it there in between.
    pinnedToBottom.current = true;
  }, [messages]);

  useEffect(() => {
    const handleJumpBottom = () => {
      pinnedToBottom.current = true;
    };
    window.addEventListener('transcript:jump-bottom', handleJumpBottom);
    return () => window.removeEventListener('transcript:jump-bottom', handleJumpBottom);
  }, []);

  /**
   * Expand/collapse one fold.
   *
   * This is a *view* change, not new content, but the store replaces the
   * `messages` array to flip the flag — and the auto-scroll effect keys off that
   * array identity.  Without the same guard the "load earlier" path uses, opening
   * a fold yanked the transcript to the bottom, so it never appeared to open in
   * place.
   */
  // Stable identity: a row that re-renders because this callback changed would
  // defeat the row-level memo below.
  const handleToggleExpand = useCallback(
    (id: string) => {
      skipAutoScroll.current = true;
      toggleMessageExpand(id);
    },
    [toggleMessageExpand],
  );

  return (
    <>
      {/* Minimap of the transcript, centred on the left edge (see TurnRail). */}
      <TurnRail />
      {/* Floating progress panel for the session's todo list (hidden until one
          exists). */}
      <TodoPanel />
    {/* `.no-scrollbar` keeps the reading column centred in the *pane* rather than
        in the pane minus a one-sided scrollbar, which is what makes it line up
        with the composer's `console-column` below. */}
    <div
      ref={scrollerRef}
      // No bottom padding for a floating composer: the composer is a sibling row,
      // so the scrollport's bottom edge is the last visible line.
      // `.no-scrollbar`: the wheel, touch and the keyboard still scroll it, but no
      // scrollbar takes a bite out of the reading column, so its edges line up
      // with the composer card's (the sidebar tree works the same way).
      className="console-gutter no-scrollbar console-pane-inset flex-1 overflow-y-auto font-sans"
    >
      <div className="console-column space-y-5">
        {historyAvailable === false && (
          <div className="rounded-card border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800 font-mono leading-relaxed">
            此会话在 transcript 投影中不可用（history.available=false）。以下只显示建立连接后的实时内容；
            不按“空历史”显示，也不会回退到 checkpoint。
          </div>
        )}

        {historyError !== null && (
          <div
            role="alert"
            className="rounded-card border border-red-200 bg-red-50 p-3 text-xs text-red-800 font-mono leading-relaxed"
          >
            {historyError}
            以下只显示建立连接后的实时内容，不回退到 checkpoint。
          </div>
        )}

        {historyHasMore && messages.length > 0 && (
          <div className="flex justify-center pt-1">
            {/* Reaching the top loads this page on its own (see the scroll
                listener); the button stays as the manual path and as the "there is
                more" hint. */}
            <button
              onClick={handleLoadEarlier}
              disabled={historyLoading}
              className="inline-flex h-8 cursor-pointer select-none items-center space-x-1.5 rounded-control border border-line bg-surface px-3 text-xs font-mono text-gray-600 transition-colors hover:bg-surface-hover active:bg-surface-pressed disabled:cursor-not-allowed disabled:text-fg-disabled"
            >
              <ArrowSort20Regular aria-hidden="true" className="shrink-0" style={{ fontSize: '14px' }} />
              <span>{historyLoading ? '加载更早历史中…' : '加载更早历史'}</span>
            </button>
          </div>
        )}

        {historyLoading && messages.length === 0 && (
          <div className="py-6 text-center text-gray-400 text-xs font-mono select-none">正在加载历史…</div>
        )}

        {messages.length === 0 &&
          !historyLoading &&
          historyAvailable !== false &&
          historyError === null && (
          <div className="py-16 text-center text-gray-400 text-xs font-mono select-none">
            当前会话已建立长连接，在下方输入指令即可开始与 Synapse Agent 对话
          </div>
        )}
        {messages.map((m) => {
          const meta = processMetaMap.get(m.id);
          const pending = pendingTurns.get(m.id);
          return (
            <React.Fragment key={m.id}>
              <TranscriptRow
                message={m}
                handleToggleExpand={handleToggleExpand}
                processMeta={
                  meta
                    ? {
                        isFirst: meta.isFirst,
                        isExpanded: meta.isExpanded,
                        totalDurationText: meta.totalDurationText,
                        groupStatus: meta.groupStatus,
                        onToggleExpand: () => toggleTurnExpanded(meta.turnKey),
                      }
                    : undefined
                }
              />
              {pending && (
                <PendingTurnRow
                  turnKey={m.id}
                  text={formatWorkDuration(workSeconds(pending, now))}
                  expanded={m.workExpanded === true}
                  group={pending}
                  activity={pending.running ? activity : null}
                  onToggleExpand={toggleTurnExpanded}
                />
              )}
            </React.Fragment>
          );
        })}

        {/* HITL Pending Approval Dialog */}
        {pendingApproval && (
          <div className="space-y-3 rounded-card border border-amber-200 bg-amber-50 p-4 shadow-card">
            <div className="flex items-center space-x-2 text-amber-800 font-medium text-xs">
              <Shield20Regular aria-hidden="true" className="shrink-0" style={{ fontSize: '18px' }} />
              <span>需要审批危险操作 (Turn: {pendingApproval.turn_id})</span>
            </div>
            <div className="space-y-1.5 font-mono text-xs text-gray-700">
              {pendingApproval.actions.map((act, idx) => (
                <div key={idx} className="rounded-control border border-amber-200 bg-surface p-2">
                  <div className="font-bold text-gray-900">{act.name}</div>
                  <div className="truncate text-xs text-gray-600">{JSON.stringify(act.args)}</div>
                </div>
              ))}
            </div>
            <div className="flex space-x-2 pt-1">
              <button
                onClick={() => resolveApproval('allow_once')}
                className="h-8 cursor-pointer rounded-control bg-accent px-3 text-xs font-medium text-on-accent transition-colors hover:bg-blue-700"
              >
                批准本次
              </button>
              <button
                onClick={() => resolveApproval('reject_once')}
                className="h-8 cursor-pointer rounded-control bg-red-500 px-3 text-xs font-medium text-on-accent transition-colors hover:bg-red-700"
              >
                拒绝
              </button>
            </div>
          </div>
        )}
        {/* The running status, as the fallback for a turn no row of its own can
            carry it: a turn whose header is owned by a thought / tool row, or
            activity that arrived before any turn at all.  A pending row prints the
            same line inside its own fold, so painting it here as well put "model
            waiting for model" on screen twice. */}
        {activity !== null && ![...pendingTurns.values()].some((g) => g.running) && <ActivityLine activity={activity} />}
        <div ref={bottomRef} />
      </div>
    </div>
    </>
  );
};
