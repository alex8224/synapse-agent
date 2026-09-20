/**
 * Entry-local lifecycle for the window-capture task.
 *
 * The capture itself is the runtime's: this store only starts one task, polls
 * its status, and turns a finished result into composer rows.  It is kept out of
 * `useConsoleStore` because it is a small, entry-scoped flow (like `codexUsage`),
 * but it reaches into the console store for the two things it needs — the live
 * client and the composer's attachment rows.
 *
 * Three rules the UI depends on:
 *
 *  - **No duplicate task.**  A second activation while one is queued/running
 *    neither starts a job nor polls twice; the store is the one guard the button
 *    and the runtime both honour.
 *  - **A result is bound to its draft.**  A capture records the session and the
 *    draft generation it was started in *before* any await, so a draft sent while
 *    the start is in flight cannot be mistaken for the originating one.  If the
 *    reader is still there, the frames are filled into the composer; if they
 *    switched session or sent a new draft, the frames are kept as a visible,
 *    confirmable result with explicit "restore" / "discard" actions instead of
 *    being dropped into a draft they never asked to fill.
 *  - **A late answer never wins.**  Every async read (probe, poll, refresh,
 *    cancel) captures an epoch, the client and the session it was issued for and
 *    is dropped if any of those changed while it was in flight, so a slow result
 *    can neither overwrite a newer task's state nor auto-fill the wrong draft.
 */
import { create } from 'zustand';
import { useConsoleStore, currentComposerGeneration } from './useConsoleStore.ts';
import type { SynapseRuntimeClient } from '../client/SynapseRuntimeClient.ts';
import {
  SCREENSHOT_MAX_FRAMES,
  isTerminalScreenshotState,
  sameScreenshotOrigin,
  screenshotErrorMessage,
  type ScreenshotFrameView,
  type ScreenshotOrigin,
  type ScreenshotStatusView,
  type ScreenshotTaskState,
} from '../runtime-client/screenshot.ts';

/** How often the console asks the runtime for a fresh task snapshot. */
const POLL_INTERVAL_MS = 350;

/**
 * Hard deadline for one capture RPC (probe, start, refresh, settings, cancel).
 *
 * The runtime already bounds its own tool calls, but a slow or wedged daemon
 * must never leave the UI stuck on "starting": every await in this store is
 * raced against this deadline so a start always resolves into a visible state.
 */
export const SCREENSHOT_RPC_TIMEOUT_MS = 15_000;

/** Hard deadline for one status poll (shorter: a poll is cheap and retried). */
export const SCREENSHOT_POLL_TIMEOUT_MS = 10_000;

/** The wording a start/probe timeout shows in the banner. */
export const SCREENSHOT_START_TIMEOUT_MESSAGE = '启动截图超时：运行时未在限定时间内响应，请重试';

/**
 * The wording shown when a `completed` snapshot never grew any frames.
 *
 * The runtime only publishes `completed` once every frame is finalized, so this
 * is the fallback for a daemon that flips the state early and then never fills
 * it: it names the situation and points at the banner's manual refresh.
 */
export const SCREENSHOT_IMPORT_TIMEOUT_MESSAGE =
  '截图已完成，但运行时尚未回填图片附件；请稍后点击「刷新状态」重试。';

/**
 * The bounded window a premature `completed` snapshot is re-read for.
 *
 * A daemon built before the "finalize first, complete last" fix can flip
 * `completed` while its attachments are still empty or partial.  Re-reading for
 * a short, bounded window turns that into an automatic recovery; once the window
 * lapses the console settles on whatever arrived (a partial result is still
 * added) and, when nothing arrived, shows the timeout notice above.  The object
 * is mutable so a test (and the browser acceptance fixture) can shrink it.
 */
export const SCREENSHOT_IMPORT_WAIT = {
  /** Total time to keep re-reading a premature `completed` snapshot. */
  deadlineMs: 6_000,
  /** Delay between those bounded re-reads. */
  recheckMs: 400,
};

/**
 * How many frames a `completed` snapshot is expected to carry.
 *
 * The runtime budgets a task to the composer's free slots (never more than
 * `SCREENSHOT_MAX_FRAMES`), so a result is "complete" once it holds
 * `min(requested, SCREENSHOT_MAX_FRAMES)` frames; `requested` is never trusted
 * past that ceiling.
 */
function expectedFrameCount(status: ScreenshotStatusView): number {
  if (status.requested <= 0) return 1;
  return Math.min(status.requested, SCREENSHOT_MAX_FRAMES);
}

/**
 * Whether a terminal snapshot is a premature `completed` still worth waiting on.
 *
 * Only `completed` is considered: every other terminal state is final.  The
 * runtime finalizes all frames before it reports `completed`, so a snapshot with
 * fewer frames than expected is one an older daemon published early — never a
 * success to stop on.
 */
function needsImportWait(status: ScreenshotStatusView): boolean {
  if (status.state !== 'completed') return false;
  return status.attachments.length < expectedFrameCount(status);
}

/**
 * Reject a promise if it does not settle within `ms`.
 *
 * A late resolution is ignored, so a timeout can never be mistaken for a
 * result that arrived after the caller had already given up on it.
 */
function withTimeout<T>(promise: Promise<T>, ms: number, message: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(message)), ms);
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (err) => {
        clearTimeout(timer);
        reject(err);
      },
    );
  });
}

/** A finished capture the reader has not yet accepted into the composer. */
export interface ScreenshotPendingResult {
  origin: ScreenshotOrigin;
  attachments: ScreenshotFrameView[];
}

export interface ScreenshotStore {
  /** Whether the host capture tool is runnable (last known). */
  toolAvailable: boolean;
  /** Why the tool is unavailable, when it is. */
  toolReason: string;
  /** The session the current/last task was started in. */
  origin: ScreenshotOrigin | null;
  /** The current task snapshot, or null before the first read. */
  status: ScreenshotStatusView | null;
  /** A finished result awaiting confirmation (not auto-filled). */
  pending: ScreenshotPendingResult | null;
  /** A visible notice (a refusal, a dropped frame, ...). */
  notice: string | null;
  /**
   * A premature `completed` snapshot whose frames are still being finalized.
   *
   * Distinct from a running capture: the task is done, the attachments are not
   * there yet, and the banner shows the same "importing" feedback while the
   * store re-reads a bounded number of times instead of giving up.
   */
  importing: boolean;
  /** A start request is in flight. */
  starting: boolean;

  refreshTool: () => Promise<void>;
  openSettings: () => Promise<void>;
  /** Queue one capture.  `timeoutMs` overrides the default RPC deadline (tests). */
  start: (timeoutMs?: number) => Promise<void>;
  cancel: () => Promise<void>;
  /** Fill a pending result into the *current* composer on request. */
  restorePending: () => void;
  /** Drop a pending result without adding it. */
  discardPending: () => void;
  dismissNotice: () => void;
  /** Clear everything (logout / client reset). */
  reset: () => void;
}

let pollTimer: ReturnType<typeof setTimeout> | null = null;
let pollToken = 0;

/**
 * When the bounded re-read of a premature `completed` snapshot gives up.
 *
 * `0` means "not waiting yet"; the first premature snapshot starts the clock.
 * Reset on a new task, on a settle, and on a store reset so a later task can
 * never inherit an expired window.
 */
let importDeadlineAt = 0;

function resetImportWait(): void {
  importDeadlineAt = 0;
}

/**
 * Monotonic epoch guarding every async capture operation.
 *
 * Bumped whenever the task binding changes (a reset, a new task, a cancel), so a
 * probe/poll/refresh that resolves after the change is dropped instead of
 * overwriting the new state or auto-filling a draft it never belonged to.
 */
let captureEpoch = 0;

function clearPoll(): void {
  if (pollTimer !== null) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
  pollToken += 1;
}

/** Invalidate every in-flight async capture result. */
function bumpEpoch(): void {
  captureEpoch += 1;
}

function currentOrigin(): ScreenshotOrigin {
  const session = useConsoleStore.getState().currentSession;
  return {
    projectId: session.project_id,
    threadId: session.thread_id,
    generation: currentComposerGeneration(),
  };
}

/** Whether two origins name the same session *and* draft generation. */
function sameOrigin(a: ScreenshotOrigin | null, b: ScreenshotOrigin | null): boolean {
  if (a === null || b === null) return a === b;
  return sameScreenshotOrigin(a, b);
}

/** Composer image slots occupied by *any* row (uploading / ready / failed). */
function composerImageCount(): number {
  return useConsoleStore.getState().attachments.length;
}

function describeError(err: unknown): string {
  return err instanceof Error && err.message ? err.message : String(err);
}

export const useScreenshotStore = create<ScreenshotStore>((set, get) => {
  // Remember handled results for this page lifetime, including explicit discard.
  // A reload cannot prove draft identity, so recovery then requires confirmation.
  const handledResults = new Set<string>();
  const resultKey = (status: ScreenshotStatusView, origin: ScreenshotOrigin): string =>
    JSON.stringify([origin.projectId, origin.threadId, status.taskId]);

  const schedulePoll = (
    taskId: string,
    origin: ScreenshotOrigin,
    client: SynapseRuntimeClient,
    epoch: number,
  ): void => {
    clearPoll();
    const token = pollToken;
    pollTimer = setTimeout(() => {
      if (token !== pollToken || epoch !== captureEpoch) return;
      void poll(taskId, origin, client, epoch);
    }, POLL_INTERVAL_MS);
  };

  /**
   * Re-read a premature `completed` snapshot once, or give up on the window.
   *
   * The deadline is started by the *first* premature snapshot and never
   * extended, so a daemon that never fills its frames cannot keep the console
   * polling forever.  Returns `false` once the window has lapsed, which lets the
   * caller settle on whatever arrived (the last partial list is still added).
   */
  const scheduleImportRecheck = (
    taskId: string,
    origin: ScreenshotOrigin,
    client: SynapseRuntimeClient,
    epoch: number,
  ): boolean => {
    const now = Date.now();
    if (importDeadlineAt === 0) importDeadlineAt = now + SCREENSHOT_IMPORT_WAIT.deadlineMs;
    if (now >= importDeadlineAt) return false;
    clearPoll();
    const token = pollToken;
    pollTimer = setTimeout(() => {
      if (token !== pollToken || epoch !== captureEpoch) return;
      void poll(taskId, origin, client, epoch);
    }, SCREENSHOT_IMPORT_WAIT.recheckMs);
    // Keep the importing feedback and drop any earlier error: the task is done,
    // only its attachments are still on the way.
    set({ importing: true, notice: null });
    return true;
  };

  /**
   * Apply a terminal snapshot, waiting out a premature completion first.
   *
   * Shared by the poll loop and the recovery refresh so a `completed` snapshot
   * that an older daemon published early recovers the same way whichever path
   * saw it.
   */
  const handleTerminal = (
    status: ScreenshotStatusView,
    origin: ScreenshotOrigin,
    client: SynapseRuntimeClient,
    epoch: number,
  ): void => {
    if (needsImportWait(status) && scheduleImportRecheck(status.taskId, origin, client, epoch)) {
      return;
    }
    settle(status, origin);
  };

  const settle = (status: ScreenshotStatusView, origin: ScreenshotOrigin): void => {
    clearPoll();
    bumpEpoch(); // A late running poll must not overwrite this terminal snapshot.
    resetImportWait();
    set({ importing: false });
    if (status.state === 'completed') {
      if (status.attachments.length === 0) {
        set({ notice: SCREENSHOT_IMPORT_TIMEOUT_MESSAGE });
        return;
      }
      const key = resultKey(status, origin);
      if (handledResults.has(key)) {
        // Already added in this page lifetime: never duplicate, but a stale
        // "no frames yet" notice from an earlier empty snapshot must go.
        if (get().notice === SCREENSHOT_IMPORT_TIMEOUT_MESSAGE) set({ notice: null });
        return;
      }
      handledResults.add(key);
      if (handledResults.size > 64) {
        handledResults.delete(handledResults.values().next().value!);
      }
      const session = useConsoleStore.getState().currentSession;
      const existing = origin.projectId === session.project_id && origin.threadId === session.thread_id
        ? new Set(useConsoleStore.getState().attachments.map((row) => row.attachmentId))
        : new Set<string | null>();
      const frames = status.attachments.filter((frame) => !existing.has(frame.attachmentId));
      if (frames.length === 0) return;
      if (sameOrigin(origin, currentOrigin())) {
        const { added, dropped } = useConsoleStore.getState().addReadyAttachments(frames);
        // A frame that did not fit is kept, not dropped: the reader can send the
        // current draft and add the rest with the banner's own action.
        const remaining = frames.slice(added);
        if (dropped > 0 && remaining.length > 0) {
          set({
            pending: { origin, attachments: remaining },
            notice: `已加入 ${added} 张截图，另有 ${remaining.length} 张超出单次上限，请发送后再次加入`,
          });
        } else {
          set({ pending: null, notice: null });
        }
        return;
      }
      // The reader moved on: keep the frames visible and confirmable.
      set({ pending: { origin, attachments: frames }, notice: null });
      return;
    }
    if (status.state === 'failed' || status.state === 'target_required') {
      set({
        notice: screenshotErrorMessage(status.errorCode, status.errorMessage ?? '截图失败'),
      });
    }
  };

  /** Whether an in-flight result no longer belongs to the live binding. */
  const stale = (
    epoch: number,
    client: SynapseRuntimeClient,
    origin: ScreenshotOrigin,
  ): boolean =>
    epoch !== captureEpoch ||
    useConsoleStore.getState().client !== client ||
    !sameOrigin(get().origin, origin);

  const poll = async (
    taskId: string,
    origin: ScreenshotOrigin,
    client: SynapseRuntimeClient,
    epoch: number,
  ): Promise<void> => {
    try {
      const status = await withTimeout(
        client.getScreenshotStatus(
          { project_id: origin.projectId, thread_id: origin.threadId },
          taskId,
        ),
        SCREENSHOT_POLL_TIMEOUT_MS,
        '截图状态读取超时',
      );
      if (stale(epoch, client, origin)) return;
      set({
        status,
        toolAvailable: status.available,
        toolReason: status.unavailableReason,
      });
      if (isTerminalScreenshotState(status.state)) {
        handleTerminal(status, origin, client, epoch);
        return;
      }
      schedulePoll(taskId, origin, client, epoch);
    } catch (err) {
      // A dropped read is not a failed capture: keep the last snapshot and retry
      // while the runtime is still reachable.
      if (stale(epoch, client, origin)) return;
      set({ notice: describeError(err) });
      schedulePoll(taskId, origin, client, epoch);
    }
  };

  return {
    toolAvailable: true,
    toolReason: '',
    origin: null,
    status: null,
    pending: null,
    notice: null,
    importing: false,
    starting: false,

    refreshTool: async () => {
      const client = useConsoleStore.getState().client;
      const session = useConsoleStore.getState().currentSession;
      if (!client || !session.project_id || !session.thread_id) return;
      const epoch = captureEpoch;
      const bound = get().origin;
      const taskId = bound?.projectId === session.project_id && bound.threadId === session.thread_id
        ? get().status?.taskId ?? ''
        : '';
      const probe = await withTimeout(
        client.getScreenshotStatus(session, taskId),
        SCREENSHOT_RPC_TIMEOUT_MS,
        '截图状态读取超时',
      );
      if (epoch !== captureEpoch || useConsoleStore.getState().client !== client) return;
      // The host capability is global, so it always refreshes.
      const capability = { toolAvailable: probe.available, toolReason: probe.unavailableReason };
      set(capability);
      const current = useConsoleStore.getState().currentSession;
      if (get().starting || current.project_id !== session.project_id || current.thread_id !== session.thread_id) return;
      const origin = get().origin;
      if (origin === null) {
        if (probe.state === 'idle') {
          set({ ...capability, status: probe });
          return;
        }
        const adopted: ScreenshotOrigin = {
          projectId: session.project_id,
          threadId: session.thread_id,
          // No original draft survives a page reload. Never infer ownership
          // from whichever draft happens to be visible when recovery finishes.
          generation: -1,
        };
        bumpEpoch();
        set({ ...capability, status: probe, origin: adopted });
        if (isTerminalScreenshotState(probe.state)) {
          handleTerminal(probe, adopted, client, captureEpoch);
        } else {
          schedulePoll(probe.taskId, adopted, client, captureEpoch);
        }
        return;
      }
      if (origin.projectId === session.project_id && origin.threadId === session.thread_id) {
        const previous = get().status;
        if (taskId && probe.taskId !== taskId) return;
        if (previous && isTerminalScreenshotState(previous.state) && !isTerminalScreenshotState(probe.state)) return;
        if (handledResults.has(resultKey(probe, origin))) {
          // Already added in this page lifetime: never duplicate, but a stale
          // empty-completion notice must not outlive the frames it complained about.
          if (get().notice === SCREENSHOT_IMPORT_TIMEOUT_MESSAGE) set({ notice: null });
          return;
        }
        set({ ...capability, status: probe });
        if (isTerminalScreenshotState(probe.state)) {
          handleTerminal(probe, origin, client, captureEpoch);
        } else if (probe.state !== 'idle') {
          schedulePoll(probe.taskId, origin, client, captureEpoch);
        }
        return;
      }
      // The probe is for a different session than the bound task: never let its
      // (usually idle) snapshot clobber the running task's state or origin.
      set(capability);
    },

    openSettings: async () => {
      const client = useConsoleStore.getState().client;
      const session = useConsoleStore.getState().currentSession;
      if (!client) throw new Error('运行时客户端尚未就绪');
      if (!session.project_id || !session.thread_id) throw new Error('请先打开一个会话');
      try {
        const tool = await withTimeout(
          client.openScreenshotSettings(session),
          SCREENSHOT_RPC_TIMEOUT_MS,
          '打开截图设置超时：运行时未在限定时间内响应',
        );
        set({
          toolAvailable: tool.available,
          toolReason: tool.reason,
          notice: tool.available ? null : tool.reason || '窗口截图工具不可用',
        });
      } catch (err) {
        set({ notice: describeError(err) });
        throw err;
      }
    },

    start: async (timeoutMs = SCREENSHOT_RPC_TIMEOUT_MS) => {
      if (get().starting) return;
      if (get().pending !== null) {
        set({ notice: '有未处理的截图结果，请先加入输入框或丢弃' });
        return;
      }
      const active = get().status;
      if (active && (active.state === 'queued' || active.state === 'running')) {
        set({ notice: '已有截图任务正在进行，请等待完成或取消' });
        return;
      }
      const client = useConsoleStore.getState().client;
      const session = useConsoleStore.getState().currentSession;
      if (!client) throw new Error('运行时客户端尚未就绪');
      if (!session.project_id || !session.thread_id) throw new Error('请先打开一个会话');
      // Freeze the binding *now*: a probe/start await must never adopt a draft
      // generation that changed while it was in flight.
      const origin: ScreenshotOrigin = {
        projectId: session.project_id,
        threadId: session.thread_id,
        generation: currentComposerGeneration(),
      };
      const epoch = captureEpoch;
      // Every composer row counts against the budget (uploading / ready /
      // failed), not just the ready ones: the frames are appended after them.
      const remaining = SCREENSHOT_MAX_FRAMES - composerImageCount();
      if (remaining <= 0) {
        set({ notice: `最多 ${SCREENSHOT_MAX_FRAMES} 张图片，请先发送或移除已有图片` });
        return;
      }
      // Clear the in-flight flag only while this flow still owns the binding: a
      // reset (which bumps the epoch) has already cleared it, and a newer flow
      // owns any later value.
      const clearStarting = (): void => {
        if (epoch === captureEpoch) set({ starting: false });
      };
      set({ starting: true, notice: null });
      try {
        const probe = await withTimeout(
          client.getScreenshotStatus(session, ''),
          timeoutMs,
          SCREENSHOT_START_TIMEOUT_MESSAGE,
        );
        if (epoch !== captureEpoch) {
          clearStarting();
          return;
        }
        if (!probe.available) {
          set({ toolAvailable: false, toolReason: probe.unavailableReason });
          throw new Error(probe.unavailableReason || '窗口截图工具不可用');
        }
        // No settings are sent: the capture uses the tool's own saved params
        // (chosen in the tool's settings window).  The composer's free slots are
        // passed as an explicit budget so a saved count of 600 can never
        // over-capture, and the runtime bounds the frames on the way back too.
        const started = await withTimeout(
          client.startScreenshotCapture({ session, maxFrames: remaining }),
          timeoutMs,
          SCREENSHOT_START_TIMEOUT_MESSAGE,
        );
        if (epoch !== captureEpoch) {
          clearStarting();
          return;
        }
        // A new task is a new binding: any in-flight result for the old one is
        // now stale and must not touch this state.
        bumpEpoch();
        resetImportWait();
        set({
          origin,
          status: {
            taskId: started.taskId,
            state: started.state,
            requested: started.requested,
            captured: 0,
            attachments: [],
            available: true,
            unavailableReason: '',
            errorCode: null,
            errorMessage: null,
          },
          importing: false,
          starting: false,
        });
        schedulePoll(started.taskId, origin, client, captureEpoch);
      } catch (err) {
        clearStarting();
        if (epoch === captureEpoch) set({ notice: describeError(err) });
        throw err;
      }
    },

    cancel: async () => {
      const origin = get().origin;
      const status = get().status;
      const client = useConsoleStore.getState().client;
      if (!origin || !status || !client) return;
      const epoch = captureEpoch;
      try {
        await withTimeout(
          client.cancelScreenshotCapture(
            { project_id: origin.projectId, thread_id: origin.threadId },
            status.taskId,
          ),
          SCREENSHOT_RPC_TIMEOUT_MS,
          '取消截图超时：运行时未在限定时间内响应',
        );
        if (epoch !== captureEpoch) return;
        const latest = get().status ?? status;
        // The cancel is the new binding: a poll already in flight for the old
        // task must not overwrite the settled state.
        bumpEpoch();
        resetImportWait();
        set({ status: { ...latest, state: 'cancelled' }, importing: false });
        clearPoll();
      } catch (err) {
        if (epoch === captureEpoch) set({ notice: describeError(err) });
      }
    },

    restorePending: () => {
      const pending = get().pending;
      if (pending === null) return;
      const session = useConsoleStore.getState().currentSession;
      // Only the session the capture was started in may receive its frames: a
      // cross-session fill would drop images into a draft that never asked.
      if (
        pending.origin.projectId !== session.project_id ||
        pending.origin.threadId !== session.thread_id
      ) {
        set({ notice: '请先切回截图所在的会话，再加入输入框' });
        return;
      }
      const { added, dropped } = useConsoleStore.getState().addReadyAttachments(pending.attachments);
      // Frames that did not fit stay confirmable instead of being lost.
      const remaining = pending.attachments.slice(added);
      if (dropped > 0 && remaining.length > 0) {
        set({
          pending: { origin: pending.origin, attachments: remaining },
          notice: `已加入 ${added} 张截图，另有 ${remaining.length} 张超出单次上限，请发送后再次加入`,
        });
        return;
      }
      set({ pending: null, notice: null });
    },

    discardPending: () => set({ pending: null }),

    dismissNotice: () => set({ notice: null }),

    reset: () => {
      clearPoll();
      bumpEpoch();
      resetImportWait();
      handledResults.clear();
      set({ origin: null, status: null, pending: null, notice: null, importing: false, starting: false });
    },
  };
});

/** Narrow view of the task state for a component (never the store itself). */
export function screenshotTaskState(status: ScreenshotStatusView | null): ScreenshotTaskState {
  return status?.state ?? 'idle';
}
