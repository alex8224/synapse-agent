/**
 * Offline tests for the attachment failure-message mapping.  Pure: no socket.
 *
 * The wire carries a generic `message` for every service failure, so the reason a
 * reader sees must come from `data.service_code` - otherwise a refused upload only
 * ever says "runtime service error" and cannot be acted on.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  attachmentErrorMessage,
  attachmentServiceCode,
} from '../src/runtime-client/attachments.ts';

test('a store at its byte cap explains the quota instead of "runtime service error"', () => {
  const message = attachmentErrorMessage('attachment_quota', 'runtime service error');
  assert.match(message, /上限/);
  assert.match(message, /attachments/);
  assert.notEqual(message, 'runtime service error');
});

test('every attachment refusal gets its own reason', () => {
  assert.match(attachmentErrorMessage('attachment_too_large', 'x'), /4 MB/);
  assert.match(attachmentErrorMessage('attachment_unsafe', 'x'), /校验/);
  assert.match(attachmentErrorMessage('attachment_not_found', 'x'), /不存在/);
  assert.match(attachmentErrorMessage('attachment_forbidden', 'x'), /其他会话/);
  assert.match(attachmentErrorMessage('attachment_conflict', 'x'), /冲突/);
  assert.match(attachmentErrorMessage('attachment_unavailable', 'x'), /不可用/);
});

test('an unknown or absent code falls back to the wire message', () => {
  assert.equal(attachmentErrorMessage(null, 'runtime service error'), 'runtime service error');
  assert.equal(attachmentErrorMessage('something_else', 'fallback'), 'fallback');
});

test('the code is read structurally and never from the message text', () => {
  assert.equal(attachmentServiceCode({ service_code: 'attachment_quota' }), 'attachment_quota');
  assert.equal(attachmentServiceCode(new Error('runtime service error')), null);
  assert.equal(attachmentServiceCode(null), null);
  assert.equal(attachmentServiceCode({ service_code: 42 }), null);
});
