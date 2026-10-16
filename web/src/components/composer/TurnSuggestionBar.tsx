import { Dismiss16Regular, Sparkle20Regular } from '@fluentui/react-icons';
import React, { useEffect, useMemo, useState } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { fetchTurnSuggestions } from '../../client/turnSuggestions.ts';
import type { TranscriptMessage } from '../../stores/historyMapper.ts';
import { latestTodos } from '../../stores/todoView.ts';
import {
  buildRuleSuggestions,
  extractLastTurnSnapshot,
  mergeSuggestedActions,
  normalizeModelSuggestions,
  type ModelSuggestionInput,
  type SuggestedAction,
} from '../../stores/turnSuggestions.ts';
import { useConsoleStore } from '../../stores/useConsoleStore.ts';

export interface TurnSuggestionBarProps {
  /** Hide when the user is already typing a new draft or a turn is running. */
  hidden?: boolean;
  onSelectAction: (action: SuggestedAction) => void;
}

const MAX_CACHED_TURNS = 16;
const EMPTY_MESSAGES: readonly TranscriptMessage[] = [];
const modelSuggestionCache = new Map<string, ModelSuggestionInput[]>();

/**
 * Post-turn action reminder strip rendered directly above the composer input.
 *
 * Combines zero-latency deterministic rules (`buildRuleSuggestions`) with
 * asynchronous post-turn model suggestions (`fetchTurnSuggestions`) via
 * `mergeSuggestedActions`.
 */
export const TurnSuggestionBar: React.FC<TurnSuggestionBarProps> = ({
  hidden = false,
  onSelectAction,
}) => {
  const {
    pairingState,
    runtimeStatus,
    pendingApproval,
    currentSession,
    modelName,
    goal,
    settledTurnIds,
    messages,
  } = useConsoleStore(
    // Selector values must remain referentially stable for `useShallow` /
    // `useSyncExternalStore`; derive snapshots via `useMemo` below instead of
    // allocating fresh objects inside the selector.
    useShallow((state) => ({
      pairingState: state.pairingState,
      runtimeStatus: state.runtimeStatus,
      pendingApproval: state.pendingApproval,
      currentSession: state.currentSession,
      modelName: state.modelName,
      goal: state.goal,
      settledTurnIds: state.settledTurnIds,
      messages:
        state.runtimeStatus === 'running' || state.pendingApproval !== null
          ? EMPTY_MESSAGES
          : state.messages,
    })),
  );

  const snapshot = useMemo(
    () => extractLastTurnSnapshot(messages, runtimeStatus, pendingApproval),
    [messages, runtimeStatus, pendingApproval],
  );

  const todos = useMemo(
    () => (snapshot === null ? null : latestTodos(messages)),
    [messages, snapshot],
  );

  const turnScopeKey =
    snapshot === null
      ? null
      : `${currentSession.project_id}:${currentSession.thread_id}:${snapshot.turnKey}`;

  const [dismissedKey, setDismissedKey] = useState<string | null>(null);
  const [modelResult, setModelResult] = useState<{
    key: string;
    items: ModelSuggestionInput[];
  } | null>(null);

  const isLiveSettledTurn = Boolean(
    snapshot !== null &&
      snapshot.turnId !== null &&
      settledTurnIds?.includes(snapshot.turnId),
  );

  const snapshotOutcome = snapshot?.outcome ?? null;
  const snapshotUserPrompt = snapshot?.userPrompt ?? '';
  const snapshotConclusion = snapshot?.conclusion ?? '';

  useEffect(() => {
    if (
      hidden ||
      pairingState !== 'paired' ||
      turnScopeKey === null ||
      dismissedKey === turnScopeKey ||
      snapshotOutcome !== 'completed' ||
      snapshotConclusion === '' ||
      !isLiveSettledTurn
    ) {
      return;
    }

    if (modelSuggestionCache.has(turnScopeKey)) {
      return;
    }

    let cancelled = false;
    const key = turnScopeKey;
    void fetchTurnSuggestions({
      user_prompt: snapshotUserPrompt,
      conclusion: snapshotConclusion,
      project_id: currentSession.project_id,
      model: modelName || null,
    }).then((items) => {
      if (modelSuggestionCache.size >= MAX_CACHED_TURNS) {
        const oldest = modelSuggestionCache.keys().next().value;
        if (oldest !== undefined) modelSuggestionCache.delete(oldest);
      }
      modelSuggestionCache.set(key, items);
      if (!cancelled) {
        setModelResult({ key, items });
      }
    });

    return () => {
      cancelled = true;
    };
  }, [
    hidden,
    pairingState,
    turnScopeKey,
    dismissedKey,
    snapshotOutcome,
    snapshotUserPrompt,
    snapshotConclusion,
    isLiveSettledTurn,
    currentSession.project_id,
    modelName,
  ]);

  const actions = useMemo(() => {
    if (snapshot === null || turnScopeKey === null) return [];
    const ruleActions = buildRuleSuggestions(snapshot, todos, goal);
    const activeModelInputs =
      (modelResult !== null && modelResult.key === turnScopeKey
        ? modelResult.items
        : undefined) ??
      modelSuggestionCache.get(turnScopeKey) ??
      [];
    const modelActions = normalizeModelSuggestions(activeModelInputs);
    return mergeSuggestedActions(ruleActions, modelActions);
  }, [snapshot, turnScopeKey, todos, goal, modelResult]);

  if (
    hidden ||
    runtimeStatus === 'running' ||
    pendingApproval !== null ||
    snapshot === null ||
    turnScopeKey === null ||
    dismissedKey === turnScopeKey ||
    actions.length === 0
  ) {
    return null;
  }

  return (
    <div
      role="region"
      aria-label="后续动作建议"
      className="mx-3.5 mt-2.5 mb-0.5 flex items-center justify-between gap-2 border-b border-line/40 pb-2"
    >
      <div className="flex min-w-0 flex-1 flex-wrap items-center gap-1.5">
        <span className="inline-flex shrink-0 items-center gap-1 text-[11px] text-gray-400 select-none">
          <Sparkle20Regular aria-hidden="true" style={{ fontSize: '13px' }} />
          <span>建议下一步</span>
        </span>
        {actions.map((action) => (
          <button
            key={action.id}
            type="button"
            onClick={() => onSelectAction(action)}
            title={action.prompt}
            className="inline-flex max-w-[16rem] items-center gap-1 truncate rounded-full border border-line/70 bg-surface/80 px-2.5 py-0.5 text-xs text-gray-700 hover:border-accent-fg/60 hover:bg-surface-hover hover:text-gray-900 cursor-pointer transition-colors"
          >
            <span className="truncate">{action.label}</span>
          </button>
        ))}
      </div>
      <button
        type="button"
        onClick={() => setDismissedKey(turnScopeKey)}
        title="关闭本轮建议"
        aria-label="关闭本轮建议"
        className="shrink-0 rounded p-0.5 text-gray-400 hover:bg-surface-hover hover:text-gray-600 cursor-pointer transition-colors"
      >
        <Dismiss16Regular aria-hidden="true" style={{ fontSize: '13px' }} />
      </button>
    </div>
  );
};
