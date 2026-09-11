/**
 * Offline tests for the transcript display labels (design-spec wording).
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  expandHint,
  thoughtLabel,
  toolGroupLabel,
  toolStatusLabel,
} from '../src/stores/transcriptLabels.ts';

test('toolStatusLabel maps the runtime statuses to the console vocabulary', () => {
  assert.equal(toolStatusLabel('running'), '运行中');
  assert.equal(toolStatusLabel('pending'), '等待');
  assert.equal(toolStatusLabel('completed'), '完成');
  assert.equal(toolStatusLabel('failed'), '失败');
  assert.equal(toolStatusLabel('error'), '错误');
  assert.equal(toolStatusLabel('cancelled'), '已取消');
  assert.equal(toolStatusLabel('canceled'), '已取消');
});

test('toolStatusLabel is case-insensitive and never hides an unknown status', () => {
  assert.equal(toolStatusLabel('RUNNING'), '运行中');
  assert.equal(toolStatusLabel('weird_state'), 'weird_state');
  assert.equal(toolStatusLabel(''), '');
});

test('toolGroupLabel uses the design-spec wording and pluralises', () => {
  assert.equal(toolGroupLabel(1), '1 tool executed');
  assert.equal(toolGroupLabel(15), '15 tools executed');
  assert.equal(toolGroupLabel(0), '0 tools executed');
  assert.equal(toolGroupLabel(2, true), '2 tools executed (parallel)');
});

test('thoughtLabel distinguishes streaming, completed and projected rows', () => {
  assert.equal(thoughtLabel('streaming'), '◆ Thinking...');
  assert.equal(thoughtLabel('0.1s'), '◆ Thought for 0.1s');
  assert.equal(thoughtLabel('2.9s'), '◆ Thought for 2.9s');
  assert.equal(thoughtLabel('done'), '◆ Thought');
  assert.equal(thoughtLabel(undefined), '◆ Thought');
  assert.equal(thoughtLabel(''), '◆ Thought');
});

test('expandHint reflects the collapsed state', () => {
  assert.equal(expandHint(true), '(收起)');
  assert.equal(expandHint(false), '(展开)');
});
