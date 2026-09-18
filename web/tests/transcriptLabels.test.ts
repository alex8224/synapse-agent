/**
 * Offline tests for the transcript display labels (design-spec wording).
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  expandHint,
  formatToolArgs,
  groupToolsForView,
  isTerminalTool,
  thoughtIcon,
  thoughtLabel,
  toolCommand,
  toolFailureReason,
  toolPreviewLanguage,
  toolStatusLabel,
} from '../src/stores/transcriptLabels.ts';
import type { ToolItemView } from '../src/stores/historyMapper.ts';

test('toolStatusLabel maps the runtime statuses to the console vocabulary', () => {
  assert.equal(toolStatusLabel('running'), '运行中');
  assert.equal(toolStatusLabel('pending'), '等待');
  assert.equal(toolStatusLabel('completed'), '完成');
  assert.equal(toolStatusLabel('failed'), '失败');
  assert.equal(toolStatusLabel('error'), '错误');
  assert.equal(toolStatusLabel('cancelled'), '已取消');
  assert.equal(toolStatusLabel('canceled'), '已取消');
});

test('toolStatusLabel is case-insensitive and never hides an unknown status', () => {
  assert.equal(toolStatusLabel('RUNNING'), '运行中');
  assert.equal(toolStatusLabel('weird_state'), 'weird_state');
  assert.equal(toolStatusLabel(''), '');
});

test('thoughtLabel distinguishes streaming, completed and projected rows', () => {
  assert.equal(thoughtLabel('streaming'), 'Thinking...');
  assert.equal(thoughtLabel('0.1s'), 'Thought for 0.1s');
  assert.equal(thoughtLabel('2.9s'), 'Thought for 2.9s');
  assert.equal(thoughtLabel('done'), 'Thought');
  assert.equal(thoughtLabel(undefined), 'Thought');
  assert.equal(thoughtLabel(''), 'Thought');
});

test('expandHint reflects the collapsed state', () => {
  assert.equal(expandHint(true), '(收起)');
  assert.equal(expandHint(false), '(展开)');
});

test('a reasoning row carries a thinking glyph, wired while it runs', () => {
  assert.equal(thoughtIcon(false), 'psychology');
  assert.equal(thoughtIcon(true), 'neurology');
});

test('toolCommand reads the invocation out of a history row\'s own args', () => {
  assert.equal(toolCommand({ args: { command: 'pytest -q' } }), 'pytest -q');
  assert.equal(toolCommand({ args: { cmd: 'ls -la' } }), 'ls -la');
  assert.equal(toolCommand({ args: { script: 'echo hi' } }), 'echo hi');
  // `command` wins over the other keys, and the value is trimmed.
  assert.equal(toolCommand({ args: { script: 'echo hi', command: '  npm test  ' } }), 'npm test');
  // A call with no invocation prints nothing rather than an empty prompt.
  assert.equal(toolCommand({ args: { file_path: '/a.ts' } }), '');
  assert.equal(toolCommand({ args: { command: 42 } }), '');
  assert.equal(toolCommand({ args: {} }), '');
  assert.equal(toolCommand({ args: null }), '');
  assert.equal(toolCommand({}), '');
});

test('toolCommand reads the invocation back out of a live args_preview repr', () => {
  // A live batch event carries only `repr(args)`, so the command has to be read
  // back out of it -- including the escapes Python wrote.
  assert.equal(
    toolCommand({ argsPreview: "{'command': 'cd /f/x && git status', 'timeout_s': 30}" }),
    'cd /f/x && git status',
  );
  assert.equal(toolCommand({ argsPreview: "{'cmd': 'ls -la'}" }), 'ls -la');
  // Python switches to double quotes for a value that contains a single quote.
  assert.equal(
    toolCommand({ argsPreview: "{'command': \"echo 'x'\"}" }),
    "echo 'x'",
  );
  assert.equal(toolCommand({ argsPreview: "{'intent': 'run tests', 'command': 'pytest -q'}" }), 'pytest -q');
  assert.equal(
    toolCommand({ argsPreview: "{'command': 'echo \\'quoted\\' && ls'}" }),
    "echo 'quoted' && ls",
  );
  assert.equal(toolCommand({ argsPreview: "{'command': 'a\\nb'}" }), 'a\nb');
  // A repr without the key, a non-string value, or nothing at all yields no prompt.
  assert.equal(toolCommand({ argsPreview: "{'file_path': '/a.ts'}" }), '');
  assert.equal(toolCommand({ argsPreview: "{'command': 42}" }), '');
  assert.equal(toolCommand({ argsPreview: null }), '');
  assert.equal(toolCommand({ argsPreview: 'not a repr' }), '');
});

test('toolFailureReason keeps a failure reason for the detail and nothing else', () => {
  // The runtime's status for a failure is the error's own first line.
  assert.equal(
    toolFailureReason('ENOENT: no such file or directory', true),
    'ENOENT: no such file or directory',
  );
  assert.equal(toolFailureReason('error: command not found', true), 'error: command not found');
  assert.equal(toolFailureReason('  timeout after 30s  ', true), 'timeout after 30s');
  // A success digest is not a reason, and neither is a state word.
  assert.equal(toolFailureReason('ok (48 chars, 2 lines)', true), '');
  assert.equal(toolFailureReason('ok', true), '');
  assert.equal(toolFailureReason('failed', true), '');
  assert.equal(toolFailureReason('FAILED', true), '');
  assert.equal(toolFailureReason('error', true), '');
  assert.equal(toolFailureReason('cancelled', true), '');
  assert.equal(toolFailureReason('', true), '');
  // Only a failed call has a reason at all.
  assert.equal(toolFailureReason('ok (48 chars, 2 lines)', false), '');
  assert.equal(toolFailureReason('ENOENT: no such file or directory', false), '');
});

test('formatToolArgs prints the call arguments as one bounded line', () => {
  assert.equal(
    formatToolArgs({ command: 'pytest -q', timeout_s: 30 }),
    'command=pytest -q · timeout_s=30',
  );
  // `intent` is the row's own label, so it must not be printed twice.
  assert.equal(formatToolArgs({ intent: 'run checks', command: 'ls' }), 'command=ls');
  assert.equal(formatToolArgs({ label: 'Run', command: 'ls' }), 'command=ls');
  // A multi-line command collapses to one line.
  assert.equal(formatToolArgs({ command: 'a\n  b\tc' }), 'command=a b c');
  // Non-string values are stringified, and nothing is invented for a missing one.
  assert.equal(formatToolArgs({ pattern: 'x', all: true, n: null }), 'pattern=x · all=true');
  assert.equal(formatToolArgs({}), '');
  assert.equal(formatToolArgs(null), '');
  assert.equal(formatToolArgs('not an object'), '');
  assert.equal(formatToolArgs([1, 2]), '');
  assert.equal(formatToolArgs(undefined), '');
});

test('formatToolArgs bounds the value, the key count and the whole line', () => {
  const long = 'x'.repeat(500);
  const value = formatToolArgs({ command: long });
  assert.equal(value, 'command=' + 'x'.repeat(159) + '…', 'one value must stay bounded');

  const many = formatToolArgs({ a: 1, b: 2, c: 3, d: 4, e: 5 });
  assert.equal(many, 'a=1 · b=2 · c=3 · d=4');
  assert.equal(formatToolArgs({ a: 1, b: 2 }, 1), 'a=1');
  assert.equal(formatToolArgs({ a: 1 }, 0), '');

  const wide = formatToolArgs({ a: long, b: long, c: long });
  assert.ok(wide.length <= 400, 'the finished line must stay bounded');
});

/** One tool item of a batch, with the fields a projection would carry. */
const tool = (name: string, extra: Partial<ToolItemView> = {}): ToolItemView => ({
  id: name,
  callId: null,
  name,
  label: name,
  category: 'other',
  path: null,
  status: 'completed',
  preview: null,
  error: false,
  sub: false,
  parentId: null,
  subagentStatus: null,
  subagentName: null,
  icon: 'build',
  ...extra,
});

/** The one node a single-item batch must produce. */
function onlyNode(tools: ToolItemView[]) {
  const nodes = groupToolsForView(tools);
  assert.equal(nodes.length, 1, 'the batch must fold into one node');
  return nodes[0];
}

test('plain tool calls stay flat rows', () => {
  const nodes = groupToolsForView([tool('read_file'), tool('edit_file')]);
  assert.deepEqual(nodes.map((n) => n.type), ['single', 'single']);
  assert.equal(nodes[0].type === 'single' ? nodes[0].tool.name : '', 'read_file');
  assert.equal(nodes[1].type === 'single' ? nodes[1].tool.name : '', 'edit_file');
});

test('a task call opens a subagent group, named and goal-ed from its arguments', () => {
  const node = onlyNode([
    tool('task', { id: 'c1', callId: 'call-1', args: { subagent_type: 'researcher', intent: 'survey the repo' } }),
  ]);
  assert.equal(node.type, 'subagent');
  assert.ok(node.type === 'subagent');
  assert.equal(node.subagentName, 'researcher');
  assert.equal(node.subagentGoal, 'survey the repo');
  assert.equal(node.parent.name, 'task');
  assert.deepEqual(node.tools, [], 'a task with no steps yet carries none');
});

test('sub-tools land inside the task that started them, by item id or call id', () => {
  const nodes = groupToolsForView([
    tool('task', { id: 'c1', callId: 'call-1', args: { subagent_type: 'researcher' } }),
    tool('read_file', { id: 'c1-1', sub: true, parentId: 'c1' }),
    tool('grep', { id: 'c1-2', sub: true, parentId: 'call-1' }),
    tool('edit_file', { id: 'm1' }),
  ]);
  assert.deepEqual(nodes.map((n) => n.type), ['subagent', 'single']);
  const group = nodes[0];
  assert.ok(group.type === 'subagent');
  assert.deepEqual(group.tools.map((t) => t.name), ['read_file', 'grep']);
  // A main-agent call after the group stays its own row, and closes the group.
  assert.equal(nodes[1].type === 'single' ? nodes[1].tool.name : '', 'edit_file');
});

test('a history row that names its subagent opens a group, and its steps join it', () => {
  // The projection drops `parent_id`, so the open group is what a nested row
  // hangs off.
  const nodes = groupToolsForView([
    tool('task', { id: 'h1', subagentName: 'planner', label: 'plan the work' }),
    tool('read_file', { id: 'h2', sub: true }),
  ]);
  assert.deepEqual(nodes.map((n) => n.type), ['subagent']);
  const group = nodes[0];
  assert.ok(group.type === 'subagent');
  assert.equal(group.subagentName, 'planner');
  assert.equal(group.subagentGoal, 'plan the work');
  assert.deepEqual(group.tools.map((t) => t.name), ['read_file']);
});

test('a subagent group is named and goal-ed from the best field it has', () => {
  const named = onlyNode([tool('task', { subagentName: 'planner' })]);
  assert.ok(named.type === 'subagent');
  assert.equal(named.subagentName, 'planner', 'the row\'s own name wins');

  const described = onlyNode([tool('task', { args: { description: 'find the flaky test' } })]);
  assert.ok(described.type === 'subagent');
  assert.equal(described.subagentName, 'subagent', 'an unnamed task still reads as a subagent');
  assert.equal(described.subagentGoal, 'find the flaky test');

  const bare = onlyNode([tool('task')]);
  assert.ok(bare.type === 'subagent');
  assert.equal(bare.subagentGoal, '子代理任务', 'a goal-less task keeps a placeholder');
});

test('a nested row with no subagent to hang off stays a plain row', () => {
  const nodes = groupToolsForView([tool('read_file', { sub: true, parentId: 'missing' })]);
  assert.deepEqual(nodes.map((n) => n.type), ['single']);
  // An unattributed nested row after a main-agent row does not re-open the group.
  const after = groupToolsForView([
    tool('task', { id: 'c1' }),
    tool('edit_file', { id: 'm1' }),
    tool('read_file', { id: 's1', sub: true }),
  ]);
  assert.deepEqual(after.map((n) => n.type), ['subagent', 'single', 'single']);
});

test('a nested row that names its subagent is a step, never a new group', () => {
  const nodes = groupToolsForView([
    tool('task', { id: 'c1', args: { subagent_type: 'researcher' } }),
    tool('read_file', { id: 's1', sub: true, parentId: 'c1', subagentName: 'researcher' }),
  ]);
  assert.deepEqual(nodes.map((n) => n.type), ['subagent']);
  const group = nodes[0];
  assert.ok(group.type === 'subagent');
  assert.deepEqual(group.tools.map((t) => t.name), ['read_file']);
});

test('a file-content tool body takes the language of its path', () => {
  assert.equal(toolPreviewLanguage('read_file', 'src/app.py', 'print(1)'), 'python');
  assert.equal(toolPreviewLanguage('read', 'web/src/App.tsx', 'const a = 1;'), 'typescript');
  assert.equal(toolPreviewLanguage('edit_file', 'rust/core/src/lib.rs', 'fn main() {}'), 'rust');
  assert.equal(toolPreviewLanguage('write_file', 'a/b.yaml', 'k: v'), 'yaml');
  assert.equal(toolPreviewLanguage('patch', 'a/b.mjs', 'export const a = 1;'), 'javascript');
  assert.equal(toolPreviewLanguage('read_file', 'a/b.jsonl', '{}'), 'json');
});

test('a tool body is only highlighted when the console can tokenize it', () => {
  // An unknown extension, an unknown tool and a missing body all stay plain.
  assert.equal(toolPreviewLanguage('read_file', 'a/b.kt', 'val x = 1'), '');
  assert.equal(toolPreviewLanguage('read_file', 'notes.txt', 'plain text'), '');
  assert.equal(toolPreviewLanguage('read_file', 'Makefile', 'all:'), '');
  assert.equal(toolPreviewLanguage('execute', 'a/b.py', 'print(1)'), '');
  assert.equal(toolPreviewLanguage('search_files', 'a/b.py', 'a/b.py:1: hit'), '');
  assert.equal(toolPreviewLanguage('read_file', 'a/b.py', ''), '');
  assert.equal(toolPreviewLanguage('read_file', null, 'print(1)'), '');
});

test('an edit result highlights as a diff whatever the file is', () => {
  const patch = '--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-old\n+new';
  assert.equal(toolPreviewLanguage('edit_file', 'src/app.py', patch), 'diff');
  assert.equal(toolPreviewLanguage('patch', 'unknown.ext', patch), 'diff');
  // A body that merely starts with a rule is not a patch.
  assert.equal(toolPreviewLanguage('read_file', 'a/b.css', '--- x\nbody {}'), 'css');
  assert.equal(toolPreviewLanguage('read_file', 'a/b.py', 'x = 1  # @@ marker'), 'python');
});

test('only a program-running tool gets the terminal renderer', () => {
  assert.equal(isTerminalTool('execute'), true);
  assert.equal(isTerminalTool('RUN'), true);
  assert.equal(isTerminalTool('bash'), true);
  // A file body and a search result are not terminal output.
  assert.equal(isTerminalTool('read_file'), false);
  assert.equal(isTerminalTool('search_files'), false);
  assert.equal(isTerminalTool(''), false);
});
