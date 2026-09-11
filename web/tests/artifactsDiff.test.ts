/**
 * Tests for the real line diff used by the workspace file panel.
 *
 * The panel diffs two texts it actually holds (the snapshot taken when the file
 * was opened and the current content), so the algorithm itself must be a genuine
 * line diff: minimal edits where the LCS budget allows it, an explicitly flagged
 * coarse replacement block beyond it, and bounded output.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DIFF_MAX_LINES, diffLines } from '../src/client/artifactsDiff.ts';

test('identical content reports no diff at all', () => {
  const result = diffLines('a\nb\nc\n', 'a\nb\nc\n');
  assert.equal(result.identical, true);
  assert.deepEqual(result.lines, []);
  assert.equal(result.added, 0);
  assert.equal(result.removed, 0);
});

test('a CRLF-only rewrite is not a content change', () => {
  const result = diffLines('a\r\nb\r\n', 'a\nb\n');
  assert.equal(result.identical, true);
  assert.deepEqual(result.lines, []);
});

test('an added line is reported once, with its current line number', () => {
  const result = diffLines('a\nc\n', 'a\nb\nc\n');
  assert.equal(result.identical, false);
  assert.equal(result.added, 1);
  assert.equal(result.removed, 0);
  assert.deepEqual(
    result.lines.map((line) => [line.kind, line.baselineLine, line.currentLine, line.text]),
    [
      ['context', 1, 1, 'a'],
      ['add', null, 2, 'b'],
      ['context', 2, 3, 'c'],
    ],
  );
});

test('a changed line is one removal plus one addition', () => {
  const result = diffLines('a\nold\nc\n', 'a\nnew\nc\n');
  assert.equal(result.added, 1);
  assert.equal(result.removed, 1);
  assert.deepEqual(
    result.lines.map((line) => [line.kind, line.text]),
    [
      ['context', 'a'],
      ['remove', 'old'],
      ['add', 'new'],
      ['context', 'c'],
    ],
  );
});

test('the LCS keeps shared lines instead of replacing the whole block', () => {
  const result = diffLines('one\ntwo\nthree\nfour\n', 'one\nthree\nfour\nfive\n');
  assert.equal(result.removed, 1);
  assert.equal(result.added, 1);
  assert.equal(result.coarse, false);
  assert.deepEqual(
    result.lines.filter((line) => line.kind === 'context').map((line) => line.text),
    ['one', 'three', 'four'],
  );
});

test('common prefix and suffix carry their real line numbers', () => {
  const baseline = Array.from({ length: 40 }, (_, i) => `line ${i + 1}`).join('\n');
  const current = `${baseline}\nline 41`;
  const result = diffLines(baseline, current);
  assert.equal(result.added, 1);
  assert.equal(result.removed, 0);
  assert.equal(result.baselineLines, 40);
  assert.equal(result.currentLines, 41);
  const last = result.lines[result.lines.length - 1];
  assert.deepEqual([last.kind, last.baselineLine, last.currentLine, last.text], [
    'add',
    null,
    41,
    'line 41',
  ]);
  assert.equal(result.lines[0].baselineLine, 1);
});

test('a middle block beyond the cell budget is flagged coarse', () => {
  const baseline = Array.from({ length: 600 }, (_, i) => `old ${i}`).join('\n');
  const current = Array.from({ length: 600 }, (_, i) => `new ${i}`).join('\n');
  const result = diffLines(baseline, current);
  assert.equal(result.coarse, true, 'an inexact diff must say so');
  assert.equal(result.removed, 600);
  assert.equal(result.added, 600);
});

test('the rendered diff is capped by the line budget', () => {
  const baseline = Array.from({ length: DIFF_MAX_LINES + 500 }, (_, i) => `old ${i}`).join('\n');
  const current = Array.from({ length: DIFF_MAX_LINES + 500 }, (_, i) => `new ${i}`).join('\n');
  const result = diffLines(baseline, current);
  assert.equal(result.truncated, true);
  assert.equal(result.lines.length, DIFF_MAX_LINES);

  const small = diffLines('a\nb\n', 'a\nc\n', { maxLines: 2 });
  assert.equal(small.truncated, true);
  assert.equal(small.lines.length, 2);
});

test('empty and single-line inputs stay sane', () => {
  assert.equal(diffLines('', '').identical, true);
  const added = diffLines('', 'only\n');
  assert.deepEqual([added.added, added.removed], [1, 0]);
  const removed = diffLines('only\n', '');
  assert.deepEqual([removed.added, removed.removed], [0, 1]);
});
