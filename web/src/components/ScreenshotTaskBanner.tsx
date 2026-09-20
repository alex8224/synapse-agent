/**
 * The window-capture banner: progress, cancel, and the confirmable result.
 *
 * It sits just above the composer (not inside it) so the composer card itself
 * stays free of any screenshot branch, and it is the *only* place the capture
 * lifecycle is painted.  It reads the entry-local capture store and the console
 * store's current session, and it never talks to the runtime directly — every
 * action is one of the capture store's own.
 *
 * What it has to make unambiguous:
 *
 *  - a running capture shows its progress and offers cancel;
 *  - a capture that needs a target says so and offers a retry (the tool's own
 *    picker is already open);
 *  - a failure names its reason instead of looking like a success;
 *  - a result that landed after the reader switched session or sent a new draft
 *    stays visible with explicit "add to composer" / "discard" actions, so it is
 *    never silently dropped into a draft they did not ask to fill.
 */
import React from 'react';
import { useConsoleStore } from '../stores/useConsoleStore.ts';
import { useScreenshotStore } from '../stores/screenshotTask.ts';

const BANNER_SHELL =
  'console-gutter pointer-events-none absolute inset-x-0 z-20 flex w-full justify-center';
const BANNER_CARD =
  'console-column pointer-events-auto flex items-center gap-2 rounded-card border px-3 py-2 text-[12px] shadow-flyout material-flyout flyout-in';

function ActionButton({
  label,
  onClick,
  tone = 'default',
}: {
  label: string;
  onClick: () => void;
  tone?: 'default' | 'danger';
}): React.ReactElement {
  const color =
    tone === 'danger'
      ? 'border-red-200 text-red-700 hover:bg-red-50'
      : 'border-line text-gray-700 hover:bg-black/5';
  return (
    <button
      type="button"
      onClick={onClick}
      className={`shrink-0 rounded-control border px-2 py-0.5 transition-colors ${color}`}
    >
      {label}
    </button>
  );
}

export const ScreenshotTaskBanner: React.FC = () => {
  const origin = useScreenshotStore((s) => s.origin);
  const status = useScreenshotStore((s) => s.status);
  const pending = useScreenshotStore((s) => s.pending);
  const notice = useScreenshotStore((s) => s.notice);
  const importing = useScreenshotStore((s) => s.importing);
  const starting = useScreenshotStore((s) => s.starting);
  const cancel = useScreenshotStore((s) => s.cancel);
  const start = useScreenshotStore((s) => s.start);
  const refresh = useScreenshotStore((s) => s.refreshTool);
  const restorePending = useScreenshotStore((s) => s.restorePending);
  const discardPending = useScreenshotStore((s) => s.discardPending);
  const dismissNotice = useScreenshotStore((s) => s.dismissNotice);
  const currentSession = useConsoleStore((s) => s.currentSession);

  const sameSession =
    origin !== null &&
    origin.projectId === currentSession.project_id &&
    origin.threadId === currentSession.thread_id;
  // A pending result may belong to a session the reader has since left: it can
  // only be filled back into that session, so the banner says so instead of
  // offering an action that would silently drop the frames elsewhere.
  const pendingOtherSession =
    pending !== null &&
    (pending.origin.projectId !== currentSession.project_id ||
      pending.origin.threadId !== currentSession.thread_id);
  const state = status?.state ?? 'idle';
  const running = sameSession && (state === 'queued' || state === 'running');
  // A `completed` snapshot whose frames are still being finalized (an older
  // daemon): the task is done, so it is neither "running" nor a failure — it
  // keeps the importing feedback while the store re-reads, bounded.
  const importingNow = sameSession && importing;
  const targetRequired = sameSession && state === 'target_required';
  const failed = sameSession && state === 'failed';
  const cancelled = sameSession && state === 'cancelled';

  const visible =
    starting || running || importingNow || targetRequired || failed || cancelled || pending !== null || notice !== null;
  if (!visible) return null;

  return (
    <div className={BANNER_SHELL} style={{ bottom: 'calc(var(--composer-h, 7rem) + 0.5rem)' }}>
      <div
        className={`${BANNER_CARD} ${
          failed || notice !== null ? 'border-red-200 bg-red-50/80 text-red-700' : 'border-line/70'
        }`}
        role={failed || notice !== null ? 'alert' : 'status'}
      >
        {starting && <span>正在启动截图…</span>}

        {running && status !== null && (
          <>
            <span>
              {status.requested > 0 && status.captured >= status.requested
                ? '正在回填截图附件…'
                : `正在截图… ${status.captured}/${status.requested || 1}`}
            </span>
            <span className="h-1.5 w-24 overflow-hidden rounded-full bg-black/10">
              <span
                className="block h-full bg-blue-500 transition-[width] duration-200"
                style={{
                  width: `${Math.min(100, Math.round((status.captured / Math.max(1, status.requested || 1)) * 100))}%`,
                }}
              />
            </span>
            <ActionButton label="刷新状态" onClick={() => void refresh().catch(() => undefined)} />
            <ActionButton label="取消" tone="danger" onClick={() => void cancel()} />
          </>
        )}

        {importingNow && (
          <>
            <span>正在回填截图附件…</span>
            <ActionButton label="刷新状态" onClick={() => void refresh().catch(() => undefined)} />
          </>
        )}

        {targetRequired && (
          <>
            <span>
              {notice ?? '尚未选择窗口：已在截图工具中打开窗口列表，请选择后重试。'}
            </span>
            <ActionButton label="重试" onClick={() => void start().catch(() => undefined)} />
          </>
        )}

        {failed && <span>{notice ?? status?.errorMessage ?? '截图失败'}</span>}
        {failed && (
          <ActionButton label="重试" onClick={() => void start().catch(() => undefined)} />
        )}

        {cancelled && <span>截图已取消。</span>}

        {notice !== null && !failed && !targetRequired && (
          <>
            <span>{notice}</span>
            {!running && <ActionButton label="刷新状态" onClick={() => void refresh().catch(() => undefined)} />}
            <ActionButton label="关闭" onClick={dismissNotice} />
          </>
        )}

        {pending !== null && (
          <>
            <span>
              截图已完成（{pending.attachments.length} 张），
              {pendingOtherSession
                ? '属于其他会话，请先切回该会话再加入。'
                : '请确认后加入当前输入框。'}
            </span>
            {!pendingOtherSession && (
              <ActionButton label="加入输入框" onClick={restorePending} />
            )}
            <ActionButton label="丢弃" tone="danger" onClick={discardPending} />
          </>
        )}
      </div>
    </div>
  );
};
