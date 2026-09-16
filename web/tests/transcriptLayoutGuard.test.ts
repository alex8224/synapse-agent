/**
 * Source guards for the transcript's chat layout.
 *
 * The side a turn is on *is* the role, so neither side carries a "User" /
 * "Assistant" heading, and every turn is framed the same way.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));
const transcript = readFileSync(
  join(here, '..', 'src', 'components', 'Transcript.tsx'),
  'utf8',
);
const foldRules = readFileSync(
  join(here, '..', 'src', 'stores', 'turnWork.ts'),
  'utf8',
);
const terminalOutput = readFileSync(
  join(here, '..', 'src', 'components', 'TerminalOutput.tsx'),
  'utf8',
);

test('the user turn sits on the right, the assistant turn on the left', () => {
  assert.ok(
    transcript.includes('className="flex justify-end"'),
    'the user turn must be pushed to the right',
  );
  assert.ok(
    transcript.includes('flex max-w-[80%] flex-col items-end'),
    'the user content must hug the right edge',
  );
  assert.ok(
    transcript.includes('flex max-w-[80%] flex-col items-start'),
    'the assistant turn must hug the left edge',
  );
});

test('a collapsed body is not reachable by keyboard', () => {
  const tools = transcript.slice(
    transcript.indexOf("if (m.type === 'tool_group')"),
    transcript.indexOf("if (m.type === 'assistant')"),
  );
  assert.ok(tools.includes('aria-hidden={!subExpanded}'), 'the subagent body must expose its visibility');
  assert.ok(tools.includes('inert={!subExpanded}'), 'the collapsed subagent body must be inert');
  // A tool's own body is not mounted until its row is opened, so there is nothing
  // to tab into while it is closed and nothing to mark inert.
  assert.ok(tools.includes('{toolExpanded && ('), 'an unopened tool body must not be mounted');
});

test('the user turn shares the assistant body right edge', () => {
  // The assistant body block is capped at 80% of the reading column, so the user's
  // turn is inset by the remaining 20%: the bubble ends on the same line as the
  // answer instead of hanging past it.  The two literals are a pair -- if either
  // one moves, the other has to.
  assert.ok(
    transcript.includes('mr-[20%] flex max-w-[80%]'),
    'the user turn must be inset by the rest of the reading column',
  );
  assert.equal(
    (transcript.match(/max-w-\[80%\]/g) ?? []).length,
    2,
    'only the user turn and the assistant body may carry the 80% cap',
  );
});

test('the side a turn is on replaces the role heading', () => {
  assert.ok(!transcript.includes('>User</span>'), 'the user turn must not print a role heading');
  assert.ok(
    !transcript.includes('>Assistant</span>'),
    'the assistant turn must not print a role heading',
  );
});

test('the conversation carries no bubble frame', () => {
  assert.ok(
    !transcript.includes('bg-gray-50/70 px-3 py-2'),
    'the user turn must not be boxed',
  );
  assert.ok(
    !transcript.includes('bg-white p-3.5'),
    'the assistant turn must not be boxed',
  );
});

test('the column is the shared reading width and the message text is larger', () => {
  // The width itself lives in `index.css` (`shellLayout.test.ts` pins the pairing
  // with the composer); here it only has to be that shared column, not a literal.
  assert.ok(
    transcript.includes('console-column'),
    'the transcript column must use the shared reading width',
  );
  assert.equal(
    (transcript.match(/text-base leading-relaxed/g) ?? []).length,
    2,
    'both sides must render at the larger size',
  );
});

test('assistant-side activity stays inside the left column', () => {
  // Thoughts, tools and info stay bounded instead of running the full width.
  assert.equal(
    (transcript.match(/max-w-\[85%\]/g) ?? []).length,
    3,
    'the thought, tool group and info rows must be bounded to the left column',
  );
});

test('the run log stays subordinate to the answer', () => {
  // Thought and tool rows are log lines: no filled chip, no box of their own, and
  // a smaller size than the answer they precede.
  const thought = transcript.slice(
    transcript.indexOf("if (m.type === 'thought')"),
    transcript.indexOf("if (m.type === 'tool_group')"),
  );
  assert.equal(thought.includes('bg-[#f3f4f5]'), false, 'a thought must not be a filled chip');
  assert.ok(thought.includes('text-xs'), 'a thought line must be smaller than the answer');
  const tools = transcript.slice(
    transcript.indexOf("if (m.type === 'tool_group')"),
    transcript.indexOf("if (m.type === 'assistant')"),
  );
  assert.equal(
    tools.includes('bg-[#f3f4f5]'),
    false,
    'a collapsed tool group must not be a filled chip either',
  );
  assert.ok(tools.includes('text-xs'), 'a tool line must be smaller than the answer');
  // A batch is no longer a container, so the row itself carries the level: a
  // border and a fill per call would give ten calls the weight of the answer.
  assert.equal(
    tools.includes('rounded-control border font-mono text-xs'),
    false,
    'a tool row must be a run-log line, not a filled card',
  );
  assert.equal(tools.includes('border-red-200'), false, 'a failed call must not paint a red box');
});

test('the run log reads at a Fluent type step', () => {
  // Fluent's ramp steps by caption2 (10px), caption1 (12px), body1 (14px) and up;
  // 11px is not on it.  The log lines take caption1 and the metadata stays at
  // caption2, so a hand-picked size cannot drift back in.
  assert.equal(
    transcript.includes('text-[11px]'),
    false,
    'an off-ramp size must not come back',
  );
  assert.ok(transcript.includes('text-xs'), 'the log lines read at caption1');
  assert.ok(transcript.includes('text-[10px]'), 'the metadata stays at caption2');
});

test('the scroller scrolls with no visible scrollbar', () => {
  // The column keeps both its edges -- and so stays on the composer card's -- only
  // if no scrollbar takes a bite out of it, the way the sidebar tree already
  // works.  Scrolling itself (wheel, touch, keyboard) must stay.
  assert.ok(
    transcript.includes('no-scrollbar'),
    'the transcript scroller must hide its scrollbar',
  );
  assert.ok(transcript.includes('overflow-y-auto'), 'the transcript must still scroll');
  assert.equal(
    transcript.includes('scrollbar-gutter'),
    false,
    'a hidden scrollbar leaves nothing to reserve',
  );
});

test('a tool group without items paints nothing', () => {
  // A batch opens its group before the first item lands (and a batch can end up
  // carrying none), so the row must be skipped until it has items - the TUI never
  // paints an empty "0 tools" placeholder either.  The rule lives in `rowPaints`,
  // which both the row and its list wrapper ask, so it cannot drift between them.
  assert.ok(
    /if \(!message\.tools\?\.length\) return false;/.test(foldRules),
    'an empty tool group must paint nothing',
  );
  assert.ok(
    transcript.includes('if (!rowPaints(m, processMeta)) return null;'),
    'the transcript must apply that rule to every row',
  );
});

test('a tool row is a bare summary with its own detail fold', () => {
  // A batch is a data boundary, not a visual one: every call paints its own row
  // (name, intent, path, state) and opens its own result body.  There is no
  // batch header to expand first and no labelled argument line to read.
  const tools = transcript.slice(
    transcript.indexOf("if (m.type === 'tool_group')"),
    transcript.indexOf("if (m.type === 'assistant')"),
  );
  assert.ok(tools.includes('>{t.name}</span>'), 'the tool name must always be printed');
  assert.equal(tools.includes('终端'), false, 'the name must not be replaced by a kind word');
  assert.ok(tools.includes('{intent}'), 'the model-provided intent must print beside the name');
  assert.equal(tools.includes('意图：'), false, 'the summary must carry no explanatory label');
  assert.equal(tools.includes('入参'), false, 'the summary must not print the call arguments');
  assert.equal(tools.includes('formatToolArgs'), false, 'no row may print an argument line');
  assert.equal(
    tools.includes('展开工具列表'),
    false,
    'a batch must not be a fold of its own any more',
  );
  assert.ok(
    tools.includes('aria-expanded={hasDetail ? toolExpanded : undefined}'),
    'each tool needs its own fold state',
  );
  assert.ok(tools.includes('{toolExpanded && ('), 'the result body must be hidden by default');
  assert.ok(tools.includes('toolExpansions[toolKey]'), 'call detail state must stay above the rows');
  assert.ok(tools.includes('onToggleTool(m.id, toolKey'), 'the call fold must open that call alone');
  // The row itself, without the subagent card that follows it.
  const row = tools.slice(
    tools.indexOf('const renderToolRow'),
    tools.indexOf('const renderSubagentCard'),
  );
  // A call in flight is shown rather than spelled out: the row leads with a
  // spinner, so "运行中" never becomes a word beside the tool name.
  assert.ok(
    row.includes("const active = t.status === 'running' || t.status === 'pending'"),
    'the in-flight state must be derived from the status, not from a label',
  );
  assert.ok(row.includes('animate-spin'), 'an in-flight call must animate');
  assert.ok(
    row.includes('active && !nested'),
    'a subagent step must not animate twice, once on the rail and once in its row',
  );
  // A failure is colour, not copy: the row and the call's name turn red and no
  // reason text is appended.  The runtime's status is never a word to print -- a
  // success carries a body digest ("ok (48 chars, 2 lines)"), a failure its reason.
  assert.ok(row.includes('t.error ? "text-red-600" : "text-gray-600"'), 'a failed call must read red');
  assert.ok(row.includes('t.error ? "text-red-700" : "text-gray-900"'), 'its name must read red too');
  assert.equal(row.includes('const notable'), false, 'no status reason may be appended to the row');
  assert.ok(
    row.includes('toolFailureReason(t.status, t.error)'),
    'a failure reason must be read for the detail, not for the row',
  );
  assert.ok(
    row.includes('{reasonShown && ('),
    'the reason must be painted inside the opened detail',
  );
  assert.ok(
    row.includes("t.status === 'cancelled'") && row.includes("t.status === 'canceled'"),
    'a cancellation must keep its word even though it is not an error',
  );
  // A right-aligned tail would pin the words and the chevron to the end of the
  // line, at a fixed position that says nothing about this call.
  assert.equal(row.includes('ml-auto'), false, 'the row tail must not be pinned to the end');
  // Calls are one list whatever batch they came in: the batch wrapper adds no
  // padding of its own, and the row gap shrinks to the in-batch gap when the next
  // row is another batch of the same turn.  Otherwise the boundary between two
  // batches would space calls differently than the batch itself does -- which read
  // as "parallel calls are closer" when it was only a container edge.
  assert.ok(
    tools.includes('<div key={m.id} className="max-w-[85%]">'),
    'a batch must add no vertical padding of its own',
  );
  assert.ok(
    transcript.includes("continuesCalls ? 'pb-0.5' : 'pb-5'"),
    'a batch boundary must not open a gap between two calls',
  );
  assert.ok(
    transcript.includes("next.type === 'tool_group'"),
    'the tighter gap is for a following batch, not for any following row',
  );
  assert.ok(
    row.includes('group-hover:opacity-100'),
    'the detail chevron must stay quiet until the row is pointed at',
  );
});

test('a read or edit body is highlighted through the shared code block', () => {
  // A file body is code, so it takes the same highlighter as a markdown fence;
  // every other body (a command's output, a search result) stays plain text.
  const tools = transcript.slice(
    transcript.indexOf("if (m.type === 'tool_group')"),
    transcript.indexOf("if (m.type === 'assistant')"),
  );
  assert.ok(
    tools.includes('toolPreviewLanguage(t.name, t.path, t.preview)'),
    'the body language must come from the tool name, path and body',
  );
  assert.ok(tools.includes('<CodeBlock'), 'a code body must render highlighted');
  assert.ok(
    tools.includes('whitespace-pre-wrap break-all text-gray-600'),
    'a non-code body must keep the plain-text renderer',
  );
});

test('a command body is a terminal session, not tokenized source', () => {
  // A program's output carries its own colour sequences, so it goes through the
  // terminal renderer; tokenizing it as source code would be meaningless.  The
  // invocation is the session's first line, behind a prompt.
  const tools = transcript.slice(
    transcript.indexOf("if (m.type === 'tool_group')"),
    transcript.indexOf("if (m.type === 'assistant')"),
  );
  assert.ok(tools.includes('isTerminalTool(t.name)'), 'a run tool must be recognised');
  assert.ok(
    tools.includes("<TerminalOutput text={t.preview ?? ''} command={command} />"),
    'its body must render as one terminal session',
  );
  assert.ok(tools.includes('toolCommand(t)'), 'the invocation must come from the call itself');
  assert.ok(
    tools.includes('!terminal'),
    'a terminal body must not also be tokenized as source code',
  );
  assert.ok(terminalOutput.includes('{prompt !== \'\''), 'an invocation must be painted when there is one');
  assert.ok(terminalOutput.includes('$ </span>'), 'the invocation must sit behind a prompt, not a label');
  assert.ok(
    terminalOutput.includes('whitespace-pre'),
    'the prompt and the output must share the terminal\'s own line breaks',
  );
});

test('a subagent card is a neutral raised layer with an animated persona and state', () => {
  // The card groups nested rows, so it takes the raised layer role and keeps the
  // purple for the persona mark alone.  Its states are animated, and the rail
  // circle carries the step's own outcome instead of one purple dot per step.
  // The card's own `return (` opens its markup; the next one belongs to the step
  // map, so the slice holds the whole header and the step state it derives.
  const start = transcript.indexOf('renderSubagentCard');
  const end = transcript.indexOf('return (', transcript.indexOf('return (', start) + 1);
  const card = transcript.slice(start, end);
  assert.ok(card.includes('bg-raised'), 'the card must sit on the neutral raised layer');
  assert.equal(card.includes('bg-purple-50/40'), false, 'the card must not paint a purple fill');
  assert.ok(
    card.indexOf('Bot20Regular') < card.indexOf('@{node.subagentName}'),
    'the persona mark must precede the @name tag',
  );
  assert.ok(card.includes('animate-spin'), 'the running state must animate');
  assert.ok(
    card.includes('stepRunning') && card.includes('stepFailed'),
    'the rail circle must carry the step state',
  );
  assert.ok(
    transcript.includes('renderToolRow(t, true)'),
    'the rail owns the step\'s activity, so its row must not repeat it',
  );
});
