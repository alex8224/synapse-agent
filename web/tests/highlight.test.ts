/**
 * Offline tests for the conservative code-block highlighter.
 *
 * The critical invariant is losslessness: the concatenation of every token must
 * equal the input byte for byte, so highlighting can never alter the code.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { highlight, isHighlightedLanguage, type HighlightToken } from '../src/markdown/highlight.ts';

function joined(tokens: HighlightToken[]): string {
  return tokens.map((token) => token.text).join('');
}

function textsOf(tokens: HighlightToken[], kind: string): string[] {
  return tokens.filter((token) => token.kind === kind).map((token) => token.text);
}

test('an unknown language returns the input as one plain token', () => {
  const tokens = highlight('anything at all', 'brainfuck');
  assert.deepEqual(tokens, [{ text: 'anything at all', kind: 'plain' }]);
  assert.equal(isHighlightedLanguage('brainfuck'), false);
  assert.equal(isHighlightedLanguage('python'), true);
  assert.equal(isHighlightedLanguage('diff'), true);
});

test('highlighting is lossless for every supported language', () => {
  const samples: Array<[string, string]> = [
    ['python', '# c\ndef f(x):\n    return "s" + str(1.5)'],
    ['javascript', 'const a = 1; // c\n/* b */ `t${a}`'],
    ['typescript', 'interface X { a: string }\nconst y: X = { a: "z" };'],
    ['json', '{"a": 1, "b": true, "c": null}'],
    ['bash', '# c\necho "hi" | grep -v x'],
    ['yaml', 'key: "value"  # note'],
    ['rust', 'fn main() { let x = 1; }'],
    ['sql', 'SELECT a FROM t -- c\nWHERE b = 1'],
    ['diff', '--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new'],
    ['css', 'a { color: "red"; } /* c */'],
  ];
  for (const [lang, code] of samples) {
    assert.equal(joined(highlight(code, lang)), code, `lossless for ${lang}`);
  }
});

test('python comments, strings, numbers and keywords are classified', () => {
  const code = '# note\ndef f():\n    return "hi" + 42';
  const tokens = highlight(code, 'python');
  assert.deepEqual(textsOf(tokens, 'comment'), ['# note']);
  assert.deepEqual(textsOf(tokens, 'keyword'), ['def', 'return']);
  assert.deepEqual(textsOf(tokens, 'string'), ['"hi"']);
  assert.deepEqual(textsOf(tokens, 'number'), ['42']);
  assert.equal(joined(tokens), code);
});

test('a language keyword prefix inside an identifier is not a keyword', () => {
  const tokens = highlight('returns = 1', 'python');
  assert.deepEqual(textsOf(tokens, 'keyword'), []);
  assert.equal(joined(tokens), 'returns = 1');
});

test('javascript block comments span multiple lines', () => {
  const code = '/* a\nb */ const x = 1';
  const tokens = highlight(code, 'javascript');
  assert.deepEqual(textsOf(tokens, 'comment'), ['/* a\nb */']);
  assert.equal(joined(tokens), code);
});

test('a hash inside a string is not a comment', () => {
  const tokens = highlight('x = "a # b"', 'python');
  assert.deepEqual(textsOf(tokens, 'comment'), []);
  assert.deepEqual(textsOf(tokens, 'string'), ['"a # b"']);
});

test('an unterminated string stops at the end of its line', () => {
  const code = 'x = "open\nprint(1)';
  const tokens = highlight(code, 'python');
  assert.deepEqual(textsOf(tokens, 'string'), ['"open']);
  assert.equal(joined(tokens), code);
});

test('json keywords and numbers are classified', () => {
  const code = '{"a": 1.5, "b": true, "c": null}';
  const tokens = highlight(code, 'json');
  assert.deepEqual(textsOf(tokens, 'keyword'), ['true', 'null']);
  assert.deepEqual(textsOf(tokens, 'number'), ['1.5']);
  assert.equal(joined(tokens), code);
});

test('diff lines are classified by their marker', () => {
  const code = '--- a/f\n+++ b/f\n@@ -1 +1 @@\n-old\n+new\n context';
  const tokens = highlight(code, 'diff');
  assert.deepEqual(textsOf(tokens, 'added'), ['+new\n']);
  assert.deepEqual(textsOf(tokens, 'removed'), ['-old\n']);
  assert.deepEqual(textsOf(tokens, 'meta'), ['--- a/f\n', '+++ b/f\n', '@@ -1 +1 @@\n']);
  assert.equal(joined(tokens), code);
});

test('highlighting never throws and is lossless on pathological input', () => {
  const samples = ['', '\n', '"', '/*', '`', '#', '0x', '"""', '~~~'];
  for (const sample of samples) {
    for (const lang of ['python', 'javascript', 'json', 'diff', 'rust']) {
      const tokens = highlight(sample, lang);
      assert.equal(joined(tokens), sample, `lossless for ${lang} / ${JSON.stringify(sample)}`);
    }
  }
});
