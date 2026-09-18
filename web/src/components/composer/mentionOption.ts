import type { MentionEntry } from './mentionCatalog.ts';

/**
 * Stable DOM id for one `@` option.
 *
 * The editor keeps the focus while the list is open, so the row the arrows are
 * on is named by `aria-activedescendant` rather than by moving focus into the
 * list.  That means the id has to be derivable from the entry alone (the editor
 * and the flyout must agree without passing anything around) and stable across a
 * re-filter, so walking the arrows does not rename the row under the caret.
 */
export function mentionOptionId(entry: MentionEntry): string {
  return `composer-mention-${entry.kind}-${entry.id}`;
}
