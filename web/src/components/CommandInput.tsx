import { Stop20Filled, ArrowUp20Regular, Mic20Regular, RecordStop20Filled } from '@fluentui/react-icons';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { AddProjectDialog } from './AddProjectDialog.tsx';
import { ModelControls } from './ModelControls.tsx';
import { ActionMenu } from './composer/actions/ActionMenu.tsx';
import { ScreenshotActionArea } from './composer/actions/ScreenshotActionArea.tsx';
import { COMPOSER_ACTIONS, IMAGE_ACTIONS } from './composer/actions/manifest.ts';
import { RichComposer, type RichComposerHandle } from './composer/RichComposer.tsx';
import { isSnapshotEmpty, type ComposerSnapshot } from './composer/composerDocument.ts';
import { useSpeechInput, type SpeechInputOptions } from './composer/useSpeechInput.ts';
import { useLocalSpeechInput } from './composer/useLocalSpeechInput.ts';
import { captionText } from './composer/speechInput.ts';
import { needsWarmUp, usesLocalEngine, usesRuntimeEngine } from './composer/sttEngine.ts';
import { useConsoleStore } from '../stores/useConsoleStore';
import type { SttStatusView } from '../runtime-client/types.ts';
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
 * The bottom-left controls are the image overflow menu, add-project, and the
 * wide-screen screenshot operation area.  The screenshot area combines capture
 * and settings beside the project button; on narrow screens it is hidden and
 * the full action menu provides the same screenshot rows.
 * `AddProjectDialog` walks the host filesystem and registers a workspace
 * directory as a new project (then switches to it and opens a session).
 *
 * The microphone sits beside the action menu because it is an input affordance
 * with state to show, not a menu row: it toggles speech recognition (the
 * browser's own, so Chrome and Edge only) and hands each finalized phrase to the
 * editor at the caret.  Its lifecycle lives in `composer/useSpeechInput.ts` and
 * is entry-local -- no store, no runtime, no wire surface -- so this card only
 * decides *where* a phrase lands.
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
    client,
    currentSession,
    sttRevision,
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
      client: state.client,
      currentSession: state.currentSession,
      sttRevision: state.sttRevision,
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

  // Which speech engine the runtime is configured for.  `engine` is the mode the
  // runtime reports and `available`/`reason` describe the *local* engine, so a
  // missing model set is a plain `available: false` with a reason, never a
  // failed turn.  Until the status arrives -- and whenever the read fails -- the
  // browser engine stays the default, exactly as before.
  // The answer is tagged with the client/session it was read for and only
  // published while that pairing is still current, so a late read can never
  // select the local engine for a different session.
  const sttKey = client === null ? null : `${currentSession.project_id}/${currentSession.thread_id}`;
  const [sttRead, setSttRead] = useState<{ key: string; view: SttStatusView | null } | null>(null);
  const sttStatus = sttRead !== null && sttRead.key === sttKey ? sttRead.view : null;
  // Building the local models costs over a minute on a CPU, and doing it on mount
  // was worse than the problem it solved: merely opening the console started a
  // build nobody asked for, and while it ran the reader could not switch to another
  // engine and get on with their work.  The build is therefore triggered by the one
  // action that needs it -- pressing the microphone -- and by the settings screen's
  // explicit "load now" button.  `sttReadToken` forces a fresh status read after it.
  const [warmUp, setWarmUp] = useState<{ key: string; state: 'idle' | 'warming' }>({
    key: '',
    state: 'idle',
  });
  const [sttReadToken, setSttReadToken] = useState(0);
  const warming = warmUp.key === sttKey && warmUp.state === 'warming';
  useEffect(() => {
    if (client === null || sttKey === null) return;
    const key = sttKey;
    let cancelled = false;
    const session = { project_id: currentSession.project_id, thread_id: currentSession.thread_id };
    void client.sttStatus(session).then(
      (view) => {
        if (cancelled) return;
        setSttRead({ key, view });
      },
      () => {
        if (!cancelled) setSttRead({ key, view: null });
      },
    );
    return () => {
      cancelled = true;
    };
  }, [client, sttKey, sttRevision, sttReadToken, currentSession.project_id, currentSession.thread_id]);

  // Speech input: both engines expose the same controller and share one
  // insertion sink, so the card only chooses *which* one runs -- it still says
  // where a recognized phrase goes (the caret of the editor the reader is typing
  // into), never into the prompt text directly.  Both hooks are called
  // unconditionally (rules of hooks); the one that is not selected stays idle
  // until it is toggled.
  const speechOptions: SpeechInputOptions = {
    onTranscript: (phrase) => composerRef.current?.insertSpoken(phrase),
  };
  const browserSpeech = useSpeechInput(speechOptions);
  const localSpeech = useLocalSpeechInput(speechOptions);
  const runtimeEngine = sttStatus !== null && sttStatus.engine !== 'browser' ? sttStatus : null;
  const usingRuntimeEngine = usesRuntimeEngine(sttStatus);
  const speech = usingRuntimeEngine ? localSpeech : browserSpeech;
  const activeEngineLabel = usingRuntimeEngine
    ? sttStatus?.providers?.find((item) => item.id === sttStatus.engine)?.label
      ?? sttStatus?.engine ?? '运行时语音引擎'
    : '浏览器内置';

  /**
   * The microphone's own trigger: build the models first when the selected engine
   * needs them and has not built them yet, then start dictating.
   *
   * The status is re-read afterwards rather than taken from the warm-up's answer: by
   * the time a minute-long build returns the reader may have chosen another engine,
   * and an old answer must never overwrite a newer choice.
   */
  const activateMic = useCallback(() => {
    if (client === null || sttKey === null || !needsWarmUp(sttStatus)) {
      speech.toggle();
      return;
    }
    const key = sttKey;
    const session = { project_id: currentSession.project_id, thread_id: currentSession.thread_id };
    setWarmUp({ key, state: 'warming' });
    void client.sttWarmUp(session).then(
      () => {
        setWarmUp({ key, state: 'idle' });
        setSttReadToken((token) => token + 1);
        speech.toggle();
      },
      () => {
        // A refused build is not a dead button: start the dictation anyway, so the
        // engine answers through its own channel with a readable reason (the models
        // are built on demand by `begin` too, so this may simply succeed).
        setWarmUp({ key, state: 'idle' });
        setSttReadToken((token) => token + 1);
        speech.toggle();
      },
    );
  }, [client, sttKey, sttStatus, speech, currentSession.project_id, currentSession.thread_id]);

  /**
   * One notice region for the things that can refuse input: a refused
   * attachment, a microphone that could not be used, and a local engine the
   * runtime reports as unavailable (no models, missing extra).  One region keeps
   * the card's height change -- which the transcript's reserved space is
   * measured from -- in a single place, and a refused upload outranks the rest
   * when several are set, because it is the one the reader can still fix here.
   */
  const speechNotice =
    runtimeEngine !== null && !runtimeEngine.available
      ? `${runtimeEngine.reason ?? '语音引擎不可用'}；当前使用浏览器内置识别`
      : null;
  const composerNotice = attachmentError ?? speech.error ?? speechNotice;

  // The reason a disabled button cannot be used is in the label as well as the
  // tooltip: a disabled control is not focusable, so the tooltip alone would leave
  // a screen-reader user with a button that silently does nothing.
  // The active engine changes what "unsupported" means and whether the network is
  // needed at all, so the copy follows the engine the runtime reported.
  const usingLocalEngine = usesLocalEngine(sttStatus);
  const speechLabel = warming
    ? '语音输入（正在加载本地模型）'
    : !speech.supported
      ? usingRuntimeEngine
        ? '语音输入（运行时语音输入不可用）'
        : '语音输入（当前浏览器不支持）'
      : speech.listening
        ? '停止语音输入'
        : '语音输入';
  const speechTitle = warming
    ? '正在加载本地语音模型：首次约 1 分钟，之后常驻内存'
    : !speech.supported
      ? usingRuntimeEngine
        ? `${activeEngineLabel}不可用，请检查麦克风权限与运行时设置`
        : '当前浏览器不支持语音输入（Chrome / Edge 可用）'
      : speech.listening
        ? `停止语音输入：${activeEngineLabel}`
        : usingLocalEngine
          ? '语音输入：本地识别，结果追加到光标处'
          : `语音输入：${activeEngineLabel}，识别结果追加到光标处（需要联网）`;
  /** The phrase being revised right now, bounded and ready to paint. */
  const speechCaption = captionText(speech.interim);

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

        {composerNotice !== null && (
          <div
            role="alert"
            className="mx-3.5 mt-2 rounded border border-red-200 bg-red-50/70 px-2 py-1 font-mono text-[11px] leading-relaxed text-red-700"
          >
            {composerNotice}
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
          {/* The live caption: what the recognizer has heard but not yet finalized.
              It is shown, never inserted -- an interim result is rewritten as the
              recognizer revises it, so unstable text in the draft would fight the
              caret, the pills and undo, and would overwrite a reader who typed
              while speaking.  The words move into the draft the moment the phrase
              is finalized.  Hidden from the accessibility tree: it is a visual
              echo of text that is about to land in the textbox, and announcing
              every revision would talk over the reader. */}
          {speechCaption !== '' && (
            <div className="composer-speech-caption" aria-hidden="true">
              <span className="composer-speech-caption-label">识别中</span>
              <span className="composer-speech-caption-text">{speechCaption}</span>
              <span className="composer-speech-caption-caret" />
            </div>
          )}
          {/* Control row: add on the left, what the next turn runs on the right.
              It is also the pickers' anchor (`relative`): anchored to their own
              trigger, a 320px model menu ran past the left edge of a narrow pane. */}
          <div className="ui-composer-toolbar relative">
            <div className="composer-actions-wide">
              <ActionMenu
                actions={IMAGE_ACTIONS}
                onPickImages={pickImages}
                onStartWindowScreenshot={startWindowScreenshot}
                onOpenScreenshotSettings={openScreenshotSettings}
              />
            </div>
            <div className="composer-actions-narrow">
              <ActionMenu
                actions={COMPOSER_ACTIONS}
                onPickImages={pickImages}
                onStartWindowScreenshot={startWindowScreenshot}
                onOpenScreenshotSettings={openScreenshotSettings}
              />
            </div>
            {/* The microphone: an input affordance with state, so it is a control
                of its own rather than a row of the `+` menu (a menu row cannot
                paint "listening").  `mousedown` is cancelled so activating it
                never moves the caret out of the editor -- a phrase would
                otherwise land at the end of the draft instead of where the
                reader was typing. */}
            <button
              type="button"
              onMouseDown={(event) => event.preventDefault()}
              onClick={activateMic}
              disabled={!speech.supported || warming}
              aria-pressed={speech.listening}
              aria-label={speechLabel}
              title={speechTitle}
              className={`ui-icon-button composer-speech${speech.listening ? ' composer-speech-on' : ''}`}
            >
              {speech.listening ? <RecordStop20Filled /> : <Mic20Regular />}
            </button>
            {warming && (
              // The build is a one-time cost, so the wait says so instead of
              // looking like a dead button.
              <span className="composer-speech-status" role="status">
                正在加载语音模型（首次约 1 分钟）
              </span>
            )}
            {speech.listening && (
              <span className="composer-speech-status" role="status">
                正在聆听
                {/* Decoration over the status word, which is what a reader is
                    told: the bars are hidden from the accessibility tree. */}
                <span className="composer-speech-bars" aria-hidden="true">
                  <span />
                  <span />
                  <span />
                </span>
              </span>
            )}
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
            <div className="composer-screenshot-wide">
              <ScreenshotActionArea
                startWindowScreenshot={startWindowScreenshot}
                openScreenshotSettings={openScreenshotSettings}
                pickImages={pickImages}
              />
            </div>
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
