/**
 * Mid-run guidance (steer) with images.
 *
 * `runtime.turn.steer` now carries the same `attachment_refs` a submitted turn
 * does, so a busy composer must send them instead of refusing, keep the images
 * on the local transcript row, and only drop them once the daemon accepted the
 * note.  These tests drive the store directly with a fake client: no host, no
 * daemon, no socket.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

function fakeStorage(): unknown {
  const map = new Map<string, string>();
  return {
    get length() {
      return map.size;
    },
    key: (index: number) => [...map.keys()][index] ?? null,
    getItem: (key: string) => map.get(key) ?? null,
    setItem: (key: string, value: string) => {
      map.set(key, value);
    },
    removeItem: (key: string) => {
      map.delete(key);
    },
    clear: () => {
      map.clear();
    },
  };
}

(globalThis as any).window = { location: { protocol: 'http:', host: '127.0.0.1:8080' } };
(globalThis as any).localStorage = fakeStorage();
(globalThis as any).sessionStorage = fakeStorage();
(globalThis as any).WebSocket = class {
  static readonly OPEN = 1;
  readyState = 3;
  send(): void {}
  close(): void {}
};

const { useConsoleStore } = await import('../src/stores/useConsoleStore.ts');

const ATTACHMENT_ID = 'a'.repeat(32);

function readyAttachment() {
  return {
    localId: 'local-1',
    name: 'shot.png',
    mime: 'image/png',
    size: 16,
    status: 'ready' as const,
    uploadedBytes: 16,
    attachmentId: ATTACHMENT_ID,
    error: null,
  };
}

function primeStore(client: unknown, attachments: unknown[]): void {
  useConsoleStore.setState({
    client: client as never,
    pairingState: 'paired',
    rpcBlockedReason: null,
    runtimeStatus: 'running',
    activeTurnId: 'turn-1',
    currentSession: { project_id: 'p', thread_id: 't' },
    attachments: attachments as never,
    attachmentError: null,
    messages: [],
    steerQueueCount: 0,
  });
}

test('a busy steer sends attachment_refs and keeps the images on the local row', async () => {
  const calls: any[] = [];
  primeStore(
    {
      steerTurn: async (params: any) => {
        calls.push(params);
        return { turn_id: 'turn-1', accepted: true, pending_count: 1 };
      },
    },
    [readyAttachment()],
  );

  const accepted = await useConsoleStore.getState().submitPrompt('look at this');

  assert.equal(accepted, true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].expected_turn_id, 'turn-1');
  assert.equal(calls[0].text, 'look at this');
  assert.deepEqual(calls[0].attachment_refs, [ATTACHMENT_ID]);

  const state = useConsoleStore.getState();
  // Accepted: the images belong to the running turn now.
  assert.deepEqual(state.attachments, []);
  assert.equal(state.steerQueueCount, 1);
  const row = state.messages.at(-1) as any;
  assert.equal(row.type, 'user');
  assert.equal(row.steer, true);
  assert.equal(row.content, 'look at this');
  assert.equal(row.attachments[0].attachmentId, ATTACHMENT_ID);
});

test('a text-only steer never sends an empty attachment_refs field', async () => {
  const calls: any[] = [];
  primeStore(
    {
      steerTurn: async (params: any) => {
        calls.push(params);
        return { turn_id: 'turn-1', accepted: true, pending_count: 1 };
      },
    },
    [],
  );

  assert.equal(await useConsoleStore.getState().submitPrompt('just this'), true);
  assert.equal('attachment_refs' in calls[0], false);
  const row = useConsoleStore.getState().messages.at(-1) as any;
  assert.equal(row.attachments, undefined);
});

test('a refused steer keeps the composer attachments and reports why', async () => {
  primeStore(
    {
      steerTurn: async () => {
        throw new Error('conflict');
      },
    },
    [readyAttachment()],
  );

  assert.equal(await useConsoleStore.getState().submitPrompt('look'), false);
  const state = useConsoleStore.getState();
  // Never silently drop the images on a refusal: the draft stays intact.
  assert.equal(state.attachments.length, 1);
  assert.match(state.attachmentError ?? '', /插话未被接受/);
});
