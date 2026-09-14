/**
 * Offline tests for the artifact failure-message mapping.  Pure: no socket.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { artifactErrorMessage } from '../src/runtime-client/artifacts.ts';

test('a forbidden path explains the workspace ignore policy', () => {
  const message = artifactErrorMessage('artifact_forbidden', 'runtime service error');
  assert.match(message, /忽略规则/);
  assert.notEqual(message, 'runtime service error');
});

test('the other artifact codes get their own reasons', () => {
  assert.match(artifactErrorMessage('artifact_not_found', 'x'), /不存在/);
  assert.match(artifactErrorMessage('artifact_unavailable', 'x'), /不可用/);
  assert.match(artifactErrorMessage('artifact_changed', 'x'), /已变化/);
});

test('an unknown code falls back to the wire message', () => {
  assert.equal(artifactErrorMessage(null, 'runtime service error'), 'runtime service error');
  assert.equal(artifactErrorMessage('something_else', 'fallback'), 'fallback');
});
