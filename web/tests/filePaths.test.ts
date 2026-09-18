/**
 * Offline tests for transcript file-path recognition and normalisation.  Pure
 * functions only: no DOM, no host, no socket.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  looksLikeFileRef,
  splitFileRefs,
  stripLineColumn,
  toWorkspacePath,
} from '../src/markdown/filePaths.ts';

function files(text: string): string[] {
  return splitFileRefs(text)
    .filter((part) => part.type === 'file')
    .map((part) => part.text);
}

function joined(text: string): string {
  return splitFileRefs(text)
    .map((part) => part.text)
    .join('');
}

test('a Windows absolute path is a file reference', () => {
  const text = '截图：F:\\project\\agent\\synapse\\.tmp\\baidu-hot\\baidu.png 完成';
  assert.deepEqual(files(text), ['F:\\project\\agent\\synapse\\.tmp\\baidu-hot\\baidu.png']);
});

test('a relative path with a separator is a file reference', () => {
  assert.deepEqual(files('打开 web/src/components/Markdown.tsx 看看'), [
    'web/src/components/Markdown.tsx',
  ]);
  assert.deepEqual(files('见 .tmp/baidu-hot/baidu.png'), ['.tmp/baidu-hot/baidu.png']);
});

test('a bare file name with a known extension is a file reference', () => {
  assert.deepEqual(files('修改 agent.py 和 Markdown.tsx'), ['agent.py', 'Markdown.tsx']);
  assert.deepEqual(files('读取 README.md'), ['README.md']);
});

test('prose that only looks like a path stays text', () => {
  assert.deepEqual(files('e.g. this, i.e. that, version 1.2.3, v1.2, example.com, 12:30'), []);
  assert.deepEqual(files('a/b and and/or are not files'), []);
});

test('a URL is never a file reference', () => {
  assert.deepEqual(files('https://example.com/a/b.py'), []);
});

test('trailing sentence punctuation stays text', () => {
  assert.deepEqual(files('见 src/app.py.'), ['src/app.py']);
  assert.equal(joined('见 src/app.py.'), '见 src/app.py.');
});

test('a :line:col suffix is kept on the reference', () => {
  assert.deepEqual(files('src/app.py:12:5'), ['src/app.py:12:5']);
  assert.deepEqual(files('web/src/App.tsx:7'), ['web/src/App.tsx:7']);
});

test('splitting never loses or reorders characters', () => {
  const text = '先 F:\\a\\b.png 后 web/x.ts:3 再 agent.py 结束';
  assert.equal(joined(text), text);
});

test('stripLineColumn leaves a Windows drive letter intact', () => {
  assert.equal(stripLineColumn('C:\\dir\\file.py'), 'C:\\dir\\file.py');
  assert.equal(stripLineColumn('C:\\dir\\file.py:10'), 'C:\\dir\\file.py');
  assert.equal(stripLineColumn('src/app.py:12'), 'src/app.py');
});

test('looksLikeFileRef accepts paths and rejects prose', () => {
  assert.equal(looksLikeFileRef('web/src/App.tsx'), true);
  assert.equal(looksLikeFileRef('agent.py'), true);
  assert.equal(looksLikeFileRef('/etc/hosts.conf'), true);
  assert.equal(looksLikeFileRef('e.g'), false);
  assert.equal(looksLikeFileRef('1.2.3'), false);
  assert.equal(looksLikeFileRef('example.com'), false);
});

test('inline-code contents decide like any other token', () => {
  // Paths the model wrapped in backticks (a table of build artifacts).
  assert.equal(looksLikeFileRef('web/dist/index.html'), true);
  assert.equal(looksLikeFileRef('web/dist/assets/index-DhuwA1zF.js'), true);
  // Real code that only resembles a path stays code.
  assert.equal(looksLikeFileRef('renderSpans(first.spans, itemKey)'), false);
  assert.equal(looksLikeFileRef("block.type === 'table'"), false);
  assert.equal(looksLikeFileRef('2388 modules transformed'), false);
});

test('toWorkspacePath maps a Windows path under the root', () => {
  const root = 'F:\\project\\agent\\autoagents\\synapse';
  assert.equal(
    toWorkspacePath('F:\\project\\agent\\autoagents\\synapse\\.tmp\\baidu-hot\\baidu.png', root),
    '.tmp/baidu-hot/baidu.png',
  );
  // Case-insensitive drive/root on Windows.
  assert.equal(toWorkspacePath('f:\\PROJECT\\agent\\autoagents\\synapse\\x.py', root), 'x.py');
});

test('toWorkspacePath treats a leading slash as the workspace root', () => {
  assert.equal(toWorkspacePath('/web/src/App.tsx', 'F:\\proj'), 'web/src/App.tsx');
  assert.equal(toWorkspacePath('web/src/App.tsx', 'F:\\proj'), 'web/src/App.tsx');
});

test('toWorkspacePath drops the line/column suffix', () => {
  assert.equal(toWorkspacePath('src/app.py:12:5', ''), 'src/app.py');
});

test('toWorkspacePath refuses what it cannot map', () => {
  assert.equal(toWorkspacePath('', ''), null);
  assert.equal(toWorkspacePath('../secret.txt', ''), null);
  assert.equal(toWorkspacePath('F:\\proj', 'F:\\proj'), null);
  assert.equal(toWorkspacePath('.', ''), null);
});
