/**
 * Offline tests for the dependency-free math / mermaid presentation.
 *
 * The console ships no TeX or mermaid renderer, so the parser must at least
 * *classify* both explicitly (never silently degrading a formula into prose or a
 * diagram into an anonymous code block), and must stay conservative enough that
 * ordinary prose about money is still text.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { parseMarkdown } from '../src/markdown/parse.ts';

test('a single-line $$...$$ becomes a display-math block', () => {
  const blocks = parseMarkdown('$$E = mc^2$$');
  assert.equal(blocks.length, 1);
  const block = blocks[0];
  assert.equal(block?.type, 'math');
  assert.equal(block.type === 'math' ? block.tex : '', 'E = mc^2');
  assert.equal(block.type === 'math' ? block.closed : false, true);
});

test('a multi-line $$ block keeps its body and closes', () => {
  const blocks = parseMarkdown('$$\n\\int_0^1 x\\,dx\n$$');
  assert.equal(blocks.length, 1);
  const block = blocks[0];
  assert.equal(block?.type, 'math');
  assert.equal(block.type === 'math' ? block.tex : '', '\\int_0^1 x\\,dx');
  assert.equal(block.type === 'math' ? block.closed : false, true);
});

test('an unterminated $$ block stays a visible formula while streaming', () => {
  const blocks = parseMarkdown('$$\na + b');
  assert.equal(blocks.length, 1);
  const block = blocks[0];
  assert.equal(block?.type, 'math');
  assert.equal(block.type === 'math' ? block.closed : true, false);
  assert.equal(block.type === 'math' ? block.tex : '', 'a + b');
});

test('inline $...$ becomes an inline math span', () => {
  const blocks = parseMarkdown('the value $x^2$ is small');
  assert.equal(blocks.length, 1);
  const block = blocks[0];
  assert.equal(block?.type, 'paragraph');
  const spans = block.type === 'paragraph' ? block.spans : [];
  assert.deepEqual(spans.map((span) => span.type), ['text', 'math', 'text']);
  assert.equal(spans[1]?.type === 'math' ? spans[1].tex : '', 'x^2');
});

test('prose about money is never mistaken for math', () => {
  const blocks = parseMarkdown('it costs $5 and $6 more');
  const block = blocks[0];
  const spans = block?.type === 'paragraph' ? block.spans : [];
  assert.equal(spans.some((span) => span.type === 'math'), false);
  assert.equal(spans.map((span) => (span.type === 'text' ? span.text : '')).join(''), 'it costs $5 and $6 more');
});

test('an inline $ pair spanning a line break is not math', () => {
  const blocks = parseMarkdown('a $b\nc$ d');
  const block = blocks[0];
  const spans = block?.type === 'paragraph' ? block.spans : [];
  assert.equal(spans.some((span) => span.type === 'math'), false);
});

test('a mermaid fence stays a code block whose language is mermaid', () => {
  const blocks = parseMarkdown('```mermaid\ngraph TD;\n  A-->B;\n```');
  assert.equal(blocks.length, 1);
  const block = blocks[0];
  assert.equal(block?.type, 'code');
  assert.equal(block.type === 'code' ? block.lang : '', 'mermaid');
  assert.equal(block.type === 'code' ? block.code : '', 'graph TD;\n  A-->B;');
  assert.equal(block.type === 'code' ? block.closed : false, true);
});

test('math inside a fenced code block is left alone', () => {
  const blocks = parseMarkdown('```\n$$not math$$\n```');
  const block = blocks[0];
  assert.equal(block?.type, 'code');
  assert.equal(block.type === 'code' ? block.code : '', '$$not math$$');
});
