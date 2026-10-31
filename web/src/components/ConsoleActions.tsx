import {
  Info20Regular,
  WeatherMoon20Regular,
  WeatherSunny20Regular,
} from '@fluentui/react-icons';
import React, { useEffect, useState } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { useConsoleStore } from '../stores/useConsoleStore';
import { DARK_THEME, prefersDark, themeFor, useAppearanceStore } from '../stores/appearance.ts';
import { ArtifactsPanel } from './ArtifactsPanel.tsx';
import { ConsolePanel, ConsolePanelRow } from './consolePanel.tsx';
import { SessionInfoPanel } from './SessionInfoPanel.tsx';
import { THEME_SHORTCUT_CHORD } from './consoleShortcuts.ts';

/**
 * The console's context actions: the read-only workspace file browser, runtime
 * diagnostics, the session info, the one-click theme toggle and logout.
 *
 * They live at the sidebar's settings row rather than in the header, so the
 * header keeps only identity (workspace, branch, session) and the actions sit
 * with the other app-level entry point.  The session info moved here for a second
 * reason: it used to open from the header's title, which in an installed window is
 * the caption's drag handle -- the one place a reader expects to be able to move
 * the window, and a click there opened a panel instead.  `orientation` picks the
 * layout: a row for the expanded sidebar's footer, a column for the collapsed
 * 44px rail.
 *
 * Each panel opens *above* its trigger because the triggers are at the bottom of the
 * window; Escape closes the open one.  They share the `ConsolePanel` shell, which
 * floats rather than laying out inside the rail (see that module for why).
 *
 * The theme toggle is the exception among them: it opens nothing.  It flips the
 * palette in one click (the settings dialog keeps the three-way choice, including
 * "follow the system"), and its chord does the same from anywhere -- both entry
 * points read the chord from `consoleShortcuts`, the table the F1 list prints.
 */
export const ConsoleActions: React.FC<{ orientation?: 'row' | 'column' }> = ({
  orientation = 'row',
}) => {
  const {
    runtimeDiagnostics,
  } = useConsoleStore(
    // Only the fields these actions paint: a reasoning delta must not re-render
    // them.
    useShallow((state) => ({
      runtimeDiagnostics: state.runtimeDiagnostics,
    })),
  );

  const [openPanel, setOpenPanel] = useState<'session' | 'diagnostics' | 'artifacts' | null>(null);
  // The row the panels hang from, held in state rather than read off a ref while
  // rendering: the anchor then belongs to the render that uses it.
  const [row, setRow] = useState<HTMLDivElement | null>(null);
  const appearance = useAppearanceStore((state) => state.appearance);
  const toggleAppearance = useAppearanceStore((state) => state.toggleAppearance);

  // The palette *on screen* decides which way the next click goes: while the
  // preference is still "system" the toggle resolves the operating system first.
  const dark = themeFor(appearance, prefersDark()) === DARK_THEME;
  const themeTitle = dark ? '切换到浅色主题' : '切换到深色主题';

  useEffect(() => {
    if (openPanel === null) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpenPanel(null);
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [openPanel]);

  const diagnostics = runtimeDiagnostics;
  const trigger = 'ui-icon-button';

  return (
    <div className="shrink-0" ref={setRow}>
      <div className={orientation === 'row' ? 'flex items-center gap-1' : 'flex flex-col items-center gap-1'}>
        {/* The icon is the theme the click switches *to*, so the button never reads
            as a status badge of the current one.  It lands next to the settings entry
            (`SideBar` paints that one), the console's other appearance preference. */}
        <button
          onClick={toggleAppearance}
          title={`${themeTitle} (${THEME_SHORTCUT_CHORD})`}
          aria-label={themeTitle}
          className={trigger}
        >
          {dark
            ? <WeatherSunny20Regular aria-hidden="true" />
            : <WeatherMoon20Regular aria-hidden="true" />}
        </button>
        {/* What the console knows about the open session: workspace, branch, model,
            connection and usage.  It used to hang off the header's title, which is
            the caption's drag handle in an installed window. */}
        <button
          onClick={() => setOpenPanel((open) => (open === 'session' ? null : 'session'))}
          title="会话信息"
          aria-label="会话信息"
          aria-haspopup="dialog"
          aria-expanded={openPanel === 'session'}
          className={trigger}
        >
          <Info20Regular aria-hidden="true" />
        </button>
      </div>

      {openPanel === 'session' && (
        <SessionInfoPanel anchor={row} onClose={() => setOpenPanel(null)} />
      )}

      {openPanel === 'diagnostics' && (
        <ConsolePanel title="运行时诊断" anchor={row} onClose={() => setOpenPanel(null)}>
          <ConsolePanelRow label="状态" value={diagnostics.status} />
          <ConsolePanelRow
            label="daemon"
            value={
              diagnostics.view?.endpoint
                ? `${diagnostics.view.endpoint.host}:${diagnostics.view.endpoint.port}`
                : 'unknown'
            }
          />
          <ConsolePanelRow label="state dir" value={diagnostics.view?.state_dir ?? '-'} />
          <ConsolePanelRow label="hint" value={diagnostics.view?.hint ?? '-'} />
          {diagnostics.reason !== null && (
            <ConsolePanelRow label="失败原因" value={diagnostics.reason} />
          )}
          <p className="pt-1 text-[10px] leading-relaxed text-gray-500">
            取自宿主只读端点 GET /api/runtime-status（endpoint / state_dir / hint），不含任何凭据。
          </p>
        </ConsolePanel>
      )}

      {openPanel === 'artifacts' && (
        <ArtifactsPanel anchor={row} onClose={() => setOpenPanel(null)} />
      )}
    </div>
  );
};
