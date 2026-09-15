/**
 * Offline tests for image-source classification.  Pure functions only: no DOM,
 * no client, no socket.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { classifyImageSrc, sanitizeImageSrc } from '../src/markdown/imageRefs.ts';

test('a relative or workspace path is local', () => {
  assert.equal(classifyImageSrc('token_report.png'), 'local');
  assert.equal(classifyImageSrc('.tmp/token_report.png'), 'local');
  assert.equal(classifyImageSrc('docs/assets/a.svg'), 'local');
  assert.equal(classifyImageSrc('/docs/assets/a.png'), 'local');
  assert.equal(classifyImageSrc('sub dir/a b.png'), 'local');
});

test('a windows drive path is local, not a URL scheme', () => {
  assert.equal(classifyImageSrc('F:\\project\\a.png'), 'local');
  assert.equal(classifyImageSrc('C:/tmp/a.png'), 'local');
  assert.equal(classifyImageSrc('c:/tmp/a.png'), 'local');
});

test('http and https are remote', () => {
  assert.equal(classifyImageSrc('https://example.com/a.png'), 'remote');
  assert.equal(classifyImageSrc('HTTP://example.com/a.png'), 'remote');
});

test('a data payload is inline', () => {
  assert.equal(classifyImageSrc('data:image/png;base64,AAAA'), 'inline');
  assert.equal(classifyImageSrc('DATA:image/svg+xml,<svg/>'), 'inline');
});

test('script-bearing and scheme-relative sources are invalid', () => {
  assert.equal(classifyImageSrc('javascript:alert(1)'), 'invalid');
  assert.equal(classifyImageSrc('file:///C:/a.png'), 'invalid');
  assert.equal(classifyImageSrc('blob:https://example.com/x'), 'invalid');
  assert.equal(classifyImageSrc('//example.com/a.png'), 'invalid');
  assert.equal(classifyImageSrc('   '), 'invalid');
});

test('sanitizeImageSrc keeps local and remote sources and drops the rest', () => {
  assert.equal(sanitizeImageSrc('.tmp/token_report.png'), '.tmp/token_report.png');
  assert.equal(sanitizeImageSrc('  https://example.com/a.png  '), 'https://example.com/a.png');
  assert.equal(sanitizeImageSrc('data:image/png;base64,AAAA'), null);
  assert.equal(sanitizeImageSrc('javascript:alert(1)'), null);
  assert.equal(sanitizeImageSrc(''), null);
});
