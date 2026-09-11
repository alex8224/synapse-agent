/**
 * Offline tests for the dependency-free Markdown parser used by the console
 * transcript.  Pure functions only: no DOM, no host, no socket.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  parseInline,
  parseMarkdown,
  sanitizeHref,
  type BlockCode,
  type BlockList,
  type BlockParagraph,
  type BlockTable,
} from '../src/markdown/parse.ts';

function paragraphOf(blocks: ReturnType<typeof parseMarkdown>, index = 0): BlockParagraph {
  const block = blocks[index];
  assert.equal(block.type, 'paragraph');
  return block as BlockParagraph;
}

function plainText(spans: ReturnType<typeof parseInline>): string {
  return spans
    .map((span) => {
      if (span.type === 'text') return span.text;
      if (span.type === 'code') return `[code:${span.text}]`;
      if (span.type === 'link') return `[link:${span.href}]${plainText(span.spans)}`;
      if (span.type === 'strong') return `[b]${plainText(span.spans)}[/b]`;
      if (span.type === 'em') return `[i]${plainText(span.spans)}[/i]`;
      return `[del]${plainText(span.spans)}[/del]`;
    })
    .join('');
}

test('a plain line becomes one paragraph', () => {
  const blocks = parseMarkdown('hello world');
  assert.equal(blocks.length, 1);
  assert.equal(plainText(paragraphOf(blocks).spans), 'hello world');
});

test('consecutive lines join into one paragraph and blank lines split it', () => {
  const blocks = parseMarkdown('one\ntwo\n\nthree');
  assert.equal(blocks.length, 2);
  assert.equal(plainText(paragraphOf(blocks, 0).spans), 'one\ntwo');
  assert.equal(plainText(paragraphOf(blocks, 1).spans), 'three');
});

test('atx headings carry their level', () => {
  const blocks = parseMarkdown('# a\n\n### b');
  assert.deepEqual(
    blocks.map((b) => (b.type === 'heading' ? b.level : -1)),
    [1, 3],
  );
});

test('a fenced block keeps its language, body and closed flag', () => {
  const blocks = parseMarkdown('```python\nprint(1)\n```');
  assert.equal(blocks.length, 1);
  const code = blocks[0] as BlockCode;
  assert.equal(code.type, 'code');
  assert.equal(code.lang, 'python');
  assert.equal(code.code, 'print(1)');
  assert.equal(code.closed, true);
});

test('an unterminated fence stays a visible, open code block', () => {
  const blocks = parseMarkdown('text\n\n```ts\nconst a = 1;');
  const code = blocks[1] as BlockCode;
  assert.equal(code.type, 'code');
  assert.equal(code.lang, 'ts');
  assert.equal(code.code, 'const a = 1;');
  assert.equal(code.closed, false);
});

test('a fenced block preserves markdown syntax inside it verbatim', () => {
  const body = '# not a heading\n**not bold**\n- not a list';
  const blocks = parseMarkdown(`\`\`\`md\n${body}\n\`\`\``);
  const code = blocks[0] as BlockCode;
  assert.equal(code.code, body);
  assert.equal(blocks.length, 1);
});

test('tilde fences close on the same marker only', () => {
  const blocks = parseMarkdown('~~~\n```\n~~~');
  const code = blocks[0] as BlockCode;
  assert.equal(code.code, '```');
  assert.equal(code.closed, true);
});

test('inline code, bold, italic and strikethrough are typed spans', () => {
  assert.equal(plainText(parseInline('a `x` b')), 'a [code:x] b');
  assert.equal(plainText(parseInline('**bold**')), '[b]bold[/b]');
  assert.equal(plainText(parseInline('*em*')), '[i]em[/i]');
  assert.equal(plainText(parseInline('~~gone~~')), '[del]gone[/del]');
});

test('intraword underscores and spaced asterisks stay literal', () => {
  assert.equal(plainText(parseInline('snake_case_name')), 'snake_case_name');
  assert.equal(plainText(parseInline('2 * 3 * 4')), '2 * 3 * 4');
});

test('links are typed and unsafe targets are dropped to plain text', () => {
  assert.equal(plainText(parseInline('[docs](https://example.com/x)')), '[link:https://example.com/x]docs');
  assert.equal(plainText(parseInline('[bad](javascript:alert(1))')), 'bad');
  assert.equal(plainText(parseInline('[deep](https://e.com/a_(b))')), '[link:https://e.com/a_(b)]deep');
  assert.equal(
    plainText(parseInline('see <https://example.com>')),
    'see [link:https://example.com]https://example.com',
  );
});

test('sanitizeHref keeps safe schemes and relative paths only', () => {
  assert.equal(sanitizeHref('https://a/b'), 'https://a/b');
  assert.equal(sanitizeHref('mailto:a@b.c'), 'mailto:a@b.c');
  assert.equal(sanitizeHref('#section'), '#section');
  assert.equal(sanitizeHref('docs/readme.md'), 'docs/readme.md');
  assert.equal(sanitizeHref('javascript:alert(1)'), null);
  assert.equal(sanitizeHref('data:text/html,x'), null);
  assert.equal(sanitizeHref('//evil.example'), null);
});

test('backslash escapes suppress inline syntax', () => {
  assert.equal(plainText(parseInline('\\*not em\\*')), '*not em*');
  assert.equal(plainText(parseInline('\\`not code\\`')), '`not code`');
});

test('unordered and ordered lists are parsed with their start number', () => {
  const bullets = parseMarkdown('- one\n- two')[0] as BlockList;
  assert.equal(bullets.type, 'list');
  assert.equal(bullets.ordered, false);
  assert.equal(bullets.items.length, 2);
  assert.equal(plainText((bullets.items[0][0] as BlockParagraph).spans), 'one');

  const ordered = parseMarkdown('3. three\n4. four')[0] as BlockList;
  assert.equal(ordered.ordered, true);
  assert.equal(ordered.start, 3);
  assert.equal(ordered.items.length, 2);
});

test('an indented continuation nests a list inside an item', () => {
  const list = parseMarkdown('- outer\n  - inner')[0] as BlockList;
  assert.equal(list.items.length, 1);
  const nested = list.items[0].find((b) => b.type === 'list') as BlockList;
  assert.equal(nested.type, 'list');
  assert.equal(plainText((nested.items[0][0] as BlockParagraph).spans), 'inner');
});

test('blockquotes recurse into their own blocks', () => {
  const blocks = parseMarkdown('> quoted **text**\n> more');
  assert.equal(blocks[0].type, 'quote');
  const inner = (blocks[0] as { type: 'quote'; blocks: ReturnType<typeof parseMarkdown> }).blocks;
  assert.equal(plainText((inner[0] as BlockParagraph).spans), 'quoted [b]text[/b]\nmore');
});

test('horizontal rules are recognized', () => {
  assert.equal(parseMarkdown('a\n\n---\n\nb')[1].type, 'rule');
  assert.equal(parseMarkdown('***')[0].type, 'rule');
});

test('gfm tables split header and rows', () => {
  const blocks = parseMarkdown('| a | b |\n| --- | --- |\n| 1 | 2 |');
  const table = blocks[0] as BlockTable;
  assert.equal(table.type, 'table');
  assert.equal(table.header.length, 2);
  assert.equal(plainText(table.header[0]), 'a');
  assert.equal(table.rows.length, 1);
  assert.equal(plainText(table.rows[0][1]), '2');
});

test('a pipe line without a delimiter row stays a paragraph', () => {
  const blocks = parseMarkdown('a | b\n---');
  assert.equal(blocks[0].type, 'paragraph');
  assert.equal(blocks[1].type, 'rule');
});

test('a heading directly after a paragraph starts its own block', () => {
  const blocks = parseMarkdown('text\n# head');
  assert.deepEqual(blocks.map((b) => b.type), ['paragraph', 'heading']);
});

test('parsing never throws on pathological input', () => {
  const samples = ['', '```', '~~~x', '>', '- ', '#', '|', '|||', '*', '\\', '[a](', '**', '\n\n\n'];
  for (const sample of samples) {
    assert.doesNotThrow(() => parseMarkdown(sample), `parseMarkdown(${JSON.stringify(sample)})`);
  }
});
