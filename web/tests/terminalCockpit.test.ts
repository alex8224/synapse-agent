/**
 * Unit and contract tests for the Tiling Multi-Agent CLI Cockpit & Chat-Terminal Linkage.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  AGENT_CLI_PRESETS,
  formatPtySubmission,
  isSameWorkspace,
  normalizeWorkspacePath,
  reconcileTerminalSessions,
  useTerminalStore,
} from '../src/stores/useTerminalStore.ts';

function resetStore() {
  useTerminalStore.setState({
    open: false,
    height: 300,
    isMaximized: false,
    displayMode: 'dock',
    splitWorkspaceRatio: 0.48,
    gridLayout: 'auto',
    zoomedSessionId: null,
    colWeights: {},
    rowWeights: {},
    masterRatio: 0.5,
    activeSessionId: null,
    sessions: [],
    terminalQuotes: {},
    pendingDispatches: {},
    reflowReadySessionIds: [],
  });
}

test('AGENT_CLI_PRESETS includes agy, claude, codex, synapse, and pwsh', () => {
  const ids = AGENT_CLI_PRESETS.map((p) => p.id);
  assert.ok(ids.includes('agy'));
  assert.ok(ids.includes('claude'));
  assert.ok(ids.includes('codex'));
  assert.ok(ids.includes('synapse'));
  assert.ok(ids.includes('pwsh'));
});

test('formatPtySubmission wraps multi-line prompts in Bracketed Paste sequences', () => {
  assert.equal(formatPtySubmission('git status'), 'git status\r');
  assert.equal(
    formatPtySubmission('line 1\r\nline 2'),
    '\x1b[200~line 1\nline 2\x1b[201~\r',
  );
});

test('tiling layouts, split workspace mode, interactive pane weights, and auto-reflow watch work cleanly', () => {
  resetStore();
  useTerminalStore.setState({
    open: true,
    activeSessionId: 's1',
    sessions: [
      {
        id: 's1',
        seq: 1,
        ptyId: 1,
        title: '1: agy CLI',
        shell: 'pwsh',
        agentId: 'agy',
        command: 'agy',
        isAgent: true,
        status: 'running',
        activity: 'working',
        broadcastEnabled: true,
        autoReflow: true,
        hasActiveTask: true,
        tailLines: [],
      },
      {
        id: 's2',
        seq: 2,
        ptyId: 2,
        title: '2: Claude Code',
        shell: 'pwsh',
        agentId: 'claude',
        command: 'claude',
        isAgent: true,
        status: 'running',
        activity: 'idle',
        broadcastEnabled: true,
        autoReflow: true,
        hasActiveTask: false,
        tailLines: [],
      },
    ],
  });

  // 3-mode workspace switching: dock -> split -> stage
  useTerminalStore.getState().toggleSplitWorkspaceMode();
  assert.equal(useTerminalStore.getState().displayMode, 'split');
  useTerminalStore.getState().setSplitWorkspaceRatio(0.45);
  assert.equal(useTerminalStore.getState().splitWorkspaceRatio, 0.45);

  useTerminalStore.getState().toggleStageMode();
  assert.equal(useTerminalStore.getState().displayMode, 'stage');

  // Auto-reflow triggers when an autoReflow session transitions from working -> idle with tail output
  useTerminalStore.getState().setSessionActivity('s1', 'idle', undefined, ['All tests passed']);
  assert.ok(useTerminalStore.getState().reflowReadySessionIds.includes('s1'));
  useTerminalStore.getState().dismissReflowNotice('s1');
  assert.equal(useTerminalStore.getState().reflowReadySessionIds.includes('s1'), false);

  // Selection quote registration for @term:quote:<id> pills
  const quote = useTerminalStore.getState().registerSelectionQuote('s1', 'line A\nline B');
  assert.ok(quote !== null);
  assert.equal(quote?.lineCount, 2);
  assert.equal(quote?.seq, 1);
});

test('updateActivityFromOsc parses Rex OSC 7501 status payloads', () => {
  resetStore();
  useTerminalStore.setState({
    sessions: [
      {
        id: 's1',
        seq: 1,
        ptyId: 1,
        title: '1: agy CLI',
        shell: 'pwsh',
        agentId: 'agy',
        command: 'agy',
        isAgent: true,
        status: 'running',
        activity: 'idle',
        broadcastEnabled: true,
        autoReflow: true,
        hasActiveTask: false,
        tailLines: [],
      },
    ],
  });

  useTerminalStore.getState().updateActivityFromOsc('s1', 'working;Running cargo test');
  let s1 = useTerminalStore.getState().sessions[0];
  assert.equal(s1.activity, 'working');
  assert.equal(s1.activityDetail, 'Running cargo test');

  useTerminalStore
    .getState()
    .updateActivityFromOsc('s1', '{"state":"blocked","message":"Allow command? [y/n]"}');
  s1 = useTerminalStore.getState().sessions[0];
  assert.equal(s1.activity, 'waiting_input');
  assert.equal(s1.activityDetail, 'Allow command? [y/n]');

  useTerminalStore.getState().updateActivityFromOsc('s1', 'done');
  s1 = useTerminalStore.getState().sessions[0];
  assert.equal(s1.activity, 'idle');
});

test('pane aliases preserve identity, survive a process restart, and can be cleared', async () => {
  resetStore();
  useTerminalStore.setState({
    sessions: [1, 2].map((seq) => ({
      id: `s${seq}`,
      seq,
      ptyId: seq,
      title: `${seq}: agy CLI`,
      shell: 'pwsh',
      agentId: 'agy',
      command: 'agy',
      isAgent: true,
      status: 'running',
      activity: 'idle',
      broadcastEnabled: true,
      autoReflow: false,
      hasActiveTask: false,
      tailLines: [],
    })),
  });

  const store = useTerminalStore.getState();
  const [first, second] = store.sessions;
  store.renameSession(first.id, '  主刀开发  ');
  assert.equal(useTerminalStore.getState().sessions[0].title, '1: agy CLI (主刀开发)');
  store.renameSession(first.id, 'reviewer');
  assert.deepEqual(useTerminalStore.getState().sessions[0], {
    ...first, title: '1: agy CLI (reviewer)', originalTitle: first.title, alias: 'reviewer',
  });
  assert.equal(useTerminalStore.getState().sessions[1], second);
  assert.equal(await store.broadcastInput('@reviewer test'), 0, 'alias must not become a route');

  store.setAgentConversationTitle(first.id, 1, 'Indexed conversation');
  store.setAgentConversationTitle(second.id, 1, 'Wrong PTY');
  let [firstPane, secondPane] = useTerminalStore.getState().sessions;
  assert.equal(firstPane.agentConversationTitle, 'Indexed conversation');
  assert.equal(firstPane.seq, 1);
  assert.equal(secondPane, second);

  // Native PTY creation is unavailable in Node; even the restart error path preserves the title.
  await store.restartSession(first.id);
  store.setAgentConversationTitle(first.id, 1, 'Late title from previous PTY');
  firstPane = useTerminalStore.getState().sessions[0];
  assert.equal(firstPane.title, '1: agy CLI (reviewer)');
  assert.equal(firstPane.agentConversationTitle, undefined);
  store.renameSession(first.id, '   ');
  assert.equal(useTerminalStore.getState().sessions[0].title, first.title);
});

test('normalizeWorkspacePath and isSameWorkspace handle Windows casing and slashes', () => {
  assert.equal(normalizeWorkspacePath('C:\\Users\\Demo\\Repo\\'), 'c:/users/demo/repo');
  assert.equal(isSameWorkspace('C:\\Users\\Demo\\Repo\\', 'c:/users/demo/repo'), true);
  assert.equal(isSameWorkspace('C:\\ProjectA', 'C:\\ProjectB'), false);
});

test('reconcileTerminalSessions restores matching bootId and isolates cross-project terminals', () => {
  const reconciledSameProject = reconcileTerminalSessions(
    {
      bootId: 'boot-1',
      sessions: [
        {
          ptyId: 7,
          workspace: 'C:\\ProjectA',
          shell: 'pwsh.exe',
          command: 'agy',
          cols: 100,
          rows: 30,
          exited: false,
          conversationId: 'conv-1',
          title: 'Fix attach bug',
          lastAgentState: {
            seq: 10,
            ptyId: 7,
            state: 'working',
            event: 'PreInvocation',
            detail: '正在思考与推理...',
          },
        },
      ],
    },
    {
      bootId: 'boot-1',
      open: true,
      displayMode: 'split',
      gridLayout: 'auto',
      splitWorkspaceRatio: 0.52,
      activeSessionId: 'term-old-7',
      zoomedSessionId: null,
      sessions: [
        {
          id: 'term-old-7',
          seq: 3,
          ptyId: 7,
          title: '3: agy CLI (主刀)',
          originalTitle: '3: agy CLI',
          alias: '主刀',
          agentId: 'agy',
          shell: 'pwsh.exe',
          command: 'agy',
          isAgent: true,
          workspace: 'C:\\ProjectA',
          autoReflow: true,
          broadcastEnabled: true,
          hasActiveTask: true,
        },
      ],
    },
    'c:/projecta/',
  );

  assert.equal(reconciledSameProject.open, true);
  assert.equal(reconciledSameProject.displayMode, 'split');
  assert.equal(reconciledSameProject.sessions.length, 1);
  assert.equal(reconciledSameProject.sessions[0].id, 'term-old-7');
  assert.equal(reconciledSameProject.sessions[0].seq, 3);
  assert.equal(reconciledSameProject.sessions[0].alias, '主刀');
  assert.equal(reconciledSameProject.sessions[0].activity, 'working');
  assert.equal(reconciledSameProject.sessions[0].agentConversationTitle, 'Fix attach bug');
  assert.equal(reconciledSameProject.sessions[0].broadcastEnabled, true);

  // Project A's agy is still recovered when opening Project B, but stays closed & non-broadcasting.
  const crossProject = reconcileTerminalSessions(
    {
      bootId: 'boot-1',
      sessions: [
        {
          ptyId: 7,
          workspace: 'C:\\ProjectA',
          shell: 'pwsh.exe',
          command: 'agy',
          cols: 100,
          rows: 30,
          exited: false,
          conversationId: null,
          title: null,
          lastAgentState: null,
        },
      ],
    },
    {
      bootId: 'boot-1',
      open: true,
      displayMode: 'split',
      gridLayout: 'auto',
      splitWorkspaceRatio: 0.48,
      activeSessionId: 'term-old-7',
      zoomedSessionId: null,
      sessions: [
        {
          id: 'term-old-7',
          seq: 3,
          ptyId: 7,
          title: '3: agy CLI',
          agentId: 'agy',
          shell: 'pwsh.exe',
          command: 'agy',
          isAgent: true,
          workspace: 'C:\\ProjectA',
          autoReflow: true,
          broadcastEnabled: true,
          hasActiveTask: false,
        },
      ],
    },
    'C:\\ProjectB',
  );

  assert.equal(crossProject.open, false);
  assert.equal(crossProject.sessions.length, 1);
  assert.equal(crossProject.sessions[0].broadcastEnabled, false);

  // Different bootId discards stale persisted UI metadata, preventing ptyId collision.
  const staleBoot = reconcileTerminalSessions(
    {
      bootId: 'boot-2',
      sessions: [
        {
          ptyId: 7,
          workspace: 'C:\\ProjectA',
          shell: 'pwsh.exe',
          command: 'agy',
          cols: 80,
          rows: 24,
          exited: true,
          conversationId: null,
          title: null,
          lastAgentState: null,
        },
      ],
    },
    {
      bootId: 'boot-1',
      open: true,
      displayMode: 'stage',
      gridLayout: 'auto',
      splitWorkspaceRatio: 0.48,
      activeSessionId: 'term-old-7',
      zoomedSessionId: null,
      sessions: [
        {
          id: 'term-old-7',
          seq: 9,
          ptyId: 7,
          title: '9: stale',
          agentId: 'agy',
          shell: 'pwsh.exe',
          command: 'agy',
          isAgent: true,
          workspace: 'C:\\ProjectA',
          autoReflow: true,
          broadcastEnabled: true,
          hasActiveTask: false,
        },
      ],
    },
    'C:\\ProjectA',
  );

  assert.equal(staleBoot.open, false);
  assert.equal(staleBoot.sessions[0].id, 'term-reattach-7');
  assert.equal(staleBoot.sessions[0].seq, 1);
  assert.equal(staleBoot.sessions[0].status, 'exited');
});
