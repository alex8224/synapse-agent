import {
  FolderOpen20Regular, WeatherMoon20Regular, WeatherSunny20Regular,
  WindowConsole20Regular,
} from '@fluentui/react-icons';
import React, { useEffect, useRef, useState } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { useConsoleStore } from '../stores/useConsoleStore';
import { DARK_THEME, prefersDark, themeFor, useAppearanceStore } from '../stores/appearance.ts';
import { ArtifactsPanel } from './ArtifactsPanel.tsx';
import { ConsolePanel, ConsolePanelRow } from './consolePanel.tsx';
import { THEME_SHORTCUT_CHORD } from './consoleShortcuts.ts';

/**
 * The console's context actions: the read-only workspace file browser, runtime
 * diagnostics, the one-click theme toggle and logout.
 *
 * They live at the sidebar's settings row rather than in the header, so the
 * header keeps only identity (workspace, branch, session) and the actions sit
 * with the other app-level entry point.  The session info is not one of them: it
 * opens from the session title in the header (see `SessionInfoPanel`), which is
 * where a reader looks for what the console knows about the session.  `orientation`
 * picks the layout: a row for the expanded sidebar's footer, a column for the
 * collapsed 44px rail.
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
    loadRuntimeDiagnostics,
  } = useConsoleStore(
    // Only the fields these actions paint: a reasoning delta must not re-render
    // them.
    useShallow((state) => ({
      runtimeDiagnostics: state.runtimeDiagnostics,
      loadRuntimeDiagnostics: state.loadRuntimeDiagnostics,
    })),
  );

  const [openPanel, setOpenPanel] = useState<'diagnostics' | 'artifacts' | null>(null);
  const rowRef = useRef<HTMLDivElement | null>(null);
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
    <div className="shrink-0" ref={rowRef}>
      <div className={orientation === 'row' ? 'flex items-center gap-1' : 'flex flex-col items-center gap-1'}>
        <button
          onClick={() => setOpenPanel((v) => (v === 'artifacts' ? null : 'artifacts'))}
          title="工作区文件（只读，按块读取）"
          aria-label="工作区文件"
          aria-expanded={openPanel === 'artifacts'}
          className={trigger}
        >
          <FolderOpen20Regular aria-hidden="true" />
        </button>
        <button
          onClick={() => {
            setOpenPanel((v) => (v === 'diagnostics' ? null : 'diagnostics'));
            void loadRuntimeDiagnostics({ trigger: 'manual', force: true });
          }}
          title="运行时诊断（读取宿主只读端点）"
          aria-label="运行时诊断"
          aria-expanded={openPanel === 'diagnostics'}
          className={trigger}
        >
          <WindowConsole20Regular aria-hidden="true" />
        </button>
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
      </div>

      {openPanel === 'diagnostics' && (
        <ConsolePanel title="运行时诊断" anchor={rowRef.current} onClose={() => setOpenPanel(null)}>
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
        <ArtifactsPanel anchor={rowRef.current} onClose={() => setOpenPanel(null)} />
      )}
    </div>
  );
};
