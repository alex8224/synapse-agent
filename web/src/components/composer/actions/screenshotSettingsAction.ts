/**
 * The composer action menu's "screenshot settings" row.
 *
 * The row asks the composer host to open the capture tool's own settings window
 * (`context.openScreenshotSettings`).  The runtime launches the tool GUI and
 * returns its status; the row itself touches neither the tool nor the store, and
 * it captures nothing and saves nothing on its own.
 *
 * The row paints its own mark, so the host needs no glyph table for it.
 */
import React from 'react';
import { Settings20Regular } from '@fluentui/react-icons';
import type { ComposerActionDefinition } from './contract.ts';

export const SCREENSHOT_SETTINGS_ACTION_ID = 'screenshot-settings';

export const screenshotSettingsAction: ComposerActionDefinition<React.ReactNode> = {
  id: SCREENSHOT_SETTINGS_ACTION_ID,
  label: '截图设置',
  detail: '打开截图工具设置（选择窗口、帧数、间隔等）',
  icon: React.createElement(Settings20Regular, {
    'aria-hidden': true,
    className: 'shrink-0 text-gray-500',
  }),
  // Returned so the host can await it and surface a refusal (e.g. the tool is
  // unavailable) in the menu instead of closing as if it had worked.
  run: ({ openScreenshotSettings }) => openScreenshotSettings(),
};
