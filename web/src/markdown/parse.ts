/**
 * Minimal, dependency-free Markdown parser for agent answers.
 *
 * The console renders untrusted model output, so this module:
 * - never produces HTML: it returns typed nodes and the React layer renders
 *   them, so there is no HTML-injection path;
 * - sanitizes link targets (only http/https/mailto, fragments and relative
 *   paths survive; `javascript:` and friends are dropped);
 * - tolerates a truncated document, because answers stream in token by token:
 *   an unterminated fence stays a visible code block instead of vanishing.
 */

export interface SpanText {
  type: 'text';
  text: string;
}
export interface SpanCode {
  type: 'code';
  text: string;
}
export interface SpanStrong {
  type: 'strong';
  spans: Span[];
}
export interface SpanEm {
  type: 'em';
  spans: Span[];
}
export interface SpanDel {
  type: 'del';
  spans: Span[];
}
export interface SpanLink {
  type: 'link';
  href: string;
  spans: Span[];
}
/** Inline math (`$...$`), carried as raw TeX with no delimiters. */
export interface SpanMath {
  type: 'math';
  tex: string;
}
/**
 * A hard line break written as `<br>`, `<br/>` or `<br />`.
 *
 * Model answers routinely use the tag to stack several values inside one table
 * cell, where there is no other way to ask for a break.  It stays a typed node
 * so the React layer emits a real `<br />` element: the tag itself is never
 * passed through as markup.
 */
export interface SpanBreak {
  type: 'break';
}
export type Span =
  | SpanText
  | SpanCode
  | SpanStrong
  | SpanEm
  | SpanDel
  | SpanLink
  | SpanMath
  | SpanBreak;

export interface BlockParagraph {
  type: 'paragraph';
  spans: Span[];
}
export interface BlockHeading {
  type: 'heading';
  level: number;
  spans: Span[];
}
export interface BlockCode {
  type: 'code';
  lang: string;
  code: string;
  /** False while a streamed fence has not been closed yet. */
  closed: boolean;
}
export interface BlockList {
  type: 'list';
  ordered: boolean;
  start: number;
  items: Block[][];
}
export interface BlockQuote {
  type: 'quote';
  blocks: Block[];
}
export interface BlockRule {
  type: 'rule';
}
/** Display math (`$$...$$`), carried as raw TeX with no delimiters. */
export interface BlockMath {
  type: 'math';
  tex: string;
  /** False while a streamed display-math block has not been closed yet. */
  closed: boolean;
}
export interface BlockTable {
  type: 'table';
  header: Span[][];
  rows: Span[][][];
}
export type Block =
  | BlockParagraph
  | BlockHeading
  | BlockCode
  | BlockList
  | BlockQuote
  | BlockRule
  | BlockMath
  | BlockTable;

const ESCAPABLE = '\\`*_{}[]()#+-.!~|>';
const FENCE_RE = /^(\s*)(`{3,}|~{3,})[ \t]*(.*)$/;
const HEADING_RE = /^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$/;
const RULE_RE = /^\s{0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/;
const QUOTE_RE = /^\s{0,3}>/;
const ITEM_RE = /^(\s*)([-*+]|\d{1,9}[.)])[ \t]+(.*)$/;
/** Display-math opener: `$$` plus whatever follows it on the same line. */
const DISPLAY_MATH_RE = /^\s{0,3}\$\$(.*)$/;
/** Hard line break: `<br>`, `<br/>` or `<br />`, in any casing. */
const BREAK_RE = /^<br\s*\/?>/i;

/**
 * Keep only targets that cannot execute script.  A bare relative path is
 * allowed; anything carrying a scheme other than http/https/mailto is not.
 */
export function sanitizeHref(raw: string): string | null {
  const href = raw.trim();
  if (href === '') return null;
  // Checked before the leading-slash case: `//host` is protocol-relative and
  // would otherwise be accepted as a same-site path.
  if (href.startsWith('//')) return null;
  if (/^(https?:|mailto:)/i.test(href)) return href;
  if (href.startsWith('#') || href.startsWith('/')) return href;
  if (href.includes(':')) return null;
  return href;
}

/** Parse one line of inline Markdown into typed spans. */
export function parseInline(text: string): Span[] {
  const spans: Span[] = [];
  let buffer = '';
  let i = 0;

  const flush = (): void => {
    if (buffer !== '') {
      spans.push({ type: 'text', text: buffer });
      buffer = '';
    }
  };

  while (i < text.length) {
    const ch = text[i];

    if (ch === '\\' && ESCAPABLE.includes(text[i + 1] ?? '')) {
      buffer += text[i + 1];
      i += 2;
      continue;
    }

    if (ch === '`') {
      const ticks = /^`+/.exec(text.slice(i))?.[0] ?? '`';
      const close = text.indexOf(ticks, i + ticks.length);
      const inner = close === -1 ? '' : text.slice(i + ticks.length, close);
      if (close !== -1 && inner.trim() !== '') {
        flush();
        spans.push({ type: 'code', text: inner.trim() });
        i = close + ticks.length;
        continue;
      }
      buffer += ticks;
      i += ticks.length;
      continue;
    }

    if (ch === '*' || ch === '_') {
      const doubled = text.startsWith(ch + ch, i);
      const marker = doubled ? ch + ch : ch;
      const close = text.indexOf(marker, i + marker.length);
      if (close !== -1) {
        const after = text[i + marker.length] ?? '';
        const before = text[close - 1] ?? '';
        const inner = text.slice(i + marker.length, close);
        // CommonMark intraword rule: `_` cannot open or close inside a word.
        const intrawordBlocked =
          ch === '_' &&
          (/[A-Za-z0-9]/.test(text[i - 1] ?? '') ||
            /[A-Za-z0-9]/.test(text[close + marker.length] ?? ''));
        if (inner.trim() !== '' && !/\s/.test(after) && !/\s/.test(before) && !intrawordBlocked) {
          flush();
          const children = parseInline(inner);
          spans.push(
            doubled ? { type: 'strong', spans: children } : { type: 'em', spans: children },
          );
          i = close + marker.length;
          continue;
        }
      }
      buffer += marker;
      i += marker.length;
      continue;
    }

    if (ch === '~' && text.startsWith('~~', i)) {
      const close = text.indexOf('~~', i + 2);
      const inner = close === -1 ? '' : text.slice(i + 2, close);
      if (close !== -1 && inner.trim() !== '') {
        flush();
        spans.push({ type: 'del', spans: parseInline(inner) });
        i = close + 2;
        continue;
      }
      buffer += '~~';
      i += 2;
      continue;
    }

    if (ch === '$') {
      // Math is recognized conservatively so ordinary prose about money
      // ("$5 and $6") stays text: the content must be non-empty, must not span
      // a newline, and must not touch whitespace on either side of the pair.
      const display = text.startsWith('$$', i);
      const open = display ? 2 : 1;
      const close = text.indexOf(display ? '$$' : '$', i + open);
      const inner = close === -1 ? '' : text.slice(i + open, close);
      const after = text[i + open] ?? '';
      const before = text[close - 1] ?? '';
      if (
        close !== -1 &&
        inner.trim() !== '' &&
        !inner.includes('\n') &&
        !/\s/.test(after) &&
        !/\s/.test(before)
      ) {
        flush();
        spans.push({ type: 'math', tex: display ? inner.trim() : inner });
        i = close + open;
        continue;
      }
      buffer += ch;
      i += 1;
      continue;
    }

    if (ch === '[') {
      // One level of parentheses is allowed inside the target, so
      // `[x](javascript:alert(1))` is consumed whole instead of leaving a
      // stray `)` behind.
      const link = /^\[([^\]]*)\]\([ \t]*((?:[^()\s]|\([^()\s]*\))+)(?:[ \t]+"[^"]*")?[ \t]*\)/.exec(
        text.slice(i),
      );
      if (link) {
        flush();
        const href = sanitizeHref(link[2]);
        const label = parseInline(link[1]);
        if (href === null) {
          spans.push(...label);
        } else {
          spans.push({ type: 'link', href, spans: label });
        }
        i += link[0].length;
        continue;
      }
    }

    if (ch === '<') {
      const br = BREAK_RE.exec(text.slice(i));
      if (br) {
        flush();
        spans.push({ type: 'break' });
        i += br[0].length;
        continue;
      }
      const auto = /^<(https?:\/\/[^>\s]+)>/.exec(text.slice(i));
      if (auto) {
        flush();
        spans.push({ type: 'link', href: auto[1], spans: [{ type: 'text', text: auto[1] }] });
        i += auto[0].length;
        continue;
      }
    }

    buffer += ch;
    i += 1;
  }

  flush();
  return spans;
}

function splitRawCells(line: string): string[] {
  let text = line.trim();
  if (text.startsWith('|')) text = text.slice(1);
  if (text.endsWith('|')) text = text.slice(0, -1);
  const cells: string[] = [];
  let current = '';
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (ch === '\\' && text[i + 1] === '|') {
      current += '|';
      i += 1;
      continue;
    }
    if (ch === '|') {
      cells.push(current);
      current = '';
      continue;
    }
    current += ch;
  }
  cells.push(current);
  return cells.map((cell) => cell.trim());
}

function isTableDelimiter(line: string): boolean {
  if (!line.includes('|')) return false;
  const cells = splitRawCells(line);
  return cells.length > 0 && cells.every((cell) => /^:?-+:?$/.test(cell));
}

function startsBlock(lines: string[], index: number): boolean {
  const line = lines[index];
  if (FENCE_RE.test(line)) return true;
  if (DISPLAY_MATH_RE.test(line)) return true;
  if (HEADING_RE.test(line)) return true;
  if (RULE_RE.test(line)) return true;
  if (QUOTE_RE.test(line)) return true;
  if (ITEM_RE.test(line)) return true;
  return line.includes('|') && index + 1 < lines.length && isTableDelimiter(lines[index + 1]);
}

/** Parse a whole Markdown document into typed blocks. */
export function parseMarkdown(source: string): Block[] {
  return parseLines(source.replace(/\r\n?/g, '\n').split('\n'));
}

function parseLines(lines: string[]): Block[] {
  const blocks: Block[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (line.trim() === '') {
      i += 1;
      continue;
    }

    const fence = FENCE_RE.exec(line);
    if (fence) {
      const marker = fence[2][0];
      const minimum = fence[2].length;
      const closing = new RegExp(`^\\s*${marker === '`' ? '`' : '~'}{${minimum},}\\s*$`);
      const body: string[] = [];
      let j = i + 1;
      let closed = false;
      while (j < lines.length) {
        if (closing.test(lines[j])) {
          closed = true;
          break;
        }
        body.push(lines[j]);
        j += 1;
      }
      blocks.push({
        type: 'code',
        lang: (fence[3].trim().split(/\s+/)[0] ?? '').toLowerCase(),
        code: body.join('\n'),
        closed,
      });
      i = closed ? j + 1 : j;
      continue;
    }

    const math = DISPLAY_MATH_RE.exec(line);
    if (math) {
      const opener = math[1];
      // Single-line `$$ ... $$`.
      if (opener.trim() !== '' && opener.trimEnd().endsWith('$$')) {
        blocks.push({
          type: 'math',
          tex: opener.trimEnd().slice(0, -2).trim(),
          closed: true,
        });
        i += 1;
        continue;
      }
      const body: string[] = opener.trim() === '' ? [] : [opener];
      let j = i + 1;
      let closed = false;
      while (j < lines.length) {
        const current = lines[j];
        const trimmed = current.trimEnd();
        if (trimmed.endsWith('$$') && trimmed.trim() !== '') {
          body.push(trimmed.slice(0, -2));
          closed = true;
          j += 1;
          break;
        }
        body.push(current);
        j += 1;
      }
      // A streamed answer can be cut mid-formula: the block stays visible as a
      // formula instead of vanishing (same rule as an unterminated fence).
      blocks.push({ type: 'math', tex: body.join('\n').trim(), closed });
      i = j;
      continue;
    }

    const heading = HEADING_RE.exec(line);
    if (heading) {
      blocks.push({ type: 'heading', level: heading[1].length, spans: parseInline(heading[2]) });
      i += 1;
      continue;
    }

    if (RULE_RE.test(line)) {
      blocks.push({ type: 'rule' });
      i += 1;
      continue;
    }

    if (QUOTE_RE.test(line)) {
      const inner: string[] = [];
      let j = i;
      while (j < lines.length && QUOTE_RE.test(lines[j])) {
        inner.push(lines[j].replace(/^\s{0,3}>[ \t]?/, ''));
        j += 1;
      }
      blocks.push({ type: 'quote', blocks: parseLines(inner) });
      i = j;
      continue;
    }

    if (line.includes('|') && i + 1 < lines.length && isTableDelimiter(lines[i + 1])) {
      const header = splitRawCells(line).map(parseInline);
      const rows: Span[][][] = [];
      let j = i + 2;
      while (j < lines.length && lines[j].trim() !== '' && lines[j].includes('|')) {
        rows.push(splitRawCells(lines[j]).map(parseInline));
        j += 1;
      }
      blocks.push({ type: 'table', header, rows });
      i = j;
      continue;
    }

    const item = ITEM_RE.exec(line);
    if (item) {
      const ordered = /\d/.test(item[2][0]);
      const start = ordered ? Number.parseInt(item[2], 10) : 1;
      const items: Block[][] = [];
      let j = i;
      while (j < lines.length) {
        const head = ITEM_RE.exec(lines[j]);
        if (!head) break;
        const indent = head[1].length;
        const content: string[] = [head[3]];
        j += 1;
        while (j < lines.length) {
          const next = lines[j];
          if (next.trim() === '') {
            const after = lines[j + 1];
            if (after === undefined || after.trim() === '' || !/^\s{2,}/.test(after)) break;
            content.push('');
            j += 1;
            continue;
          }
          const nextIndent = next.length - next.trimStart().length;
          if (nextIndent <= indent) break;
          content.push(next.slice(Math.min(indent + 2, nextIndent)));
          j += 1;
        }
        items.push(parseLines(content));
      }
      blocks.push({ type: 'list', ordered, start, items });
      i = j;
      continue;
    }

    const paragraph: string[] = [line];
    let j = i + 1;
    while (j < lines.length && lines[j].trim() !== '' && !startsBlock(lines, j)) {
      paragraph.push(lines[j]);
      j += 1;
    }
    blocks.push({ type: 'paragraph', spans: parseInline(paragraph.join('\n')) });
    i = j;
  }

  return blocks;
}
