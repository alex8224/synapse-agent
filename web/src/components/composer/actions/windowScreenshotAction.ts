/**
 * The composer action menu's "window screenshot" row.
 *
 * The row asks the composer host to queue one capture task
 * (`context.startWindowScreenshot`); the runtime starts the tool, polls it, and
 * finalizes each frame as an attachment.  The row never talks to the tool and
 * never captures anything itself, so this action stays free of the store and of
 * the DOM — the progress and the result are shown by the capture banner.
 *
 * When no target is chosen the tool opens its own picker and the runtime reports
 * `target_required`; the banner then asks the reader to choose and retry.  A row
 * that cannot run (the tool is unavailable) says why through the host's own
 * error surface instead of pretending a capture happened.
 *
 * The row paints its own mark, so the host needs no glyph table for it.
 */
import React from 'react';
import { Screenshot20Regular } from '@fluentui/react-icons';
import type { ComposerActionDefinition } from './contract.ts';

export const WINDOW_SCREENSHOT_ACTION_ID = 'window-screenshot';

export const windowScreenshotAction: ComposerActionDefinition<React.ReactNode> = {
  id: WINDOW_SCREENSHOT_ACTION_ID,
  label: '窗口截图',
  detail: '截取一个窗口，完成后作为图片加入输入框（不会自动发送）',
  icon: React.createElement(Screenshot20Regular, {
    'aria-hidden': true,
    className: 'shrink-0 text-gray-500',
  }),
  // Returned so the host can await it and surface a refusal (e.g. the tool is
  // unavailable) in the menu instead of closing as if it had worked.
  run: ({ startWindowScreenshot }) => startWindowScreenshot(),
};
