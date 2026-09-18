/**
 * The title bar's "open with" control: a split button plus its application menu.
 *
 * The left half starts the application the console currently points at; the right
 * half opens the menu, which lists what *this host* enumerated (`runtime.apps.list`)
 * with the glyph the host named, the applications that claim the file's extension
 * first.  Nothing here decides what may be started: the menu can only offer an id the
 * host published, and the host re-checks the path and the id on every launch.
 *
 * Two placement facts shape the implementation:
 *
 * - the menu is portaled to the document body.  Both hosts of this control are
 *   windows whose title bar is a drag handle (`FloatingWindow` starts a drag on any
 *   pointerdown that is not inside a `<button>`), and a menu inside that subtree would
 *   drag the window from its own search field and be clipped by the window's overflow.
 *   A portaled box is outside the drag subtree and outside the clipping.
 * - `Escape` is owned here while the menu is open.  Every window listens for it on
 *   `window` to close itself, so the menu listens in the capture phase and stops the
 *   event: the first `Escape` closes the menu, the next one closes the window.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Apps16Regular,
  Checkmark16Regular,
  ChevronDown16Regular,
  Code16Regular,
  FolderOpen16Regular,
  Image16Regular,
  Notepad16Regular,
  Search16Regular,
  Window16Regular,
} from '@fluentui/react-icons';
import { Portal } from './Portal.tsx';
import { useDialogKeyboardNav } from './keyboardNav.ts';
import { useConsoleStore } from '../stores/useConsoleStore';
import { useAppearanceStore } from '../stores/appearance.ts';
import {
  RECOMMENDED_LIMIT,
  extensionOf,
  otherApps,
  preferredApp,
  recommendedApps,
} from '../runtime-client/externalApps.ts';
import type { ExternalAppView } from '../runtime-client/externalApps.ts';

/** Menu geometry: one width, one height, and the gap kept from the viewport edge. */
const MENU_WIDTH = 336;
const MENU_MAX_HEIGHT = 380;
const MENU_GAP = 6;
const MENU_EDGE = 8;

/** The glyph ids this build ships a mark for; anything else falls back by role. */
const KNOWN_GLYPHS = new Set([
  'vscode',
  'cursor',
  'zed',
  'sublime',
  'notepadpp',
  'notepad',
  'terminal',
  'explorer',
  'photos',
  'system',
]);

/**
 * Which shipped mark stands for one application.
 *
 * The host names a glyph id; a glyph this build does not know (or an application the
 * host added later) still gets a role-appropriate mark rather than a blank cell.
 */
function glyphKey(app: ExternalAppView): string {
  if (app.icon.kind === 'glyph' && KNOWN_GLYPHS.has(app.icon.value)) return app.icon.value;
  if (app.kind === 'shell') return 'explorer';
  if (app.kind === 'system') return 'system';
  if (app.kind === 'viewer') return 'notepad';
  return 'vscode';
}

/** The mark itself.  A module-level switch, so no component is built during render. */
function appGlyph(app: ExternalAppView): React.ReactNode {
  const className = 'shrink-0 text-gray-500';
  switch (glyphKey(app)) {
    case 'notepad':
      return <Notepad16Regular aria-hidden="true" className={className} />;
    case 'terminal':
      return <Window16Regular aria-hidden="true" className={className} />;
    case 'explorer':
      return <FolderOpen16Regular aria-hidden="true" className={className} />;
    case 'photos':
      return <Image16Regular aria-hidden="true" className={className} />;
    case 'system':
      return <Apps16Regular aria-hidden="true" className={className} />;
    default:
      return <Code16Regular aria-hidden="true" className={className} />;
  }
}

const AppIcon: React.FC<{ app: ExternalAppView }> = ({ app }) => {
  if (app.icon.kind === 'data_url' && app.icon.value.startsWith('data:image/')) {
    return (
      <img
        src={app.icon.value}
        alt=""
        aria-hidden="true"
        className="h-4 w-4 shrink-0 rounded-control object-contain"
      />
    );
  }
  return <>{appGlyph(app)}</>;
};

const AppRow: React.FC<{
  app: ExternalAppView;
  selected: boolean;
  onPick: (app: ExternalAppView) => void;
}> = ({ app, selected, onPick }) => (
  // A real button with radio semantics: the arrow keys reach it and `aria-checked`
  // is what tells a reader (and the initial focus) which application is current.
  <button
    type="button"
    role="menuitemradio"
    aria-checked={selected}
    onClick={() => onPick(app)}
    className="ui-menu-item flex items-center gap-2 rounded-control px-2 py-1 text-left text-[13px] text-gray-700"
  >
    <AppIcon app={app} />
    <span className="min-w-0 flex-1 truncate">{app.name}</span>
    {app.extensions.length > 0 && (
      <span className="shrink-0 font-mono text-[11px] text-gray-400">
        {app.extensions
          .slice(0, 3)
          .map((entry) => `.${entry}`)
          .join(' ')}
      </span>
    )}
    {app.isSystemDefault && (
      <span className="shrink-0 rounded-full border border-line/60 bg-blue-50/70 px-1.5 text-[11px] text-blue-700">
        系统默认
      </span>
    )}
    <Checkmark16Regular
      aria-hidden="true"
      className={`shrink-0 text-accent ${selected ? '' : 'invisible'}`}
    />
  </button>
);

export const OpenWithMenu: React.FC<{
  /** The workspace-relative path to open, or null when nothing is selected. */
  path: string | null;
  /** Why the control is disabled, used as its tooltip. */
  disabledReason?: string;
}> = ({ path, disabledReason = '先选择一个文件' }) => {
  const apps = useConsoleStore((s) => s.externalApps);
  const appsError = useConsoleStore((s) => s.externalAppsError);
  const loadExternalApps = useConsoleStore((s) => s.loadExternalApps);
  const openExternal = useConsoleStore((s) => s.openExternal);
  const lastOpenWithAppId = useConsoleStore((s) => s.lastOpenWithAppId);
  const openWith = useAppearanceStore((s) => s.openWith);
  const rememberOpenWith = useAppearanceStore((s) => s.rememberOpenWith);

  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState('');
  const [anchor, setAnchor] = useState<{ left: number; top: number; width: number } | null>(null);
  const triggerRef = useRef<HTMLDivElement | null>(null);
  const caretRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);

  const catalog = useMemo(() => apps ?? [], [apps]);
  const extension = path === null ? '' : extensionOf(path);
  const rememberedId = openWith[extension] ?? null;
  const current =
    path === null ? null : preferredApp(catalog, path, rememberedId, lastOpenWithAppId);
  const loading = apps === null && appsError === null;
  const remember = current !== null && rememberedId === current.id;
  const shellApp = catalog.find((app) => app.kind === 'shell') ?? null;

  useEffect(() => {
    void loadExternalApps();
  }, [loadExternalApps]);

  const place = useCallback(() => {
    const trigger = triggerRef.current;
    if (trigger === null) return;
    const rect = trigger.getBoundingClientRect();
    const width = Math.min(MENU_WIDTH, window.innerWidth - 2 * MENU_EDGE);
    const left = Math.min(
      Math.max(MENU_EDGE, rect.right - width),
      Math.max(MENU_EDGE, window.innerWidth - width - MENU_EDGE),
    );
    const below = window.innerHeight - rect.bottom - MENU_GAP - MENU_EDGE;
    const top =
      below >= MENU_MAX_HEIGHT
        ? rect.bottom + MENU_GAP
        : Math.max(MENU_EDGE, rect.top - MENU_GAP - MENU_MAX_HEIGHT);
    setAnchor({ left, top, width });
  }, []);

  useEffect(() => {
    // A closed menu renders nothing, so a stale anchor is never painted: it is
    // simply re-measured on the next open.
    if (!open) return;
    place();
    const onMove = () => place();
    window.addEventListener('resize', onMove);
    window.addEventListener('scroll', onMove, true);
    return () => {
      window.removeEventListener('resize', onMove);
      window.removeEventListener('scroll', onMove, true);
    };
  }, [open, place]);

  const close = useCallback(() => {
    setOpen(false);
    setFilter('');
  }, []);

  // Escape belongs to the menu while it is open (see the module comment).
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      event.stopImmediatePropagation();
      close();
    };
    window.addEventListener('keydown', onKeyDown, { capture: true });
    return () => window.removeEventListener('keydown', onKeyDown, { capture: true });
  }, [open, close]);

  // A click outside both the trigger and the portaled menu closes it.
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      const target = event.target as Node | null;
      if (target === null) return;
      if (triggerRef.current?.contains(target) === true) return;
      if (menuRef.current?.contains(target) === true) return;
      close();
    };
    document.addEventListener('mousedown', onPointerDown);
    return () => document.removeEventListener('mousedown', onPointerDown);
  }, [open, close]);

  // The rows arrive with the host's catalog, so the menu only claims focus once it
  // has something to focus -- and it starts on the application that is current.
  // `anchor` is part of the condition, not just `open`: the menu is measured before
  // it is rendered, so the first commit has no box to focus and the hook must not
  // consume the open edge there (it would never run again while the menu stays open).
  const onMenuKeyDown = useDialogKeyboardNav(
    menuRef,
    open && anchor !== null && !loading && catalog.length > 0,
    '[aria-checked="true"]',
  );

  const pick = useCallback(
    (app: ExternalAppView) => {
      if (path === null) return;
      close();
      if (remember) rememberOpenWith(extension, app.id);
      void openExternal(path, app.id);
    },
    [close, extension, openExternal, path, remember, rememberOpenWith],
  );

  const reveal = useCallback(() => {
    if (path === null || shellApp === null) return;
    close();
    void openExternal(path, shellApp.id, 'reveal');
  }, [close, openExternal, path, shellApp]);

  const needle = filter.trim().toLowerCase();
  const matches = (app: ExternalAppView): boolean =>
    needle === '' ||
    `${app.name} ${app.shortName} ${app.extensions.join(' ')}`.toLowerCase().includes(needle);
  const recommended =
    path === null ? [] : recommendedApps(catalog, path).slice(0, RECOMMENDED_LIMIT).filter(matches);
  const others = path === null ? [] : otherApps(catalog, path).filter(matches);
  const nothingMatches = needle !== '' && recommended.length === 0 && others.length === 0;
  const disabled = path === null || current === null;

  return (
    <>
      <div
        ref={triggerRef}
        className="flex shrink-0 items-stretch overflow-hidden rounded-control border border-line"
      >
        <button
          type="button"
          disabled={disabled}
          onClick={() => {
            if (path !== null && current !== null) pick(current);
          }}
          title={
            disabled
              ? disabledReason
              : `用 ${current.shortName} 打开 ${path}`
          }
          className="ui-button ui-compact rounded-none text-[12px]"
        >
          {current === null ? (
            <Apps16Regular aria-hidden="true" className="shrink-0 text-gray-400" />
          ) : (
            <AppIcon app={current} />
          )}
          <span className="max-w-[9rem] truncate">
            {current === null ? (loading ? '读取应用…' : '打开方式') : current.shortName}
          </span>
        </button>
        <button
          ref={caretRef}
          type="button"
          disabled={path === null}
          aria-haspopup="menu"
          aria-expanded={open}
          aria-controls="open-with-menu"
          onClick={() => setOpen((value) => !value)}
          title="选择打开方式"
          aria-label="选择打开方式"
          className="ui-icon-button ui-compact rounded-none border-l border-line"
        >
          <ChevronDown16Regular aria-hidden="true" />
        </button>
      </div>

      {open && anchor !== null && (
        <Portal>
          <div
            id="open-with-menu"
            ref={menuRef}
            role="menu"
            aria-label="选择打开文件的应用"
            tabIndex={-1}
            onKeyDown={onMenuKeyDown}
            style={{ left: anchor.left, top: anchor.top, width: anchor.width }}
            className="fixed z-50 flex max-h-[380px] flex-col rounded-card border border-line/80 material-flyout flyout-in p-2 shadow-flyout"
          >
            <div className="flex items-baseline gap-2 px-1 pb-1.5 text-[12px] text-gray-500">
              <span className="font-semibold text-gray-700">打开方式</span>
              <span className="ml-auto text-[11px] text-gray-400">
                {loading
                  ? '读取应用…'
                  : `本机 ${catalog.length} 个应用${apps !== null && apps.length === 0 ? '（未检测到）' : ''}`}
              </span>
            </div>

            <div className="relative pb-1.5">
              <Search16Regular
                aria-hidden="true"
                className="pointer-events-none absolute left-2.5 top-1.5 text-gray-400"
              />
              <input
                id="open-with-filter"
                name="open-with-filter"
                type="text"
                value={filter}
                onChange={(event) => setFilter(event.target.value)}
                placeholder="搜索应用…"
                aria-label="搜索应用"
                className="ui-field w-full pl-8 pr-2 text-[12px]"
              />
            </div>

            <div className="fluent-scrollbar min-h-0 flex-1 overflow-y-auto">
              {loading && (
                <p className="px-2 py-2 text-[12px] text-gray-500">正在读取宿主可用应用…</p>
              )}
              {appsError !== null && (
                <p className="px-2 py-2 text-[12px] leading-relaxed text-amber-700">
                  应用列表读取失败：{appsError}
                </p>
              )}
              {!loading && appsError === null && recommended.length > 0 && (
                <>
                  <p className="px-2 pb-1 pt-1.5 text-[11px] uppercase tracking-wide text-gray-400">
                    推荐 · {extension}
                  </p>
                  {recommended.map((app) => (
                    <AppRow
                      key={app.id}
                      app={app}
                      selected={current !== null && app.id === current.id}
                      onPick={pick}
                    />
                  ))}
                </>
              )}
              {!loading && appsError === null && others.length > 0 && (
                <>
                  <p className="px-2 pb-1 pt-1.5 text-[11px] uppercase tracking-wide text-gray-400">
                    其他应用
                  </p>
                  {others.map((app) => (
                    <AppRow
                      key={app.id}
                      app={app}
                      selected={current !== null && app.id === current.id}
                      onPick={pick}
                    />
                  ))}
                </>
              )}
              {nothingMatches && (
                <p className="px-2 py-2 text-[12px] text-gray-500">
                  没有匹配「{filter.trim()}」的应用
                </p>
              )}
              {!loading && appsError === null && catalog.length === 0 && (
                <p className="px-2 py-2 text-[12px] leading-relaxed text-gray-500">
                  未检测到可用应用（当前会话没有桌面环境，或应用枚举失败）。可用系统默认应用或资源管理器定位。
                </p>
              )}
            </div>

            {shellApp !== null && (
              <button
                type="button"
                role="menuitem"
                disabled={path === null}
                onClick={reveal}
                className="ui-menu-item mt-1 flex items-center gap-2 rounded-control border-t border-line/60 px-2 py-1 text-left text-[13px] text-gray-700"
              >
                <FolderOpen16Regular aria-hidden="true" className="shrink-0 text-gray-500" />
                <span className="min-w-0 flex-1 truncate">在资源管理器中显示</span>
                <span className="shrink-0 font-mono text-[11px] text-gray-400">定位</span>
              </button>
            )}

            {current !== null && extension !== '' && (
              <label className="mt-1 flex items-center gap-2 border-t border-line/60 px-2 pt-1.5 text-[12px] text-gray-600">
                <input
                  id="open-with-remember"
                  name="open-with-remember"
                  type="checkbox"
                  checked={remember}
                  onChange={() =>
                    rememberOpenWith(extension, remember ? null : current.id)
                  }
                  className="ui-check"
                />
                <span className="truncate">
                  始终用 <b>{current.shortName}</b> 打开 <span className="font-mono">{extension}</span> 文件
                </span>
              </label>
            )}
          </div>
        </Portal>
      )}
    </>
  );
};
