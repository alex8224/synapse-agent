/**
 * The F1 help entry.
 *
 * It is an entry like any other — same `openId`, same single-overlay rule — but
 * it declares no `Trigger`: the strip paints no help control, so the right track
 * stays the symmetric spacer it has always been, and F1 (the key it claims in
 * `consoleShortcuts`) is its only affordance.
 */
import { HELP_SHORTCUT_KEY } from '../consoleShortcuts.ts';
import { HelpDialog } from '../HelpDialog.tsx';
import type { BottomBarItemDefinition } from './contract.ts';

export const HELP_ITEM_ID = 'help';

export const helpItem: BottomBarItemDefinition = {
  id: HELP_ITEM_ID,
  label: '快捷键帮助',
  region: 'right',
  order: 10,
  shortcutKey: HELP_SHORTCUT_KEY,
  overlay: 'modal',
  Content: function HelpContent({ context }) {
    return <HelpDialog onClose={context.close} />;
  },
};
