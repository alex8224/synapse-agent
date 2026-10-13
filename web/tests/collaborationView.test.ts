/**
 * The collaboration projection: role orientation, state wording and stop rules.
 *
 * These are the properties a reader would notice being wrong (who asked whom,
 * whether an answer arrived, whether the stop control is offered), and they are
 * pinned here rather than in a browser test because the module is pure.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  canStopCollaboration,
  canControlAssignment,
  canDismissCollaborationNotice,
  collaborationDismissKey,
  executionReasonLabel,
  collaborationKindLabel,
  collaborationStateLabel,
  collaborationView,
  hasPendingCollaboration,
  resolveCollaborationPeer,
  shortCollaborationId,
  splitSessionMentions,
} from '../src/stores/collaborationView.ts';
import type { CollaborationResult } from '../src/runtime-client/types.ts';

const SOURCE = { project_id: 'p1', thread_id: 'a' };
const TARGET = { project_id: 'p2', thread_id: 'b' };

function page(overrides: Partial<CollaborationResult> = {}): CollaborationResult {
  return {
    requests: [
      {
        request_id: 'r1',
        scope_id: 's1',
        source_project: 'p1',
        source_thread: 'a',
        target_project: 'p2',
        target_thread: 'b',
        state: 'working',
        version: 1,
        created_at_ms: 1,
        updated_at_ms: 2,
        messages: [
          {
            message_id: 'm1',
            request_id: 'r1',
            sequence: 1,
            sender_project: 'p1',
            sender_thread: 'a',
            kind: 'request',
            body: '核对接口',
            created_at_ms: 1,
          },
          {
            message_id: 'm2',
            request_id: 'r1',
            sequence: 2,
            sender_project: 'p2',
            sender_thread: 'b',
            kind: 'reply',
            body: '结论',
            created_at_ms: 2,
          },
        ],
        message_total: 2,
        pending: 1,
      },
    ],
    total: 1,
    scope_id: 's1',
    scope_state: 'active',
    scope_version: 3,
    ...overrides,
  };
}

test('the reader side decides the role, not the newest message', () => {
  const asSource = collaborationView(page(), SOURCE);
  assert.equal(asSource.requests[0].role, 'source');
  assert.equal(asSource.requests[0].peerThreadId, 'b');
  assert.equal(asSource.requests[0].peerProjectId, 'p2');

  const asTarget = collaborationView(page(), TARGET);
  assert.equal(asTarget.requests[0].role, 'target');
  assert.equal(asTarget.requests[0].peerThreadId, 'a');
  assert.equal(asTarget.requests[0].peerProjectId, 'p1');
});

test('controls apply only to outstanding work, not completed results', () => {
  const row = collaborationView(page(), SOURCE).requests[0];
  assert.equal(canControlAssignment(row), true);
  assert.equal(canControlAssignment({ ...row, role: 'target' }), false);
  assert.equal(canControlAssignment({ ...row, active: false, pending: 0, resultState: 'processed' }), false);
  assert.equal(canControlAssignment({ ...row, active: false, pending: 0, resultState: 'received' }), false);
  assert.equal(canControlAssignment({ ...row, active: false, pending: 1, resultState: 'processing' }), true);
  assert.equal(canControlAssignment({ ...row, continuationState: 'revoked' }), false);
});

test('execution reasons are readable without raw protocol codes', () => {
  assert.equal(executionReasonLabel('execution_cancelled'), '执行已取消');
  assert.equal(executionReasonLabel('execution_failed'), '执行失败');
  assert.equal(executionReasonLabel(null), null);
  assert.equal(executionReasonLabel('future_reason'), '执行状态需要核验');
});

test('each message says whether this session sent it', () => {
  const view = collaborationView(page(), SOURCE);
  assert.deepEqual(
    view.requests[0].messages.map((message) => [message.mine, message.kindLabel, message.isReply]),
    [
      [true, '协作请求', false],
      [false, '结论', true],
    ],
  );
  const target = collaborationView(page(), TARGET);
  assert.deepEqual(target.requests[0].messages.map((message) => message.mine), [false, true]);
});

test('an answer arriving is not the same as the request being finished', () => {
  const answered = collaborationView(
    page({
      requests: [{ ...page().requests[0], state: 'replied', pending: 0 }],
    }),
    SOURCE,
  );
  assert.equal(answered.requests[0].replied, true);
  assert.equal(answered.requests[0].active, false);
  assert.equal(hasPendingCollaboration(answered), false);
  assert.equal(hasPendingCollaboration(collaborationView(page(), SOURCE)), true);
});

test('a short message window is not read as a short conversation', () => {
  const view = collaborationView(
    page({ requests: [{ ...page().requests[0], message_total: 9 }] }),
    SOURCE,
  );
  assert.equal(view.requests[0].messages.length, 2);
  assert.equal(view.requests[0].messageTotal, 9);
});

test('the stop control is offered only for a live generation', () => {
  assert.equal(canStopCollaboration(collaborationView(page(), SOURCE)), true);
  for (const state of ['receive_only', 'revoked']) {
    const view = collaborationView(page({ scope_state: state }), SOURCE);
    assert.equal(canStopCollaboration(view), false, state);
  }
  const withoutScope = collaborationView(
    page({ scope_id: null, scope_state: null, scope_version: null }),
    SOURCE,
  );
  assert.equal(canStopCollaboration(withoutScope), false);
  assert.equal(withoutScope.stopTarget, null);
});

test('no page and an empty page both read as nothing to show', () => {
  const missing = collaborationView(null, SOURCE);
  assert.deepEqual(missing, { requests: [], stopTarget: null });
  assert.equal(canStopCollaboration(missing), false);

  const empty = collaborationView(
    page({ requests: [], total: 0 }),
    SOURCE,
  );
  assert.deepEqual(empty.requests, []);
  // The generation still exists even with no requests: stopping it is meaningful.
  assert.equal(canStopCollaboration(empty), true);
});

test('an unknown future state or kind stays readable instead of blank', () => {
  assert.equal(collaborationStateLabel('working'), '对方处理中');
  assert.equal(collaborationStateLabel('renegotiating'), 'renegotiating');
  assert.equal(collaborationKindLabel('reply'), '结论');
  assert.equal(collaborationKindLabel('escalation'), 'escalation');

  const view = collaborationView(
    page({ requests: [{ ...page().requests[0], state: 'renegotiating' }] }),
    SOURCE,
  );
  assert.equal(view.requests[0].stateLabel, 'renegotiating');
  assert.equal(view.requests[0].active, false);
});

test('assignment identity and execution do not conflate result processing', () => {
  const view = collaborationView(page({ requests: [{ ...page().requests[0],
    collaboration_id: 'group', assignment_id: 'r1', control_version: 4,
    continuation_state: 'receive_only', execution_state: 'waiting_approval',
    execution_turn_id: 'target-turn', execution_reason: null, result_state: 'received',
  }] }), SOURCE);
  const assignment = view.requests[0];
  assert.equal(assignment.collaborationId, 'group');
  assert.equal(assignment.controlVersion, 4);
  assert.equal(assignment.continuationState, 'receive_only');
  assert.equal(assignment.executionState, 'waiting_approval');
  assert.equal(assignment.resultState, 'received');
  assert.equal(collaborationKindLabel('progress'), '进度报告');
});

test('peer resolution prefers project and session titles and falls back to thread id', () => {
  const catalog = {
    activeProjectId: 'p1',
    projects: [
      { project_id: 'p1', workspace_path: '/repo/source', workspace_name: 'source' },
      { project_id: 'p2', workspace_path: '/repo/target', workspace_name: 'target' },
    ],
    sessions: [{ thread_id: 'a', title: 'UI source' }],
    projectSessions: {
      p2: [{ thread_id: 'b', title: 'UI target' }],
    },
  };

  const cross = resolveCollaborationPeer('p2', 'b', catalog);
  assert.equal(cross.projectName, 'target');
  assert.equal(cross.sessionTitle, 'UI target');
  assert.equal(cross.displayName, 'target · UI target');
  assert.equal(cross.fullRef, 'p2:b');

  const sameProject = resolveCollaborationPeer('p1', 'a', catalog);
  assert.equal(sameProject.displayName, 'UI source');

  const implicitProject = resolveCollaborationPeer('', 'a', catalog);
  assert.equal(implicitProject.displayName, 'UI source');

  const unknown = resolveCollaborationPeer('p3', 'c999', catalog);
  assert.equal(unknown.displayName, 'c999');
});

test('shortCollaborationId truncates long hex ids and preserves short ones', () => {
  assert.equal(shortCollaborationId('22907f5b660c4729953e7943bc7b35fc'), '22907f5b');
  assert.equal(shortCollaborationId('r1'), 'r1');
});

test('settled collaboration notices can be dismissed until their state changes', () => {
  const activeRow = collaborationView(page(), SOURCE).requests[0];
  assert.equal(canDismissCollaborationNotice(activeRow), false);

  const settledRow = collaborationView(
    page({
      requests: [{ ...page().requests[0], state: 'replied', pending: 0, result_state: 'processed' }],
    }),
    SOURCE,
  ).requests[0];
  assert.equal(canDismissCollaborationNotice(settledRow), true);
  const key1 = collaborationDismissKey(settledRow);
  const key2 = collaborationDismissKey({ ...settledRow, controlVersion: settledRow.controlVersion + 1 });
  assert.notEqual(key1, key2);
});

test('splitSessionMentions separates inline @session tokens from surrounding text', () => {
  const segments = splitSessionMentions(
    '请让 @session:proj_1:thread_a 核对接口，再同步给 @session:thread_b。',
  );
  assert.deepEqual(segments, [
    { kind: 'text', text: '请让 ' },
    { kind: 'session', raw: '@session:proj_1:thread_a', projectId: 'proj_1', threadId: 'thread_a' },
    { kind: 'text', text: ' 核对接口，再同步给 ' },
    { kind: 'session', raw: '@session:thread_b', projectId: '', threadId: 'thread_b' },
    { kind: 'text', text: '。' },
  ]);
  assert.deepEqual(splitSessionMentions('普通消息'), [{ kind: 'text', text: '普通消息' }]);
});
