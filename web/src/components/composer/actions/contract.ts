/**
 * The composer action menu's registration contract.
 *
 * The composer's bottom-left control is a menu of *actions*, not a bare
 * "insert image" button.  Like the status strip, it grows by adding one module
 * under `composer/actions/` plus one line in `manifest.ts` — there is no mutable
 * registry, no runtime registration and no plugin surface.
 *
 * This module is deliberately free of React and of the store, so `node --test`
 * can exercise the registry rules directly:
 *
 *  - **id**: stable and unique; it is the row's React key and its `data-action`.
 *  - **availability**: an action is runnable *exactly* when it carries a `run`.
 *    An action with no `run` (a screenshot row whose runtime call is not wired
 *    yet) is painted `aria-disabled` and activating it does nothing — it never
 *    fakes a result.  Its `detail` is the visible explanation, so a reader sees
 *    *why* it is disabled.
 *  - **icon**: the row's mark is whatever the action itself puts in `icon`; the
 *    type is generic (defaulting to `unknown`), so this module names no view
 *    library and the host has no glyph table to grow.
 *  - **context**: an action receives a typed, narrow context — a named
 *    capability, never the console store.
 *  - **navigation**: `nextActionIndex` walks every row (a disabled row is
 *    reachable, so a reader can land on a screenshot row and read why it cannot
 *    run), wrapping at both ends for the arrows, with Home / End jumping to the
 *    ends.
 */

/**
 * What an action may ask the composer host to do.
 *
 * Deliberately narrow: an action gets the one capability it needs, never the
 * store.  `pickImages` opens the host's own image picker, so a picked file takes
 * exactly the `handleFiles` path a paste or a drop already takes.
 */
export interface ComposerActionContext {
  /** Open the composer's image picker (the same path as a paste or a drop). */
  pickImages: () => void;
  /**
   * Queue one window-capture task.  The runtime starts the tool and polls it;
   * the action never touches the tool, a store or the DOM itself.
   */
  startWindowScreenshot: () => void | Promise<void>;
  /** Open (or focus) the capture tool's own settings window. */
  openScreenshotSettings: () => void | Promise<void>;
}

/**
 * One row of the composer action menu.
 *
 * `Icon` is the mark the row paints: the action owns it, so the host only
 * places it and never switches on a glyph.  The default `unknown` keeps this
 * module free of React and of the view layer.
 */
export interface ComposerActionDefinition<Icon = unknown> {
  /** Stable id: the row's React key and its `data-action`. */
  id: string;
  /** Row label. */
  label: string;
  /** One-line explanation under the label; for a disabled row it says why. */
  detail: string;
  /** The row's mark, painted by the action itself. */
  icon: Icon;
  /**
   * Run the action.  Present *exactly* when the action is runnable: a row with
   * no `run` is painted `aria-disabled` and activating it does nothing.
   */
  run?: (context: ComposerActionContext) => void | Promise<void>;
}

/** Whether a row can be activated.  The presence of `run` is the whole answer. */
export function isRunnable<Icon>(action: ComposerActionDefinition<Icon>): boolean {
  return action.run !== undefined;
}

/**
 * Validate a manifest and hand it back unchanged.
 *
 * Throws on a malformed registry (a blank or duplicate id, a blank label or
 * detail) so a bad entry fails where it is written instead of becoming a silent
 * dead row.
 */
export function resolveComposerActions<Icon>(
  manifest: readonly ComposerActionDefinition<Icon>[],
): readonly ComposerActionDefinition<Icon>[] {
  const seen = new Set<string>();
  for (const action of manifest) {
    if (action.id === '' || seen.has(action.id)) {
      throw new Error(`composer action id must be unique and non-empty: '${action.id}'`);
    }
    seen.add(action.id);
    if (action.label === '' || action.detail === '') {
      throw new Error(`composer action '${action.id}' needs a label and a detail`);
    }
  }
  return manifest;
}

/** The action registered under `id`, or `undefined`. */
export function actionById<Icon>(
  manifest: readonly ComposerActionDefinition<Icon>[],
  id: string,
): ComposerActionDefinition<Icon> | undefined {
  return manifest.find((action) => action.id === id);
}

/** The keys the menu's rows answer to. */
export type ComposerActionNavKey = 'ArrowDown' | 'ArrowUp' | 'Home' | 'End';

/** Narrow a `KeyboardEvent.key` to the menu's navigation keys. */
export function isComposerActionNavKey(key: string): key is ComposerActionNavKey {
  return key === 'ArrowDown' || key === 'ArrowUp' || key === 'Home' || key === 'End';
}

/**
 * The row a navigation key moves focus to.
 *
 * `current` is the focused row's index, or `-1` when focus is still on the
 * trigger / the menu box.  Every row is reachable — including an `aria-disabled`
 * one, so a reader can land on a screenshot row and read why it cannot run.  The
 * arrows wrap at both ends; Home / End jump to the ends.
 */
export function nextActionIndex(
  count: number,
  current: number,
  key: ComposerActionNavKey,
): number {
  if (count <= 0) return -1;
  switch (key) {
    case 'Home':
      return 0;
    case 'End':
      return count - 1;
    case 'ArrowDown':
      return current < 0 ? 0 : (current + 1) % count;
    case 'ArrowUp':
      return current < 0 ? count - 1 : (current - 1 + count) % count;
  }
}
