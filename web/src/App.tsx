import { useEffect } from 'react';
import { TopBar } from './components/TopBar';
import { SideBar } from './components/SideBar';
import { Transcript } from './components/Transcript';
import { RuntimeDiagnosticsBanner } from './components/RuntimeDiagnosticsBanner';
import { CommandInput } from './components/CommandInput';
import { BottomBar } from './components/BottomBar';
import { PairingGate } from './components/PairingGate';
import { useConsoleStore } from './stores/useConsoleStore';

export function App() {
  const initClient = useConsoleStore((s) => s.initClient);
  const pairingState = useConsoleStore((s) => s.pairingState);
  const toggleSidebar = useConsoleStore((s) => s.toggleSidebar);
  const cancelActiveTurn = useConsoleStore((s) => s.cancelActiveTurn);
  const createNewSession = useConsoleStore((s) => s.createNewSession);
  const runtimeStatus = useConsoleStore((s) => s.runtimeStatus);

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
      } else if (e.ctrlKey && e.key.toLowerCase() === 'c' && runtimeStatus === 'running') {
        e.preventDefault();
        cancelActiveTurn();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [toggleSidebar, cancelActiveTurn, createNewSession, runtimeStatus]);

  // Unauthenticated: the pairing gate is the only reachable surface. The
  // workspace UI (and therefore every runtime RPC entry point) stays unmounted
  // until a valid console session exists.
  if (pairingState !== 'paired') {
    return <PairingGate />;
  }

  return (
    <div className="bg-background text-on-background h-screen w-screen overflow-hidden flex flex-col font-body-md selection:bg-editor-selection">
      <TopBar />
      <div className="flex flex-1 overflow-hidden relative">
        <SideBar />
        <main className="flex-1 flex flex-col relative bg-surface-container overflow-hidden">
          {/* Only rendered when the relay is down and the read-only diagnostics
              read succeeded; see the component for the degradation rules. */}
          <RuntimeDiagnosticsBanner />
          <Transcript />
          <CommandInput />
        </main>
      </div>
      <BottomBar />
    </div>
  );
}

export default App;
