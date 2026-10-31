import { Delete20Regular, Edit20Regular } from '@fluentui/react-icons';
import React, { useEffect, useRef } from 'react';
import { FloatingPanel } from './FloatingPanel.tsx';

/**
 * The overflow menu of one sidebar session row.
 *
 * A row used to carry four hover actions -- copy, fork, rename, delete -- which cost
 * the title roughly a third of the rail's width for controls a reader reaches for
 * occasionally.  Copy and fork stay on the row because they are the two a reader
 * uses while *reading* the list (they leave the session untouched); the two that
 * rewrite it hang off this menu.
 *
 * It is a `FloatingPanel` for the reason the shared shell exists: the rail is a
 * chrome surface with a `backdrop-filter`, so a menu laid out inside the row could
 * not blur the console behind it and would stretch the row's box.
 *
 * `alignWidth` is `w-44` (11rem = 176px) and has to stay in step with the class
 * below: the offset is computed before the panel exists, so the width cannot be
 * measured (the same pairing `consolePanel` has with `CENTRED_PANEL_WIDTH`).
 */
const ROW_MENU_WIDTH = 176;

export const SessionRowMenu: React.FC<{
  /** The row's own overflow trigger, which the menu hangs from and returns to. */
  anchor: HTMLElement | null;
  /** The session's title, so the menu can name what it acts on. */
  title: string;
  onRename: () => void;
  onDelete: () => void;
  onClose: () => void;
}> = ({ anchor, title, onRename, onDelete, onClose }) => {
  const panelRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    // Escape belongs to the menu while it is open, in the capture phase: every
    // console surface listens for it on `window`, and the first press must close the
    // menu, not the panel underneath it.
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.stopPropagation();
      onClose();
    };
    document.addEventListener('keydown', onKeyDown, true);
    return () => document.removeEventListener('keydown', onKeyDown, true);
  }, [onClose]);

  useEffect(() => {
    // A menu is dismissed by clicking away from it.  Its own trigger is exempt: that
    // click toggles, and closing here would reopen the menu in the same gesture.
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (panelRef.current?.contains(target)) return;
      if (anchor !== null && anchor.contains(target)) return;
      onClose();
    };
    document.addEventListener('pointerdown', onPointerDown, true);
    return () => document.removeEventListener('pointerdown', onPointerDown, true);
  }, [anchor, onClose]);

  // Both rows close the menu before they act: rename replaces the row with an input
  // that takes the focus itself, and delete opens a modal dialog that must own it.
  return (
    <FloatingPanel
      anchor={anchor}
      side="bottom"
      align="end"
      alignWidth={ROW_MENU_WIDTH}
      panelRef={panelRef}
      className="w-44 max-w-[calc(100vw-2rem)] rounded-card border border-line/80 material-flyout flyout-in p-1.5 shadow-flyout"
    >
      <div
        role="menu"
        aria-label={`会话操作：${title}`}
        className="flex flex-col gap-0.5 outline-none"
      >
        <button
          type="button"
          role="menuitem"
          onClick={() => {
            onClose();
            onRename();
          }}
          className="ui-menu-item flex items-center gap-2 text-left text-gray-700"
        >
          <Edit20Regular aria-hidden="true" className="shrink-0 text-gray-500" />
          <span className="truncate">重命名</span>
        </button>
        <button
          type="button"
          role="menuitem"
          // The row opens a confirmation of its own, and says so: a reader reaching
          // for the destructive action is told a second decision follows.
          aria-haspopup="dialog"
          onClick={() => {
            onClose();
            onDelete();
          }}
          className="ui-menu-item flex items-center gap-2 text-left text-red-600"
        >
          <Delete20Regular aria-hidden="true" className="shrink-0" />
          <span className="truncate">删除记录</span>
        </button>
      </div>
    </FloatingPanel>
  );
};
