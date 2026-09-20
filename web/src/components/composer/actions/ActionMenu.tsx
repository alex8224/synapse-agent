/**
 * The composer's bottom-left "add" control: a menu of actions.
 *
 * It used to be a bare `+` that opened the image picker directly.  The picker is
 * now one row of a general menu (see `composer/actions/`), so this control is a
 * *host*: it owns only what every row shares — the trigger, the one open
 * popover, the keyboard contract, the click-outside and focus-out dismissal and
 * the activation guard — and paints whatever `COMPOSER_ACTIONS` declares.  A row
 * that is not wired yet is painted `aria-disabled` with its own explanation and
 * cannot run, so a screenshot row never fakes a capture.
 *
 * Each row paints its own mark (`action.icon`): the host keeps no glyph table to
 * grow, so adding an action is one module plus one manifest line and nothing
 * here changes.
 *
 * The menu hangs *above* the trigger (the composer sits at the bottom of the
 * window) and is portalled through `FloatingPanel`, so the card's acrylic layer
 * cannot clip it and its `backdrop-filter` cannot trap it.
 *
 * The rows are portalled to `document.body`, i.e. outside the composer's
 * `<form>`: activating one can never submit the turn.  The trigger is a
 * `type="button"` for the same reason.
 */
import { Add20Regular } from '@fluentui/react-icons';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { FloatingPanel } from '../../FloatingPanel.tsx';
import { COMPOSER_ACTIONS } from './manifest.ts';
import {
  isComposerActionNavKey,
  isRunnable,
  nextActionIndex,
  type ComposerActionDefinition,
} from './contract.ts';

export interface ActionMenuProps {
  /** Rows painted by this host; the default is the complete composer menu. */
  actions?: readonly ComposerActionDefinition<React.ReactNode>[];
  /** Accessible name shared by the trigger and its menu. */
  menuLabel?: string;
  /** Trigger contents; the default is the compact plus icon. */
  triggerContent?: React.ReactNode;
  /** Trigger class; the default is the square compact icon button. */
  triggerClassName?: string;
  /**
   * Open the composer's own image picker; a picked file takes the same
   * `handleFiles` path a paste or a drop takes.
   */
  onPickImages: () => void;
  /** Queue one window-capture task through the runtime. */
  onStartWindowScreenshot: () => void;
  /** Open the capture tool's own settings window. */
  onOpenScreenshotSettings: () => void;
}

export const ActionMenu: React.FC<ActionMenuProps> = ({
  actions = COMPOSER_ACTIONS,
  menuLabel = '添加内容',
  triggerContent,
  triggerClassName = 'ui-icon-button',
  onPickImages,
  onStartWindowScreenshot,
  onOpenScreenshotSettings,
}) => {
  const [open, setOpen] = useState(false);
  // The element the popover hangs from is part of the open state, not read from
  // a ref during render: the trigger's own `onClick` hands its element in.
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);

  /** Whether `node` is the trigger or inside the open menu. */
  const insideControl = useCallback(
    (node: Node | null): boolean =>
      node !== null &&
      ((triggerRef.current?.contains(node) ?? false) ||
        (panelRef.current?.contains(node) ?? false)),
    [],
  );

  const close = useCallback((restoreFocus = false) => {
    setOpen(false);
    setError(null);
    // Escape and a completed activation hand focus back to the trigger.  Tab
    // and an outside click do not: the reader has moved on, and pulling focus
    // back would fight the move they asked for.
    if (restoreFocus) triggerRef.current?.focus();
  }, []);

  const openMenu = useCallback((element: HTMLElement) => {
    setError(null);
    setAnchor(element);
    setOpen(true);
  }, []);

  // A click anywhere but the trigger and the panel closes the menu.
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      const target = event.target as Node | null;
      if (target === null || insideControl(target)) return;
      close();
    };
    document.addEventListener('mousedown', onPointerDown);
    return () => document.removeEventListener('mousedown', onPointerDown);
  }, [open, close, insideControl]);

  // Focus that lands outside the trigger and the menu (a programmatic focus, a
  // tab into the editor) dismisses the menu, so its window listener can never
  // linger armed while the reader works somewhere else.  A focus-out with no
  // destination is left to the mousedown handler above: clicking a
  // non-focusable spot inside the panel must not close it.
  useEffect(() => {
    if (!open) return;
    let timer: number | null = null;
    const onFocusOut = (event: FocusEvent) => {
      const next = event.relatedTarget as Node | null;
      // No destination (a click on nothing) is the mousedown handler's job.
      if (next === null || insideControl(next)) return;
      // Re-check on the next tick: on the open edge the panel's ref is attached
      // one commit *after* the trigger hands focus to the first row, so a
      // synchronous check would read a still-unset ref and close the menu it
      // just opened.
      if (timer !== null) return;
      timer = window.setTimeout(() => {
        timer = null;
        if (insideControl(document.activeElement)) return;
        close();
      }, 0);
    };
    window.addEventListener('focusout', onFocusOut);
    return () => {
      window.removeEventListener('focusout', onFocusOut);
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [open, close, insideControl]);

  // The keyboard contract.
  //
  // It listens on `window` so Escape still closes the menu after focus has left
  // the rows, but every other key is scoped to the menu: the arrows / Home / End
  // move the rows only while focus is *inside the panel*, so a reader who has
  // moved on to the editor keeps the editor's own keys.  `Tab` dismisses the
  // menu and is otherwise left alone — the browser moves focus where the reader
  // asked, and the menu neither blocks it nor pulls focus back.
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Tab') {
        close();
        return;
      }
      if (event.key === 'Escape') {
        event.preventDefault();
        close(true);
        return;
      }
      if (!isComposerActionNavKey(event.key)) return;
      const box = panelRef.current;
      if (box === null || !box.contains(document.activeElement)) return;
      const rows = Array.from(box.querySelectorAll<HTMLElement>('[role="menuitem"]'));
      if (rows.length === 0) return;
      const current = rows.indexOf(document.activeElement as HTMLElement);
      event.preventDefault();
      rows[nextActionIndex(rows.length, current, event.key)]?.focus();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [open, close]);

  // Focus the first row the moment the panel paints.  A ref callback — not an
  // effect on `open` — is what makes this reliable: `FloatingPanel` paints its
  // children one commit after it mounts (it measures its anchor first), so an
  // `open` effect would run while the panel is still absent and never re-run.
  const focusMenu = useCallback((node: HTMLDivElement | null) => {
    if (node === null) return;
    const first = node.querySelector<HTMLElement>('[role="menuitem"]');
    (first ?? node).focus();
  }, []);

  const activate = useCallback(
    async (action: ComposerActionDefinition<React.ReactNode>) => {
      // A disabled row (no `run`) or one that is already running does nothing.
      if (!isRunnable(action) || busy) return;
      try {
        setBusy(true);
        await action.run?.({
          pickImages: onPickImages,
          startWindowScreenshot: onStartWindowScreenshot,
          openScreenshotSettings: onOpenScreenshotSettings,
        });
        close(true);
      } catch (err) {
        // A failed action keeps the menu open and says why, instead of closing
        // as if it had worked.
        setError(err instanceof Error && err.message ? err.message : String(err));
      } finally {
        setBusy(false);
      }
    },
    [busy, close, onPickImages, onStartWindowScreenshot, onOpenScreenshotSettings],
  );

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        onClick={(event) => {
          if (open) close();
          else openMenu(event.currentTarget);
        }}
        title={menuLabel}
        aria-label={menuLabel}
        aria-haspopup="menu"
        aria-expanded={open}
        className={triggerClassName}
      >
        {triggerContent ?? <Add20Regular aria-hidden="true" />}
      </button>
      {open && (
        <FloatingPanel
          anchor={anchor}
          side="top"
          align="start"
          offset={8}
          className="composer-action-menu-panel w-64 max-w-[calc(100vw-2rem)] rounded-card border border-line/80 material-flyout flyout-in p-1.5 shadow-flyout"
          panelRef={panelRef}
        >
          <div
            ref={focusMenu}
            role="menu"
            aria-label={menuLabel}
            tabIndex={-1}
            className="flex flex-col gap-0.5 outline-none"
          >
            {actions.map((action) => {
              const runnable = isRunnable(action) && !busy;
              return (
                <button
                  key={action.id}
                  type="button"
                  role="menuitem"
                  data-action={action.id}
                  aria-disabled={!runnable}
                  title={action.detail}
                  onClick={() => {
                    void activate(action);
                  }}
                  className={`ui-menu-item flex items-center gap-2 text-left ${
                    runnable ? 'text-gray-700' : 'text-gray-400'
                  }`}
                >
                  {action.icon}
                  <span className="flex min-w-0 flex-col">
                    <span className="truncate">{action.label}</span>
                    <span className="truncate text-[11px] font-normal text-gray-500">
                      {action.detail}
                    </span>
                  </span>
                </button>
              );
            })}
            {error !== null && (
              <div
                role="alert"
                className="mx-1 mt-1 rounded border border-red-200 bg-red-50/70 px-2 py-1 text-[11px] leading-relaxed text-red-700"
              >
                {error}
              </div>
            )}
          </div>
        </FloatingPanel>
      )}
    </>
  );
};
