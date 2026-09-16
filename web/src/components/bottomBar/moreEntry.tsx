/**
 * The 更多 entry: host infrastructure, not a manifest entry.
 *
 * It is what makes the compact policy honest — an entry the strip does not paint
 * on a narrow window is listed here, and its row opens exactly the overlay its
 * own trigger would have opened.  A low-priority control is therefore never
 * clipped away unreachable, and no entry module has to know the strip is narrow.
 *
 * The resolver only appends this entry to a track while something actually
 * overflowed (`resolveBottomBarLayout`), so the menu never appears empty.
 *
 * The menu is opened by a *trigger* that keeps the focus, so a keydown listener
 * on the menu box would never see a keystroke; it uses the console's shared
 * `useDialogKeyboardNav` instead (focus into the first row, arrows walk the rows
 * and wrap, focus back to the trigger on close).  A row that opens a *modal* is
 * special: the row unmounts with the menu while the modal is opening, and the
 * modal remembers whatever holds the focus to restore on close — so the row
 * hands the focus back to the 更多 trigger first, and the round trip ends on the
 * still-mounted trigger instead of on a detached row.
 */
import { useRef } from 'react';
import { MoreHorizontal20Regular, ChevronDown16Regular } from '@fluentui/react-icons';
import { shortcutByKey } from '../consoleShortcuts.ts';
import { useDialogKeyboardNav } from '../keyboardNav.ts';
import { MORE_ENTRY_ID } from './contract.ts';
import type { BottomBarItemDefinition } from './contract.ts';

export const MORE_ENTRY: BottomBarItemDefinition = {
  id: MORE_ENTRY_ID,
  label: '更多',
  region: 'right',
  // Last in its track: it is the overflow's own entry point.
  order: Number.MAX_SAFE_INTEGER,
  overlay: 'popover',
  panelLabel: '更多底栏入口',
  panelClassName:
    'w-60 max-w-[calc(100vw-2rem)] rounded-card border border-line/80 material-flyout flyout-in p-2 text-left shadow-flyout',
  Trigger: function MoreTrigger({ context, open, anchorRef }) {
    return (
      <button
        ref={anchorRef}
        data-entry={MORE_ENTRY_ID}
        type="button"
        onClick={(event) => context.toggle(MORE_ENTRY_ID, event.currentTarget)}
        title="更多底栏入口"
        aria-haspopup="menu"
        aria-expanded={open}
        className="flex cursor-pointer items-center gap-1 transition-colors hover:text-gray-900"
      >
        <MoreHorizontal20Regular
          aria-hidden="true"
          className="shrink-0 text-gray-400"
          style={{ fontSize: '15px' }}
        />
        <span className="text-gray-700">更多</span>
        <ChevronDown16Regular aria-hidden="true" className="shrink-0 text-gray-400" />
      </button>
    );
  },
  Content: function MoreMenu({ context }) {
    const menuRef = useRef<HTMLDivElement | null>(null);
    const onKeyDown = useDialogKeyboardNav(menuRef, true);
    return (
      <div
        ref={menuRef}
        role="menu"
        aria-label="更多底栏入口"
        tabIndex={-1}
        onKeyDown={onKeyDown}
        className="space-y-0.5"
      >
        {context.overflow.map((item) => {
          const shortcut = item.shortcutKey === undefined ? undefined : shortcutByKey(item.shortcutKey);
          return (
            <button
              key={item.id}
              type="button"
              role="menuitem"
              onClick={() => {
                // This row is about to unmount with the menu.  Handing the focus
                // back to the 更多 trigger (which stays mounted) before the
                // overlay opens is what a modal's own focus restore records, so
                // the two agree instead of fighting: without it the modal would
                // remember this row and strand the focus on `<body>` on close.
                context.anchor?.focus();
                // The entry this opens is not painted in the strip, so its overlay
                // hangs from this menu's own trigger (still mounted) instead.
                context.toggle(item.id, context.anchor);
              }}
              className="ui-menu-item flex items-center justify-between gap-3 text-gray-700"
            >
              <span className="truncate">{item.label}</span>
              {shortcut !== undefined && <span className="ui-kbd shrink-0">{shortcut.chord}</span>}
            </button>
          );
        })}
      </div>
    );
  },
};
