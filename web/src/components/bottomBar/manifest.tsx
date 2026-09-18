/**
 * The status strip's static entry list.
 *
 * Adding an entry is one module plus one line here — nothing else in the strip
 * changes, and nothing is registered at runtime: the host renders whatever this
 * list declares (see `contract.ts` for the rules).  The list is deliberately a
 * plain array of imported definitions, so there is no mutable registry, no
 * external plugin surface and no user layout file to reconcile.
 *
 * The order of the tracks is the layout's, not this list's: `region` + `order`
 * decide, and ties keep the order below.
 */
import { activityItem } from './activityItem.tsx';
import { mcpItem } from './mcpItem.tsx';
import { goalItem } from './goalItem.tsx';
import { telemetryItem } from './telemetryItem.tsx';
import { todoItem } from './todoItem.tsx';
import { helpItem } from './helpItem.tsx';
import { codexUsageItem } from './codexUsageItem.tsx';
import type { BottomBarItemDefinition } from './contract.ts';

export const BOTTOM_BAR_ITEMS: readonly BottomBarItemDefinition[] = [
  activityItem,
  codexUsageItem,
  mcpItem,
  todoItem,
  goalItem,
  telemetryItem,
  helpItem,
];
