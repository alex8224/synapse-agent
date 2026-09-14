/**
 * Offline tests for the KaTeX wrapper the transcript renders formulas with.
 *
 * `renderToString` needs no DOM, so the wrapper is testable under `node --test`;
 * what matters here is the untrusted-input contract: TeX is model output, so a
 * formula must never be able to emit a link, an inline style or a throw that
 * would break the surrounding transcript.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { renderTex } from '../src/markdown/tex.ts';

/**
 * KaTeX echoes the raw TeX inside a MathML `<annotation>` (that is what makes
 * the formula copyable and screen-reader-friendly), so security assertions have
 * to look at the *rendered* markup: the annotation would otherwise match any
 * substring of the source.
 */
function renderedHtml(html: string): string {
  return html.replace(/<annotation[\s\S]*?<\/annotation>/g, '');
}

test('a valid formula typesets and is not reported as failed', () => {
  const result = renderTex('x^2 + y^2', false);
  assert.equal(result.failed, false);
  assert.ok(result.html.includes('class="katex"'));
});

test('display mode asks KaTeX for the block layout', () => {
  const result = renderTex('\\frac{1}{2}', true);
  assert.equal(result.failed, false);
  assert.ok(result.html.includes('katex-display'));
});

test('an unparsable formula degrades to the visible KaTeX error node', () => {
  const result = renderTex('\\badcommand{', false);
  assert.equal(result.failed, true);
  assert.ok(result.html.includes('class="katex-error"'));
});

test('\\href cannot produce a link (trust is off)', () => {
  const result = renderTex('\\href{javascript:alert(1)}{x}', false);
  assert.equal(result.html.includes('<a '), false);
  assert.equal(result.html.includes('href='), false);
});

test('\\htmlStyle cannot inject a style attribute (trust is off)', () => {
  const result = renderTex('\\htmlStyle{background:url(http://example.invalid/x)}{y}', false);
  assert.equal(renderedHtml(result.html).includes('url('), false);
});

test('a macro bomb stays inside the expansion budget', () => {
  // `maxExpand` caps expansion, so this returns the error node instead of
  // hanging the tab; either way it must not throw.
  const result = renderTex('\\def\\a{\\a}\\a', false);
  assert.equal(typeof result.html, 'string');
});

test('the raw source is preserved for the copyable fallback', () => {
  const tex = '\\sum_{i=1}^{n} i';
  const result = renderTex(tex, true);
  assert.ok(result.html.includes('annotation encoding="application/x-tex"'));
  assert.ok(result.html.includes('\\sum_{i=1}^{n} i'));
});
