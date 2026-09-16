/**
 * Store-level tests for undoing one file of one finished turn.
 *
 * The runtime exposes `runtime.workspace.revert`, so the console must perform a real
 * write: it asks with the turn and the path, repaints the card as reverted (the same shape
 * a reload produces), re-reads the git chrome, and reports a refusal as a visible reason
 * instead of swallowing it -- while leaving the card alone when the write did not happen.
 *
 * Uses only the Node built-in test runner; the store is exercised with a hand-written
 * client stub (no WebSocket).
 */
import { beforeEach, test } from 'node:test';
import assert from 'node:assert/strict';
import { RpcCallError } from '../src/runtime-client/SynapseRuntimeClient.ts';
import {
  markRevertedPath,
  turnChangeViews,
  type TranscriptMessage,
} from '../src/stores/historyMapper.ts';
import { useConsoleStore } from '../src/stores/useConsoleStore.ts';

const SESSION = { project_id: 'p', thread_id: 't' };

function changesRow(turnId: string, paths: string[]): TranscriptMessage {
  return {
    id: `changes-${turnId}`,
    type: 'changes',
    timestamp: 'Turn 1',
    turnId,
    changes: turnChangeViews(
      paths.map((path) => ({
        path,
        status: 'modified',
        insertions: 2,
        deletions: 1,
        binary: false,
      })),
    ),
    changesTotal: paths.length,
  };
}

function fakeClient(overrides: Record<string, unknown> = {}): unknown {
  return {
    getState: () => 'connected',
    revertTurnChange: async () => ({
      turn_id: 'turn-1',
      path: 'a.py',
      action: 'restore',
      bytes_written: 4,
    }),
    gitStatus: async () => ({ branch: 'main', dirty: true, files: [] }),
    ...overrides,
  };
}

beforeEach(() => {
  useConsoleStore.setState({
    pairingState: 'paired',
    connectionState: 'connected',
    client: fakeClient() as never,
    currentSession: SESSION,
    messages: [changesRow('turn-1', ['a.py', 'b.py']), changesRow('turn-2', ['a.py'])],
    revertError: null,
    gitStatus: null,
  });
});

test('the wire call carries the turn and the one path', async () => {
  const calls: unknown[] = [];
  useConsoleStore.setState({
    client: fakeClient({
      revertTurnChange: async (params: unknown) => {
        calls.push(params);
        return { turn_id: 'turn-1', path: 'a.py', action: 'restore', bytes_written: 4 };
      },
    }) as never,
  });

  const ok = await useConsoleStore.getState().revertTurnChange('turn-1', 'a.py');

  assert.equal(ok, true);
  assert.deepEqual(calls, [{ session: SESSION, turn_id: 'turn-1', path: 'a.py' }]);
});

test('a successful revert marks that file of that turn, and only that one', async () => {
  const ok = await useConsoleStore.getState().revertTurnChange('turn-1', 'a.py');

  assert.equal(ok, true);
  const messages = useConsoleStore.getState().messages;
  const first = messages[0].changes ?? [];
  assert.deepEqual(
    first.map((change) => [change.path, change.reverted]),
    [
      ['a.py', true],
      ['b.py', false],
    ],
    'the other file of the same turn is untouched',
  );
  const second = messages[1].changes ?? [];
  assert.deepEqual(
    second.map((change) => [change.path, change.reverted]),
    [['a.py', false]],
    'the same path in another turn is a different change',
  );
});

test('a successful revert re-reads the git chrome', async () => {
  let statusCalls = 0;
  useConsoleStore.setState({
    client: fakeClient({
      gitStatus: async () => {
        statusCalls += 1;
        return { branch: 'main', dirty: false, files: [] };
      },
    }) as never,
  });

  await useConsoleStore.getState().revertTurnChange('turn-1', 'a.py');

  assert.equal(statusCalls, 1, 'the workspace moved, so the chrome is re-read once');
});

test('a refusal is reported and repaints nothing', async () => {
  useConsoleStore.setState({
    client: fakeClient({
      revertTurnChange: async () => {
        throw new RpcCallError('refused', -32000, 'revert_content_drift');
      },
    }) as never,
  });

  const ok = await useConsoleStore.getState().revertTurnChange('turn-1', 'a.py');

  assert.equal(ok, false);
  const state = useConsoleStore.getState();
  assert.equal(state.messages[0].changes?.[0].reverted, false, 'nothing was undone');
  assert.ok(
    state.revertError?.includes('a.py') && state.revertError.includes('又被改过'),
    `the reason names the file and the condition, got ${state.revertError}`,
  );
});

test('each refusal condition has its own wording', async () => {
  const cases: [string, string][] = [
    ['revert_turn_running', '有回合正在运行'],
    ['revert_head_moved', '新的提交'],
    ['revert_record_expired', '不再保留'],
    ['revert_path_not_in_turn', '不属于这一轮'],
    ['revert_symlink_refused', '符号链接'],
    ['permission_denied', '没有权限'],
  ];
  for (const [code, expected] of cases) {
    useConsoleStore.setState({
      client: fakeClient({
        revertTurnChange: async () => {
          throw new RpcCallError('refused', -32000, code);
        },
      }) as never,
    });
    assert.equal(await useConsoleStore.getState().revertTurnChange('turn-1', 'a.py'), false);
    const message = useConsoleStore.getState().revertError ?? '';
    assert.ok(message.includes(expected), `${code} -> ${message}`);
  }
});

test('an unrunnable call reports nothing and changes nothing', async () => {
  useConsoleStore.setState({ pairingState: 'unpaired', client: null });
  assert.equal(await useConsoleStore.getState().revertTurnChange('turn-1', 'a.py'), false);
  assert.equal(useConsoleStore.getState().messages[0].changes?.[0].reverted, false);

  useConsoleStore.setState({ pairingState: 'paired', client: fakeClient() as never });
  assert.equal(await useConsoleStore.getState().revertTurnChange('', 'a.py'), false);
  assert.equal(await useConsoleStore.getState().revertTurnChange('turn-1', ''), false);
});

test('a refusal can be dismissed', () => {
  useConsoleStore.setState({ revertError: 'x' });
  useConsoleStore.getState().dismissRevertError();
  assert.equal(useConsoleStore.getState().revertError, null);
});

test('a reload paints the reverted state the runtime reports', () => {
  // The history read carries the reverted paths, so a card painted after a reload cannot
  // claim the edit still stands.
  const views = turnChangeViews(
    [
      { path: 'a.py', status: 'modified', insertions: 2, deletions: 1, binary: false },
      { path: 'b.py', status: 'modified', insertions: 1, deletions: 0, binary: false },
    ],
    ['a.py', 7, null],
  );
  assert.deepEqual(
    views.map((change) => [change.path, change.reverted]),
    [
      ['a.py', true],
      ['b.py', false],
    ],
  );
  assert.deepEqual(turnChangeViews([], ['a.py']), []);
});

test('marking a reverted path is idempotent and scoped to its turn', () => {
  const messages = [changesRow('turn-1', ['a.py']), changesRow('turn-2', ['a.py'])];
  const once = markRevertedPath(messages, 'turn-1', 'a.py');
  assert.equal(once[0].changes?.[0].reverted, true);
  assert.equal(once[1].changes?.[0].reverted, false);
  const twice = markRevertedPath(once, 'turn-1', 'a.py');
  assert.equal(twice, once, 'a second mark is not a new array');
  assert.equal(markRevertedPath(messages, 'turn-9', 'a.py'), messages);
  assert.equal(markRevertedPath(messages, 'turn-1', 'missing.py'), messages);
});
