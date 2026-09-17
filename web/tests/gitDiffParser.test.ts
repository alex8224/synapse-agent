/**
 * Tests for the unified diff parser and word-level intra-line diff computation.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  parseUnifiedDiff,
  computeWordDiff,
  buildSplitHunkRows,
  tokenizeLine,
  type DiffHunk,
  type DiffLine,
} from '../src/runtime-client/gitDiffParser.ts';

// One hunk: context (80), deletion (81), addition (81), context (82) -- three
// lines on each side, so the header counts are 3/3 and match `lines`.
const SAMPLE_DIFF = `diff --git a/docs/example.md b/docs/example.md
index a532395..9cb6f40 100644
--- a/docs/example.md
+++ b/docs/example.md
@@ -80,3 +80,3 @@ flowchart LR
 unchanged line 1
-deleted line 1 old
+deleted line 1 new
 unchanged line 2`;

// Both sides end without a trailing newline, so git emits the marker after each
// of them; it must fold onto the line it follows, not become a row of its own.
const NO_NEWLINE_DIFF = `diff --git a/f.txt b/f.txt
index 1111111..2222222 100644
--- a/f.txt
+++ b/f.txt
@@ -1,2 +1,2 @@
 keep
-old
\\ No newline at end of file
+new
\\ No newline at end of file`;

const MULTI_HUNK_DIFF = `diff --git a/m.txt b/m.txt
--- a/m.txt
+++ b/m.txt
@@ -1,2 +1,2 @@
 a
-b
+B
@@ -10,2 +10,3 @@
 j
+k
 l`;

// More additions than deletions on one hunk, and more deletions than additions on
// the other, so the split view has to pad a missing side on both directions.
const UNEVEN_DIFF = `diff --git a/u.txt b/u.txt
--- a/u.txt
+++ b/u.txt
@@ -1,2 +1,4 @@
 same
-old one
+new one
+new two
+new three
@@ -20,3 +20,2 @@
 same
-gone one
-gone two
+kept
`;

test('tokenizeLine splits words, punctuation and spaces', () => {
  const tokens = tokenizeLine('const a = 12; // comment');
  assert.deepEqual(tokens, ['const', ' ', 'a', ' ', '=', ' ', '12', ';', ' ', '/', '/', ' ', 'comment']);
});

test('computeWordDiff finds intra-line word changes', () => {
  const oldText = 'const name = "Alice";';
  const newText = 'const name = "Bob";';
  const { oldParts, newParts } = computeWordDiff(oldText, newText);

  // Common prefix: 'const name = "'
  // Changed: 'Alice' vs 'Bob'
  // Common suffix: '";'
  assert.equal(oldParts.map((p) => p.text).join(''), oldText);
  assert.equal(newParts.map((p) => p.text).join(''), newText);

  const oldChanged = oldParts.filter((p) => p.type === 'removed');
  const newChanged = newParts.filter((p) => p.type === 'added');

  assert.equal(oldChanged.length, 1);
  assert.equal(oldChanged[0].text, 'Alice');

  assert.equal(newChanged.length, 1);
  assert.equal(newChanged[0].text, 'Bob');
});

test('parseUnifiedDiff parses headers, hunks and lines', () => {
  const parsed = parseUnifiedDiff(SAMPLE_DIFF);

  assert.equal(parsed.fromFile, 'a/docs/example.md');
  assert.equal(parsed.toFile, 'b/docs/example.md');
  assert.equal(parsed.hunks.length, 1);

  const hunk = parsed.hunks[0];
  assert.equal(hunk.oldStart, 80);
  assert.equal(hunk.oldCount, 3);
  assert.equal(hunk.newStart, 80);
  assert.equal(hunk.newCount, 3);
  assert.equal(hunk.heading, 'flowchart LR');

  // Lines:
  // 1. context: oldLine 80, newLine 80
  // 2. deletion: oldLine 81, newLine null
  // 3. addition: oldLine null, newLine 81
  // 4. context: oldLine 82, newLine 82
  assert.equal(hunk.lines.length, 4);
  assert.equal(hunk.lines[0].type, 'context');
  assert.equal(hunk.lines[0].oldLineNumber, 80);
  assert.equal(hunk.lines[0].newLineNumber, 80);

  assert.equal(hunk.lines[1].type, 'deletion');
  assert.equal(hunk.lines[1].oldLineNumber, 81);
  assert.equal(hunk.lines[1].newLineNumber, null);
  assert.ok(hunk.lines[1].wordParts !== undefined);

  assert.equal(hunk.lines[2].type, 'addition');
  assert.equal(hunk.lines[2].oldLineNumber, null);
  assert.equal(hunk.lines[2].newLineNumber, 81);
  assert.ok(hunk.lines[2].wordParts !== undefined);

  assert.equal(hunk.lines[3].type, 'context');
  assert.equal(hunk.lines[3].oldLineNumber, 82);
  assert.equal(hunk.lines[3].newLineNumber, 82);

  assert.equal(parsed.addedCount, 1);
  assert.equal(parsed.removedCount, 1);
});

test('buildSplitHunkRows pairs deletions with additions', () => {
  const parsed = parseUnifiedDiff(SAMPLE_DIFF);
  const rows = buildSplitHunkRows(parsed.hunks[0]);

  // Row 0: context 1 (both sides)
  assert.equal(rows[0].left?.type, 'context');
  assert.equal(rows[0].right?.type, 'context');

  // Row 1: left is deletion, right is addition
  assert.equal(rows[1].left?.type, 'deletion');
  assert.equal(rows[1].right?.type, 'addition');
  assert.equal(rows[1].left?.oldLineNumber, 81);
  assert.equal(rows[1].right?.newLineNumber, 81);

  // Row 2: context 2 (both sides)
  assert.equal(rows[2].left?.type, 'context');
  assert.equal(rows[2].right?.type, 'context');
});

test('parseUnifiedDiff folds "\\ No newline at end of file" onto its line', () => {
  const parsed = parseUnifiedDiff(NO_NEWLINE_DIFF);

  assert.equal(parsed.hunks.length, 1);
  const hunk = parsed.hunks[0];
  assert.equal(hunk.oldCount, 2);
  assert.equal(hunk.newCount, 2);

  // The marker is not a row: the hunk keeps exactly its three real lines.
  assert.equal(hunk.lines.length, 3);
  assert.equal(hunk.lines.some((line) => line.rawText.startsWith('\\')), false);

  assert.equal(hunk.lines[0].type, 'context');
  assert.equal(hunk.lines[0].noNewline, undefined);

  assert.equal(hunk.lines[1].type, 'deletion');
  assert.equal(hunk.lines[1].text, 'old');
  assert.equal(hunk.lines[1].noNewline, true);

  assert.equal(hunk.lines[2].type, 'addition');
  assert.equal(hunk.lines[2].text, 'new');
  assert.equal(hunk.lines[2].noNewline, true);
});

test('parseUnifiedDiff reads every hunk of a multi-hunk diff', () => {
  const parsed = parseUnifiedDiff(MULTI_HUNK_DIFF);

  assert.equal(parsed.hunks.length, 2);

  const [first, second] = parsed.hunks;
  assert.equal(first.oldStart, 1);
  assert.equal(first.newStart, 1);
  assert.equal(first.oldCount, 2);
  assert.equal(first.newCount, 2);
  assert.equal(first.heading, '');
  assert.equal(first.lines.length, 3);

  assert.equal(second.oldStart, 10);
  assert.equal(second.newStart, 10);
  assert.equal(second.oldCount, 2);
  assert.equal(second.newCount, 3);
  assert.equal(second.lines.length, 3);
  // Line numbers are seeded per hunk, not carried over from the previous one.
  assert.equal(second.lines[0].oldLineNumber, 10);
  assert.equal(second.lines[0].newLineNumber, 10);
  assert.equal(second.lines[2].oldLineNumber, 11);
  assert.equal(second.lines[2].newLineNumber, 12);

  assert.equal(parsed.addedCount, 2);
  assert.equal(parsed.removedCount, 1);
});

test('buildSplitHunkRows pads the short side with a blank placeholder', () => {
  const parsed = parseUnifiedDiff(UNEVEN_DIFF);

  // Hunk 1: one deletion, three additions -> the last two rows have no left.
  const moreAdds = buildSplitHunkRows(parsed.hunks[0]);
  assert.equal(moreAdds.length, 4);
  assert.equal(moreAdds[0].left?.type, 'context');
  assert.equal(moreAdds[0].right?.type, 'context');
  assert.equal(moreAdds[1].left?.type, 'deletion');
  assert.equal(moreAdds[1].right?.type, 'addition');
  assert.equal(moreAdds[2].left, null);
  assert.equal(moreAdds[2].right?.type, 'addition');
  assert.equal(moreAdds[3].left, null);
  assert.equal(moreAdds[3].right?.type, 'addition');

  // Hunk 2: two deletions, one addition -> the last row has no right.
  const moreDels = buildSplitHunkRows(parsed.hunks[1]);
  assert.equal(moreDels.length, 3);
  assert.equal(moreDels[1].left?.type, 'deletion');
  assert.equal(moreDels[1].right?.type, 'addition');
  assert.equal(moreDels[2].left?.type, 'deletion');
  assert.equal(moreDels[2].right, null);

  // Every row is addressed, so the two sides stay aligned row for row.
  for (const row of [...moreAdds, ...moreDels]) {
    assert.ok(row.id.length > 0);
  }
});

test('buildSplitHunkRows cannot spin on a line it cannot place', () => {
  // Defensive: the parser only emits context/deletion/addition, but a stray line
  // type must still terminate instead of looping forever on an unadvanced index.
  const stray = { type: 'hunk-header' } as unknown as DiffLine;
  const hunk: DiffHunk = {
    id: 'hunk-x',
    oldStart: 1,
    oldCount: 1,
    newStart: 1,
    newCount: 1,
    heading: '',
    rawHeader: '',
    lines: [stray],
  };
  assert.deepEqual(buildSplitHunkRows(hunk), []);
});

test('parseUnifiedDiff handles empty and edge diffs', () => {
  const emptyParsed = parseUnifiedDiff('');
  assert.equal(emptyParsed.hunks.length, 0);
  assert.equal(emptyParsed.addedCount, 0);
  assert.equal(emptyParsed.removedCount, 0);
});
