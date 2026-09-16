import {
  Bot20Regular,
  Checkmark16Regular,
  ChevronDown16Regular,
  ChevronRight16Regular,
  DismissCircle20Regular,
  SpinnerIos20Regular,
  WindowConsole20Regular,
  Wrench20Regular,
} from '@fluentui/react-icons';
import React from 'react';
import type { ToolItemView } from '../../stores/historyMapper.ts';
import {
  groupToolsForView,
  isTerminalTool,
  toolCommand,
  toolFailureReason,
  toolPreviewLanguage,
  toolStatusLabel,
  type SubagentToolGroup,
  type ToolRenderNode,
} from '../../stores/transcriptLabels.ts';
import { CodeBlock } from '../CodeBlock.tsx';
import { TerminalOutput } from '../TerminalOutput.tsx';
import { FoldStatusPill } from './FoldStatusPill.tsx';
import type { RowRenderProps } from './context.ts';
import { updateSpotlight } from './spotlight.ts';

/**
 * One tool batch: a run of call rows, with a subagent's own steps nested under the
 * call that started it.
 *
 * The batch is a *data* boundary, not a visual one -- the wire sends it flat and the
 * runtime brackets each model step with `tool_batch_started/finished` -- so its calls
 * are painted as sibling rows rather than inside a group of their own, and a subagent
 * gets a card because its steps are a fold of their own inside the batch.
 *
 * Like the thought row, the batch carries the turn's "已工作" header when the fold says
 * it is first.
 */
export const ToolGroupRow = React.memo(function ToolGroupRow({
  message,
  toolExpansions,
  subagentExpansions,
  processMeta,
  actions,
}: RowRenderProps) {
  const toolList = message.tools || [];
  // The batch is flat on the wire; this is where a subagent's own steps are put back
  // under the call that started it, so they can be painted as one card instead of as
  // N more rows of the main agent's list.
  const toolNodes = groupToolsForView(toolList);

  /**
   * One tool row: the call's own name, the intent the model gave it and its body.
   * Shared by a main-agent row and by a subagent step, so a nested call reads exactly
   * like a top-level one.
   *
   * A batch is a data boundary, not a visual one: its calls are painted as sibling
   * rows, so what ran is on screen without opening a group first.
   */
  const renderToolRow = (t: ToolItemView, nested = false) => {
    // A live batch first identifies a call by call_id and later replaces it with the
    // item_id from tool_started.  Prefer call_id so the local fold does not close
    // while that lifecycle update arrives.
    const toolKey = t.callId || t.id;
    const toolExpanded = toolExpansions[toolKey] === true;
    const intent = t.label && t.label !== t.name ? t.label : '';
    // A run tool's detail is its terminal session: the invocation, then what the
    // program wrote.  The invocation is known before the output is, so a call that is
    // still running can be opened to read what it is running.
    const terminal = isTerminalTool(t.name);
    const command = terminal ? toolCommand(t) : '';
    // A failure's reason belongs to the detail, not to the row: the row only turns
    // red.  It is skipped when the body already opens with it, because the runtime's
    // summary is the body's own first line.
    const reason = toolFailureReason(t.status, t.error);
    const reasonShown = reason !== '' && !(t.preview ?? '').trimStart().startsWith(reason);
    // A row opens when it has a body -- or, for a run tool, an invocation.
    const hasDetail = Boolean(t.preview) || command !== '' || reasonShown;
    // A call in flight is shown, not spelled out: the leading spinner carries "still
    // working" the way a terminal's cursor does.
    const active = t.status === 'running' || t.status === 'pending';
    // A failure is colour, not copy: the row and the call's name turn red.  The
    // runtime's status is not a state to print either way -- a success carries a body
    // digest ("ok (48 chars, 2 lines)") and a failure its reason.  Only a cancellation,
    // which is neither success nor failure, keeps its word, and a subagent's own phase
    // is never dropped.
    const statusText = t.status === 'cancelled' || t.status === 'canceled'
      ? [toolStatusLabel(t.status), t.subagentStatus].filter(Boolean).join(' · ')
      : (t.subagentStatus ?? '');
    const previewLang = toolExpanded && t.preview && !terminal
      ? toolPreviewLanguage(t.name, t.path, t.preview)
      : '';
    return (
      // A run-log line, not a card: the batch it belongs to is not a container on
      // screen, so a border and a fill per call would give the log the same visual
      // weight as the answer it is subordinate to.
      <div key={t.id} className="group">
        <button
          type="button"
          onClick={() => actions.onToggleTool(message.id, toolKey, hasDetail)}
          aria-expanded={hasDetail ? toolExpanded : undefined}
          title={hasDetail ? (toolExpanded ? '收起工具详情' : '展开工具详情') : undefined}
          onMouseMove={updateSpotlight}
          className={"flex w-full min-w-0 cursor-pointer select-none items-center gap-2 rounded-control px-2 py-1 text-left font-mono text-xs transition-colors hover:bg-surface-hover active:bg-surface-pressed fluent-spotlight " + (t.error ? "text-red-600" : "text-gray-600")}
        >
          {/* A nested step's activity is already on its card's rail, so the row must
              not animate a second time beside it. */}
          {active && !nested && (
            <SpinnerIos20Regular aria-hidden="true" className="shrink-0 animate-spin text-blue-500" style={{ fontSize: '12px' }} />
          )}
          {t.name === 'execute' ? (
            <WindowConsole20Regular aria-hidden="true" className="shrink-0 text-gray-400" style={{ fontSize: '13px' }} />
          ) : (
            <Wrench20Regular aria-hidden="true" className="shrink-0 text-gray-400" style={{ fontSize: '13px' }} />
          )}
          <span className={"shrink-0 font-medium " + (t.error ? "text-red-700" : "text-gray-900")}>{t.name}</span>
          {intent !== '' && (
            <span className="min-w-0 truncate text-gray-500" title={intent}>
              {intent}
            </span>
          )}
          {t.path && (
            <span className="min-w-0 truncate text-gray-400" title={t.path}>
              · {t.path}
            </span>
          )}
          {t.sub && (
            <span className="shrink-0 rounded-control bg-sunken px-1 text-[10px] text-gray-500">
              sub
            </span>
          )}
          {t.subagentName && (
            <span className="shrink-0 text-[10px] text-gray-400">@{t.subagentName}</span>
          )}
          {/* The row's own words stay next to the content they describe: a
              right-aligned tail would put them -- and the fold's chevron -- at a fixed
              end position that says nothing about this call. */}
          <span className="flex min-w-0 items-center gap-2">
            {statusText !== '' && (
              <span className="min-w-0 truncate text-[10px] text-gray-500" title={statusText}>
                {statusText}
              </span>
            )}
            {hasDetail && (
              // The fold stays quiet until the row is pointed at or focused: the
              // detail is there for a reader who asks for it, not an invitation
              // repeated on every line.
              <span className={"flex shrink-0 text-gray-400 " + (toolExpanded ? "opacity-100" : "opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100")}>
                {toolExpanded ? (
                  <ChevronDown16Regular aria-hidden="true" style={{ fontSize: '13px' }} />
                ) : (
                  <ChevronRight16Regular aria-hidden="true" style={{ fontSize: '13px' }} />
                )}
              </span>
            )}
          </span>
        </button>
        {toolExpanded && (
          <div className="ml-4 border-l border-line pb-1.5 pl-2.5 pr-2 pt-1">
            {/* Result bodies can be large or expensive to parse, so they are mounted
                only for the individual call the reader opened. */}
            {reasonShown && (
              <div className="whitespace-pre-wrap break-all font-mono text-[12px] leading-5 text-red-600">
                {reason}
              </div>
            )}
            {terminal && (t.preview || command !== '') ? (
              <TerminalOutput text={t.preview ?? ''} command={command} />
            ) : previewLang !== '' && t.preview ? (
              <CodeBlock
                lang={previewLang}
                code={t.preview}
              />
            ) : (
              t.preview && (
                <div className="whitespace-pre-wrap break-all text-gray-600">
                  {t.preview}
                </div>
              )
            )}
          </div>
        )}
      </div>
    );
  };

  /**
   * One subagent: the call that started it, the goal it was given, and its own steps
   * behind a rail.  The rail is what makes the nesting readable at a glance -- a step
   * hangs off the card, not off the main agent's list.
   */
  const renderSubagentCard = (node: SubagentToolGroup, key: string) => {
    const subExpanded = subagentExpansions[key] === true;
    const subRunning = node.parent.status === 'running'
      || node.tools.some((s) => s.status === 'running' || s.status === 'pending');
    // Only the task call itself decides the card's outcome: a subagent that recovered
    // from a failed step still completed, and its own tool errors are counted on the
    // step line instead of painting the whole card red.
    const parentFailed = node.parent.error || node.parent.status === 'failed';
    const failedSteps = node.tools.filter((s) => s.error || s.status === 'failed').length;
    return (
      <div key={key} className="rounded-control border border-line bg-raised">
        {/* The raised fill equals the dark palette's hover step, so the header takes
            the pressed step to stay visible when it is hovered. */}
        <button
          type="button"
          onClick={() => actions.onToggleSubagent(message.id, key)}
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
        <div
          className="fluent-accordion"
          data-expanded={subExpanded}
          aria-hidden={!subExpanded}
          inert={!subExpanded}
        >
          <div className="fluent-accordion-content">
            {/* The guide rail: one line the steps hang off, with a state circle per
                step sitting on it.  The circle is offset by the rail's own inset, so
                it stays centred on the line at any text size. */}
            <div className="border-l border-line ml-3.5 pl-3 space-y-2 pb-2 pr-2.5">
              {node.tools.map((t) => {
                // The step's own outcome, so the circle can carry it at a glance; the
                // row therefore prints no state of its own while it runs.
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
                    {renderToolRow(t, true)}
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
    <div className="max-w-[85%]">
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
          <div className="space-y-0.5">
            {toolNodes.map((node: ToolRenderNode, index) =>
              node.type === 'subagent' ? (
                renderSubagentCard(
                  node,
                  node.parent.callId || node.parent.id || `${node.subagentName}-${index}`,
                )
              ) : (
                renderToolRow(node.tool)
              ),
            )}
          </div>
        </>
      )}
    </div>
  );
});
