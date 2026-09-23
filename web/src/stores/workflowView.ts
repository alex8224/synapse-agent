/**
 * The console's workflow view: the project's runs, the selected run, and the actions a
 * reader can take.
 *
 * Deliberately its own store rather than part of `useConsoleStore`: a workflow has its own
 * lifecycle (a run outlives a turn, and its progress arrives as run records rather than
 * transcript rows), and mixing it into the session store would rebuild the transcript on
 * every workflow poll.
 *
 * The client is read from the session store at call time, so this store never holds a
 * second connection and never survives a re-pairing with a stale one.
 */
import { create } from 'zustand';
import {
  parseWorkflowDraftResult,
  parseWorkflowRunPage,
  parseWorkflowRunResult,
  type WorkflowDraftView,
  type WorkflowRunView,
} from '../runtime-client/workflows.ts';
import type { JsonValue } from '../runtime-client/contract.generated.ts';
import { useConsoleStore } from './useConsoleStore.ts';

export interface WorkflowState {
  runs: WorkflowRunView[];
  /** The run whose detail is open, or `null`. */
  selected: WorkflowRunView | null;
  /** The draft being edited or approved, when a draft surface is open. */
  draft: WorkflowDraftView | null;
  loading: boolean;
  /** A bounded, reader-facing failure message; `null` when the last call succeeded. */
  error: string | null;
  loadRuns: () => Promise<void>;
  select: (runId: string) => Promise<void>;
  start: (workflowId: string, inputs?: JsonValue) => Promise<void>;
  cancel: (runId: string) => Promise<void>;
  approve: (workflowId: string, revision: number) => Promise<void>;
  clear: () => void;
}

function projectId(): string {
  return useConsoleStore.getState().currentSession.project_id;
}

function describe(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  return 'workflow request failed';
}

export const useWorkflowStore = create<WorkflowState>((set, get) => ({
  runs: [],
  selected: null,
  draft: null,
  loading: false,
  error: null,

  loadRuns: async () => {
    const client = useConsoleStore.getState().client;
    const project = projectId();
    if (client === null || project === '') return;
    set({ loading: true, error: null });
    try {
      const page = parseWorkflowRunPage(
        await client.listWorkflowRuns({ project_id: project, limit: 20 }),
      );
      set({ runs: page.runs, loading: false });
    } catch (error) {
      set({ loading: false, error: describe(error) });
    }
  },

  select: async (runId: string) => {
    const client = useConsoleStore.getState().client;
    const project = projectId();
    if (client === null || project === '') return;
    try {
      const run = parseWorkflowRunResult(
        await client.getWorkflowRun({ project_id: project, run_id: runId }),
      );
      set({ selected: run, error: null });
    } catch (error) {
      set({ error: describe(error) });
    }
  },

  start: async (workflowId: string, inputs?: JsonValue) => {
    const client = useConsoleStore.getState().client;
    const project = projectId();
    if (client === null || project === '') return;
    try {
      const run = parseWorkflowRunResult(
        await client.startWorkflowRun({
          project_id: project,
          workflow_id: workflowId,
          inputs: inputs ?? null,
        }),
      );
      // The receipt means the run was accepted, not that it finished: read the list back
      // rather than assuming the run is done.
      set({ selected: run, error: null });
      await get().loadRuns();
    } catch (error) {
      set({ error: describe(error) });
    }
  },

  cancel: async (runId: string) => {
    const client = useConsoleStore.getState().client;
    const project = projectId();
    if (client === null || project === '') return;
    try {
      const run = parseWorkflowRunResult(
        await client.cancelWorkflowRun({ project_id: project, run_id: runId }),
      );
      set({ selected: run, error: null });
      await get().loadRuns();
    } catch (error) {
      set({ error: describe(error) });
    }
  },

  approve: async (workflowId: string, revision: number) => {
    const client = useConsoleStore.getState().client;
    const project = projectId();
    if (client === null || project === '') return;
    try {
      const draft = parseWorkflowDraftResult(
        await client.approveWorkflowDraft({
          project_id: project,
          workflow_id: workflowId,
          revision,
        }),
      );
      set({ draft, error: null });
    } catch (error) {
      set({ error: describe(error) });
    }
  },

  clear: () => set({ runs: [], selected: null, draft: null, error: null }),
}));
