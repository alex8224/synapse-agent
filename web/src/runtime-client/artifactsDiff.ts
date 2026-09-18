/**
 * Real line-level diff for the workspace file panel.
 *
 * The artifact wire surface exposes no revision *history*: `revision` is a stat
 * fingerprint used to detect that a file changed, and there is no way to fetch an
 * older revision's bytes. The panel therefore diffs two texts it actually holds —
 * the snapshot captured when the file was first opened and the current content —
 * and this module computes a genuine line diff between them (longest common
 * subsequence, not "looks like a diff" colouring).
 *
 * Pure and dependency-free so it is exercised directly with the Node test runner.
 */

export interface DiffLine {
  kind: 'context' | 'add' | 'remove';
  /** 1-based line number in the baseline, or null for an added line. */
  baselineLine: number | null;
  /** 1-based line number in the current text, or null for a removed line. */
  currentLine: number | null;
  text: string;
}

export interface DiffResult {
  lines: DiffLine[];
  added: number;
  removed: number;
  /** True when both sides are identical, so there is nothing to show. */
  identical: boolean;
  /**
   * True when the middle block was too large for an exact LCS and was reported as
   * one replacement block instead (still real content, just coarser).
   */
  coarse: boolean;
  /** True when the rendered line list hit `maxLines` and was cut. */
  truncated: boolean;
  /** Total lines in the baseline / current text (before any truncation). */
  baselineLines: number;
  currentLines: number;
}

/** Exact LCS is only attempted while the middle block stays this small. */
export const DIFF_MAX_CELLS = 250_000;
/** Rendered diff lines are capped so a huge file cannot flood the panel. */
export const DIFF_MAX_LINES = 4_000;

function splitLines(text: string): string[] {
  // CRLF is normalized so a line-ending-only change is not reported as an edit.
  const normalized = text.replace(/\r\n/g, '\n');
  // An empty file has no lines at all (not one empty line), so "add a line to an
  // empty file" is one addition rather than a replacement.
  if (normalized === '') return [];
  const lines = normalized.split('\n');
  // A trailing newline produces one empty trailing element that is not a line.
  if (lines.length > 1 && lines[lines.length - 1] === '') lines.pop();
  return lines;
}

function contextLine(text: string, baseline: number, current: number): DiffLine {
  return { kind: 'context', baselineLine: baseline, currentLine: current, text };
}

/** Longest common subsequence of two line arrays (exact, small inputs only). */
function lcsMatrix(a: string[], b: string[]): number[][] {
  const table: number[][] = Array.from({ length: a.length + 1 }, () =>
    new Array<number>(b.length + 1).fill(0),
  );
  for (let i = a.length - 1; i >= 0; i -= 1) {
    for (let j = b.length - 1; j >= 0; j -= 1) {
      table[i][j] =
        a[i] === b[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
    }
  }
  return table;
}

/**
 * Compute a real line diff between `baseline` and `current`.
 *
 * Common prefix/suffix are trimmed first (exact and cheap), then the remaining
 * middle block is diffed with an LCS when it is small enough.  A middle block
 * beyond `DIFF_MAX_CELLS` is reported as one replacement block and flagged
 * `coarse` — the panel says so instead of pretending the diff is minimal.
 */
export function diffLines(
  baseline: string,
  current: string,
  opts: { maxLines?: number } = {},
): DiffResult {
  const maxLines = opts.maxLines ?? DIFF_MAX_LINES;
  const a = splitLines(baseline);
  const b = splitLines(current);
  const result: DiffResult = {
    lines: [],
    added: 0,
    removed: 0,
    // Compared after line-ending normalization: a CRLF/LF-only rewrite is not a
    // content change and must not be reported as a diff.
    identical: a.length === b.length && a.every((line, index) => line === b[index]),
    coarse: false,
    truncated: false,
    baselineLines: a.length,
    currentLines: b.length,
  };
  if (result.identical) return result;

  let head = 0;
  while (head < a.length && head < b.length && a[head] === b[head]) head += 1;
  let tail = 0;
  while (
    tail < a.length - head &&
    tail < b.length - head &&
    a[a.length - 1 - tail] === b[b.length - 1 - tail]
  ) {
    tail += 1;
  }

  const push = (line: DiffLine): boolean => {
    if (result.lines.length >= maxLines) {
      result.truncated = true;
      return false;
    }
    result.lines.push(line);
    return true;
  };

  for (let index = 0; index < head; index += 1) {
    if (!push(contextLine(a[index], index + 1, index + 1))) break;
  }

  const midA = a.slice(head, a.length - tail);
  const midB = b.slice(head, b.length - tail);
  if (!result.truncated) {
    if (midA.length * midB.length <= DIFF_MAX_CELLS) {
      const table = lcsMatrix(midA, midB);
      let i = 0;
      let j = 0;
      while (i < midA.length && j < midB.length) {
        if (midA[i] === midB[j]) {
          if (!push(contextLine(midA[i], head + i + 1, head + j + 1))) break;
          i += 1;
          j += 1;
        } else if (table[i + 1][j] >= table[i][j + 1]) {
          if (!push({ kind: 'remove', baselineLine: head + i + 1, currentLine: null, text: midA[i] })) break;
          result.removed += 1;
          i += 1;
        } else {
          if (!push({ kind: 'add', baselineLine: null, currentLine: head + j + 1, text: midB[j] })) break;
          result.added += 1;
          j += 1;
        }
      }
      while (i < midA.length && !result.truncated) {
        if (!push({ kind: 'remove', baselineLine: head + i + 1, currentLine: null, text: midA[i] })) break;
        result.removed += 1;
        i += 1;
      }
      while (j < midB.length && !result.truncated) {
        if (!push({ kind: 'add', baselineLine: null, currentLine: head + j + 1, text: midB[j] })) break;
        result.added += 1;
        j += 1;
      }
    } else {
      // Too large for an exact LCS: report the whole middle as one replacement
      // block and say so, rather than silently claiming a minimal diff.
      result.coarse = true;
      for (let index = 0; index < midA.length; index += 1) {
        if (!push({ kind: 'remove', baselineLine: head + index + 1, currentLine: null, text: midA[index] })) break;
        result.removed += 1;
      }
      for (let index = 0; index < midB.length && !result.truncated; index += 1) {
        if (!push({ kind: 'add', baselineLine: null, currentLine: head + index + 1, text: midB[index] })) break;
        result.added += 1;
      }
    }
  }

  if (!result.truncated) {
    for (let index = 0; index < tail; index += 1) {
      const baselineLine = a.length - tail + index + 1;
      const currentLine = b.length - tail + index + 1;
      if (!push(contextLine(a[baselineLine - 1], baselineLine, currentLine))) break;
    }
  }
  return result;
}
