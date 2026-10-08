import assert from 'node:assert/strict';
import { test } from 'node:test';
import { highlight } from '../src/markdown/highlight.ts';
import { runCodeFailureReason, runCodeInput, runCodeOutput } from '../src/stores/transcriptLabels.ts';
import { rowSource } from './helpers/transcriptSource.ts';

test('run_code history input preserves the exact Python body', () => {
  const code = '\n  print("hello")  \n';
  assert.equal(runCodeInput({ args: { code } }), code);
  assert.equal(runCodeInput({ args: { code: '' }, argsPreview: "{'code': 'old'}" }), '');
  assert.equal(runCodeInput({ args: { command: 'not Python input' } }), '');
  assert.equal(runCodeInput({}), '');
});

test('run_code live input decodes repr escapes without stripping whitespace', () => {
  const preview = "{'code': 'await tools.read_file(file_path=\"/a.py\")\\nreturn 1  \\n', 'intent': 'read'}";
  assert.equal(runCodeInput({ argsPreview: preview }), 'await tools.read_file(file_path="/a.py")\nreturn 1  \n');
  // Old projections can have only a summary: use any live preview still available.
  assert.equal(runCodeInput({ args: { summary: '…' }, argsPreview: "{'code': 'return 2'}" }), 'return 2');
  assert.equal(runCodeInput({ argsPreview: "{'code': 'return 3" }), 'return 3');
});

test('live code decodes Python hex and Unicode escapes, keeping incomplete ones', () => {
  assert.equal(runCodeInput({ argsPreview: "{'code': 'a\\x1b\\u00a0\\U0001f600b'}" }), 'a\x1b\u00a0\u{1f600}b');
  assert.equal(runCodeInput({ argsPreview: "{'code': 'a\\u00" }), 'a\\u00');
});

test('a complete error envelope reports failure independently of the legacy status', () => {
  assert.equal(runCodeFailureReason('{"logs":[],"value":null,"error":{"kind":"timeout","message":"expired"}}'), 'timeout: expired');
  assert.equal(runCodeFailureReason('{"error":{"message":"bad input"}}'), 'bad input');
  assert.equal(runCodeFailureReason('{"error":{}}'), 'run_code 执行失败');
  for (const preview of [null, '{"value":1}', '{"error":null}', '{"error":', 'Permission denied']) {
    assert.equal(runCodeFailureReason(preview), '');
  }
});

test('run_code output pretty-prints logs, value, errors and additional fields', () => {
  const envelope = { logs: ['hello\nworld'], value: { count: 2 }, error: null, warning: 'notice' };
  assert.equal(runCodeOutput(JSON.stringify(envelope)), JSON.stringify(envelope, null, 2));
  const error = { logs: [], value: null, error: { kind: 'timeout', message: 'expired' } };
  assert.equal(runCodeOutput(JSON.stringify(error)), JSON.stringify(error, null, 2));
});

test('run_code truncated or non-JSON output stays visible verbatim', () => {
  for (const preview of ['{"logs":["partial', 'Permission denied', '  incomplete…  ']) {
    assert.equal(runCodeOutput(preview), preview);
  }
  assert.equal(runCodeOutput(null), '');
  assert.equal(runCodeOutput(undefined), '');
});

test('input Python and output JSON are tokenized without changing their text', () => {
  for (const [code, lang] of [
    ['result = await tools.call("read_file", {})\nreturn result', 'python'],
    [runCodeOutput('{"logs":["hello"],"value":null}'), 'json'],
  ]) {
    const tokens = highlight(code, lang);
    assert.equal(tokens.map((t) => t.text).join(''), code);
    assert.ok(tokens.some((t) => t.kind === 'keyword'));
    assert.ok(tokens.some((t) => t.kind === 'string'));
  }
});

test('the run_code card renders parent input and output independently of child calls', () => {
  const source = rowSource('ToolGroupRow');
  const card = source.slice(source.indexOf('const renderRunCodeCard'), source.indexOf('\n  return (', source.indexOf('const renderRunCodeCard')));
  assert.ok(card.includes('runCodeInput(node.parent)'));
  assert.ok(card.includes('runCodeOutput(node.parent.preview)'));
  assert.ok(card.includes('<CodeBlock lang="python" code={code} />'));
  assert.ok(card.includes('<CodeBlock lang="json" code={output} />'));
  assert.ok(card.includes('runCodeFailureReason(node.parent.preview)'));
  assert.ok(card.includes('const failureReason = resultFailure || toolFailureReason'));
  assert.ok(card.includes('{expanded && ('), 'large bodies mount only when expanded');
  assert.ok(card.indexOf('run_code 输入代码') < card.indexOf('node.tools.map'));
  assert.ok(card.indexOf('node.tools.map') < card.indexOf('run_code 执行输出'));
  assert.ok(card.includes('执行中，等待输出…'));
  assert.ok(card.includes('长输入或输出可能已截断'));
});
