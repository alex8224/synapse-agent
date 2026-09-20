import { useCallback, useEffect, useRef, useState } from 'react';
import { Dismiss20Regular } from '@fluentui/react-icons';
import { TopBar } from './components/TopBar';
import { SideBar } from './components/SideBar';
import { Transcript } from './components/Transcript';
import { RuntimeDiagnosticsBanner } from './components/RuntimeDiagnosticsBanner';
import { RecoveryNotice } from './components/RecoveryNotice.tsx';
import { CommandInput } from './components/CommandInput';
import { ScreenshotTaskBanner } from './components/ScreenshotTaskBanner.tsx';
import { BottomBar } from './components/BottomBar';
import { FileViewerHost } from './components/FileViewerHost';
import { BackgroundAlerts } from './components/BackgroundAlerts';
import { PairingGate } from './components/PairingGate';
import { NEW_SESSION_ACTION, readShortcutAction } from './client/deepLink';
import { useAppearanceStore } from './stores/appearance.ts';
import { useConsoleStore } from './stores/useConsoleStore';
import { useScreenshotStore } from './stores/screenshotTask.ts';

export function App() {
  const initClient = useConsoleStore((s) => s.initClient);
  const pairingState = useConsoleStore((s) => s.pairingState);
  const connectionState = useConsoleStore((s) => s.connectionState);
  const runtimeClient = useConsoleStore((s) => s.client);
  const currentProjectId = useConsoleStore((s) => s.currentSession.project_id);
  const currentThreadId = useConsoleStore((s) => s.currentSession.thread_id);
  const toggleSidebar = useConsoleStore((s) => s.toggleSidebar);
  const cancelActiveTurn = useConsoleStore((s) => s.cancelActiveTurn);
  const createNewSession = useConsoleStore((s) => s.createNewSession);
  const runtimeStatus = useConsoleStore((s) => s.runtimeStatus);
  const requestSessionSearchFocus = useConsoleStore((s) => s.requestSessionSearchFocus);
  const toggleAppearance = useAppearanceStore((s) => s.toggleAppearance);
  const refreshScreenshotTool = useScreenshotStore((s) => s.refreshTool);

  const [mobile, setMobile] = useState(() => window.matchMedia('(max-width: 767px)').matches);
  const [tablet, setTablet] = useState(() => window.matchMedia('(min-width: 768px) and (max-width: 1023px)').matches);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [tabletCollapsed, setTabletCollapsed] = useState(true);
  const drawerRef = useRef<HTMLDivElement>(null);
  const closeDrawer = useCallback(() => setDrawerOpen(false), []);
  const toggleNavigation = useCallback(() => {
    if (mobile) setDrawerOpen((open) => !open);
    else if (tablet) setTabletCollapsed((collapsed) => !collapsed);
    else toggleSidebar();
  }, [mobile, tablet, toggleSidebar]);

  useEffect(() => {
    const phone = window.matchMedia('(max-width: 767px)');
    const medium = window.matchMedia('(min-width: 768px) and (max-width: 1023px)');
    const update = () => {
      setMobile(phone.matches);
      setTablet(medium.matches);
      setDrawerOpen(false);
    };
    phone.addEventListener('change', update);
    medium.addEventListener('change', update);
    return () => {
      phone.removeEventListener('change', update);
      medium.removeEventListener('change', update);
    };
  }, []);

  useEffect(() => useConsoleStore.subscribe((state, previous) => {
    if (state.currentSession.thread_id !== previous.currentSession.thread_id ||
        state.currentSession.project_id !== previous.currentSession.project_id ||
        state.pairingState !== previous.pairingState) closeDrawer();
    if (state.searchFocusToken !== previous.searchFocusToken) {
      if (mobile) setDrawerOpen(true);
      if (tablet) setTabletCollapsed(false);
    }
  }), [closeDrawer, mobile, tablet]);

  useEffect(() => {
    if (!mobile || !drawerOpen) return;
    const previous = document.activeElement as HTMLElement | null;
    drawerRef.current?.querySelector<HTMLButtonElement>('button')?.focus();
    const handleKey = (event: KeyboardEvent) => {
      // Portalled dialogs own their keyboard handling while they are open.
      if (!drawerRef.current?.contains(event.target as Node)) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        closeDrawer();
      }
      if (event.key === 'Tab') {
        const items = Array.from(drawerRef.current.querySelectorAll<HTMLElement>(
          'button:not(:disabled), input:not(:disabled), [tabindex="0"]',
        )).filter((item) => item.getClientRects().length > 0 && !item.closest('[inert]'));
        const first = items[0];
        const last = items[items.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault(); last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault(); first?.focus();
        }
      }
    };
    document.addEventListener('keydown', handleKey);
    return () => {
      document.removeEventListener('keydown', handleKey);
      previous?.focus();
    };
  }, [mobile, drawerOpen, closeDrawer]);

  useEffect(() => {
    initClient();
  }, [initClient]);

  // Learn the capture tool's availability once per session so a menu activation
  // either starts a capture or names the reason it cannot, without a surprise.
  useEffect(() => {
    if (pairingState !== 'paired' || connectionState !== 'connected') return;
    void refreshScreenshotTool().catch(() => undefined);
  }, [pairingState, connectionState, runtimeClient, currentProjectId, currentThreadId, refreshScreenshotTool]);

  // Installable-app shortcut (`/?action=new-session`, see `public/manifest.webmanifest`):
  // a taskbar right-click should land in a fresh session.  It is read once, only
  // after the console is paired *and* the relay is up, and then stripped from the
  // address bar so that a reload cannot replay it.  When boot already created the
  // empty session there is nothing to do: the shortcut replaces an *attached*
  // session, never doubles it.
  useEffect(() => {
    if (pairingState !== 'paired' || connectionState !== 'connected') return;
    const { action, remainingSearch } = readShortcutAction(window.location.search);
    if (action !== NEW_SESSION_ACTION) return;
    window.history.replaceState(
      null,
      '',
      `${window.location.pathname}${remainingSearch ? `?${remainingSearch}` : ''}${window.location.hash}`,
    );
    if (currentThreadId) void createNewSession();
  }, [pairingState, connectionState, currentThreadId, createNewSession]);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'b') {
        e.preventDefault();
        toggleNavigation();
      } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'n') {
        e.preventDefault();
        createNewSession();
      } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        if (mobile) setDrawerOpen(true);
        if (tablet) setTabletCollapsed(false);
        requestSessionSearchFocus();
      } else if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === 'l') {
        // The same one click the sidebar's toggle makes, from anywhere: the shell
        // owns the chord and `consoleShortcuts` owns its copy (the F1 list).
        e.preventDefault();
        toggleAppearance();
      } else if (e.ctrlKey && e.key.toLowerCase() === 'c' && runtimeStatus === 'running') {
        e.preventDefault();
        cancelActiveTurn();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [
    toggleNavigation,
    mobile,
    tablet,
    cancelActiveTurn,
    createNewSession,
    requestSessionSearchFocus,
    toggleAppearance,
    runtimeStatus,
  ]);

  // Unauthenticated: the pairing gate is the only reachable surface. The
  // workspace UI (and therefore every runtime RPC entry point) stays unmounted
  // until a valid console session exists.
  if (pairingState !== 'paired') {
    return <PairingGate />;
  }

  return (
    /*
      Two columns, not three stacked rows: the navigation is a full-height column
      on the left, and everything that belongs to the open session (header chips,
      transcript, composer, status strip) is the workspace column on the right.
      The header and the status strip used to span the whole window and ran
      underneath the sidebar; scoping them here is what makes the sidebar read as
      one continuous rail from the top edge to the bottom.
    */
    <div className="console-shell material-canvas text-on-background flex h-screen w-screen overflow-hidden font-body-md selection:bg-editor-selection">
      {mobile && drawerOpen && <button className="navigation-scrim" aria-label="关闭导航" onClick={closeDrawer} tabIndex={-1} />}
      <div ref={drawerRef} id="console-navigation" className={mobile ? 'navigation-drawer' : 'navigation-column'}
        hidden={mobile && !drawerOpen} role={mobile ? 'dialog' : undefined}
        aria-modal={mobile && drawerOpen ? true : undefined} aria-label={mobile ? '项目与会话导航' : undefined}>
        {mobile && (
          <button className="ui-icon-button navigation-close" aria-label="关闭导航" onClick={closeDrawer}>
            <Dismiss20Regular aria-hidden="true" />
          </button>
        )}
        <SideBar collapsed={mobile ? false : tablet ? tabletCollapsed : undefined}
          onExpand={tablet ? () => setTabletCollapsed(false) : undefined} />
      </div>
      <div className="console-workspace flex min-w-0 flex-1 flex-col overflow-hidden" inert={mobile && drawerOpen}>
        <TopBar onToggleNavigation={toggleNavigation} navigationExpanded={mobile ? drawerOpen : undefined} />
        {/*
          Deliberately *not* `overflow-hidden`: the transcript scroller reaches up
          behind the header (`.console-under-chrome`) so the header's acrylic has
          something to blur.  Clipping the pane here cut that strip off and left
          the material invisible.  Everything in the pane is a flex row that takes
          its own height, so nothing else can overflow it.
        */}
        <main className="relative flex min-h-0 flex-1 flex-col material-pane">
          {/* Only rendered when the relay is down and the read-only diagnostics
              read succeeded; see the component for the degradation rules. */}
          <RuntimeDiagnosticsBanner />
          {/* Recovery / truncation state of the running turn's live replay.  An
              in-flow strip like the banner above it, not a floating layer: the
              transcript scroller reaches up behind the header, and a positioned
              overlay would cover the newest lines it is describing. */}
          <RecoveryNotice />
          <Transcript />
          <CommandInput />
          <ScreenshotTaskBanner />
        </main>
        <BottomBar />
      </div>
      {/* Centered workspace-file viewer, opened from a file path in a model answer. */}
      <FileViewerHost />
      {/* Invisible: system notifications and the app badge for this window. */}
      <BackgroundAlerts />
    </div>
  );
}

export default App;
