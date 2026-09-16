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

test('an expanded tool row keeps the tool name and shows its intent and arguments', () => {
  // The name is what the call *was*, the intent what the model said it was for.
  // Replacing the name with a kind word ("终端") dropped it entirely, and the
  // arguments -- the reason to open the row -- were not printed at all.
  const tools = transcript.slice(
    transcript.indexOf("if (m.type === 'tool_group')"),
    transcript.indexOf("if (m.type === 'assistant')"),
  );
  assert.ok(tools.includes('>{t.name}</span>'), 'the tool name must always be printed');
  assert.equal(tools.includes('终端'), false, 'the name must not be replaced by a kind word');
  assert.ok(tools.includes('{t.label}'), 'the model-provided intent must print beside it');
  assert.ok(tools.includes('formatToolArgs(t.args)'), 'the call arguments must be printed');
  assert.ok(
    tools.includes("argsLine !== ''"),
    'a call without arguments must not print an empty line',
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
  assert.ok(tools.includes('<CodeBlock lang={previewLang} code={t.preview} />'), 'a code body must render highlighted');
  assert.ok(
    tools.includes('whitespace-pre-wrap break-all text-gray-600'),
    'a non-code body must keep the plain-text renderer',
  );
});

test('a command body is painted as terminal output', () => {
  // A program's output carries its own colour sequences, so it goes through the
  // terminal renderer; tokenizing it as source code would be meaningless.
  const tools = transcript.slice(
    transcript.indexOf("if (m.type === 'tool_group')"),
    transcript.indexOf("if (m.type === 'assistant')"),
  );
  assert.ok(tools.includes('isTerminalTool(t.name)'), 'a run tool must be recognised');
  assert.ok(tools.includes('<TerminalOutput text={t.preview} />'), 'its body must render as terminal output');
  assert.ok(
    tools.includes('t.preview && !terminal'),
    'a terminal body must not also be tokenized as source code',
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
});
