/**
 * Offline tests for the rules that decide whether a `mermaid` fence is drawn.
 *
 * The refusal reasons are user-visible, and each one exists for a concrete
 * reason (size, the CSS-bearing directive channel), so they are pinned here
 * rather than only asserted as a regex in the component.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  DIRECTIVE_RE,
  MAX_DIAGRAM_CHARS,
  describeMermaidError,
  freezeSvgSize,
  rejectionReason,
  svgIntrinsicSize,
} from '../src/markdown/mermaid.ts';

test('an ordinary diagram is accepted', () => {
  assert.equal(rejectionReason('sequenceDiagram\n  A->>B: hi'), null);
});

test('an empty fence is refused with a visible reason', () => {
  assert.equal(rejectionReason(''), '图形定义为空');
});

test('an oversized diagram is refused before mermaid sees it', () => {
  const reason = rejectionReason('graph TD\n'.repeat(MAX_DIAGRAM_CHARS));
  assert.equal(reason, `图形定义超过 ${MAX_DIAGRAM_CHARS} 字符`);
});

test('a diagram carrying a directive is refused', () => {
  for (const source of [
    "%%{init: {'theme': 'dark'}}%%\ngraph TD\n A-->B",
    "%%{config: {'themeCSS': 'body{background:url(http://x)}'}}%%\ngraph TD\n A-->B",
    '%% { init: { } }%%',
  ]) {
    assert.ok(DIRECTIVE_RE.test(source), source);
    assert.equal(rejectionReason(source), '图形含 mermaid 指令（%%{...}%%），不渲染');
  }
});

test('a comment that merely starts with %% is not a directive', () => {
  assert.equal(rejectionReason('graph TD\n  %% a comment\n  A-->B'), null);
});

test('YAML frontmatter is refused (the second config channel)', () => {
  // mermaid merges frontmatter into the same config object as `%%{...}%%`
  // directives, so `themeCSS` reaches the SVG <style> through either one.
  for (const source of [
    '---\nconfig:\n  themeCSS: ".a{fill:red}"\n---\nflowchart TD\n A-->B',
    '---\ntitle: hi\n---\nflowchart TD\n A-->B',
    '  ---\nconfig:\n  theme: dark\n---\nflowchart TD\n A-->B',
  ]) {
    assert.equal(rejectionReason(source), '图形含 YAML frontmatter 配置（---），不渲染', source);
  }
});

test('a rule or a dashed edge is not mistaken for frontmatter', () => {
  assert.equal(rejectionReason('flowchart TD\n    A-->B\n    A --- B'), null);
  assert.equal(rejectionReason('graph TD\n    A --> B'), null);
});

test('a multi-line mermaid error is reduced to one capped line', () => {
  const error = new Error('\n  Parse error on line 2: ' + 'x'.repeat(400) + '\n  ...expecting X...\n');
  const described = describeMermaidError(error);
  assert.equal(described.startsWith('Parse error on line 2:'), true);
  assert.equal(described.includes('\n'), false);
  assert.equal(described.length, 161);
});

test('a short multi-line error keeps its first non-empty line verbatim', () => {
  const described = describeMermaidError(new Error('\n  Parse error on line 2:\n  ...expecting X...\n'));
  assert.equal(described, 'Parse error on line 2:');
});

test('a non-Error failure still produces a readable reason', () => {
  assert.equal(describeMermaidError('boom'), 'boom');
  assert.equal(describeMermaidError(undefined), '未知错误');
  assert.equal(describeMermaidError(new Error('   ')), '未知错误');
});

/**
 * What mermaid actually emits for a 1600x900 flowchart: `useMaxWidth` gives the
 * root `width="100%"` plus an inline `max-width`, `setupGraphViewbox` adds the
 * `viewBox`, and the theme CSS is scoped by the id it was rendered under.
 */
const RENDERED = [
  '<svg id="synapse-mermaid-7" width="100%" xmlns="http://www.w3.org/2000/svg"',
  ' style="max-width: 1600px;" class="flowchart" viewBox="0 0 1600 900"',
  ' role="graphics-document document">',
  '<style>#synapse-mermaid-7 .node{fill:#fff}</style>',
  '<g class="node" stroke-width="2"><rect x="0" y="0" width="10" height="20"/></g>',
  '</svg>',
].join('');

test('a diagram reports the size mermaid measured it at', () => {
  assert.deepEqual(svgIntrinsicSize(RENDERED), { width: 1600, height: 900 });
});

test('the size falls back to the width/height attributes', () => {
  assert.deepEqual(
    svgIntrinsicSize('<svg width="320" height="180" style="max-width: 320px;"><g/></svg>'),
    { width: 320, height: 180 },
  );
});

test('a percentage or a missing attribute is not a size', () => {
  // `width="100%"` must never be read as 100 user units.
  assert.equal(svgIntrinsicSize('<svg width="100%" height="900"><g/></svg>'), null);
  assert.equal(svgIntrinsicSize('<svg class="flowchart"><g/></svg>'), null);
  assert.equal(svgIntrinsicSize('<g/>'), null);
  assert.equal(svgIntrinsicSize('<svg viewBox="0 0 0 0"><g/></svg>'), null);
});

test('freezing replaces mermaid sizing with the diagram own size', () => {
  const frozen = freezeSvgSize(RENDERED);
  assert.deepEqual(frozen.size, { width: 1600, height: 900 });
  const tag = /<svg\b[^>]*>/i.exec(frozen.html)?.[0] ?? '';
  assert.ok(tag.includes('width="1600"'), tag);
  assert.ok(tag.includes('height="900"'), tag);
  assert.equal(tag.includes('width="100%"'), false, 'the container-width attribute must go');
  assert.equal(/max-width/.test(tag), false, 'the inline max-width must go');
  // Everything else on the root survives, and the body is untouched.
  assert.ok(tag.includes('id="synapse-mermaid-7"'));
  assert.ok(tag.includes('viewBox="0 0 1600 900"'));
  assert.ok(tag.includes('role="graphics-document document"'));
  assert.ok(frozen.html.includes('stroke-width="2"'), 'a body attribute must not be stripped');
  assert.ok(frozen.html.includes('<rect x="0" y="0" width="10" height="20"/>'));
  assert.ok(frozen.html.includes('#synapse-mermaid-7 .node{fill:#fff}'));
});

test('freezing keeps the style declarations it did not come for', () => {
  const frozen = freezeSvgSize(
    '<svg width="100%" style="max-width: 200px; overflow: visible;" viewBox="0 0 200 100"/>',
  );
  const tag = /<svg\b[^>]*>/i.exec(frozen.html)?.[0] ?? '';
  assert.ok(tag.includes('style="overflow: visible"'), tag);
});

test('freezing drops an empty style attribute', () => {
  const frozen = freezeSvgSize('<svg width="100%" style="max-width: 200px;" viewBox="0 0 200 100"/>');
  assert.equal(/style=/.test(frozen.html), false, frozen.html);
});

test('an unmeasurable diagram is handed back untouched', () => {
  const markup = '<svg width="100%"><g/></svg>';
  assert.deepEqual(freezeSvgSize(markup), { html: markup, size: null });
});

