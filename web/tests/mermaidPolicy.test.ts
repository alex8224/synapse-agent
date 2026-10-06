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
  DIAGRAM_EDGE_STROKE_WIDTH,
  DIAGRAM_PAGE_INK_DARK,
  DIRECTIVE_RE,
  MERMAID_THEME,
  MERMAID_THEME_VARIABLES,
  MAX_DIAGRAM_CHARS,
  diagramPaletteFor,
  describeMermaidError,
  rejectionReason,
} from '../src/markdown/mermaid.ts';

test('a dark document theme selects the dark diagram palette', () => {
  assert.equal(diagramPaletteFor('fluent-dark'), 'dark');
});

test('the light theme and the shipped fallback use the light diagram palette', () => {
  for (const theme of ['fluent-light', null, undefined]) {
    assert.equal(diagramPaletteFor(theme ?? null), 'light', String(theme));
  }
});

test('an unknown theme is treated as light, never as a dark guess', () => {
  for (const theme of ['', 'solarized', 'dark-ish', 'dark']) {
    // Only the shipped `*-dark` names opt into the dark palette; anything else
    // keeps the light one so a typo cannot darken the diagram by accident.
    assert.equal(diagramPaletteFor(theme), 'light', theme);
  }
});

test('both console themes draw with mermaid’s own default palette', () => {
  // The blocks are the same drawing in either theme; only the connectors differ.
  assert.equal(MERMAID_THEME, 'default');
});

test('the light theme is left exactly as mermaid ships it', () => {
  // Light was never the problem, and an empty table is also what clears the dark
  // overrides when the console switches back (mermaid merges the config it is
  // re-initialized with, so omitting the key would keep the dark values).
  assert.deepEqual(MERMAID_THEME_VARIABLES.light, {});
});

test('dark overrides only the roles drawn on the page, never a block', () => {
  // An exact list, because every entry is a claim that the role is painted on the
  // page rather than on a light block.  A fill in here (mainBkg, nodeBorder,
  // edgeLabelBackground, ...) is how the light theme got painted dark once.
  assert.deepEqual(Object.keys(MERMAID_THEME_VARIABLES.dark).sort(), [
    'arrowheadColor',
    'defaultLinkColor',
    'emArrowhead',
    'emRelationStroke',
    'lineColor',
    'loopTextColor',
    'relationColor',
    'signalColor',
    'signalTextColor',
    'specialStateColor',
    'strokeWidth',
    'taskTextOutsideColor',
    'transitionColor',
  ]);
});

test('every page-drawn role is light, because the default palette’s are near-black', () => {
  // The palette strokes its connectors and writes their labels in #333333 (and
  // black), which is invisible on the console's dark surfaces.
  for (const role of Object.keys(MERMAID_THEME_VARIABLES.dark)) {
    if (role === 'strokeWidth') continue;
    assert.equal(MERMAID_THEME_VARIABLES.dark[role], DIAGRAM_PAGE_INK_DARK, role);
  }
  assert.equal(DIAGRAM_PAGE_INK_DARK, 'lightgrey');
});

test('dark connectors are widened so they survive the downscale', () => {
  // mermaid's own themes set `strokeWidth: 1`, and the flowchart CSS reads
  // `stroke-width: ${strokeWidth ?? 2}px`.  The diagram is then scaled to fit the
  // column, so one unit anti-aliases away.
  assert.equal(MERMAID_THEME_VARIABLES.dark.strokeWidth, DIAGRAM_EDGE_STROKE_WIDTH);
  assert.equal(DIAGRAM_EDGE_STROKE_WIDTH > 1, true);
});

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
