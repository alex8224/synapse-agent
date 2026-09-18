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
  rejectionReason,
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
