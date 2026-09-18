/**
 * Offline tests for the terminal-output reader.
 *
 * Two invariants matter: the visible text must survive parsing byte for byte, and
 * a control sequence that is not a colour must never reach the screen.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { hasAnsi, parseAnsi } from '../src/markdown/ansi.ts';

const ESC = '\u001b';
const visible = (text: string): string => parseAnsi(text).map((span) => span.text).join('');

test('text without escapes is one plain run', () => {
  assert.deepEqual(parseAnsi('plain output'), [{ text: 'plain output', className: '' }]);
  assert.equal(hasAnsi('plain output'), false);
  assert.equal(hasAnsi('[32m not an escape'), false);
});

test('a coloured run carries its colour and the reset ends it', () => {
  const spans = parseAnsi(ESC + '[32mok' + ESC + '[0m');
  assert.deepEqual(spans, [{ text: 'ok', className: 'text-emerald-600' }]);
  assert.equal(hasAnsi(ESC + '[0m'), true);
});

test('attributes and colours combine on one run', () => {
  const spans = parseAnsi(ESC + '[1;31mfail' + ESC + '[0m');
  assert.equal(spans.length, 1);
  assert.equal(spans[0].text, 'fail');
  assert.equal(spans[0].className, 'font-semibold text-red-600');
});

test('a background colour rides with the foreground', () => {
  const spans = parseAnsi(ESC + '[37;44mlabel' + ESC + '[0m');
  assert.equal(spans[0].className, 'text-gray-500 bg-blue-600');
});

test('the visible text is never altered by parsing', () => {
  const sample = 'a' + ESC + '[31mb' + ESC + '[0mc' + ESC + '[1;32md';
  assert.equal(visible(sample), 'abcd');
  const reset = ESC + '[0m';
  assert.equal(visible('x' + reset + 'y'), 'xy');
});

test('cursor and screen sequences are consumed, never printed', () => {
  assert.equal(visible(ESC + '[2J' + 'cleared'), 'cleared');
  assert.equal(visible(ESC + '[1A' + 'up'), 'up');
  assert.equal(visible(ESC + '[?25l' + 'hidden'), 'hidden');
});

test('an operating-system title is dropped whole', () => {
  assert.equal(visible(ESC + ']0;window title\u0007after'), 'after');
  assert.equal(visible(ESC + ']0;title' + ESC + '\\after'), 'after');
});

test('256-colour and true-colour sequences resolve to explicit colours', () => {
  const indexed = parseAnsi(ESC + '[38;5;196mred');
  assert.equal(indexed[0].text, 'red');
  assert.equal(indexed[0].color, '#ff0000');
  assert.equal(indexed[0].className, '');

  const trueColour = parseAnsi(ESC + '[48;2;1;2;3mcell' + ESC + '[0m');
  assert.equal(trueColour[0].backgroundColor, '#010203');

  const grey = parseAnsi(ESC + '[38;5;232mdim');
  assert.equal(grey[0].color, '#080808');
});

test('an unknown parameter is ignored rather than swallowing the text', () => {
  assert.equal(visible(ESC + '[99mtext'), 'text');
  assert.deepEqual(parseAnsi(ESC + '[mreset'), [{ text: 'reset', className: '' }]);
});