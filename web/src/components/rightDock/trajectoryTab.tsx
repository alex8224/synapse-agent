/**
 * Trajectory & Model Execution Trace Tab for the Right Auxiliary Dock (inspired by ZCode).
 *
 * Implements:
 * - Summary Header: Total model invocations, cumulative IN/OUT token breakdown, active model identifier
 * - Step Timeline: Chronological list of turns and tool operations (Step 01, 02, ...)
 * - ZCode Structured Inspection:
 *   - Input block: System / User messages with preview and copy
 *   - Output block: Thinking trace (CoT), Tool calls with parameters and status, Final assistant message
 *   - Telemetry details: Execution duration (seconds), exact timestamp, status pill
 * - In-panel search filter to quickly find specific tool calls, parameters, or reasoning steps
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
} from '@fluentui/react-icons';
import React, { useMemo, useState, useCallback } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { useConsoleStore } from '../../stores/useConsoleStore.ts';
import type { TranscriptMessage, ToolItemView } from '../../stores/historyMapper.ts';
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

function formatStepIndex(num: number): string {
  return num < 10 ? `0${num}` : `${num}`;
}

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
  const [expandedSteps, setExpandedSteps] = useState<Set<string>>(new Set());
  const [copiedKey, setCopiedKey] = useState<string | null>(null);

  // Group or flatten messages into trajectory execution steps
  const steps: TrajectoryStep[] = useMemo(() => {
    const list: TrajectoryStep[] = [];
    let counter = 1;

    for (let i = 0; i < messages.length; i++) {
      const msg = messages[i];
      if (msg.type === 'user') {
        list.push({
          index: counter++,
          id: msg.id,
          turnId: msg.turnId,
          timestamp: msg.timestamp,
          title: '用户输入',
          kind: 'user',
          status: 'completed',
          userContent: msg.content ?? '',
        });
      } else if (msg.type === 'thought') {
        list.push({
          index: counter++,
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
        list.push({
          index: counter++,
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
        list.push({
          index: counter++,
          id: msg.id,
          turnId: msg.turnId,
          timestamp: msg.timestamp,
          title: '助手回复',
          kind: 'assistant',
          status: 'completed',
          duration: msg.duration,
          assistantContent: msg.content ?? '',
        });
      }
    }

    return list;
  }, [messages]);

  const filteredSteps = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    if (!q) return steps;
    return steps.filter((step) => {
      if (step.title.toLowerCase().includes(q)) return true;
      if (step.userContent?.toLowerCase().includes(q)) return true;
      if (step.thoughtContent?.toLowerCase().includes(q)) return true;
      if (step.assistantContent?.toLowerCase().includes(q)) return true;
      if (step.tools?.some((t) => t.name.toLowerCase().includes(q) || t.preview?.toLowerCase().includes(q))) {
        return true;
      }
      return false;
    });
  }, [steps, searchQuery]);

  const toggleStep = useCallback((id: string) => {
    setExpandedSteps((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const expandAll = useCallback(() => {
    setExpandedSteps(new Set(steps.map((s) => s.id)));
  }, [steps]);

  const collapseAll = useCallback(() => {
    setExpandedSteps(new Set());
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

  return (
    <div className="flex h-full flex-col overflow-hidden text-xs bg-surface select-none">
      {/* Top summary metric header */}
      <div className="flex flex-col gap-1.5 border-b border-line bg-sunken/40 p-2.5 shrink-0">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-1.5">
            <span className="font-semibold text-gray-800">{steps.length} 次执行</span>
            <span className="text-gray-300">·</span>
            <span className="font-mono text-gray-700 font-semibold" title={`输入 ${promptTokens} + 输出 ${completionTokens}`}>
              {compactCount(totalTokens)} tok
            </span>
            {modelName && (
              <>
                <span className="text-gray-300">·</span>
                <span className="max-w-[8rem] truncate rounded bg-sunken px-1.5 py-0.5 font-mono text-[10px] text-gray-600" title={modelName}>
                  {modelName}
                </span>
              </>
            )}
          </div>

          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={expandedSteps.size > 0 ? collapseAll : expandAll}
              className="rounded px-1.5 py-0.5 text-[11px] text-gray-500 hover:bg-surface-hover hover:text-gray-800 transition-colors"
            >
              {expandedSteps.size > 0 ? '全部收起' : '全部展开'}
            </button>
          </div>
        </div>

        {/* Search bar */}
        <div className="relative">
          <Search16Regular className="pointer-events-none absolute left-2 top-1.5 text-gray-400 text-xs" />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="搜索调用轨迹、思考与工具..."
            className="w-full rounded border border-line bg-surface py-1 pl-6 pr-2 text-xs text-gray-800 outline-none focus:border-accent"
          />
        </div>
      </div>

      {/* Trajectory step cards */}
      <div className="fluent-scrollbar flex-1 overflow-y-auto overflow-x-hidden p-2 space-y-1.5">
        {filteredSteps.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-12 text-center text-gray-400 font-sans">
            <History16Regular className="mb-2 text-2xl text-gray-300" />
            <p>暂无轨迹记录</p>
            <p className="text-[11px] text-gray-400">当前会话尚未产生模型调用</p>
          </div>
        ) : (
          filteredSteps.map((step) => {
            const isExpanded = expandedSteps.has(step.id);

            return (
              <div
                key={step.id}
                className="overflow-hidden rounded-control border border-line/80 bg-surface shadow-xs"
              >
                {/* Step header row */}
                <div
                  onClick={() => toggleStep(step.id)}
                  className={`group flex cursor-pointer items-center justify-between px-2.5 py-2 transition-colors ${
                    isExpanded ? 'bg-sunken/60 border-b border-line/60' : 'hover:bg-surface-hover'
                  }`}
                >
                  <div className="flex min-w-0 items-center gap-2">
                    <span className="font-mono text-[11px] font-bold text-accent">
                      {formatStepIndex(step.index)}
                    </span>

                    <span className="text-gray-400">
                      {isExpanded ? (
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

                      <span className="font-medium text-gray-800 truncate">{step.title}</span>
                    </div>
                  </div>

                  {/* Telemetry pill */}
                  <div className="flex shrink-0 items-center gap-2 font-mono text-[10px] text-gray-400">
                    {step.duration && (
                      <span className="flex items-center gap-0.5 text-gray-500">
                        <Timer16Regular className="text-[10px]" />
                        {step.duration}
                      </span>
                    )}
                    <span className="text-gray-400">{step.timestamp}</span>
                  </div>
                </div>

                {/* Expanded step details */}
                {isExpanded && (
                  <div className="p-2 space-y-2 text-xs bg-surface/50">
                    {/* User message block */}
                    {step.userContent && (
                      <div className="rounded border border-line bg-surface p-2">
                        <div className="flex items-center justify-between pb-1 border-b border-line/40 text-[11px] font-semibold text-gray-600">
                          <span>用户消息</span>
                          <button
                            type="button"
                            onClick={() => copyText(`user-${step.id}`, step.userContent || '')}
                            title="复制消息内容"
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
                        <p className="mt-1.5 whitespace-pre-wrap font-sans text-gray-800 select-text leading-relaxed">
                          {step.userContent}
                        </p>
                      </div>
                    )}

                    {/* Thought process block */}
                    {step.thoughtContent && (
                      <div className="rounded border border-purple-200/60 bg-purple-50/30 p-2 dark:border-purple-900/40 dark:bg-purple-950/20">
                        <div className="flex items-center justify-between pb-1 border-b border-purple-200/40 text-[11px] font-semibold text-purple-700 dark:text-purple-300">
                          <span className="flex items-center gap-1">
                            <Sparkle16Regular className="text-xs" />
                            思考过程 (Chain-of-Thought)
                          </span>
                          <button
                            type="button"
                            onClick={() => copyText(`thought-${step.id}`, step.thoughtContent || '')}
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
                        <p className="mt-1.5 whitespace-pre-wrap font-mono text-[11px] text-gray-700 dark:text-gray-300 select-text leading-relaxed max-h-56 overflow-y-auto">
                          {step.thoughtContent}
                        </p>
                      </div>
                    )}

                    {/* Tool calls block */}
                    {step.tools && step.tools.length > 0 && (
                      <div className="space-y-1.5">
                        {step.tools.map((tool, idx) => (
                          <div key={tool.id || idx} className="rounded border border-line bg-surface p-2">
                            <div className="flex items-center justify-between pb-1 border-b border-line/40 text-[11px]">
                              <div className="flex items-center gap-1.5 font-semibold text-gray-700">
                                <Wrench16Regular className="text-amber-600 text-xs" />
                                <span>{tool.name}</span>
                                {tool.path && (
                                  <span className="font-mono text-[10px] text-gray-400 font-normal">
                                    {tool.path}
                                  </span>
                                )}
                              </div>
                              <button
                                type="button"
                                onClick={() => copyText(`tool-${tool.id || idx}`, JSON.stringify(tool.args || tool.preview || {}, null, 2))}
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

                            {tool.args && (
                              <pre className="mt-1.5 overflow-x-auto rounded bg-sunken/60 p-1.5 font-mono text-[10px] text-gray-700 select-text">
                                {JSON.stringify(tool.args, null, 2)}
                              </pre>
                            )}

                            {tool.preview && (
                              <div className="mt-1.5 max-h-40 overflow-y-auto rounded bg-sunken/40 p-1.5 font-mono text-[10px] text-gray-600 select-text whitespace-pre-wrap">
                                {tool.preview}
                              </div>
                            )}
                          </div>
                        ))}
                      </div>
                    )}

                    {/* Assistant response block */}
                    {step.assistantContent && (
                      <div className="rounded border border-line bg-surface p-2">
                        <div className="flex items-center justify-between pb-1 border-b border-line/40 text-[11px] font-semibold text-gray-600">
                          <span>助手回复</span>
                          <button
                            type="button"
                            onClick={() => copyText(`assistant-${step.id}`, step.assistantContent || '')}
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
                        <p className="mt-1.5 whitespace-pre-wrap font-sans text-gray-800 select-text leading-relaxed max-h-60 overflow-y-auto">
                          {step.assistantContent}
                        </p>
                      </div>
                    )}
                  </div>
                )}
              </div>
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
