/**
 * Caret and `@`-query bookkeeping for the composer's editable surface.
 *
 * Everything here is deliberately expressed in terms of the editor element
 * itself rather than the document: a stale `Range` captured before a re-render
 * can point into a detached subtree, and deleting through it would either throw
 * or remove the wrong text.  Every helper therefore re-checks that both ends of
 * a range still belong to the live editor before anything is mutated.
 */

import { PILL_ATTRIBUTE } from './composerDocument.ts';

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
  if (at > 0 && !/\s/.test(before[at - 1])) return null;

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

/** The pill element a range's start container sits in, or null. */
export function pillAtSelection(root: HTMLElement): HTMLElement | null {
  const selection = editorSelection(root);
  if (selection === null) return null;
  const start = selection.getRangeAt(0).startContainer;
  const element = start.nodeType === Node.ELEMENT_NODE ? (start as HTMLElement) : start.parentElement;
  return element?.closest<HTMLElement>(`[${PILL_ATTRIBUTE}]`) ?? null;
}
