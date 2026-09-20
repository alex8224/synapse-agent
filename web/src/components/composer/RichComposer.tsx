import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { Dismiss20Regular } from '@fluentui/react-icons';
import { flushSync } from 'react-dom';
import { useShallow } from 'zustand/react/shallow';
import { AttachmentPreview } from '../AttachmentPreview.tsx';
import { AttachmentIdThumb } from './AttachmentIdThumb.tsx';
import { ImagePreviewFlyout } from './ImagePreviewFlyout.tsx';
import { useAttachmentObjectUrl } from './attachmentUrl.ts';
import { NO_ATTACHMENT, useAttachmentResource } from '../useAttachmentResource.ts';
import { MentionFlyout } from './MentionFlyout.tsx';
import { mentionOptionId } from './mentionOption.ts';
import {
  CONTEXT_MENTIONS,
  fileMention,
  MENTION_KIND_ICON,
  MENTION_KIND_LABEL,
  rankMentions,
  SKILL_MENTIONS,
  skillMention,
  type MentionEntry,
} from './mentionCatalog.ts';
import {
  nextPillId,
  PILL_ATTRIBUTE,
  PILL_ID_ATTRIBUTE,
  serializeComposer,
  stripCaretAnchors,
  type ComposerMentionPill,
  type ComposerPill,
  type ComposerSnapshot,
} from './composerDocument.ts';
import {
  caretAfter,
  caretInTextAfter,
  editorSelection,
  insertLineBreakAtCaret,
  insertTextAtCaret,
  isRangeLive,
  mentionQueryAt,
  placeCaret,
} from './composerSelection.ts';
import { useConsoleStore } from '../../stores/useConsoleStore';
import type { PendingAttachment } from '../../stores/useConsoleStore.ts';
import type { TranscriptAttachment } from '../../stores/historyAttachments.ts';
import { ARTIFACT_LIST_LIMIT } from '../../runtime-client/artifacts.ts';

/** A pill the editor is currently rendering, in document order. */
type MountedPill =
  | { kind: 'mention'; pill: ComposerMentionPill }
  | { kind: 'image'; pillId: string; localId: string };

/** `Node.ELEMENT_NODE`, written out so the helper needs no DOM global. */
const ELEMENT_NODE = 1;

export interface RichComposerHandle {
  /** Current content, projected onto the wire contract. */
  snapshot: () => ComposerSnapshot;
  /** Drop everything the editor holds (after an accepted submit). */
  reset: () => void;
  /** Put a draft back exactly as it was (when a submit is refused). */
  restore: (snapshot: ComposerSnapshot) => void;
  focus: () => void;
}

export interface RichComposerProps {
  placeholder: string;
  busy: boolean;
  attachments: readonly PendingAttachment[];
  onSubmit: (snapshot: ComposerSnapshot) => void;
  onFiles: (files: FileList | null) => void;
  onRemoveAttachment: (localId: string) => void;
  /** Whether the editor holds anything worth sending. */
  onContentChange: (hasContent: boolean) => void;
  handleRef: React.RefObject<RichComposerHandle | null>;
}

/**
 * The composer's editable surface: multi-line text with inline atomic pills.
 *
 * The wire contract is unchanged — one `text` string plus the store's own
 * attachment refs — so the only job here is to make *editing* rich while
 * *submission* stays what it was.  Three consequences drive the design:
 *
 * - A pill is a `contentEditable={false}` element inside the text, so the caret
 *   steps over it as one unit and Backspace removes it whole.  Its meaning lives
 *   in `pillsRef`, keyed by the pill's local id — never in its markup — so a
 *   filename, a progress percentage or a tooltip can never reach the prompt.
 * - `Enter` submits and `Shift+Enter` breaks the line.  The break is inserted
 *   explicitly (`insertLineBreak`), because letting the browser wrap the line in
 *   a `<div>` would make the flattened text depend on layout.
 * - The `@` list is anchored to the typed `@` and driven from the keyboard:
 *   arrows walk it, Enter/Tab inserts, Escape dismisses.  A pick *replaces* the
 *   `@` and its query, so no stray `@` is left behind.
 */
export const RichComposer: React.FC<RichComposerProps> = ({
  placeholder,
  busy,
  attachments,
  onSubmit,
  onFiles,
  onRemoveAttachment,
  onContentChange,
  handleRef,
}) => {
  const editorRef = useRef<HTMLDivElement | null>(null);
  /** Meaning of every pill in the editor, keyed by its DOM-only local id. */
  const pillsRef = useRef<Map<string, ComposerPill>>(new Map());
  /** The range covering the `@` and the query typed after it, if any. */
  const queryRangeRef = useRef<Range | null>(null);
  /** The current query text (a ref: typing must not re-render the card). */
  const queryRef = useRef('');
  /**
   * The query an Escape dismissed.
   *
   * A keyup, a click or a caret move re-derives the mention query, so without
   * this the list would reopen the instant it was dismissed.  It is cleared as
   * soon as the reader changes the query, which is the only thing that should
   * bring the list back.
   */
  const dismissedQueryRef = useRef<string | null>(null);
  const [pills, setPills] = useState<MountedPill[]>([]);
  /**
   * Where a pill that is about to mount has to go.
   *
   * Captured *before* React commits (the paste/pick happens while the caret is
   * still where the reader put it) and consumed by the layout effect below, which
   * is the first moment the rendered pill exists in the DOM.
   */
  const pendingCaretRef = useRef<Range | null>(null);
  /** Pill ids already moved to their caret (so a re-render never moves them again). */
  const placedPillsRef = useRef<Set<string>>(new Set());
  const [empty, setEmpty] = useState(true);
  /**
   * File offers read from the runtime, tagged with the directory they came from.
   *
   * State, not a ref: the list is read while rendering (the flyout needs it),
   * and a ref read during render would paint a stale page on the frame after a
   * directory finishes loading.  It only changes when a *directory* changes,
   * never per keystroke, so it cannot thrash the surface being typed into.
   *
   * The tag is what makes the read declarative: the effect re-reads whenever the
   * directory the query names is not the one already loaded, so there is no
   * "have I read this yet" flag to keep in step with the render.
   */
  const [files, setFiles] = useState<{ dir: string; entries: MentionEntry[] }>({
    dir: '',
    entries: [],
  });
  const [skills, setSkills] = useState<MentionEntry[]>([]);
  const [mention, setMention] = useState<{ rect: DOMRect; query: string } | null>(null);
  const [activeIndex, setActiveIndex] = useState(0);
  const [filesLoading, setFilesLoading] = useState(false);
  const [filesError, setFilesError] = useState<string | null>(null);
  const [preview, setPreview] = useState<{ element: HTMLElement; entry: PendingAttachment } | null>(
    null,
  );

  const { listArtifacts, listSkills, currentSession } = useConsoleStore(
    // Only what a directory read needs: a reasoning delta must never re-render
    // the surface the reader is typing into.
    useShallow((state) => ({
      listArtifacts: state.listArtifacts,
      listSkills: state.listSkills,
      currentSession: state.currentSession,
    })),
  );

  /** Recompute emptiness and publish it. */
  const syncEmpty = useCallback(() => {
    const editor = editorRef.current;
    if (editor === null) return;
    const next = isEmptyEditor(editor);
    setEmpty(next);
    onContentChange(!next);
  }, [onContentChange]);

  const snapshot = useCallback(
    (): ComposerSnapshot => serializeComposer(editorRef.current, { pills: pillsRef.current }),
    [],
  );

  const reset = useCallback(() => {
    const editor = editorRef.current;
    if (editor === null) return;
    clearUserContent(editor);
    pillsRef.current.clear();
    setPills([]);
    queryRangeRef.current = null;
    queryRef.current = '';
    dismissedQueryRef.current = null;
    pendingCaretRef.current = null;
    placedPillsRef.current.clear();
    setFiles({ dir: '\u0000closed', entries: [] });
    setFilesError(null);
    setMention(null);
    setPreview(null);
    syncEmpty();
  }, [syncEmpty]);

  const restore = useCallback(
    (draft: ComposerSnapshot) => {
      const editor = editorRef.current;
      if (editor === null) return;
      reset();
      // The draft's own text is what was submitted, so it comes back as a plain
      // text node: the pills it referred to went with the rows the store took.
      editor.appendChild(document.createTextNode(draft.text));
      syncEmpty();
      editor.focus();
    },
    [reset, syncEmpty],
  );

  const focus = useCallback(() => editorRef.current?.focus(), []);

  useEffect(() => {
    handleRef.current = { snapshot, reset, restore, focus };
    return () => {
      handleRef.current = null;
    };
  }, [handleRef, snapshot, reset, restore, focus]);

  /** The offers the flyout shows for one query. */
  const offersFor = useCallback(
    (query: string): MentionEntry[] => {
      const needle = query.trim().toLowerCase();
      const matched = files.entries.filter(
        (entry) => needle === '' || entry.detail.toLowerCase().includes(needle),
      );
      const activeSkills = skills.length > 0 ? skills : SKILL_MENTIONS;
      const statics = rankMentions([...activeSkills, ...CONTEXT_MENTIONS], query);
      return [...matched, ...statics];
    },
    [files, skills],
  );

  /** Open, update or close the `@` list for the caret's current position. */
  const refreshMention = useCallback(() => {
    const editor = editorRef.current;
    if (editor === null) return;
    const found = mentionQueryAt(editor);
    if (found === null) {
      queryRangeRef.current = null;
      queryRef.current = '';
      dismissedQueryRef.current = null;
      setMention(null);
      return;
    }
    // Escape dismissed this very query: stay closed until it changes.
    if (dismissedQueryRef.current === found.query) {
      queryRangeRef.current = found.range;
      queryRef.current = found.query;
      setMention(null);
      return;
    }
    dismissedQueryRef.current = null;
    const opening = queryRangeRef.current === null;
    queryRangeRef.current = found.range;
    queryRef.current = found.query;
    if (opening) setActiveIndex(0);
    setMention({ rect: found.range.getBoundingClientRect(), query: found.query });
  }, []);

  /**
   * The directory the open query names (`''` for the workspace root).
   *
   * Derived during render so it can be the effect's dependency: keying the read
   * on the query would re-read on every keystroke, and keying it on a ref guard
   * would skip the very first read (the ref starts at the value the guard
   * compares against).  `null` means the list is closed, which is what clears the
   * loaded rows.
   *
   * The trailing slash is this component's own "the query is inside that
   * directory" bookkeeping.  The wire takes a canonical path and rejects a
   * trailing one (`invalid_artifact_path`), so `@web/` has to be asked for as
   * `web` — otherwise every nested query shows "runtime service error" and the
   * list can never leave the workspace root.
   */
  const mentionDir =
    mention === null ? null : mention.query.slice(0, mention.query.lastIndexOf('/') + 1);

  // Read the directory the query names, once per directory (not per keystroke).
  useEffect(() => {
    // Nothing open: the list is gone and so is its page.  The sentinel is a
    // value no query can produce, so "closed" can never look like "loaded".
    if (mentionDir === null) {
      setFiles({ dir: '\u0000closed', entries: [] });
      setFilesError(null);
      return;
    }
    // Already holding this directory: the query changed inside it, which the
    // filter below handles without another read.
    if (mentionDir === files.dir) return;
    let active = true;
    setFilesLoading(true);
    void listArtifacts(
      currentSession,
      mentionDir === '' ? '.' : mentionDir.replace(/\/+$/, ''),
      null,
      ARTIFACT_LIST_LIMIT,
    )
      .then((page) => {
        if (!active) return;
        setFiles({
          dir: mentionDir,
          entries: page.entries
            .filter((entry) => entry.kind === 'file')
            .map((entry) => fileMention(entry.path)),
        });
        setFilesError(null);
      })
      .catch((err: unknown) => {
        if (!active) return;
        setFiles({ dir: mentionDir, entries: [] });
        setFilesError(err instanceof Error && err.message ? err.message : String(err));
      })
      .finally(() => {
        if (active) setFilesLoading(false);
      });
    return () => {
      active = false;
    };
  }, [mentionDir, files.dir, listArtifacts, currentSession]);

  // Read discoverable Agent Skills through the runtime RPC once the mention
  // list is opened (falling back to the static mirror if offline/unpaired).
  useEffect(() => {
    if (mention === null) return;
    let active = true;
    void listSkills(currentSession.project_id)
      .then((page) => {
        if (!active) return;
        setSkills(page.skills.map(skillMention));
      })
      .catch(() => {
        if (!active) return;
        setSkills([...SKILL_MENTIONS]);
      });
    return () => {
      active = false;
    };
  }, [mention, listSkills, currentSession.project_id]);

  /** Insert the rows the store just accepted as pills at the caret. */
  const mountImagePills = useCallback(
    (localIds: readonly string[]) => {
      const editor = editorRef.current;
      if (editor === null || localIds.length === 0) return;
      // Remember where the caret is *now*: React renders the pills into the
      // container's end, and the layout effect below moves them here once they
      // exist.  Leaving them where React put them would drop a paste into the
      // wrong line whenever the caret is not already at the end.
      const selection = editorSelection(editor);
      pendingCaretRef.current =
        selection !== null ? selection.getRangeAt(0).cloneRange() : null;
      const mounted: MountedPill[] = localIds.map((localId) => {
        const pillId = nextPillId();
        pillsRef.current.set(pillId, { kind: 'image', pillId, localId });
        return { kind: 'image', pillId, localId };
      });
      setPills((current) => [...current, ...mounted]);
    },
    [],
  );

  /**
   * Move freshly mounted pills to the caret, and put the caret behind them.
   *
   * A layout effect, not part of `mountImagePills`: React only commits the pill
   * node during the render that follows, so a `querySelector` in the same tick
   * finds nothing and the placement would silently do nothing (which is how a
   * pasted image ended up at the end of the draft with the caret left *before*
   * it, so the next keystroke appeared to the left of the image).
   */
  useLayoutEffect(() => {
    const editor = editorRef.current;
    if (editor === null) return;
    // A pill the browser removed without telling us — select-all + Backspace, or
    // any editing command that rewrote the subtree — has to leave the state too.
    // React would otherwise try to remove a node that is no longer there, throw
    // inside its own commit, and unmount the whole console.
    const orphaned = pills.filter(
      (row) =>
        editor.querySelector(
          `[${PILL_ID_ATTRIBUTE}="${row.kind === 'image' ? row.pillId : row.pill.pillId}"]`,
        ) === null,
    );
    if (orphaned.length > 0) {
      for (const row of orphaned) {
        const pillId = row.kind === 'image' ? row.pillId : row.pill.pillId;
        pillsRef.current.delete(pillId);
        placedPillsRef.current.delete(pillId);
      }
      setPills((current) => current.filter((row) => !orphaned.includes(row)));
      return;
    }
    const anchor = pendingCaretRef.current;
    pendingCaretRef.current = null;
    if (anchor !== null) {
      let tail: ChildNode | null = null;
      for (const row of pills) {
        const pillId = row.kind === 'image' ? row.pillId : row.pill.pillId;
        if (placedPillsRef.current.has(pillId)) continue;
        const rendered = editor.querySelector<HTMLElement>(
          `[${PILL_ID_ATTRIBUTE}="${pillId}"]`,
        );
        if (rendered === null) continue;
        placedPillsRef.current.add(pillId);
        anchor.insertNode(rendered);
        let last: ChildNode = rendered;
        if (row.kind === 'mention') {
          // A token must not be glued to whatever is typed next: the space is
          // part of the draft (the store trims a trailing one).
          const space = document.createTextNode(' ');
          rendered.after(space);
          last = space;
        }
        anchor.setStartAfter(last);
        anchor.collapse(true);
        tail = last;
      }
      // Not `caretAfter`: a container-level caret would send the next keystroke
      // into the text *before* the pill, so the reader's typing would appear to
      // the left of the image they just pasted.
      if (tail !== null) caretInTextAfter(tail);
    }
    // Emptiness is decided *here*, after the commit: a pill that is not in the
    // DOM yet makes the check answer "empty", which left the placeholder painted
    // over the pill the reader had just inserted.
    syncEmpty();
    // A hover preview whose thumbnail just went away (its pill was removed, or
    // the turn was submitted) must not stay on screen: nothing will ever fire a
    // `mouseleave` for an element that no longer exists.
    setPreview((current) =>
      current !== null && !current.element.isConnected ? null : current,
    );
  }, [pills, syncEmpty]);

  // Rows the card inserted arrive through `attachments`, so the editor mounts a
  // pill for every row it has not seen and drops a pill whose row went away (a
  // session switch cancels every upload).
  const mountedIdsRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    const mounted = mountedIdsRef.current;
    const fresh: string[] = [];
    for (const entry of attachments) {
      if (mounted.has(entry.localId)) continue;
      mounted.add(entry.localId);
      fresh.push(entry.localId);
    }
    const gone = new Set<string>();
    for (const localId of Array.from(mounted)) {
      if (attachments.some((entry) => entry.localId === localId)) continue;
      mounted.delete(localId);
      gone.add(localId);
    }
    if (fresh.length > 0) mountImagePills(fresh);
    if (gone.size > 0) {
      setPills((current) => {
        const kept = current.filter((row) => row.kind !== 'image' || !gone.has(row.localId));
        for (const row of current) {
          if (row.kind === 'image' && gone.has(row.localId)) {
            pillsRef.current.delete(row.pillId);
            placedPillsRef.current.delete(row.pillId);
          }
        }
        return kept;
      });
    }
  }, [attachments, mountImagePills]);

  /** Replace the typed `@` query with the chosen pill. */
  const pickMention = useCallback(
    (entry: MentionEntry) => {
      const editor = editorRef.current;
      if (editor === null) return;
      const pill: ComposerMentionPill = {
        kind: entry.kind,
        pillId: nextPillId(),
        token: entry.token,
        label: entry.label,
        detail: entry.detail,
      };
      const range = queryRangeRef.current;
      const live = range !== null && isRangeLive(editor, range);
      // The `@` and its query go away here; the pill React renders is moved into
      // that place by the layout effect above.  No placeholder node is left
      // behind, so a pick cannot leave stray markup in the draft.
      if (live && range !== null) {
        range.deleteContents();
        const seat = document.createRange();
        seat.setStart(range.startContainer, range.startOffset);
        seat.collapse(true);
        pendingCaretRef.current = seat;
      } else {
        // No live query (a stale range after a re-render): the pill lands at the
        // end, which is where an unplaceable pick belongs.
        pendingCaretRef.current = null;
      }
      queryRangeRef.current = null;
      queryRef.current = '';
      setMention(null);

      pillsRef.current.set(pill.pillId, pill);
      setPills((current) => [...current, { kind: 'mention', pill }]);
    },
    [],
  );

  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>): void => {
    // An IME owns the keyboard while composing: Enter commits a candidate.
    if (event.nativeEvent.isComposing) return;

    if (mention !== null) {
      const entries = offersFor(mention.query);
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        setActiveIndex((current) => (entries.length === 0 ? 0 : (current + 1) % entries.length));
        return;
      }
      if (event.key === 'ArrowUp') {
        event.preventDefault();
        setActiveIndex((current) =>
          entries.length === 0 ? 0 : (current - 1 + entries.length) % entries.length,
        );
        return;
      }
      if ((event.key === 'Enter' || event.key === 'Tab') && entries.length > 0) {
        event.preventDefault();
        pickMention(entries[activeIndex] ?? entries[0]);
        return;
      }
      if (event.key === 'Escape') {
        event.preventDefault();
        dismissedQueryRef.current = mention.query;
        setMention(null);
        queryRangeRef.current = null;
        queryRef.current = '';
        return;
      }
    }

    if (event.key === 'Enter') {
      event.preventDefault();
      if (event.shiftKey) {
        // One break, inserted in the shape the browser itself keeps: two literal
        // newlines with the caret on the second one.  Leaving the shape to the
        // browser's editing command is what put an extra blank line in front of a
        // pasted image, while a single newline sends the next keystroke back up
        // into the line above the break.
        const editor = editorRef.current;
        if (editor !== null) insertLineBreakAtCaret(editor);
        syncEmpty();
        return;
      }
      onSubmit(snapshot());
    }
  };

  const entries = mention === null ? [] : offersFor(mention.query);

  /**
   * Drop one pill the reader removed through its own `×`.
   *
   * Removing the element by hand would desynchronize React from the DOM (the
   * next render would try to remove a node that is no longer where it left it),
   * so the pill goes away by leaving the state, exactly like the `gone` path for
   * an attachment row.
   */
  const removePill = useCallback((pillId: string) => {
    pillsRef.current.delete(pillId);
    placedPillsRef.current.delete(pillId);
    setPills((current) =>
      current.filter((row) => (row.kind === 'image' ? row.pillId : row.pill.pillId) !== pillId),
    );
  }, []);

  return (
    <div className="relative">
      {empty && (
        <span
          aria-hidden="true"
          className="pointer-events-none absolute left-3.5 top-2.5 z-10 select-none text-[13px] text-gray-500"
        >
          {placeholder}
        </span>
      )}
      <div
        ref={editorRef}
        id="console-composer"
        role="textbox"
        aria-multiline="true"
        aria-label="消息输入"
        aria-controls={mention !== null ? 'composer-mention-list' : undefined}
        aria-activedescendant={
          mention !== null && entries[activeIndex] !== undefined
            ? mentionOptionId(entries[activeIndex])
            : undefined
        }
        contentEditable
        suppressContentEditableWarning
        spellCheck={false}
        onInput={() => {
          syncEmpty();
          refreshMention();
        }}
        onKeyUp={refreshMention}
        onClick={refreshMention}
        onBlur={() => setMention(null)}
        onKeyDown={onKeyDown}
        onPaste={(event) => {
          const files = event.clipboardData?.files;
          if (files && files.length > 0) {
            // An image paste is an attachment, not text: exactly the path a
            // picked or dropped file takes.  Propagation stops here as well,
            // because the card that hosts this editor also listens for a paste
            // and would otherwise upload the same clipboard twice.
            event.preventDefault();
            event.stopPropagation();
            onFiles(files);
            return;
          }
          // A text paste keeps the insertion, but never imports arbitrary
          // clipboard markup into the draft.
          event.preventDefault();
          const text = event.clipboardData?.getData('text/plain') ?? '';
          // Whitespace-only clipboard text is not content: a clipboard holding
          // only a bitmap (a screenshot, or an image the browser exposes as
          // markup rather than as a `File`) offers exactly that, and inserting it
          // left a blank line above the image the reader pasted next.
          const editor = editorRef.current;
          if (editor !== null && text.trim() !== '') insertTextAtCaret(editor, text);
          syncEmpty();
          refreshMention();
        }}
        onDrop={(event) => {
          const files = event.dataTransfer?.files;
          if (!files || files.length === 0) return;
          event.preventDefault();
          onFiles(files);
        }}
        className="ui-composer-input fluent-scrollbar max-h-56 w-full overflow-y-auto whitespace-pre-wrap break-words bg-transparent font-sans text-gray-900 outline-none"
      >
        {pills.map((row) =>
          row.kind === 'mention' ? (
            <MentionPillView
              key={row.pill.pillId}
              pill={row.pill}
              onRemove={() => removePill(row.pill.pillId)}
            />
          ) : (
            <ImagePill
              key={row.pillId}
              pillId={row.pillId}
              entry={attachmentOf(attachments, row.localId)}
              onRemove={onRemoveAttachment}
              onHover={(element, entry) => setPreview(element === null ? null : { element, entry })}
            />
          ),
        )}
      </div>
      <HoverPreview preview={preview} />
      {mention !== null && (
        <MentionFlyout
          anchorRect={mention.rect}
          entries={entries}
          activeIndex={activeIndex}
          loading={filesLoading}
          error={filesError}
          onPick={pickMention}
          onHoverIndex={setActiveIndex}
        />
      )}
      <span className="sr-only" aria-live="polite">
        {busy ? '当前轮次仍在运行，发送将排队为插话' : ''}
      </span>
    </div>
  );
};

/**
 * One attachment rendered as a pill *inside* the composer's text.
 *
 * `contentEditable={false}` is what makes it atomic: the caret steps over it as
 * one unit and Backspace removes it whole.  Its markup is UI only — the filename
 * is shown, but the prompt text never contains it, because the serializer reads
 * the pill's id out of the registry instead of its content.
 *
 * The thumbnail itself is `AttachmentPreview`, the same component that owns the
 * object-URL lifecycle, so hovering it here and hovering it anywhere else behave
 * identically; the enlarged copy is the composer's portalled flyout.
 */
const ImagePill: React.FC<{
  pillId: string;
  entry: PendingAttachment;
  onRemove: (localId: string) => void;
  onHover: (element: HTMLElement | null, entry: PendingAttachment) => void;
}> = ({ pillId, entry, onRemove, onHover }) => {
  const uploading = entry.status === 'uploading';
  const percent =
    entry.size > 0 ? Math.min(100, Math.round((entry.uploadedBytes / entry.size) * 100)) : 0;

  return (
    <span
      {...{ [PILL_ATTRIBUTE]: '', [PILL_ID_ATTRIBUTE]: pillId }}
      contentEditable={false}
      className={`mx-0.5 inline-flex max-w-[16rem] select-none items-center gap-1.5 rounded-control border px-1 py-px align-middle font-mono text-[11px] leading-[18px] ${
        entry.status === 'failed'
          ? 'border-red-300 bg-red-50/80 text-red-700'
          : 'border-line bg-sunken text-gray-800'
      }`}
      title={entry.error ?? `${entry.name} · ${entry.mime}`}
      onMouseEnter={(event) => onHover(event.currentTarget, entry)}
      onMouseLeave={() => onHover(null, entry)}
    >
      {entry.source === undefined && entry.attachmentId !== null ? (
        // A screenshot row arrives already finalized (no local pick): its
        // thumbnail is loaded by id through the history loader.
        <AttachmentIdThumb
          attachment={{
            attachmentId: entry.attachmentId,
            imageId: null,
            name: entry.name,
            mime: entry.mime,
            size: entry.size,
            revision: null,
          }}
        />
      ) : (
        <AttachmentPreview
          source={entry.source}
          label={entry.name}
          mime={entry.mime}
          size={entry.size}
          failed={entry.status === 'failed'}
        />
      )}
      <span className="min-w-0 truncate">{entry.name}</span>
      {uploading && <span className="shrink-0 tabular-nums text-gray-500">{percent}%</span>}
      <button
        type="button"
        tabIndex={-1}
        title={uploading ? '取消上传' : '移除图片'}
        aria-label={uploading ? '取消上传' : '移除图片'}
        onMouseDown={(event) => event.preventDefault()}
        onClick={() => onRemove(entry.localId)}
        className="shrink-0 rounded-full p-0.5 text-gray-400 hover:bg-black/10 hover:text-red-600"
      >
        <Dismiss20Regular aria-hidden="true" style={{ fontSize: '12px' }} />
      </button>
    </span>
  );
};

/** One `@` pill, rendered as an atomic inline element inside the editor. */
const MentionPillView: React.FC<{ pill: ComposerMentionPill; onRemove: () => void }> = ({
  pill,
  onRemove,
}) => {
  const KindIcon = MENTION_KIND_ICON[pill.kind];
  return (
    <span
      {...{ [PILL_ATTRIBUTE]: '', [PILL_ID_ATTRIBUTE]: pill.pillId }}
      contentEditable={false}
      title={`${pill.label} · ${MENTION_KIND_LABEL[pill.kind]} · ${pill.detail}`}
      className="mx-0.5 inline-flex select-none items-center gap-1 rounded-control border border-line bg-sunken px-1.5 py-px align-middle font-mono text-[11px] leading-[18px] text-gray-800"
    >
      {/* A pill carries no kind text, so the icon names it: a file path, an Agent
          Skill and a runtime fact must not look alike in the draft. */}
      <KindIcon
        role="img"
        aria-label={MENTION_KIND_LABEL[pill.kind]}
        className="shrink-0 text-gray-500"
        style={{ fontSize: '12px' }}
      />
      <span className="max-w-[14rem] truncate">{pill.label}</span>
      <button
        type="button"
        tabIndex={-1}
        title="移除引用"
        aria-label={`移除引用 ${pill.label}`}
        onMouseDown={(event) => event.preventDefault()}
        onClick={onRemove}
        className="shrink-0 rounded-full p-0.5 text-gray-400 hover:bg-black/10 hover:text-red-600"
      >
        <Dismiss20Regular aria-hidden="true" style={{ fontSize: '12px' }} />
      </button>
    </span>
  );
};

/**
 * The enlarged copy of the hovered image.
 *
 * Portalled and anchored to the *thumbnail*, not to the pill: the pill is one
 * line tall inside a scrolling editor, and anchoring to the pill put the preview
 * either behind the composer's own acrylic or a screen away from the image it
 * describes.  Split into its own component so the object URL it needs comes from
 * a hook instead of a callback.
 */
const HoverPreview: React.FC<{
  preview: { element: HTMLElement; entry: PendingAttachment } | null;
}> = ({ preview }) => {
  const entry = preview?.entry ?? null;
  // A row with a local pick previews its own blob; a finalized screenshot row
  // (no `source`, only an id) resolves its bytes through the same by-id loader
  // the pills use, so hovering a chip always shows the real picture.  Both hooks
  // run unconditionally, with a sentinel descriptor when there is no by-id read.
  const localUrl = useAttachmentObjectUrl(entry?.source ?? EMPTY_SOURCE);
  const byIdDescriptor: TranscriptAttachment =
    entry !== null && entry.source === undefined && entry.attachmentId !== null
      ? {
          attachmentId: entry.attachmentId,
          imageId: null,
          name: entry.name,
          mime: entry.mime,
          size: entry.size,
          revision: null,
        }
      : NO_ATTACHMENT;
  const byId = useAttachmentResource(byIdDescriptor);
  if (preview === null || entry === null) return null;
  const byIdOnly = entry.source === undefined;
  const url = byIdOnly ? (byId?.status === 'ready' ? byId.url : null) : localUrl;
  const failed =
    byIdOnly && byId !== null && (byId.status === 'error' || byId.status === 'unsupported');
  const pending = url === null && !failed;
  return (
    <ImagePreviewFlyout
      anchor={preview.element}
      url={url}
      pending={pending}
      failed={failed}
      label={preview.entry.name}
      mime={preview.entry.mime}
      size={preview.entry.size}
    />
  );
};

/** A source that never becomes a URL, so the hook can be called unconditionally. */
const EMPTY_SOURCE = {
  name: '',
  type: '',
  size: 0,
  arrayBuffer: async (): Promise<ArrayBuffer> => new ArrayBuffer(0),
};

/**
 * Drop everything the reader typed, leaving React's own pill nodes alone.
 *
 * A pill is rendered by React *into* the editable element, so removing one by
 * hand desynchronizes React from the DOM: the next render tries to remove a node
 * that is no longer where it left it and throws (`removeChild` on a node that is
 * not a child), which unmounts the whole console.  Clearing the state is what
 * removes a pill — React does it during reconciliation — so this helper only
 * touches the nodes the browser created around them.
 */
function clearUserContent(editor: HTMLElement): void {
  for (const child of Array.from(editor.childNodes)) {
    if (child.nodeType === ELEMENT_NODE && (child as HTMLElement).hasAttribute(PILL_ATTRIBUTE)) {
      continue;
    }
    editor.removeChild(child);
  }
}

/** Whether the editor holds nothing but (at most) an empty line. */
function isEmptyEditor(editor: HTMLElement): boolean {
  // Line breaks and caret anchors are not content: an editor holding only those
  // is still empty (the placeholder stays, the primary button stays disabled).
  const text = stripCaretAnchors(editor.textContent ?? '').replace(/\n/g, '');
  if (text.trim() !== '') return false;
  // A pill is content even when it contributes no text: an attachment-only turn
  // is legal on the wire.
  return editor.querySelector(`[${PILL_ATTRIBUTE}]`) === null;
}

/** The store row one image pill stands for (missing only during teardown). */
function attachmentOf(attachments: readonly PendingAttachment[], localId: string): PendingAttachment {
  return (
    attachments.find((entry) => entry.localId === localId) ?? {
      localId,
      name: 'image',
      mime: '',
      size: 0,
      status: 'failed',
      uploadedBytes: 0,
      attachmentId: null,
      error: null,
      source: EMPTY_SOURCE,
    }
  );
}
