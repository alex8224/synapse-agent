/**
 * The F1 help overlay: the shortcut list, straight from the one table that owns
 * its copy (`consoleShortcuts`).
 *
 * It is a *modal*, so it owns everything a modal owns: the scrim, its own Escape
 * dismissal and the focus round trip (`useDialogKeyboardNav` focuses into the
 * list on open and hands focus back to whatever held it — the composer, the
 * trigger — on close).  The status strip deliberately keeps no second Escape
 * listener for it.
 */
import React, { useEffect, useRef } from 'react';
import { Dismiss20Regular } from '@fluentui/react-icons';
import { Portal } from './Portal.tsx';
import { useDialogKeyboardNav } from './keyboardNav.ts';
import { helpRows } from './consoleShortcuts.ts';

export interface HelpDialogProps {
  onClose: () => void;
}

export const HelpDialog: React.FC<HelpDialogProps> = ({ onClose }) => {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const onKeyDown = useDialogKeyboardNav(dialogRef, true);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    // `Portal`: a modal belongs to the window, not to the status strip that
    // opened it.
    <Portal>
      <div
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/25 backdrop-blur-sm p-4 scrim-in"
        onClick={onClose}
      >
        <div
          ref={dialogRef}
          role="dialog"
          aria-modal="true"
          aria-label="快捷键与使用帮助"
          tabIndex={-1}
          onKeyDown={onKeyDown}
          className="no-scrollbar max-h-[85vh] w-full max-w-md space-y-2 overflow-y-auto rounded-card border border-line/80 material-flyout flyout-in p-5 font-sans shadow-flyout"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="mb-3 flex items-center justify-between border-b pb-2">
            <span className="text-sm font-bold text-gray-900">快捷键与使用帮助</span>
            <button
              onClick={onClose}
              title="关闭 (Esc)"
              aria-label="关闭"
              className="ui-icon-button ui-compact text-gray-400 hover:text-gray-600"
            >
              <Dismiss20Regular aria-hidden="true" />
            </button>
          </div>
          <div className="space-y-1.5 font-mono text-xs text-gray-600">
            {helpRows().map((row) => (
              <div
                key={row.keys}
                className="flex items-center justify-between border-b border-gray-50 py-1 last:border-b-0"
              >
                <span className="ui-kbd">{row.keys}</span>
                <span className="font-sans">{row.label}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </Portal>
  );
};
