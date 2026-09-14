import { useEffect } from 'react';
import { TopBar } from './components/TopBar';
import { SideBar } from './components/SideBar';
import { Transcript } from './components/Transcript';
import { RuntimeDiagnosticsBanner } from './components/RuntimeDiagnosticsBanner';
import { CommandInput } from './components/CommandInput';
import { BottomBar } from './components/BottomBar';
import { FileViewerHost } from './components/FileViewerHost';
import { PairingGate } from './components/PairingGate';
import { useConsoleStore } from './stores/useConsoleStore';

export function App() {
  const initClient = useConsoleStore((s) => s.initClient);
  const pairingState = useConsoleStore((s) => s.pairingState);
  const toggleSidebar = useConsoleStore((s) => s.toggleSidebar);
  const cancelActiveTurn = useConsoleStore((s) => s.cancelActiveTurn);
  const createNewSession = useConsoleStore((s) => s.createNewSession);
  const runtimeStatus = useConsoleStore((s) => s.runtimeStatus);
  const requestSessionSearchFocus = useConsoleStore((s) => s.requestSessionSearchFocus);

  useEffect(() => {
    initClient();
  }, [initClient]);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'b') {
        e.preventDefault();
        toggleSidebar();
      } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'n') {
        e.preventDefault();
        createNewSession();
      } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        requestSessionSearchFocus();
      } else if (e.ctrlKey && e.key.toLowerCase() === 'c' && runtimeStatus === 'running') {
        e.preventDefault();
        cancelActiveTurn();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [
    toggleSidebar,
    cancelActiveTurn,
    createNewSession,
    requestSessionSearchFocus,
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
    <div className="material-canvas text-on-background flex h-screen w-screen overflow-hidden font-body-md selection:bg-editor-selection">
      <SideBar />
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <TopBar />
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
          <Transcript />
          <CommandInput />
        </main>
        <BottomBar />
      </div>
      {/* Centered workspace-file viewer, opened from a file path in a model answer. */}
      <FileViewerHost />
    </div>
  );
}

export default App;
