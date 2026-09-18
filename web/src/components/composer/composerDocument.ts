/**
 * The composer's document model.
 *
 * The editable card holds three kinds of node: plain text, line breaks, and
 * *atomic pills*.  A pill is a `contentEditable={false}` element whose content
 * is UI only — the meaning lives in the registry below, keyed by the pill's
 * local id.  Nothing is ever read back out of the DOM: a pill's label, its
 * serialized token and (for an image) its attachment id all come from the
 * registry, so a tooltip, a truncated filename or a progress overlay can never
 * leak into the prompt that is submitted.
 *
 * That separation is the whole point.  The wire contract is unchanged
 * (`submitPrompt(text)` plus the store's own attachment refs), so the rich
 * editor has to *project* back onto it: text out of text nodes, `\n` out of a
 * line break, a token out of a mention pill, and — for an image — nothing in the
 * text at all, because the image travels as an attachment ref the store already
 * owns.  Serializing structurally (instead of reading `innerText`) is what keeps
 * a multi-line paste, a pill mid-sentence and a trailing newline all exact.
 */

/** The three mention kinds the `@` flyout offers, plus the image pill. */
export type ComposerPillKind = 'file' | 'skill' | 'context' | 'image';

/** One non-image pill: what the `@` flyout inserted. */
export interface ComposerMentionPill {
  kind: 'file' | 'skill' | 'context';
  /** Local, DOM-only identity (never sent anywhere). */
  pillId: string;
  /** Canonical token the model receives (`@src/app.py`, `@skill:cua-driver`). */
  token: string;
  /** Visible label inside the pill. */
  label: string;
  /** Full description, used for the pill's tooltip. */
  detail: string;
}

/** One image pill: bound to a store row by the row's own local id. */
export interface ComposerImagePill {
  kind: 'image';
  pillId: string;
  /**
   * The `PendingAttachment.localId` this pill stands for.  The store owns the
   * bytes and the upload; the pill only points at the row, so a deletion in
   * either direction can be reconciled without copying state.
   */
  localId: string;
}

export type ComposerPill = ComposerMentionPill | ComposerImagePill;

/**
 * The two DOM node types this module cares about, written out rather than read
 * from the `Node` global: the serializer is pure tree-walking, so it must be
 * exercisable without a DOM present (the offline test runner has none).
 */
const TEXT_NODE = 3;
const ELEMENT_NODE = 1;

/** Everything the serializer needs that is not in the DOM. */
export interface ComposerRegistry {
  /** Pills by their DOM-only local id. */
  pills: Map<string, ComposerPill>;
}

/** Attributes the editor stamps on a pill element so it can be recognized. */
export const PILL_ATTRIBUTE = 'data-composer-pill';

/** Attribute holding the pill's local id. */
export const PILL_ID_ATTRIBUTE = 'data-pill-id';

/**
 * Zero-width space used as a caret anchor.
 *
 * An insertion point after an atomic pill (or after a pasted block) needs a real
 * text node to live in: a caret parked *between* two nodes makes the next
 * keystroke land in the text before the pill, which puts the reader's typing to
 * the left of the image they just pasted.  An *empty* text node is not enough —
 * the browser drops it while normalizing the editable, and the caret snaps back.
 * The anchor therefore carries one zero-width space, which renders as nothing and
 * is stripped from the projection below, so it can never reach the prompt.
 */
export const CARET_ANCHOR = '\u200B';

/** Remove every caret anchor from a text value (never part of the prompt). */
export function stripCaretAnchors(value: string): string {
  return value.split(CARET_ANCHOR).join('');
}

/** What one submit needs: the prompt text, and the images that ride beside it. */
export interface ComposerSnapshot {
  /** The prompt exactly as typed (never trimmed here; the store trims). */
  text: string;
  /** Image pill local ids in document order (mapped to refs by the caller). */
  imageLocalIds: string[];
}

/** Empty snapshot, used before the editor has mounted. */
export const EMPTY_SNAPSHOT: ComposerSnapshot = { text: '', imageLocalIds: [] };

/**
 * Project one editable subtree onto the wire contract.
 *
 * Walks the DOM structurally rather than reading `innerText`: the browser's own
 * flattening depends on layout (a `<div>` block reads as a newline only when it
 * is displayed as one), and it would also pick up the pills' labels and the
 * delete buttons' text.  Here a text node contributes its text, an element
 * marked as a pill contributes its registry entry, a `<br>` contributes one
 * newline, and any other element is transparent (its children are walked) —
 * which is what makes a pasted multi-line block, a `contenteditable` block
 * wrapper, and an inline `<span>` all behave the same way.
 */
export function serializeComposer(root: Node | null, registry: ComposerRegistry): ComposerSnapshot {
  if (root === null) return EMPTY_SNAPSHOT;
  const imageLocalIds: string[] = [];
  const text = walk(root, registry, imageLocalIds);
  return { text, imageLocalIds };
}

function walk(node: Node, registry: ComposerRegistry, imageLocalIds: string[]): string {
  let out = '';
  for (const child of Array.from(node.childNodes)) {
    if (child.nodeType === TEXT_NODE) {
      out += stripCaretAnchors(child.nodeValue ?? '');
      continue;
    }
    if (child.nodeType !== ELEMENT_NODE) continue;
    const element = child as HTMLElement;

    const pillId = element.getAttribute(PILL_ID_ATTRIBUTE);
    if (element.hasAttribute(PILL_ATTRIBUTE) && pillId !== null) {
      const pill = registry.pills.get(pillId);
      if (pill === undefined) continue;
      if (pill.kind === 'image') {
        // An image never becomes prompt text: it travels as an attachment ref.
        imageLocalIds.push(pill.localId);
        continue;
      }
      out += pill.token;
      continue;
    }

    const tag = element.tagName;
    if (tag === 'BR') {
      out += '\n';
      continue;
    }
    out += walk(element, registry, imageLocalIds);
    // A block wrapper (a pasted paragraph, a browser-inserted <div>) separates
    // its content from the next sibling the way a newline does.  Skipping this
    // would glue two pasted lines into one.
    if (isBlockWrapper(tag) && !isLastMeaningfulChild(node, child)) out += '\n';
  }
  return out;
}

/** Tags a browser inserts to wrap a pasted line. */
const BLOCK_WRAPPERS = new Set(['DIV', 'P', 'LI', 'BLOCKQUOTE', 'PRE', 'H1', 'H2', 'H3']);

function isBlockWrapper(tag: string): boolean {
  return BLOCK_WRAPPERS.has(tag);
}

/** Whether `child` is the last node that can contribute anything. */
function isLastMeaningfulChild(parent: Node, child: Node): boolean {
  let seen = false;
  for (const sibling of Array.from(parent.childNodes)) {
    if (sibling === child) {
      seen = true;
      continue;
    }
    if (!seen) continue;
    if (sibling.nodeType === TEXT_NODE && (sibling.nodeValue ?? '') === '') continue;
    return false;
  }
  return true;
}

/**
 * Whether a snapshot would submit nothing.
 *
 * The store applies the same rule to its own attachment refs (an
 * attachment-only turn is legal), so the composer only has to answer "is there
 * anything to send at all" to decide whether the primary button is enabled.
 */
export function isSnapshotEmpty(snapshot: ComposerSnapshot): boolean {
  return stripCaretAnchors(snapshot.text).trim() === '' && snapshot.imageLocalIds.length === 0;
}

/** Monotonic local id for pills (DOM-only identity, never sent). */
let pillSeq = 0;

export function nextPillId(): string {
  pillSeq += 1;
  return `pill-${pillSeq}`;
}
