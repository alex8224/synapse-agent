/**
 * The wide-screen screenshot operation area.
 *
 * Window capture and capture settings are one user-facing operation area: the
 * main button starts the saved capture configuration, while the adjacent gear
 * opens the tool's GUI settings.  On narrow screens CommandInput hides this
 * area and the same two action definitions remain available from the `+` menu.
 */
import React from 'react';
import { SCREENSHOT_ACTIONS } from './manifest.ts';
import type { ComposerActionContext, ComposerActionDefinition } from './contract.ts';

export type ScreenshotActionAreaProps = ComposerActionContext;

export const ScreenshotActionArea: React.FC<ScreenshotActionAreaProps> = ({
  startWindowScreenshot,
  openScreenshotSettings,
  pickImages,
}) => {
  const capture = SCREENSHOT_ACTIONS[0] as ComposerActionDefinition<React.ReactNode>;
  const settings = SCREENSHOT_ACTIONS[1] as ComposerActionDefinition<React.ReactNode>;
  const run = (action: ComposerActionDefinition<React.ReactNode>): void => {
    if (action.run === undefined) return;
    void Promise.resolve(action.run({ startWindowScreenshot, openScreenshotSettings, pickImages })).catch(
      () => undefined,
    );
  };

  return (
    <div className="composer-screenshot-actions" role="group" aria-label="窗口截图操作">
      <button
        type="button"
        className="ui-button composer-screenshot-trigger"
        title={capture.detail}
        aria-label="窗口截图"
        onClick={() => run(capture)}
      >
        {capture.icon}
        <span>截图</span>
      </button>
      <button
        type="button"
        className="ui-icon-button composer-screenshot-settings"
        title={settings.detail}
        aria-label="截图设置"
        onClick={() => run(settings)}
      >
        {settings.icon}
      </button>
    </div>
  );
};
