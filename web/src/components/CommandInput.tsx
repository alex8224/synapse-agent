import { Stop20Filled, ArrowUp20Regular } from '@fluentui/react-icons';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { AddProjectDialog } from './AddProjectDialog.tsx';
import { ModelControls } from './ModelControls.tsx';
import { ActionMenu } from './composer/actions/ActionMenu.tsx';
import { RichComposer, type RichComposerHandle } from './composer/RichComposer.tsx';
import { isSnapshotEmpty, type ComposerSnapshot } from './composer/composerDocument.ts';
import { useConsoleStore } from '../stores/useConsoleStore';
import { useScreenshotStore } from '../stores/screenshotTask.ts';
import { useShallow } from 'zustand/react/shallow';

/**
 * Floating command card: the prompt, then one control row inside the same
 * rounded box — the action menu and the add-project button on the left, the model
 * and reasoning level the next turn will run on, and the primary action on the
 * right.  Those two pickers used to sit in the status bar; they configure the next
 * turn, so they belong next to the input that starts it.
 *
 * The input itself is `RichComposer`: multi-line text with inline atomic pills
 * (an `@`-reference, an image).  This card keeps everything that is *not* the
 * editor — the store wiring, the submit/steer decision, the drag-and-drop target
 * and the pickers — so rich editing stays one component deep while the
 * submission path stays exactly what it was: one `text` string plus the store's
 * own attachment refs.
 *
 * The primary button represents the current state: `↑` sends when idle, and
 * becomes an enabled `■` stop button while a turn is running (the previous
 * behaviour left it looking disabled because the input was empty, with no way to
 * interrupt from the UI).  Typing while busy still queues a steer — that path is
 * Enter, not the button.
 *
 * Images enter through three routes and all three end in the same `handleFiles`:
 * a paste into the card, a drop onto it, or the image picker.  Only the image
 * types the runtime accepts are taken, at most eight per submit and 4 MB each;
 * every refusal is shown next to the composer instead of being silently dropped.
 * Each accepted pick becomes an inline pill at the caret (`ImagePillView`) whose
 * hover reveals the enlarged copy, so what will be sent is verifiable before the
 * turn is submitted.  A chunk still uploading disables sending, and an
 * attachment-only turn may be submitted with empty text.
 *
 * The bottom-left control is the action menu (`composer/actions/`): a general
 * menu whose first row opens the image picker and whose other rows are declared
 * by that registry.  This card only hosts it — it owns the hidden `<input>` and
 * hands the menu a `pickImages` capability, so a picked file still takes the one
 * `handleFiles` path.  "Add project" stays its own button beside the menu:
 * `AddProjectDialog` walks the host filesystem and registers a workspace
 * directory as a new project (then switches to it and opens a session).
 *
 * The card floats over the transcript (`.console-pane-inset` reserves its height
 * in the scroller), which is what makes its own acrylic visible: a blur needs
 * content behind it.  The reserved height is the card's *measured* height, not a
 * guess, so growing the card (a wrapped line, a pill, a wrapped control row) can
 * never hide the newest line behind it.
 */
export const CommandInput: React.FC = () => {
  const [dragging, setDragging] = useState(false);
  const [projectDialogOpen, setProjectDialogOpen] = useState(false);
  const [hasContent, setHasContent] = useState(false);
  const cardRef = useRef<HTMLDivElement | null>(null);
  const composerRef = useRef<RichComposerHandle | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const updateSpotlight = (e: React.MouseEvent<HTMLElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    e.currentTarget.style.setProperty('--mouse-x', `${e.clientX - rect.left}px`);
    e.currentTarget.style.setProperty('--mouse-y', `${e.clientY - rect.top}px`);
  };

  useEffect(() => {
    const card = cardRef.current;
    if (card === null) return;
    const root = document.documentElement;
    const publish = () => root.style.setProperty('--composer-h', `${card.offsetHeight}px`);
    publish();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(publish);
    observer.observe(card);
    return () => {
      observer.disconnect();
      root.style.removeProperty('--composer-h');
    };
  }, []);

  const {
    runtimeStatus,
    submitPrompt,
    cancelActiveTurn,
    attachments,
    attachmentError,
    addAttachments,
    removeAttachment,
  } = useConsoleStore(
    // Only the fields the composer paints: a reasoning delta must not re-render
    // it (and must not touch the text the user is typing).
    useShallow((state) => ({
      runtimeStatus: state.runtimeStatus,
      submitPrompt: state.submitPrompt,
      cancelActiveTurn: state.cancelActiveTurn,
      attachments: state.attachments,
      attachmentError: state.attachmentError,
      addAttachments: state.addAttachments,
      removeAttachment: state.removeAttachment,
    })),
  );

  const busy = runtimeStatus === 'running';
  const uploading = attachments.some((entry) => entry.status === 'uploading');
  const readyAttachment = attachments.some((entry) => entry.status === 'ready');
  const canSend = (hasContent || readyAttachment) && !uploading;

  /**
   * One submit path, shared by the editor's Enter, the primary button and the
   * form.
   *
   * A refused submit (an upload still in flight) keeps the draft exactly as it
   * is: the store publishes the reason, and the reader's own text is the one
   * thing a failed send must never throw away.
   */
  const handleSubmit = useCallback(
    (draft?: ComposerSnapshot) => {
      const snapshot = draft ?? composerRef.current?.snapshot();
      if (snapshot === undefined) return;
      if (uploading) {
        void submitPrompt(snapshot.text);
        return;
      }
      if (isSnapshotEmpty(snapshot) && !readyAttachment) return;
      void submitPrompt(snapshot.text);
      composerRef.current?.reset();
    },
    [submitPrompt, uploading, readyAttachment],
  );

  const handleFiles = useCallback(
    (files: FileList | null) => {
      if (!files || files.length === 0) return;
      void addAttachments(Array.from(files));
    },
    [addAttachments],
  );

  // The one capability the action menu needs from this card: open the hidden
  // picker.  The chosen files then take `handleFiles`, like a paste or a drop.
  const pickImages = useCallback(() => fileInputRef.current?.click(), []);

  // The two window-capture capabilities are the capture store's own actions: the
  // card only hands them to the menu, so no screenshot logic lives here (the
  // progress and the result are painted by the capture banner).
  const startWindowScreenshot = useScreenshotStore((s) => s.start);
  const openScreenshotSettings = useScreenshotStore((s) => s.openSettings);

  return (
    // The card floats over the transcript's bottom edge, so the transcript scrolls
    // behind it and the card's acrylic has something to blur.  The scroller reserves
    // the card's measured height (`--composer-h`, published below), so the newest
    // streamed line still lands above the card at the bottom of the scrollport
    // instead of behind the input.  It keeps the shared gutters and the shared
    // `console-column` width, and the transcript shows no scrollbar, so the card's
    // edges line up with the chat column above it.
    <div className="console-gutter pointer-events-none absolute inset-x-0 bottom-0 z-30 flex w-full justify-center pb-3">
      <div
        ref={cardRef}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          handleFiles(e.dataTransfer?.files ?? null);
        }}
        onPaste={(e) => {
          // Only an image paste is intercepted here: the editor handles its own
          // text paste, and a paste that carries no file keeps its default
          // behaviour.  A file-carrying paste is routed to `handleFiles`, the
          // same single path a pick or a drop takes.
          const files = e.clipboardData?.files;
          if (!files || files.length === 0) return;
          e.preventDefault();
          handleFiles(files);
        }}
        onMouseMove={updateSpotlight}
        className={`console-column ui-composer fluent-spotlight relative isolate pointer-events-auto flex flex-col rounded-card border shadow-card transition-all duration-150 ${
          dragging ? 'border-blue-500 ring-2 ring-blue-200/60' : 'border-line/70'
        }`}
      >
        {/* The card's acrylic is a layer, not the card's own material.
            `backdrop-filter` makes an element a *backdrop root*, so the pickers
            that hang above this card could only blur what the card painted itself --
            the transcript behind them stayed sharp and they read as transparent
            instead of frosted.  The card still gets the material; the pickers get
            the page.  `isolate` keeps the negative layer inside the card. */}
        <div
          aria-hidden="true"
          className="material-chrome pointer-events-none absolute inset-0 -z-10 rounded-card"
        />

        {attachmentError !== null && (
          <div
            role="alert"
            className="mx-3.5 mt-2 rounded border border-red-200 bg-red-50/70 px-2 py-1 font-mono text-[11px] leading-relaxed text-red-700"
          >
            {attachmentError}
          </div>
        )}

        <form
          onSubmit={(e) => {
            e.preventDefault();
            handleSubmit();
          }}
          className="flex flex-col"
        >
          <RichComposer
            handleRef={composerRef}
            placeholder={busy ? '继续输入以排队后续修改' : 'Build anything'}
            busy={busy}
            attachments={attachments}
            onSubmit={handleSubmit}
            onFiles={handleFiles}
            onRemoveAttachment={removeAttachment}
            onContentChange={setHasContent}
          />
          {/* Control row: add on the left, what the next turn runs on the right.
              It is also the pickers' anchor (`relative`): anchored to their own
              trigger, a 320px model menu ran past the left edge of a narrow pane. */}
          <div className="ui-composer-toolbar relative">
            {/* The bottom-left control is a general action menu; the image picker
                is one of its rows (`composer/actions/`), not a branch here. */}
            <ActionMenu
              onPickImages={pickImages}
              onStartWindowScreenshot={startWindowScreenshot}
              onOpenScreenshotSettings={openScreenshotSettings}
            />
            <input
              ref={fileInputRef}
              type="file"
              accept="image/*"
              multiple
              hidden
              onChange={(e) => {
                handleFiles(e.target.files);
                // Reset so picking the same file twice still fires a change.
                e.target.value = '';
              }}
            />
            <button
              type="button"
              onClick={() => setProjectDialogOpen(true)}
              title="添加项目（选择本地目录并新建会话）"
              className="ui-button ui-model-trigger"
            >
              添加项目
            </button>
            <div className="ml-auto flex min-w-0 flex-1 flex-wrap items-center justify-end gap-1">
              <ModelControls />
            </div>
            {busy ? (
              <button
                type="button"
                onClick={() => {
                  void cancelActiveTurn();
                }}
                title="停止当前轮次 (Ctrl+C)"
                aria-label="停止当前轮次"
                className="ui-icon-button ui-danger ui-round"
              >
                <Stop20Filled aria-hidden="true" />
              </button>
            ) : (
              <button
                type="submit"
                disabled={!canSend}
                title={uploading ? '附件仍在上传中' : 'Send (Enter)'}
                aria-label="发送消息"
                className="ui-icon-button ui-primary ui-round"
              >
                <ArrowUp20Regular aria-hidden="true" />
              </button>
            )}
          </div>
        </form>
      </div>
      {projectDialogOpen && <AddProjectDialog onClose={() => setProjectDialogOpen(false)} />}
    </div>
  );
};
