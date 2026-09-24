/**
 * Trajectory & Model Execution Trace Tab for the Right Auxiliary Dock (inspired by ZCode).
 *
 * Implements:
 * - Summary Header: Total turns, model invocations, cumulative IN/OUT token breakdown, active model identifier
 * - Turn-based grouping: chronologically grouped by turn (Turn 1, Turn 2, ...) with expand/collapse support
 * - Pure icon button for expand all / collapse all (no text)
 * - Optimized rendering: only latest turn is expanded by default, memoized turn cards, safe truncated large text,
 *   deferred search query to guarantee 60fps responsiveness without mouse freezing
 * - ZCode Structured Inspection inside each turn:
 *   - User Prompt with full text and copy
 *   - Thinking trace (CoT) with performance safety guards
 *   - Tool execution groups with parameters, status, and previews
 *   - Final assistant message
 *   - Duration, timestamp, and status badges
 * - In-panel search filter across turns, tool names, parameters, and reasoning
 * - Fluent Design 2 compliance: cards, pills, smooth chevrons, and fluent-scrollbar
 */
import {
  History16Regular,
  Brain16Regular,
  Person16Regular,
  Bot16Regular,
  Wrench16Regular,
  ChevronDown16Regular,
  ChevronRight16Regular,
  Copy16Regular,
  Checkmark16Regular,
  Search16Regular,
  Sparkle16Regular,
  Timer16Regular,
  CheckmarkCircle16Regular,
  DismissCircle16Regular,
  ArrowCollapseAll16Regular,
  ArrowExpandAll16Regular,
} from '@fluentui/react-icons';
import React, { useMemo, useState, useCallback, useEffect, useDeferredValue } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { useConsoleStore } from '../../stores/useConsoleStore.ts';
import type { ToolItemView } from '../../stores/historyMapper.ts';
import type { RightDockContext, RightDockTabDefinition } from './contract.ts';
import { compactCount } from '../../stores/usageView.ts';

interface TrajectoryStep {
  index: number;
  id: string;
  turnId?: string;
  timestamp: string;
  title: string;
  kind: 'user' | 'thought' | 'tool' | 'assistant';
  status: 'completed' | 'running' | 'failed';
  duration?: string;
  userContent?: string;
  thoughtContent?: string;
  assistantContent?: string;
  tools?: ToolItemView[];
}

interface TurnGroup {
  key: string;
  turnIndex: number;
  turnId?: string;
  timestamp: string;
  userPrompt: string;
  steps: TrajectoryStep[];
  status: 'completed' | 'running' | 'failed';
  duration?: string;
  toolCallCount: number;
}

function formatStepIndex(num: number): string {
  return num < 10 ? `0${num}` : `${num}`;
}

/**
 * Truncated display for large text chunks (e.g. 50k char tool outputs or thoughts)
 * to avoid locking up browser layout & paint pipelines.
 */
const TruncatedText: React.FC<{
  text: string;
  maxChars?: number;
  className?: string;
  as?: 'p' | 'div' | 'pre';
}> = ({ text, maxChars = 2500, className, as = 'p' }) => {
  const [expanded, setExpanded] = useState(false);
  const needsTruncate = text.length > maxChars;

  const Tag = as;
  if (!needsTruncate) {
    return <Tag className={className}>{text}</Tag>;
  }

  const displayText = expanded ? text : `${text.slice(0, maxChars)}\n...`;

  return (
    <div>
      <Tag className={className}>{displayText}</Tag>
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="mt-1 text-[10px] text-accent hover:underline font-sans cursor-pointer select-none inline-block font-medium"
      >
        {expanded ? '收起部分内容' : `展开显示完整内容 (共 ${text.length} 字符)`}
      </button>
    </div>
  );
};

interface TurnCardProps {
  turn: TurnGroup;
  isExpanded: boolean;
  expandedSteps: Set<string>;
  copiedKey: string | null;
  onToggleTurn: (key: string) => void;
  onToggleStep: (id: string) => void;
  onCopyText: (key: string, text: string) => void;
}

const TurnCard: React.FC<TurnCardProps> = React.memo(({
  turn,
  isExpanded,
  expandedSteps,
  copiedKey,
  onToggleTurn,
  onToggleStep,
  onCopyText,
}) => {
  return (
    <div className="overflow-hidden rounded-control border border-line bg-surface shadow-xs transition-shadow hover:shadow-card">
      {/* Turn Header Card */}
      <div
        onClick={() => onToggleTurn(turn.key)}
        className={`flex cursor-pointer items-center justify-between px-3 py-2 transition-colors select-none ${
          isExpanded ? 'bg-sunken/60 border-b border-line/60' : 'hover:bg-surface-hover'
        }`}
      >
        <div className="flex min-w-0 items-center gap-2">
          <span className="text-gray-400">
            {isExpanded ? (
              <ChevronDown16Regular className="text-xs" />
            ) : (
              <ChevronRight16Regular className="text-xs" />
            )}
          </span>

          {/* Turn Badge */}
          <span className="rounded-control bg-accent/10 px-1.5 py-0.5 font-mono text-[10px] font-bold text-accent shrink-0">
            Turn {turn.turnIndex}
          </span>

          {/* User Prompt Summary */}
          <span className="truncate font-medium text-gray-800 text-xs max-w-[190px]" title={turn.userPrompt}>
            {turn.userPrompt}
          </span>

          {/* Step count badge */}
          <span className="rounded-full bg-sunken px-1.5 py-0.2 font-mono text-[9px] text-gray-500 shrink-0">
            {turn.steps.length} 步
          </span>
        </div>

        {/* Telemetry pill */}
        <div className="flex shrink-0 items-center gap-2 font-mono text-[10px] text-gray-400">
          {turn.duration && (
            <span className="flex items-center gap-0.5 text-gray-500">
              <Timer16Regular className="text-[10px]" />
              {turn.duration}
            </span>
          )}
          {turn.status === 'failed' ? (
            <DismissCircle16Regular className="text-red-500 text-xs" title="本轮有失败或错误" />
          ) : (
            <CheckmarkCircle16Regular className="text-emerald-500 text-xs" title="已完成" />
          )}
          <span>{turn.timestamp}</span>
        </div>
      </div>

      {/* Turn Content: List of Steps */}
      {isExpanded && (
        <div className="p-2 space-y-1.5 bg-canvas/30">
          {turn.steps.map((step) => {
            const isStepExpanded = expandedSteps.has(step.id);

            return (
              <div
                key={step.id}
                className="overflow-hidden rounded-control border border-line/70 bg-surface shadow-xs"
              >
                {/* Step Header */}
                <div
                  onClick={() => onToggleStep(step.id)}
                  className={`flex cursor-pointer items-center justify-between px-2.5 py-1.5 transition-colors select-none ${
                    isStepExpanded ? 'bg-sunken/40 border-b border-line/40' : 'hover:bg-surface-hover'
                  }`}
                >
                  <div className="flex min-w-0 items-center gap-2">
                    <span className="font-mono text-[10px] font-bold text-gray-400">
                      #{formatStepIndex(step.index)}
                    </span>

                    <span className="text-gray-400">
                      {isStepExpanded ? (
                        <ChevronDown16Regular className="text-xs" />
                      ) : (
                        <ChevronRight16Regular className="text-xs" />
                      )}
                    </span>

                    <div className="flex items-center gap-1.5 truncate">
                      {step.kind === 'user' && <Person16Regular className="text-blue-600 shrink-0 text-xs" />}
                      {step.kind === 'thought' && <Brain16Regular className="text-purple-600 shrink-0 text-xs" />}
                      {step.kind === 'tool' && <Wrench16Regular className="text-amber-600 shrink-0 text-xs" />}
                      {step.kind === 'assistant' && <Bot16Regular className="text-emerald-600 shrink-0 text-xs" />}

                      <span className="font-medium text-gray-800 truncate text-[11px]">{step.title}</span>
                    </div>
                  </div>

                  <div className="flex shrink-0 items-center gap-1.5 font-mono text-[10px] text-gray-400">
                    {step.duration && (
                      <span className="flex items-center gap-0.5 text-gray-500">
                        <Timer16Regular className="text-[9px]" />
                        {step.duration}
                      </span>
                    )}
                    <span>{step.timestamp}</span>
                  </div>
                </div>

                {/* Expanded Step Details */}
                {isStepExpanded && (
                  <div className="p-2 space-y-2 text-xs bg-surface/50">
                    {/* User message block */}
                    {step.userContent && (
                      <div className="rounded-control border border-line bg-surface p-2.5">
                        <div className="flex items-center justify-between pb-1.5 border-b border-line/40 text-[11px] font-semibold text-gray-600">
                          <span>用户提示</span>
                          <button
                            type="button"
                            onClick={() => onCopyText(`user-${step.id}`, step.userContent || '')}
                            title="复制内容"
                            className="flex items-center gap-1 text-gray-400 hover:text-gray-700"
                          >
                            {copiedKey === `user-${step.id}` ? (
                              <Checkmark16Regular className="text-emerald-600 text-xs" />
                            ) : (
                              <Copy16Regular className="text-xs" />
                            )}
                            <span className="text-[10px]">复制</span>
                          </button>
                        </div>
                        <TruncatedText
                          text={step.userContent}
                          className="mt-1.5 whitespace-pre-wrap font-sans text-gray-800 select-text leading-relaxed"
                        />
                      </div>
                    )}

                    {/* Thought process block */}
                    {step.thoughtContent && (
                      <div className="rounded-control border border-purple-200/60 bg-purple-50/30 p-2.5 dark:border-purple-900/40 dark:bg-purple-950/20">
                        <div className="flex items-center justify-between pb-1.5 border-b border-purple-200/40 text-[11px] font-semibold text-purple-700 dark:text-purple-300">
                          <span className="flex items-center gap-1">
                            <Sparkle16Regular className="text-xs" />
                            思考过程 (CoT)
                          </span>
                          <button
                            type="button"
                            onClick={() => onCopyText(`thought-${step.id}`, step.thoughtContent || '')}
                            title="复制思考过程"
                            className="flex items-center gap-1 text-purple-600/70 hover:text-purple-800"
                          >
                            {copiedKey === `thought-${step.id}` ? (
                              <Checkmark16Regular className="text-emerald-600 text-xs" />
                            ) : (
                              <Copy16Regular className="text-xs" />
                            )}
                            <span className="text-[10px]">复制</span>
                          </button>
                        </div>
                        <TruncatedText
                          text={step.thoughtContent}
                          className="mt-1.5 whitespace-pre-wrap font-mono text-[11px] text-gray-700 dark:text-gray-300 select-text leading-relaxed overflow-x-auto"
                        />
                      </div>
                    )}

                    {/* Tool calls block */}
                    {step.tools && step.tools.length > 0 && (
                      <div className="space-y-1.5">
                        {step.tools.map((tool, idx) => {
                          const argsText = tool.args
                            ? typeof tool.args === 'string'
                              ? tool.args
                              : JSON.stringify(tool.args, null, 2)
                            : '';
                          return (
                            <div key={tool.id || idx} className="rounded-control border border-line bg-surface p-2.5">
                              <div className="flex items-center justify-between pb-1.5 border-b border-line/40 text-[11px]">
                                <div className="flex items-center gap-1.5 font-semibold text-gray-700">
                                  <Wrench16Regular className="text-amber-600 text-xs" />
                                  <span className="font-mono">{tool.name}</span>
                                  {tool.path && (
                                    <span className="font-mono text-[10px] text-gray-400 font-normal">
                                      {tool.path}
                                    </span>
                                  )}
                                </div>
                                <button
                                  type="button"
                                  onClick={() => onCopyText(`tool-${tool.id || idx}`, argsText || tool.preview || '')}
                                  title="复制工具调用数据"
                                  className="flex items-center gap-1 text-gray-400 hover:text-gray-700"
                                >
                                  {copiedKey === `tool-${tool.id || idx}` ? (
                                    <Checkmark16Regular className="text-emerald-600 text-xs" />
                                  ) : (
                                    <Copy16Regular className="text-xs" />
                                  )}
                                  <span className="text-[10px]">复制</span>
                                </button>
                              </div>

                              {argsText && (
                                <TruncatedText
                                  text={argsText}
                                  as="pre"
                                  maxChars={2000}
                                  className="mt-1.5 overflow-x-auto rounded-control bg-sunken/60 p-2 font-mono text-[10px] text-gray-700 select-text"
                                />
                              )}

                              {tool.preview && (
                                <TruncatedText
                                  text={tool.preview}
                                  as="div"
                                  maxChars={2500}
                                  className="mt-1.5 overflow-x-auto rounded-control bg-sunken/40 p-2 font-mono text-[10px] text-gray-600 select-text whitespace-pre-wrap"
                                />
                              )}
                            </div>
                          );
                        })}
                      </div>
                    )}

                    {/* Assistant response block */}
                    {step.assistantContent && (
                      <div className="rounded-control border border-line bg-surface p-2.5">
                        <div className="flex items-center justify-between pb-1.5 border-b border-line/40 text-[11px] font-semibold text-gray-600">
                          <span>助手回复</span>
                          <button
                            type="button"
                            onClick={() => onCopyText(`assistant-${step.id}`, step.assistantContent || '')}
                            title="复制助手内容"
                            className="flex items-center gap-1 text-gray-400 hover:text-gray-700"
                          >
                            {copiedKey === `assistant-${step.id}` ? (
                              <Checkmark16Regular className="text-emerald-600 text-xs" />
                            ) : (
                              <Copy16Regular className="text-xs" />
                            )}
                            <span className="text-[10px]">复制</span>
                          </button>
                        </div>
                        <TruncatedText
                          text={step.assistantContent}
                          className="mt-1.5 whitespace-pre-wrap font-sans text-gray-800 select-text leading-relaxed overflow-x-auto"
                        />
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
});

TurnCard.displayName = 'TurnCard';

export const TrajectoryContent: React.FC<{ context: RightDockContext }> = () => {
  const {
    messages,
    modelName,
    usage,
    sessionUsage,
  } = useConsoleStore(
    useShallow((state) => ({
      messages: state.messages,
      modelName: state.modelName,
      usage: state.usage,
      sessionUsage: state.sessionUsage,
    })),
  );

  const [searchQuery, setSearchQuery] = useState('');
  const deferredQuery = useDeferredValue(searchQuery);
  const [expandedTurns, setExpandedTurns] = useState<Set<string>>(new Set());
  const [expandedSteps, setExpandedSteps] = useState<Set<string>>(new Set());
  const [copiedKey, setCopiedKey] = useState<string | null>(null);

  // Group messages into chronological Turns and steps
  const turnGroups: TurnGroup[] = useMemo(() => {
    const turns: TurnGroup[] = [];
    let currentTurn: TurnGroup | null = null;
    let turnCounter = 0;
    let stepCounter = 1;

    for (let i = 0; i < messages.length; i++) {
      const msg = messages[i];

      if (msg.type === 'user') {
        turnCounter++;
        const turnKey = msg.turnId || `turn-${turnCounter}`;
        const firstLine = (msg.content ?? '').trim().split('\n')[0] || '用户提问';
        currentTurn = {
          key: turnKey,
          turnIndex: turnCounter,
          turnId: msg.turnId,
          timestamp: msg.timestamp,
          userPrompt: firstLine,
          steps: [],
          status: 'completed',
          toolCallCount: 0,
        };
        turns.push(currentTurn);

        currentTurn.steps.push({
          index: stepCounter++,
          id: msg.id,
          turnId: msg.turnId,
          timestamp: msg.timestamp,
          title: '用户输入',
          kind: 'user',
          status: 'completed',
          userContent: msg.content ?? '',
        });
      } else {
        if (!currentTurn) {
          turnCounter++;
          currentTurn = {
            key: `turn-${turnCounter}`,
            turnIndex: turnCounter,
            timestamp: msg.timestamp,
            userPrompt: '初始交互',
            steps: [],
            status: 'completed',
            toolCallCount: 0,
          };
          turns.push(currentTurn);
        }

        if (msg.type === 'thought') {
          currentTurn.steps.push({
            index: stepCounter++,
            id: msg.id,
            turnId: msg.turnId,
            timestamp: msg.timestamp,
            title: '深度思考 (CoT)',
            kind: 'thought',
            status: 'completed',
            duration: msg.duration,
            thoughtContent: msg.content ?? '',
          });
        } else if (msg.type === 'tool_group') {
          const toolNames = msg.tools?.map((t) => t.name).join(', ') || '工具调用';
          const hasError = msg.tools?.some((t) => t.error);
          if (hasError) currentTurn.status = 'failed';
          currentTurn.toolCallCount += msg.tools?.length ?? 1;

          currentTurn.steps.push({
            index: stepCounter++,
            id: msg.id,
            turnId: msg.turnId,
            timestamp: msg.timestamp,
            title: `工具执行: ${toolNames}`,
            kind: 'tool',
            status: hasError ? 'failed' : 'completed',
            duration: msg.duration,
            tools: msg.tools ?? [],
          });
        } else if (msg.type === 'assistant') {
          currentTurn.steps.push({
            index: stepCounter++,
            id: msg.id,
            turnId: msg.turnId,
            timestamp: msg.timestamp,
            title: '助手回复',
            kind: 'assistant',
            status: 'completed',
            duration: msg.duration,
            assistantContent: msg.content ?? '',
          });
          if (msg.duration) {
            currentTurn.duration = msg.duration;
          }
        }
      }
    }

    return turns;
  }, [messages]);

  // Performance Guard: Default to expanding ONLY the latest turn, preventing
  // massive initial DOM node creation and layout freeze on large sessions.
  useEffect(() => {
    if (turnGroups.length > 0 && expandedTurns.size === 0) {
      const latestTurn = turnGroups[turnGroups.length - 1];
      setExpandedTurns(new Set([latestTurn.key]));
    }
  }, [turnGroups.length]);

  // Filter turn groups and steps based on deferred search query (no frame dropping)
  const filteredTurnGroups = useMemo(() => {
    const q = deferredQuery.trim().toLowerCase();
    if (!q) return turnGroups;

    return turnGroups
      .map((turn) => {
        const matchesTurnHeader =
          `turn ${turn.turnIndex}`.includes(q) ||
          turn.userPrompt.toLowerCase().includes(q);

        const matchedSteps = turn.steps.filter((step) => {
          if (step.title.toLowerCase().includes(q)) return true;
          if (step.userContent && step.userContent.slice(0, 1000).toLowerCase().includes(q)) return true;
          if (step.thoughtContent && step.thoughtContent.slice(0, 1000).toLowerCase().includes(q)) return true;
          if (step.assistantContent && step.assistantContent.slice(0, 1000).toLowerCase().includes(q)) return true;
          if (step.tools?.some((t) => t.name.toLowerCase().includes(q) || (t.preview && t.preview.slice(0, 500).toLowerCase().includes(q)))) {
            return true;
          }
          return false;
        });

        if (matchesTurnHeader || matchedSteps.length > 0) {
          return {
            ...turn,
            steps: matchesTurnHeader ? turn.steps : matchedSteps,
          };
        }
        return null;
      })
      .filter((t): t is TurnGroup => t !== null);
  }, [turnGroups, deferredQuery]);

  const toggleTurn = useCallback((key: string) => {
    setExpandedTurns((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  const expandAllTurns = useCallback(() => {
    setExpandedTurns(new Set(turnGroups.map((t) => t.key)));
  }, [turnGroups]);

  const collapseAllTurns = useCallback(() => {
    setExpandedTurns(new Set());
  }, []);

  const toggleStep = useCallback((id: string) => {
    setExpandedSteps((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const copyText = useCallback((key: string, text: string) => {
    void navigator.clipboard.writeText(text);
    setCopiedKey(key);
    setTimeout(() => setCopiedKey(null), 1500);
  }, []);

  // Compute tokens
  const totalTokens = sessionUsage
    ? sessionUsage.input + sessionUsage.output
    : usage
    ? usage.turnInput + usage.turnOutput
    : 0;
  const promptTokens = sessionUsage?.input ?? usage?.turnInput ?? 0;
  const completionTokens = sessionUsage?.output ?? usage?.turnOutput ?? 0;
  const totalStepsCount = turnGroups.reduce((acc, t) => acc + t.steps.length, 0);

  return (
    <div className="flex h-full flex-col overflow-hidden text-xs bg-surface select-none font-sans">
      {/* Top summary metric header */}
      <div className="flex flex-col gap-2 border-b border-line bg-surface/80 p-2.5 shrink-0 backdrop-blur-sm">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="font-semibold text-gray-800 font-mono">
              {turnGroups.length} 轮次
            </span>
            <span className="text-gray-300">·</span>
            <span className="text-gray-600 font-mono">
              {totalStepsCount} 次步骤
            </span>
            <span className="text-gray-300">·</span>
            <span className="font-mono text-gray-700 font-semibold" title={`输入 ${promptTokens} + 输出 ${completionTokens}`}>
              {compactCount(totalTokens)} tok
            </span>
            {modelName && (
              <>
                <span className="text-gray-300">·</span>
                <span className="max-w-[8rem] truncate rounded-control bg-sunken px-1.5 py-0.5 font-mono text-[10px] text-gray-600 border border-line/40" title={modelName}>
                  {modelName}
                </span>
              </>
            )}
          </div>

          {/* Action Button: Pure icon for Expand All / Collapse All */}
          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={expandedTurns.size > 0 ? collapseAllTurns : expandAllTurns}
              title={expandedTurns.size > 0 ? '全部折叠' : '全部展开'}
              aria-label={expandedTurns.size > 0 ? '全部折叠' : '全部展开'}
              className="ui-icon-button ui-compact text-gray-500 hover:text-gray-900 border border-line/60 rounded-control transition-colors"
            >
              {expandedTurns.size > 0 ? (
                <ArrowCollapseAll16Regular aria-hidden="true" />
              ) : (
                <ArrowExpandAll16Regular aria-hidden="true" />
              )}
            </button>
          </div>
        </div>

        {/* Search bar */}
        <div className="relative">
          <Search16Regular className="pointer-events-none absolute left-2.5 top-2 text-gray-400 text-xs" />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="搜索轮次、调用轨迹、思考与工具..."
            className="w-full rounded-control border border-line bg-surface py-1 pl-7 pr-2.5 text-xs text-gray-800 outline-none focus:border-accent transition-colors"
          />
        </div>
      </div>

      {/* Trajectory Turn & Step list */}
      <div className="fluent-scrollbar flex-1 overflow-y-auto overflow-x-hidden p-2 space-y-2.5">
        {filteredTurnGroups.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-16 text-center text-gray-400">
            <History16Regular className="mb-2 text-3xl text-gray-300" />
            <p className="font-medium text-gray-500">暂无轨迹记录</p>
            <p className="text-[11px] text-gray-400 mt-0.5">当前会话尚未产生模型调用</p>
          </div>
        ) : (
          filteredTurnGroups.map((turn) => {
            const isTurnExpanded = expandedTurns.has(turn.key);

            return (
              <TurnCard
                key={turn.key}
                turn={turn}
                isExpanded={isTurnExpanded}
                expandedSteps={expandedSteps}
                copiedKey={copiedKey}
                onToggleTurn={toggleTurn}
                onToggleStep={toggleStep}
                onCopyText={copyText}
              />
            );
          })
        )}
      </div>
    </div>
  );
};

export const trajectoryTab: RightDockTabDefinition = {
  id: 'trajectory',
  label: '轨迹',
  order: 25,
  Icon: History16Regular,
  badge: () => {
    const messages = useConsoleStore.getState().messages;
    const calls = messages.filter((m) => m.type === 'tool_group' || m.type === 'thought').length;
    return calls > 0 ? calls : null;
  },
  badgeVariant: 'accent',
  Content: TrajectoryContent,
};
