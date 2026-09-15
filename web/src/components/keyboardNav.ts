/**
 * Roving keyboard navigation for the console's dialogs and popovers.
 *
 * Every box here is opened by a *trigger* -- a header chip, the composer's `+`,
 * F2 / F5, a sidebar button -- and the trigger keeps the focus.  A keydown
 * listener placed on the box itself therefore never fires: no keystroke is ever
 * delivered *inside* the box, so its rows stay mouse-only however many of them
 * are real `<button>`s.  That is the whole bug this hook exists for.
 *
 * `useDialogKeyboardNav` gives every box the same contract:
 *
 *   - on open, focus moves *into* the box (`initialSelector`, else its first
 *     control, else the box itself -- hence `tabIndex={-1}` on the box),
 *   - ArrowUp / ArrowDown walk the box's controls in reading order and wrap at
 *     both ends, so every row is reachable and Enter / Space activates it,
 *   - focus goes back to the trigger when the box closes,
 *   - a row the box replaced itself (a listing redrawn after a drill-in, a
 *     refreshed status) does not leave the box keyboard-dead again: focus is
 *     recovered when it has fallen back to `<body>`.
 *
 * `active` means "the box is open *and* its rows are rendered": a panel whose
 * rows arrive from the runtime (git status, a directory listing) must wait for
 * the list's first paint, otherwise the initial focus would land on a header
 * control instead of the first row.
 */
import React, { useCallback, useEffect } from 'react';

/** Controls the arrow keys move between, in DOM order. */
const CONTROL_SELECTOR = [
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  '[tabindex]:not([tabindex="-1"]):not([disabled])',
].join(', ');

function controlsOf(box: HTMLElement): HTMLElement[] {
  return Array.from(box.querySelectorAll<HTMLElement>(CONTROL_SELECTOR));
}

function isDisabled(element: HTMLElement): boolean {
  return (element as { disabled?: boolean }).disabled === true;
}

/**
 * A `<select>` and a `<textarea>` use the arrows themselves (option, caret); a
 * text input does not, which is why the pickers' filter field can hand
 * ArrowDown to the rows below it.
 */
function ownsArrowKeys(element: Element | null): boolean {
  const tag = element?.tagName;
  return tag === 'SELECT' || tag === 'TEXTAREA';
}

/** Focus the box's first choice: `initialSelector`, else its first control, else the box. */
function focusInto(box: HTMLElement, initialSelector?: string): void {
  const preferred =
    initialSelector === undefined ? null : box.querySelector<HTMLElement>(initialSelector);
  const target = preferred !== null && !isDisabled(preferred) ? preferred : controlsOf(box)[0] ?? box;
  target.focus();
}

export function useDialogKeyboardNav<T extends HTMLElement>(
  ref: React.RefObject<T | null>,
  active: boolean,
  initialSelector?: string,
): (event: React.KeyboardEvent<HTMLElement>) => void {
  // Focus follows the open/close edge rather than the mount: the popovers live
  // inside their trigger's component, so only their contents are conditional.
  useEffect(() => {
    if (!active) return;
    const box = ref.current;
    if (box === null) return;
    const trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    focusInto(box, initialSelector);
    return () => {
      // The trigger usually outlives the box. When a state change unmounted it
      // (a chip the closed dialog's own action removed), focus is left where it
      // is instead of being handed to a detached node.
      if (trigger !== null && trigger.isConnected) trigger.focus();
    };
  }, [active, initialSelector, ref]);

  // Focus that fell back to `<body>` means the focused row was removed under it
  // (a re-listed directory, a reloaded status): pull it back into the box so the
  // arrows keep working. A focus the user moved somewhere real is never stolen.
  useEffect(() => {
    if (!active) return;
    const box = ref.current;
    if (box === null) return;
    let timer: number | null = null;
    const onFocusOut = (): void => {
      if (timer !== null) return;
      timer = window.setTimeout(() => {
        timer = null;
        if (document.activeElement !== document.body) return;
        focusInto(box, initialSelector);
      }, 0);
    };
    box.addEventListener('focusout', onFocusOut);
    return () => {
      box.removeEventListener('focusout', onFocusOut);
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [active, initialSelector, ref]);

  return useCallback((event: React.KeyboardEvent<HTMLElement>) => {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
    if (ownsArrowKeys(document.activeElement)) return;
    const controls = controlsOf(event.currentTarget);
    if (controls.length === 0) return;
    const current = controls.indexOf(document.activeElement as HTMLElement);
    const step = event.key === 'ArrowDown' ? 1 : -1;
    // Focus still on the trigger (or on the box itself): enter the list from the
    // matching end instead of jumping into its middle.
    const next =
      current === -1
        ? step === 1
          ? 0
          : controls.length - 1
        : (current + step + controls.length) % controls.length;
    event.preventDefault();
    controls[next]?.focus();
  }, []);
}
