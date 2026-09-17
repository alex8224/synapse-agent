/**
 * Caret and `@`-query bookkeeping for the composer's editable surface.
 *
 * Everything here is deliberately expressed in terms of the editor element
 * itself rather than the document: a stale `Range` captured before a re-render
 * can point into a detached subtree, and deleting through it would either throw
 * or remove the wrong text.  Every helper therefore re-checks that both ends of
 * a range still belong to the live editor before anything is mutated.
 */

import { CARET_ANCHOR, PILL_ATTRIBUTE } from './composerDocument.ts';

/** Whether `node` is inside `root` (or is `root`). */
export function containsNode(root: Node, node: Node | null): boolean {
  return node !== null && (root === node || root.contains(node));
}

/** The editor's current selection, or null when it is somewhere else entirely. */
export function editorSelection(root: HTMLElement): Selection | null {
  const selection = window.getSelection();
  if (selection === null || selection.rangeCount === 0) return null;
  const range = selection.getRangeAt(0);
  if (!containsNode(root, range.startContainer) || !containsNode(root, range.endContainer)) {
    return null;
  }
  return selection;
}

/** A collapsed range at the very end of the editor's content. */
export function endOfEditor(root: HTMLElement): Range {
  const range = document.createRange();
  range.selectNodeContents(root);
  range.collapse(false);
  return range;
}

/** Move the caret to a range, replacing any existing selection. */
export function placeCaret(range: Range): void {
  const selection = window.getSelection();
  if (selection === null) return;
  selection.removeAllRanges();
  selection.addRange(range);
}

/** A collapsed caret immediately after `node`. */
export function caretAfter(node: Node): Range {
  const range = document.createRange();
  range.setStartAfter(node);
  range.collapse(true);
  return range;
}

/**
 * The `@` query the caret currently sits in, or null.
 *
 * A mention is only started at a caret (a selection is never a query) and only
 * when the `@` begins a token — preceded by whitespace, a line start, or the
 * editor's own start — so an email address or a decorator in pasted code does
 * not open the flyout.  The query itself must stay on one line.
 */
export interface MentionQuery {
  /** The range covering `@` plus everything typed after it. */
  range: Range;
  /** What the reader typed after `@`. */
  query: string;
}

export function mentionQueryAt(root: HTMLElement): MentionQuery | null {
  const selection = editorSelection(root);
  if (selection === null || !selection.isCollapsed) return null;
  const caret = selection.getRangeAt(0);
  const node = caret.startContainer;
  if (node.nodeType !== Node.TEXT_NODE) return null;

  const before = (node.nodeValue ?? '').slice(0, caret.startOffset);
  const at = before.lastIndexOf('@');
  if (at === -1) return null;
  // A caret anchor is a boundary too: `@` typed right after a pasted image must
  // still open the list.
  if (at > 0 && !/[\s\u200B]/.test(before[at - 1])) return null;

  const query = before.slice(at + 1);
  // A second `@`, a newline or a long paste means this is not a mention query.
  if (query.includes('@') || query.includes('\n') || query.length > 64) return null;

  const range = document.createRange();
  range.setStart(node, at);
  range.setEnd(node, caret.startOffset);
  return { range, query };
}

/** Whether a range still points into the live editor (re-checked before use). */
export function isRangeLive(root: HTMLElement, range: Range): boolean {
  return containsNode(root, range.startContainer) && containsNode(root, range.endContainer);
}

/**
 * Insert plain text at the caret, as one literal text node.
 *
 * Deliberately *not* `document.execCommand('insertText', …)`: that lets the
 * browser pick the DOM shape, and its choice does not match this editor's model.
 * A newline becomes two block `<div>` wrappers (two lines rendered, and the wrong
 * number of newlines projected onto the wire), and a whitespace-only payload
 * still mutates the tree.  Here the payload is one text node holding its own
 * literal `\n`s, which is exactly what the serializer reads back and what
 * `white-space: pre-wrap` renders as line breaks.
 */
export function insertTextAtCaret(root: HTMLElement, text: string): void {
  const selection = editorSelection(root);
  const range = selection !== null ? selection.getRangeAt(0) : endOfEditor(root);
  range.deleteContents();
  const node = document.createTextNode(text);
  range.insertNode(node);
  caretInTextAfter(node);
}

/**
 * Insert one line break at the caret, in the shape the browser itself keeps.
 *
 * `document.execCommand('insertLineBreak')` leaves two literal `"\n"` text nodes
 * here and puts the caret at the start of the second one: the first is the break,
 * the second is the empty line the caret sits on (and which the next keystroke
 * replaces).  That shape is reproduced explicitly rather than asked for, because
 * the browser's own answer varies with context — it is what put an extra blank
 * line in front of a pasted image, and a single `"\n"` (or a `<br>`) instead
 * sends the following keystroke back into the line above the break.
 */
export function insertLineBreakAtCaret(root: HTMLElement): void {
  const selection = editorSelection(root);
  const range = selection !== null ? selection.getRangeAt(0) : endOfEditor(root);
  range.deleteContents();
  const seat = document.createTextNode('\n');
  range.insertNode(seat);
  seat.before(document.createTextNode('\n'));
  const after = document.createRange();
  after.setStart(seat, 0);
  after.collapse(true);
  placeCaret(after);
}

/**
 * Put the caret at the start of a text node immediately after `node`.
 *
 * A container-level caret (an offset *between* two child nodes) is not enough: a
 * keystroke then lands in the previous text node, which silently joins the line
 * the caret was supposed to leave behind — that is what made a pasted image pill
 * end up *after* the text typed next, and what put the text after a line break
 * back onto the first line.  An explicit text node gives the caret somewhere
 * real to sit, which is the shape the browser itself keeps after a line break.
 */
export function caretInTextAfter(node: ChildNode): void {
  const next = node.nextSibling;
  const tail =
    next !== null && next.nodeType === Node.TEXT_NODE
      ? (next as Text)
      : document.createTextNode(CARET_ANCHOR);
  if (tail !== next) node.after(tail);
  const range = document.createRange();
  range.setStart(tail, 0);
  range.collapse(true);
  placeCaret(range);
}

/** The pill element a range's start container sits in, or null. */
export function pillAtSelection(root: HTMLElement): HTMLElement | null {
  const selection = editorSelection(root);
  if (selection === null) return null;
  const start = selection.getRangeAt(0).startContainer;
  const element = start.nodeType === Node.ELEMENT_NODE ? (start as HTMLElement) : start.parentElement;
  return element?.closest<HTMLElement>(`[${PILL_ATTRIBUTE}]`) ?? null;
}
