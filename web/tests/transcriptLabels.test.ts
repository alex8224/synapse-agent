/**
 * Offline tests for the transcript display labels (design-spec wording).
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  expandHint,
  formatToolArgs,
  isTerminalTool,
  thoughtIcon,
  thoughtLabel,
  toolGroupLabel,
  toolGroupIcon,
  toolPreviewLanguage,
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
  assert.equal(thoughtLabel('streaming'), 'Thinking...');
  assert.equal(thoughtLabel('0.1s'), 'Thought for 0.1s');
  assert.equal(thoughtLabel('2.9s'), 'Thought for 2.9s');
  assert.equal(thoughtLabel('done'), 'Thought');
  assert.equal(thoughtLabel(undefined), 'Thought');
  assert.equal(thoughtLabel(''), 'Thought');
});

test('expandHint reflects the collapsed state', () => {
  assert.equal(expandHint(true), '(收起)');
  assert.equal(expandHint(false), '(展开)');
});

test('a reasoning row carries a thinking glyph, wired while it runs', () => {
  assert.equal(thoughtIcon(false), 'psychology');
  assert.equal(thoughtIcon(true), 'neurology');
});

test('a tool-batch header carries its own outcome', () => {
  assert.equal(toolGroupIcon({ running: 0, failed: 0 }), 'build');
  assert.equal(toolGroupIcon({ running: 2, failed: 0 }), 'progress_activity');
  assert.equal(toolGroupIcon({ running: 1, failed: 1 }), 'error');
  assert.equal(toolGroupIcon({ running: 0, failed: 3 }), 'error');
});

test('formatToolArgs prints the call arguments as one bounded line', () => {
  assert.equal(
    formatToolArgs({ command: 'pytest -q', timeout_s: 30 }),
    'command=pytest -q · timeout_s=30',
  );
  // `intent` is the row's own label, so it must not be printed twice.
  assert.equal(formatToolArgs({ intent: 'run checks', command: 'ls' }), 'command=ls');
  assert.equal(formatToolArgs({ label: 'Run', command: 'ls' }), 'command=ls');
  // A multi-line command collapses to one line.
  assert.equal(formatToolArgs({ command: 'a\n  b\tc' }), 'command=a b c');
  // Non-string values are stringified, and nothing is invented for a missing one.
  assert.equal(formatToolArgs({ pattern: 'x', all: true, n: null }), 'pattern=x · all=true');
  assert.equal(formatToolArgs({}), '');
  assert.equal(formatToolArgs(null), '');
  assert.equal(formatToolArgs('not an object'), '');
  assert.equal(formatToolArgs([1, 2]), '');
  assert.equal(formatToolArgs(undefined), '');
});

test('formatToolArgs bounds the value, the key count and the whole line', () => {
  const long = 'x'.repeat(500);
  const value = formatToolArgs({ command: long });
  assert.equal(value, 'command=' + 'x'.repeat(159) + '…', 'one value must stay bounded');

  const many = formatToolArgs({ a: 1, b: 2, c: 3, d: 4, e: 5 });
  assert.equal(many, 'a=1 · b=2 · c=3 · d=4');
  assert.equal(formatToolArgs({ a: 1, b: 2 }, 1), 'a=1');
  assert.equal(formatToolArgs({ a: 1 }, 0), '');

  const wide = formatToolArgs({ a: long, b: long, c: long });
  assert.ok(wide.length <= 400, 'the finished line must stay bounded');
});

test('a file-content tool body takes the language of its path', () => {
  assert.equal(toolPreviewLanguage('read_file', 'src/app.py', 'print(1)'), 'python');
  assert.equal(toolPreviewLanguage('read', 'web/src/App.tsx', 'const a = 1;'), 'typescript');
  assert.equal(toolPreviewLanguage('edit_file', 'rust/core/src/lib.rs', 'fn main() {}'), 'rust');
  assert.equal(toolPreviewLanguage('write_file', 'a/b.yaml', 'k: v'), 'yaml');
  assert.equal(toolPreviewLanguage('patch', 'a/b.mjs', 'export const a = 1;'), 'javascript');
  assert.equal(toolPreviewLanguage('read_file', 'a/b.jsonl', '{}'), 'json');
});

test('a tool body is only highlighted when the console can tokenize it', () => {
  // An unknown extension, an unknown tool and a missing body all stay plain.
  assert.equal(toolPreviewLanguage('read_file', 'a/b.kt', 'val x = 1'), '');
  assert.equal(toolPreviewLanguage('read_file', 'notes.txt', 'plain text'), '');
  assert.equal(toolPreviewLanguage('read_file', 'Makefile', 'all:'), '');
  assert.equal(toolPreviewLanguage('execute', 'a/b.py', 'print(1)'), '');
  assert.equal(toolPreviewLanguage('search_files', 'a/b.py', 'a/b.py:1: hit'), '');
  assert.equal(toolPreviewLanguage('read_file', 'a/b.py', ''), '');
  assert.equal(toolPreviewLanguage('read_file', null, 'print(1)'), '');
});

test('an edit result highlights as a diff whatever the file is', () => {
  const patch = '--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-old\n+new';
  assert.equal(toolPreviewLanguage('edit_file', 'src/app.py', patch), 'diff');
  assert.equal(toolPreviewLanguage('patch', 'unknown.ext', patch), 'diff');
  // A body that merely starts with a rule is not a patch.
  assert.equal(toolPreviewLanguage('read_file', 'a/b.css', '--- x\nbody {}'), 'css');
  assert.equal(toolPreviewLanguage('read_file', 'a/b.py', 'x = 1  # @@ marker'), 'python');
});

test('only a program-running tool gets the terminal renderer', () => {
  assert.equal(isTerminalTool('execute'), true);
  assert.equal(isTerminalTool('RUN'), true);
  assert.equal(isTerminalTool('bash'), true);
  // A file body and a search result are not terminal output.
  assert.equal(isTerminalTool('read_file'), false);
  assert.equal(isTerminalTool('search_files'), false);
  assert.equal(isTerminalTool(''), false);
});
