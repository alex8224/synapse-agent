/**
 * The composer action menu's static entry list.
 *
 * Adding an action is one module plus one line here — nothing else changes, and
 * nothing is registered at runtime: the host paints whatever this list declares
 * (see `contract.ts` for the rules).  The list is a plain array of imported
 * definitions, so there is no mutable registry, no external plugin surface and
 * no user layout file to reconcile.
 *
 * The order of the list is the order of the rows.
 */
import { addImageAction } from './addImageAction.ts';
import { windowScreenshotAction } from './windowScreenshotAction.ts';
import { screenshotSettingsAction } from './screenshotSettingsAction.ts';
import { resolveComposerActions } from './contract.ts';
import type { ReactNode } from 'react';
import type { ComposerActionDefinition } from './contract.ts';

export const COMPOSER_ACTIONS: readonly ComposerActionDefinition<ReactNode>[] =
  resolveComposerActions([addImageAction, windowScreenshotAction, screenshotSettingsAction]);

/** The image-only rows kept behind the compact `+` overflow trigger. */
export const IMAGE_ACTIONS: readonly ComposerActionDefinition<ReactNode>[] =
  resolveComposerActions([addImageAction]);

/** The two window-capture operations presented as one toolbar operation area. */
export const SCREENSHOT_ACTIONS: readonly ComposerActionDefinition<ReactNode>[] =
  resolveComposerActions([windowScreenshotAction, screenshotSettingsAction]);
