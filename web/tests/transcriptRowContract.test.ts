test('the change cards are the turn\'s outcome, not another step of its fold', () => {
  // A folded turn still shows what it changed: the row is not a step, so the fold
  // neither hides it nor reads its status from it.
  const changes = rowSource('ChangesRow');
  assert.equal(ROW_POLICY.changes.step, false, 'a change row is not a fold step');
  assert.equal(isFoldStep({ type: 'changes' } as never), false);
  assert.ok(changes.includes('本轮工作区改动'), 'the block names itself');
  assert.ok(changes.includes('{total} 个文件'), 'and counts the files it lists');
  assert.ok(
    changes.includes('显示前 ${changes.length} 个'),
    'a bounded list says how many it is not showing',
  );
  assert.ok(changes.includes('{change.path}'), 'each card prints the file it names');
  assert.ok(changes.includes('+{change.insertions}'), 'and its own added lines');
  assert.ok(changes.includes('-{change.deletions}'), 'and its own removed lines');
  assert.ok(
    changes.includes('{!change.binary && !change.reverted && ('),
    'a file whose change could not be counted prints no counts at all',
  );
  assert.ok(
    changes.includes('actions.onReviewFile(change.path)'),
    'a card opens the explorer on the file it names',
  );
  assert.equal(
    changes.includes('gitStatus'),
    false,
    'the row paints the turn\'s own list, not the workspace\'s standing state',
  );
});

test('undoing a file is offered on the card, armed before it fires', () => {
  // The one control in the transcript that writes to the reader's files: it is on the
  // card it belongs to, it says what it will do, and one click only arms it.
  const changes = rowSource('ChangesRow');
  assert.ok(
    changes.includes('actions.onRevertFile(message.turnId ?? \'\', change.path)'),
    'a card raises the revert for its own turn and path',
  );
  assert.ok(changes.includes('恢复到本轮开始前？'), 'arming states what will happen');
  assert.ok(changes.includes('确认撤销'), 'a second, explicit click carries it out');
  assert.ok(changes.includes('已撤销'), 'a reverted file says so on the card');
  assert.ok(
    changes.includes('{armed ? \'取消\' : \'撤销\'}'),
    'the same control disarms itself',
  );
  assert.equal(
    changes.includes('window.confirm'),
    false,
    'the confirmation is part of the card, not a browser dialog',
  );
  assert.ok(
    changes.includes('!change.binary && ('),
    'a file the runtime kept no copy of is never offered for undo',
  );
});

/**
 * Contract guards for the transcript's rows.
 *
 * A row kind is a module under `components/transcriptRows/`, picked by
 * `ROW_RENDERERS[message.type]`, so these guards read *that directory* instead of one
 * big component: a new kind is covered as soon as its module exists, and nothing here
 * depends on the order the kinds are written in.  The layout rules that are about
 * geometry rather than about source text (where a rule sits, how far apart two rows
 * land) are pinned in `transcriptFoldSpace.verify.ts` against a real browser, which is
 * where "above"/"below" can actually be measured.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { ROW_POLICY, isFoldStep } from '../src/stores/transcriptRowPolicy.ts';
import {
  allRowSources,
  count,
  dispatcherSource,
  rowDirectoryFiles,
  rowModules,
  rowSource,
  tableKinds,
} from './helpers/transcriptSource.ts';

const here = dirname(fileURLToPath(import.meta.url));
const policySource = readFileSync(
  join(here, '..', 'src', 'stores', 'transcriptRowPolicy.ts'),
  'utf8',
);
const registrySource = readFileSync(
  join(here, '..', 'src', 'components', 'transcriptRows', 'registry.tsx'),
  'utf8',
);

test('every row kind has a renderer, and every renderer a kind', () => {
  // The two tables have to agree: `ROW_RENDERERS` is what paints a kind and
  // `ROW_POLICY` is how it folds, and a kind missing from either is a row that paints
  // nothing or folds wrongly.  TypeScript already forces both to cover the union --
  // this catches the two lists drifting apart in a way a type cannot see.
  assert.deepEqual(
    tableKinds(registrySource).sort(),
    tableKinds(policySource).sort(),
    'the renderer table and the fold table must list the same kinds',
  );
  // And each of them names a module that exists: the registry is read as text here,
  // so a rename that TypeScript would catch is still worth pinning at runtime.
  const registered = rowModules().map((row) => row.name);
  assert.deepEqual(registered.length, tableKinds(registrySource).length, 'every kind has a module');
  // A module written but never registered would never paint: catch it here.
  for (const file of rowDirectoryFiles()) {
    const source = rowSource(file.replace(/\.tsx$/, ''));
    if (!/export const \w+Row = React\.memo\(/.test(source)) continue;
    const name = file.replace(/\.tsx$/, '');
    assert.ok(registered.includes(name), `${name} is a row module but is not in the registry`);
  }
});

test('the fold hides a turn\'s steps and nothing else', () => {
  // A step is hidden by a collapsed turn; a row that is not a step is not part of the
  // fold at all -- the answer, the user's own turn and the runtime's notices stay on
  // screen whatever the fold is doing.
  const collapsed = { isFirst: false, isExpanded: false };
  const first = { isFirst: true, isExpanded: false };
  const open = { isFirst: false, isExpanded: true };
  assert.equal(ROW_POLICY.thought.step, true, 'a thought is a step');
  assert.equal(ROW_POLICY.tool_group.step, true, 'a tool batch is a step');
  assert.equal(ROW_POLICY.assistant.step, false, 'the answer is not a step');
  assert.equal(ROW_POLICY.user.step, false, 'the user\'s turn is not a step');
  assert.equal(ROW_POLICY.info.step, false, 'a notice is not a step');
  assert.equal(isFoldStep({ type: 'thought' } as never), true);
  // An empty batch is not a step: it would otherwise be a blank row of its own.
  assert.equal(isFoldStep({ type: 'tool_group', tools: [] } as never), false);
  assert.equal(isFoldStep({ type: 'tool_group', tools: [{}] } as never), true);
  assert.equal(ROW_POLICY.assistant.step || collapsed.isExpanded || first.isFirst, true);
  assert.equal(open.isExpanded, true);
});

test('a row paints nothing while the fold hides it, and measures as nothing', () => {
  // The list's wrapper exists per index, so a hidden row that still reported a height
  // would leave a blank of its own in the middle of the turn.
  const dispatcher = dispatcherSource();
  assert.ok(
    dispatcher.includes('if (!rowPaints(message, processMeta)) return null;'),
    'the dispatcher must skip a row the fold hides',
  );
  assert.ok(
    dispatcher.includes('const Row = ROW_RENDERERS[message.type];'),
    'and then pick the renderer from the table, not from a branch',
  );
  assert.equal(
    /if \(m\.type === '/.test(dispatcher),
    false,
    'no row kind may be special-cased by position in the dispatcher',
  );
});

test('the user turn and the assistant body share the reading column', () => {
  // The assistant body is capped at 80% of the reading column and the user's turn is
  // inset by the remaining 20%: the two numbers are a pair, so they are pinned as one.
  const user = rowSource('UserRow');
  const assistant = rowSource('AssistantRow');
  assert.ok(user.includes('mr-[20%] flex max-w-[80%]'), 'the user turn is inset by the rest');
  assert.ok(assistant.includes('flex max-w-[80%] flex-col items-start'), 'the answer hugs the left');
  assert.ok(user.includes('className="flex justify-end"'), 'the user turn sits on the right');
  assert.equal(
    count(allRowSources(), 'max-w-[80%]'),
    2,
    'only the user turn and the assistant body may carry the 80% cap',
  );
  assert.equal(
    count(allRowSources(), 'text-base leading-relaxed'),
    2,
    'both sides render at the body size',
  );
  assert.equal(
    count(allRowSources(), 'max-w-[85%]'),
    4,
    'the thought, tool, info and change rows stay inside the left column',
  );
});

test('the side a turn is on replaces the role heading', () => {
  const all = allRowSources();
  assert.equal(all.includes('>User</span>'), false, 'no role heading');
  assert.equal(all.includes('>Assistant</span>'), false, 'no role heading');
  assert.equal(all.includes('bg-gray-50/70 px-3 py-2'), false, 'the user turn is not boxed');
  assert.equal(all.includes('bg-white p-3.5'), false, 'the answer is not boxed');
});

test('the run log stays subordinate to the answer', () => {
  // Thoughts and tool rows are log lines: no filled chip, no box of their own, and a
  // smaller size than the answer they precede.
  const thought = rowSource('ThoughtRow');
  const tools = rowSource('ToolGroupRow');
  const info = rowSource('InfoRow');
  for (const [name, source] of [['thought', thought], ['tool', tools], ['info', info]]) {
    assert.equal(source.includes('bg-[#f3f4f5]'), false, `a ${name} row must not be a filled chip`);
    assert.ok(source.includes('text-xs'), `a ${name} line must be smaller than the answer`);
  }
  assert.ok(thought.includes('text-xs'), 'a thought line reads at caption1');
  assert.equal(
    allRowSources().includes('text-[11px]'),
    false,
    'an off-ramp size must not come back',
  );
});

test('a tool row is a bare summary with its own detail fold', () => {
  // A batch is a data boundary, not a visual one: every call paints its own row
  // (name, intent, path) and opens its own result body.  There is no batch header to
  // expand first, no labelled argument line, and no card per call.
  const tools = rowSource('ToolGroupRow');
  assert.ok(tools.includes('>{t.name}</span>'), 'the tool name must always be printed');
  assert.equal(tools.includes('终端'), false, 'the name must not be replaced by a kind word');
  assert.ok(tools.includes('{intent}'), 'the model-provided intent must print beside the name');
  assert.equal(tools.includes('意图：'), false, 'the summary must carry no explanatory label');
  assert.equal(tools.includes('入参'), false, 'the summary must not print the call arguments');
  assert.equal(tools.includes('formatToolArgs'), false, 'no row may print an argument line');
  assert.equal(tools.includes('展开工具列表'), false, 'a batch must not be a fold of its own');
  assert.equal(
    tools.includes('rounded-control border font-mono text-xs'),
    false,
    'a tool row must be a run-log line, not a filled card',
  );
  assert.ok(
    tools.includes('aria-expanded={hasDetail ? toolExpanded : undefined}'),
    'each tool needs its own fold state',
  );
  assert.ok(tools.includes('{toolExpanded && ('), 'the result body must be hidden by default');
  assert.ok(tools.includes('toolExpansions[toolKey]'), 'call detail state must stay above the rows');
  assert.ok(tools.includes('actions.onToggleTool(message.id, toolKey'), 'the call fold opens that call alone');
  // The row itself, without the subagent card that follows it.
  const row = tools.slice(tools.indexOf('const renderToolRow'), tools.indexOf('const renderSubagentCard'));
  assert.ok(
    row.includes("const active = t.status === 'running' || t.status === 'pending'"),
    'the in-flight state must be derived from the status, not from a label',
  );
  assert.ok(row.includes('animate-spin'), 'an in-flight call must animate');
  assert.ok(row.includes('active && !nested'), 'a subagent step must not animate twice');
  assert.ok(
    row.includes('toolFailureReason(t.status, t.error)'),
    'a failure reason must be read for the detail, not for the row',
  );
  assert.ok(row.includes('{reasonShown && ('), 'the reason must be painted inside the opened detail');
  assert.equal(row.includes('ml-auto'), false, 'the row tail must not be pinned to the end');
  assert.ok(row.includes('group-hover:opacity-100'), 'the chevron stays quiet until hover');
  assert.ok(row.includes('t.error ? "text-red-600" : "text-gray-600"'), 'a failed call reads red');
  assert.ok(row.includes('t.error ? "text-red-700" : "text-gray-900"'), 'its name reads red too');
  assert.ok(row.includes("t.status === 'cancelled'"), 'a cancellation keeps its word');
  assert.equal(row.includes('const notable'), false, 'no status reason may be appended');
});

test('a batch boundary is not a paragraph break', () => {
  // Two calls of one batch and the first call of the next batch are the same distance
  // apart: the batch is no longer a container on screen, so its edge must not space
  // rows differently -- which read as "parallel calls are closer".
  const dispatcher = dispatcherSource();
  const tools = rowSource('ToolGroupRow');
  assert.ok(tools.includes('<div className="max-w-[85%]">'), 'a batch adds no padding of its own');
  assert.ok(dispatcher.includes("continuesCalls ? 'pb-0.5' : 'pb-5'"), 'the gap is conditional');
  assert.ok(dispatcher.includes("next.type === 'tool_group'"), 'only for a following batch');
});

test('a read or edit body is highlighted through the shared code block', () => {
  const tools = rowSource('ToolGroupRow');
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
  const tools = rowSource('ToolGroupRow');
  const terminal = readFileSync(join(here, '..', 'src', 'components', 'TerminalOutput.tsx'), 'utf8');
  assert.ok(tools.includes('isTerminalTool(t.name)'), 'a run tool must be recognised');
  assert.ok(
    tools.includes("<TerminalOutput text={t.preview ?? ''} command={command} />"),
    'its body must render as one terminal session',
  );
  assert.ok(tools.includes('toolCommand(t)'), 'the invocation must come from the call itself');
  assert.ok(tools.includes('!terminal'), 'a terminal body must not also be tokenized as code');
  assert.ok(terminal.includes('{prompt !== \'\''), 'an invocation must be painted when there is one');
  assert.ok(terminal.includes('$ </span>'), 'the invocation must sit behind a prompt');
  assert.ok(terminal.includes('whitespace-pre'), 'prompt and output share the terminal line breaks');
});

test('a collapsed body is not reachable by keyboard', () => {
  const tools = rowSource('ToolGroupRow');
  assert.ok(tools.includes('aria-hidden={!subExpanded}'), 'the subagent body exposes its visibility');
  assert.ok(tools.includes('inert={!subExpanded}'), 'the collapsed subagent body is inert');
  // A tool's own body is not mounted until its row is opened, so there is nothing to
  // tab into while it is closed and nothing to mark inert.
  assert.ok(tools.includes('{toolExpanded && ('), 'an unopened tool body is not mounted');
});

test('a subagent card is a neutral raised layer with an animated persona and state', () => {
  const tools = rowSource('ToolGroupRow');
  const card = tools.slice(
    tools.indexOf('const renderSubagentCard'),
  );
  assert.ok(card.includes('bg-raised'), 'the card sits on the neutral raised layer');
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
  assert.ok(tools.includes('renderToolRow(t, true)'), 'the rail owns the step\'s activity');
});

test('the run log reads at a Fluent type step', () => {
  const all = allRowSources();
  assert.equal(all.includes('text-[11px]'), false, 'an off-ramp size must not come back');
  assert.ok(all.includes('text-xs'), 'the log lines read at caption1');
  assert.ok(all.includes('text-[10px]'), 'the metadata stays at caption2');
});

test('a row module is a row module, not a second dispatcher', () => {
  // The point of the table is that a kind's behaviour lives in its own module: a row
  // that reached back into the dispatcher, or that subscribed to the whole store,
  // would put the coupling back where the table removed it.
  for (const row of rowModules()) {
    assert.ok(
      row.text.includes('React.memo('),
      `${row.name} must be memoized, or every chunk re-renders it`,
    );
    assert.equal(
      /useConsoleStore\(\)/.test(row.text),
      false,
      `${row.name} must select the fields it paints, not subscribe to the store`,
    );
  }
});
