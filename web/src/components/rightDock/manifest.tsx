/**
 * Manifest of all registered tabs for the Right Auxiliary Dock.
 *
 * Plug-in extension point:
 * To add a new tab to the right dock (e.g. terminal, memory, subagents),
 * create a new tab module exporting a `RightDockTabDefinition`, and add it
 * to this array. The dock host resolves sorting, availability, and active state
 * automatically without any hardcoded layout changes.
 */
import { filesTab } from './filesTab.tsx';
import { changesTab } from './changesTab.tsx';
import { trajectoryTab } from './trajectoryTab.tsx';
import { goalsTab } from './goalsTab.tsx';
import { previewTab } from './previewTab.tsx';
import type { RightDockTabDefinition } from './contract.ts';

export const RIGHT_DOCK_MANIFEST: readonly RightDockTabDefinition[] = [
  filesTab,
  changesTab,
  trajectoryTab,
  goalsTab,
  previewTab,
];
