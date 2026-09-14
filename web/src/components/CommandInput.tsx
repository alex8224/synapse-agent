import { Add20Regular, Dismiss20Regular, Stop20Filled, ArrowUp20Regular } from '@fluentui/react-icons';
import React, { useEffect, useRef, useState } from 'react';
import { AttachmentPreview } from './AttachmentPreview.tsx';
import { ModelControls } from './ModelControls.tsx';
import { useConsoleStore } from '../stores/useConsoleStore';
import { useShallow } from 'zustand/react/shallow';
import { formatBytes } from '../runtime-client/artifacts.ts';
import { ATTACHMENT_MAX_COUNT } from '../runtime-client/attachments.ts';

/**
 * Floating command card: the prompt line, then one control row inside the same
 * rounded box — add-image on the left, the model and reasoning level the next
 * turn will run on, and the primary action on the right.  Those two pickers used
 * to sit in the status bar; they configure the next turn, so they belong next to
 * the input that starts it.
 *
 * The primary button represents the current state: `↑`
 * sends when idle, and becomes an enabled `■` stop button while a turn is
 * running (the previous behaviour left it looking disabled because the input
 * was empty, with no way to interrupt from the UI).  Typing while busy still
 * queues a steer — that path is Enter, not the button.
 *
 * Images are added with the `+` (file picker), by pasting them into the card, or
 * by dropping them onto it — all three end in the same `handleFiles`
 * path.  Only the image types the runtime accepts are taken, at most eight per
 * submit and 4 MB each; every refusal is shown next to the composer instead of
 * being silently dropped.  A chunk still uploading disables sending, and an
 * attachment-only turn may be submitted with empty text.  Each pending row is the
 * picked image itself (`AttachmentPreview`) rather than a file-name chip, and
 * hovering it enlarges the copy, so what will be sent is verifiable before the
 * turn is submitted.
 *
 * The card floats over the transcript (`.console-pane-inset` reserves its height
 * in the scroller), which is what makes its own acrylic visible: a blur needs
 * content behind it.  The reserved height is the card's *measured* height, not a
 * guess, so growing the card (attachments, a wrapped control row) can never hide
 * the newest line behind it.
 */
export const CommandInput: React.FC = () => {
  const [text, setText] = useState('');
  const [dragging, setDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const cardRef = useRef<HTMLDivElement | null>(null);
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
  const ready = text.trim() !== '' || readyAttachment;
  const canSend = ready && !uploading;

  const handleSubmit = (e?: React.FormEvent) => {
    if (e) e.preventDefault();
    if (uploading) {
      // The store refuses too (and publishes the reason); this only keeps the
      // optimistic input text in place.
      void submitPrompt(text);
      return;
    }
    if (!ready) return;
    void submitPrompt(text);
    setText('');
  };

  const handleFiles = (files: FileList | null) => {
    if (!files || files.length === 0) return;
    void addAttachments(Array.from(files));
  };

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
          // Only an image paste is intercepted: a text paste keeps its default
          // behaviour, because the composer is an ordinary text input.
          const files = e.clipboardData?.files;
          if (!files || files.length === 0) return;
          e.preventDefault();
          handleFiles(files);
        }}
        className={`console-column ui-composer relative isolate pointer-events-auto flex flex-col rounded-card border shadow-card transition-all duration-150 ${
          dragging ? 'border-blue-500 ring-2 ring-blue-200/60' : 'border-line/70'
        }`}
      >
        {/* The card's acrylic is a layer, not the card's own material.
            `backdrop-filter` makes an element a *backdrop root*, so the two pickers
            that hang above this card could only blur what the card painted itself --
            the transcript behind them stayed sharp and they read as transparent
            instead of frosted.  The card still gets the material; the pickers get
            the page.  `isolate` keeps the negative layer inside the card. */}
        <div
          aria-hidden="true"
          className="material-chrome pointer-events-none absolute inset-0 -z-10 rounded-card"
        />
        {attachments.length > 0 && (
          <div className="flex flex-wrap items-center gap-2 px-3.5 pt-2.5">
            {attachments.map((entry) => {
              const percent =
                entry.size > 0
                  ? Math.min(100, Math.round((entry.uploadedBytes / entry.size) * 100))
                  : 0;
              const failed = entry.status === 'failed';
              return (
                <div
                  key={entry.localId}
                  // The chip is the image, not a file row: the name, type and size
                  // live in the tooltip and the hover preview, and a failure is
                  // still spelled out in the alert below the composer.
                  className={`relative rounded border p-1 ${
                    failed ? 'border-red-200 bg-red-50/70' : 'border-gray-200 bg-canvas'
                  }`}
                  title={entry.error ?? `${entry.name} · ${entry.mime} · ${formatBytes(entry.size)}`}
                >
                  <AttachmentPreview
                    source={entry.source}
                    label={entry.name}
                    mime={entry.mime}
                    size={entry.size}
                    failed={failed}
                  />
                  {entry.status === 'uploading' && (
                    <span className="pointer-events-none absolute inset-1 flex items-center justify-center rounded bg-surface/70 font-mono text-[10px] tabular-nums text-blue-700">
                      {percent}%
                    </span>
                  )}
                  <button
                    type="button"
                    onClick={() => removeAttachment(entry.localId)}
                    title={entry.status === 'uploading' ? '取消上传' : '移除附件'}
                    aria-label={entry.status === 'uploading' ? '取消上传' : '移除附件'}
                    className="ui-icon-button ui-compact absolute -right-1.5 -top-1.5 border border-line bg-surface shadow-card"
                  >
                    <Dismiss20Regular aria-hidden="true" />
                  </button>
                </div>
              );
            })}
            {/* The cap belongs with the chips: there is no separate status row. */}
            <span className="font-mono text-[10px] text-gray-400">
              {attachments.length}/{ATTACHMENT_MAX_COUNT}
            </span>
          </div>
        )}

        {attachmentError !== null && (
          <div
            role="alert"
            className="mx-3.5 mt-2 rounded border border-red-200 bg-red-50/70 px-2 py-1 font-mono text-[11px] leading-relaxed text-red-700"
          >
            {attachmentError}
          </div>
        )}

        <form onSubmit={handleSubmit} className="flex flex-col">
          <input
            id="console-composer"
            name="prompt"
            type="text"
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                handleSubmit();
              }
            }}
            // The busy state lives in the placeholder (as in the reference
            // composer) instead of a status row; the steer count is already shown
            // on the transcript's own status strip.
            // No "/ for commands, @ for files" hint either: neither a command
            // palette nor file mention exists in this console.
            placeholder={busy ? '继续输入以排队后续修改' : 'Build anything'}
            aria-label="消息输入"
            className="ui-composer-input w-full bg-transparent text-gray-900 placeholder:text-gray-500 font-sans"
          />
          <input
            ref={fileInputRef}
            id="console-attachments"
            name="attachments"
            type="file"
            accept="image/*"
            multiple
            className="hidden"
            onChange={(e) => {
              handleFiles(e.target.files);
              e.target.value = '';
            }}
          />
          {/* Control row: add on the left, what the next turn runs on the right.
              It is also the pickers' anchor (`relative`): anchored to their own
              trigger, a 320px model menu ran past the left edge of a narrow pane. */}
          <div className="ui-composer-toolbar relative">
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              title={`添加图片附件（也可直接粘贴或拖入；最多 ${ATTACHMENT_MAX_COUNT} 张，每张 4 MB）`}
              aria-label="添加图片附件"
              className="ui-icon-button"
            >
              <Add20Regular aria-hidden="true" />
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
    </div>
  );
};
