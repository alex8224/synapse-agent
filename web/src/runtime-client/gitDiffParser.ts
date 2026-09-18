/**
 * Git unified diff parser and word-level intra-line diff computation.
 *
 * Pure, DOM-free module compatible with the runtime-client boundary and runnable
 * in standard Node.js unit tests.
 */

/**
 * A line inside a hunk.  The hunk header is not a line: it is the hunk's own
 * `rawHeader` / `heading`, so it never appears here (and the split-row builder
 * therefore never has to place it).
 */
export type DiffLineType = 'context' | 'addition' | 'deletion';

export interface DiffWordPart {
  text: string;
  type: 'same' | 'added' | 'removed';
}

export interface DiffLine {
  id: string;
  type: DiffLineType;
  oldLineNumber: number | null;
  newLineNumber: number | null;
  /** Text content without the leading diff prefix (+, -, space). */
  text: string;
  /** Raw line verbatim as seen in the diff. */
  rawText: string;
  /** Word-level diff parts (present on modified addition/deletion lines). */
  wordParts?: DiffWordPart[];
  /**
   * True when git emitted `\ No newline at end of file` for this line: the line
   * has no trailing newline on its side of the diff.  Carried on the line itself
   * (instead of a separate annotation line) so the viewer can badge the row the
   * marker belongs to.
   */
  noNewline?: boolean;
}

export interface DiffHunk {
  id: string;
  oldStart: number;
  oldCount: number;
  newStart: number;
  newCount: number;
  /** Header section heading (e.g. "flowchart LR" or original @@ ... @@ line). */
  heading: string;
  rawHeader: string;
  lines: DiffLine[];
}

export interface ParsedDiff {
  headerLines: string[];
  fromFile: string | null;
  toFile: string | null;
  hunks: DiffHunk[];
  addedCount: number;
  removedCount: number;
}

export interface SplitDiffRow {
  id: string;
  left: DiffLine | null;
  right: DiffLine | null;
}

/** Tokenize a code line into words, punctuation, and whitespace sequences. */
export function tokenizeLine(text: string): string[] {
  if (text === '') return [];
  const tokens: string[] = [];
  const regex = /([a-zA-Z0-9_$]+|[^\s\w]|\s+)/g;
  let match: RegExpExecArray | null;
  while ((match = regex.exec(text)) !== null) {
    tokens.push(match[0]);
  }
  return tokens;
}

/**
 * Compute word-level diff between a deleted line and an added line using LCS.
 * Falls back safely if the line is excessively long to prevent quadratic delays.
 */
export function computeWordDiff(
  oldText: string,
  newText: string,
): { oldParts: DiffWordPart[]; newParts: DiffWordPart[] } {
  // Fast paths
  if (oldText === '' && newText === '') {
    return { oldParts: [], newParts: [] };
  }
  if (oldText === '') {
    return {
      oldParts: [],
      newParts: [{ text: newText, type: 'added' }],
    };
  }
  if (newText === '') {
    return {
      oldParts: [{ text: oldText, type: 'removed' }],
      newParts: [],
    };
  }

  const oldTokens = tokenizeLine(oldText);
  const newTokens = tokenizeLine(newText);

  // Safety threshold: if tokens are too many, avoid NxM LCS matrix and mark entire lines
  if (oldTokens.length > 200 || newTokens.length > 200 || oldTokens.length * newTokens.length > 20_000) {
    return {
      oldParts: [{ text: oldText, type: 'removed' }],
      newParts: [{ text: newText, type: 'added' }],
    };
  }

  // Find common prefix tokens
  let prefix = 0;
  while (
    prefix < oldTokens.length &&
    prefix < newTokens.length &&
    oldTokens[prefix] === newTokens[prefix]
  ) {
    prefix += 1;
  }

  // Find common suffix tokens
  let suffix = 0;
  while (
    suffix < oldTokens.length - prefix &&
    suffix < newTokens.length - prefix &&
    oldTokens[oldTokens.length - 1 - suffix] === newTokens[newTokens.length - 1 - suffix]
  ) {
    suffix += 1;
  }

  const midOld = oldTokens.slice(prefix, oldTokens.length - suffix);
  const midNew = newTokens.slice(prefix, newTokens.length - suffix);

  const oldParts: DiffWordPart[] = [];
  const newParts: DiffWordPart[] = [];

  const pushPart = (target: DiffWordPart[], text: string, type: 'same' | 'added' | 'removed') => {
    if (text === '') return;
    const last = target[target.length - 1];
    if (last && last.type === type) {
      last.text += text;
    } else {
      target.push({ text, type });
    }
  };

  // Push prefix
  for (let i = 0; i < prefix; i += 1) {
    pushPart(oldParts, oldTokens[i], 'same');
    pushPart(newParts, newTokens[i], 'same');
  }

  // Compute LCS on middle tokens
  if (midOld.length > 0 || midNew.length > 0) {
    const table: number[][] = Array.from({ length: midOld.length + 1 }, () =>
      new Array<number>(midNew.length + 1).fill(0),
    );
    for (let i = midOld.length - 1; i >= 0; i -= 1) {
      for (let j = midNew.length - 1; j >= 0; j -= 1) {
        table[i][j] =
          midOld[i] === midNew[j]
            ? table[i + 1][j + 1] + 1
            : Math.max(table[i + 1][j], table[i][j + 1]);
      }
    }

    let i = 0;
    let j = 0;
    while (i < midOld.length && j < midNew.length) {
      if (midOld[i] === midNew[j]) {
        pushPart(oldParts, midOld[i], 'same');
        pushPart(newParts, midNew[j], 'same');
        i += 1;
        j += 1;
      } else if (table[i + 1][j] >= table[i][j + 1]) {
        pushPart(oldParts, midOld[i], 'removed');
        i += 1;
      } else {
        pushPart(newParts, midNew[j], 'added');
        j += 1;
      }
    }
    while (i < midOld.length) {
      pushPart(oldParts, midOld[i], 'removed');
      i += 1;
    }
    while (j < midNew.length) {
      pushPart(newParts, midNew[j], 'added');
      j += 1;
    }
  }

  // Push suffix
  for (let i = oldTokens.length - suffix; i < oldTokens.length; i += 1) {
    pushPart(oldParts, oldTokens[i], 'same');
  }
  for (let i = newTokens.length - suffix; i < newTokens.length; i += 1) {
    pushPart(newParts, newTokens[i], 'same');
  }

  return { oldParts, newParts };
}

const HUNK_REGEX = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: ?(.*))?$/;

/**
 * Parse a unified diff string into structured hunks and lines.
 */
export function parseUnifiedDiff(diffText: string): ParsedDiff {
  const normalized = diffText.replace(/\r\n/g, '\n');
  const rawLines = normalized === '' ? [] : normalized.split('\n');
  if (rawLines.length > 0 && rawLines[rawLines.length - 1] === '') {
    rawLines.pop();
  }

  const headerLines: string[] = [];
  let fromFile: string | null = null;
  let toFile: string | null = null;
  const hunks: DiffHunk[] = [];
  let currentHunk: DiffHunk | null = null;

  let oldLine = 0;
  let newLine = 0;
  let lineCounter = 0;
  let addedCount = 0;
  let removedCount = 0;

  for (let i = 0; i < rawLines.length; i += 1) {
    const rawLine = rawLines[i];

    // Detect hunk header
    const hunkMatch = HUNK_REGEX.exec(rawLine);
    if (hunkMatch) {
      const oldStart = parseInt(hunkMatch[1], 10);
      const oldCount = hunkMatch[2] !== undefined ? parseInt(hunkMatch[2], 10) : 1;
      const newStart = parseInt(hunkMatch[3], 10);
      const newCount = hunkMatch[4] !== undefined ? parseInt(hunkMatch[4], 10) : 1;
      const heading = (hunkMatch[5] ?? '').trim();

      oldLine = oldStart;
      newLine = newStart;

      currentHunk = {
        id: `hunk-${hunks.length}`,
        oldStart,
        oldCount,
        newStart,
        newCount,
        heading,
        rawHeader: rawLine,
        lines: [],
      };
      hunks.push(currentHunk);
      continue;
    }

    if (!currentHunk) {
      // In header section before any hunk
      headerLines.push(rawLine);
      if (rawLine.startsWith('--- ')) {
        fromFile = rawLine.slice(4).trim();
      } else if (rawLine.startsWith('+++ ')) {
        toFile = rawLine.slice(4).trim();
      }
      continue;
    }

    // Inside a hunk
    lineCounter += 1;
    const prefix = rawLine.length > 0 ? rawLine[0] : ' ';

    if (prefix === '+') {
      const lineText = rawLine.slice(1);
      const diffLine: DiffLine = {
        id: `line-${lineCounter}`,
        type: 'addition',
        oldLineNumber: null,
        newLineNumber: newLine,
        text: lineText,
        rawText: rawLine,
      };
      currentHunk.lines.push(diffLine);
      newLine += 1;
      addedCount += 1;
    } else if (prefix === '-') {
      const lineText = rawLine.slice(1);
      const diffLine: DiffLine = {
        id: `line-${lineCounter}`,
        type: 'deletion',
        oldLineNumber: oldLine,
        newLineNumber: null,
        text: lineText,
        rawText: rawLine,
      };
      currentHunk.lines.push(diffLine);
      oldLine += 1;
      removedCount += 1;
    } else if (prefix === '\\') {
      // e.g. "\ No newline at end of file": it annotates the line it follows, so
      // it is a flag on that line rather than a row of its own.
      const last = currentHunk.lines[currentHunk.lines.length - 1];
      if (last !== undefined) last.noNewline = true;
      continue;
    } else {
      // Context line (usually starts with ' ', or blank line)
      const lineText = prefix === ' ' ? rawLine.slice(1) : rawLine;
      const diffLine: DiffLine = {
        id: `line-${lineCounter}`,
        type: 'context',
        oldLineNumber: oldLine,
        newLineNumber: newLine,
        text: lineText,
        rawText: rawLine,
      };
      currentHunk.lines.push(diffLine);
      oldLine += 1;
      newLine += 1;
    }
  }

  // Post-process: compute intra-line word diffs for paired deletion + addition
  for (const hunk of hunks) {
    let idx = 0;
    while (idx < hunk.lines.length) {
      if (hunk.lines[idx].type === 'deletion') {
        const deletions: DiffLine[] = [];
        while (idx < hunk.lines.length && hunk.lines[idx].type === 'deletion') {
          deletions.push(hunk.lines[idx]);
          idx += 1;
        }
        const additions: DiffLine[] = [];
        while (idx < hunk.lines.length && hunk.lines[idx].type === 'addition') {
          additions.push(hunk.lines[idx]);
          idx += 1;
        }
        const pairs = Math.min(deletions.length, additions.length);
        for (let p = 0; p < pairs; p += 1) {
          const { oldParts, newParts } = computeWordDiff(deletions[p].text, additions[p].text);
          deletions[p].wordParts = oldParts;
          additions[p].wordParts = newParts;
        }
      } else {
        idx += 1;
      }
    }
  }

  return {
    headerLines,
    fromFile,
    toFile,
    hunks,
    addedCount,
    removedCount,
  };
}

/**
 * Pair lines within a hunk for side-by-side split view.
 */
export function buildSplitHunkRows(hunk: DiffHunk): SplitDiffRow[] {
  const rows: SplitDiffRow[] = [];
  let rowId = 0;

  let idx = 0;
  while (idx < hunk.lines.length) {
    const line = hunk.lines[idx];
    if (line.type === 'context') {
      rowId += 1;
      rows.push({
        id: `${hunk.id}-split-${rowId}`,
        left: line,
        right: line,
      });
      idx += 1;
    } else {
      // Collect deletions and additions block
      const deletions: DiffLine[] = [];
      while (idx < hunk.lines.length && hunk.lines[idx].type === 'deletion') {
        deletions.push(hunk.lines[idx]);
        idx += 1;
      }
      const additions: DiffLine[] = [];
      while (idx < hunk.lines.length && hunk.lines[idx].type === 'addition') {
        additions.push(hunk.lines[idx]);
        idx += 1;
      }

      const count = Math.max(deletions.length, additions.length);
      for (let i = 0; i < count; i += 1) {
        rowId += 1;
        rows.push({
          id: `${hunk.id}-split-${rowId}`,
          left: deletions[i] ?? null,
          right: additions[i] ?? null,
        });
      }
      // A type that is neither context nor a deletion/addition (a stray
      // `hunk-header`, say) collects nothing above; without this the loop would
      // never advance.  Skip it so the builder always terminates.
      if (count === 0) idx += 1;
    }
  }

  return rows;
}
