import { Dismiss20Regular } from '@fluentui/react-icons';
import React, { useEffect, useRef } from 'react';
import { Portal } from './Portal.tsx';
import { useDialogKeyboardNav } from './keyboardNav.ts';

/**
 * Confirmation for deleting one session's *record* (`runtime.session.delete`).
 *
 * A destructive action is confirmed in a dialog of its own, not in a strip at the
 * foot of the sidebar.  The strip sat ~500px below the row whose trash icon armed
 * it -- and often below the fold -- so the question ("delete *which* session?")
 * was separated from the target it named nothing about, while the result notice of
 * the *previous* delete rendered in the same corner with the same styling.  This
 * box names the session it deletes, is portalled to the body (`Portal`: an acrylic
 * ancestor would otherwise anchor its `fixed` descendants to the rail), takes the
 * focus through the shared dialog keyboard contract, and closes on Escape or a
 * scrim click.
 *
 * There is no cancel button: the header's close control, the scrim and Escape all
 * dismiss the box, so a second way to say "no" would only add a third control to a
 * two-word decision.  The close control takes the initial focus, so Enter on a
 * freshly opened confirmation dismisses it instead of deleting anything.
 *
 * Only the record goes away (metadata row + thread goal); the conversation
 * (checkpoints, transcript) stays on disk, and the body says exactly that instead
 * of claiming the conversation was erased.  A running session is refused by the
 * server (`conflict`) -- the console never cancels the turn for you -- so a refusal
 * keeps the dialog open with the reason inline, and the session it names is still
 * there to retry or to dismiss.
 */
export interface SessionDeleteDialogProps {
  /** Title of the session being deleted: the dialog is what names the target. */
  title: string;
  threadId: string;
  /** The delete is in flight: the close control and the button are disabled. */
  busy: boolean;
  /** Failure of this delete, shown inline; the caller clears it when it opens. */
  error: string | null;
  onConfirm: () => void;
  onClose: () => void;
}

export const SessionDeleteDialog: React.FC<SessionDeleteDialogProps> = ({
  title, threadId, busy, error, onConfirm, onClose,
}) => {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  // Mounted only while the confirmation is open, so the box is always active.
  const onKeyDown = useDialogKeyboardNav(dialogRef, true, '[data-initial-focus]');

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !busy) onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [busy, onClose]);

  return (
    <Portal>
      <div
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/25 backdrop-blur-sm p-4 scrim-in"
        onClick={() => {
          // A delete in flight is not cancellable by a stray click: the answer is
          // already on the wire.
          if (!busy) onClose();
        }}
      >
        <div
          ref={dialogRef}
          role="dialog"
          aria-modal="true"
          aria-labelledby="session-delete-title"
          aria-describedby="session-delete-body"
          tabIndex={-1}
          onKeyDown={onKeyDown}
          className="w-full max-w-sm rounded-card border border-line/70 material-flyout flyout-in p-5 font-sans shadow-flyout"
          onClick={(event) => event.stopPropagation()}
        >
          <div className="flex items-center justify-between gap-2 border-b border-gray-100 pb-2">
            <span id="session-delete-title" className="ui-settings-title text-gray-900">
              删除会话记录？
            </span>
            <button
              type="button"
              onClick={onClose}
              disabled={busy}
              title="关闭 (Esc)"
              aria-label="关闭删除确认"
              data-initial-focus
              className="ui-icon-button text-gray-400 hover:text-gray-600"
            >
              <Dismiss20Regular aria-hidden="true" />
            </button>
          </div>

          {/* The target is named in the sentence, not in a nested card: the row that
              opened this box can be behind the scrim, and the title is what tells two
              "新会话" rows apart.  The id stays as muted metadata underneath, since it
              is the only thing that disambiguates two sessions sharing a title. */}
          <div id="session-delete-body" className="mt-3 text-xs leading-5 text-gray-700">
            <p>
              将删除「<span className="font-medium text-gray-900">{title}</span>」的记录（元数据与目标）。
            </p>
            <p className="mt-0.5 truncate font-mono text-[11px] text-gray-400" title={threadId}>
              {threadId}
            </p>
            <p className="mt-2 text-gray-500">
              对话历史（检查点与转录）仍保留在磁盘上，不会被删除；运行中的会话需先停止当前回合。
            </p>
          </div>

          {error !== null && (
            <div
              role="alert"
              className="mt-3 rounded-control border border-red-200 bg-red-50 px-2 py-1.5 text-xs leading-5 text-red-800"
            >
              {error}
            </div>
          )}

          <div className="mt-4 flex items-center justify-end">
            <button
              type="button"
              onClick={onConfirm}
              disabled={busy}
              className="ui-button ui-danger"
            >
              {/* The title already names the object, so the button carries only the
                  verb -- the same verb, which is what Fluent asks of a confirmation. */}
              {busy ? '正在删除…' : '删除'}
            </button>
          </div>
        </div>
      </div>
    </Portal>
  );
};
