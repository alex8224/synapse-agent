/**
 * Source guards for the transcript's "已工作 N 秒" header.
 *
 * A turn paints that header from its first thought / tool row, so the two ways it
 * can go wrong are: the number never moves while the agent works (the reader cannot
 * tell a slow turn from a stuck one), and a turn that has produced no row yet has no
 * header at all -- nothing on the left between the submit and the first step.  Both
 * are pinned here, together with the running status the opened header reports.
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

test('the running latch comes from the store, not from the rows', () => {
  assert.ok(
    transcript.includes('runtimeStatus: state.runtimeStatus'),
    'the transcript must read the running latch off the store',
  );
  assert.ok(
    transcript.includes("workGroups(messages, activeTurnId, runtimeStatus === 'running')"),
    'the stopwatch must only run while the runtime reports a running turn',
  );
});

test('the elapsed time is re-read once per second', () => {
  assert.ok(
    /window\.setInterval\(read, 1000\)/.test(transcript),
    'the counter must tick once per second',
  );
  assert.ok(
    /window\.clearInterval\(timer\)/.test(transcript),
    'the tick must stop with the turn, not keep running',
  );
  // Read from the clock rather than incremented: a throttled background tab fires
  // the interval late, and a counter would then resume seconds behind the truth.
  assert.ok(
    transcript.includes('setNow(Date.now())'),
    'the tick must read the elapsed time off the wall clock',
  );
});

test('the final elapsed time is frozen per turn', () => {
  assert.ok(transcript.includes('workSeconds(group, now)'));
  assert.equal(transcript.includes('turnSeconds'), false,
    'durations must not live in a component-local map lost on refresh');
});

test('a turn with no process row yet still shows the header and its rule', () => {
  assert.ok(
    transcript.includes('已工作 {text}'),
    'the pending row must print the worked-seconds header',
  );
  assert.ok(
    transcript.includes('<div className="border-b border-line/60 my-2.5" />'),
    'the header must be followed by the thin rule',
  );
  assert.ok(
    transcript.includes('<ChevronRight16Regular') && transcript.includes('<ChevronDown16Regular'),
    'the header must show a closed and an opened chevron',
  );
  assert.ok(
    transcript.includes('pendingTurns.get(m.id)'),
    'the pending row must be rendered under its own user row',
  );
  assert.ok(
    transcript.includes('g.anchor.work || g.running'),
    'the header must be on screen from the submit, before the first tick',
  );
  assert.ok(
    transcript.includes('className="w-[85%]"'),
    'the pending row must stay inside the assistant column',
  );
});

test('an opened pending header reports what the runtime is doing', () => {
  assert.ok(
    transcript.includes('{expanded && <ActivityLine activity={activity} />}'),
    'an opened header with no step yet must show the running status',
  );
  assert.ok(
    transcript.includes('<span>{activity.phase}</span>') &&
      transcript.includes('{activity.detail && <span className="text-gray-400">{activity.detail}</span>}'),
    'the status line must carry the phase and its detail',
  );
});

test('the pending header keeps its rule directly under the button', () => {
  // The rule is the stopwatch's separator, not the last line of the fold: the
  // opened status reads *under* it.  A rule that drifts below the status (or is
  // drawn a second time at the end of the row) is the layout the reader reported.
  const row = transcript.slice(
    transcript.indexOf('function PendingTurnRow('),
    transcript.indexOf('export const Transcript'),
  );
  assert.ok(row.includes('已工作 {text}'), 'the pending row must print the worked-seconds header');
  const buttonEnd = row.indexOf('</button>');
  const rule = row.indexOf('<div className="border-b border-line/60 my-2.5" />');
  const status = row.indexOf('{expanded && <ActivityLine activity={activity} />}');
  assert.ok(buttonEnd !== -1, 'the pending row must open with its header button');
  assert.ok(rule !== -1, 'the header must be followed by the thin rule');
  assert.ok(status !== -1, 'the opened header must render the running status');
  assert.ok(rule > buttonEnd, 'the rule must sit under the header button, not above it');
  assert.ok(status > rule, 'the opened status must hang below the rule');
  assert.equal(
    (row.match(/border-b border-line\/60/g) ?? []).length,
    1,
    'the pending row must draw exactly one rule',
  );
});

test('the running status is painted once, by the row that owns it', () => {
  // A pending row prints the status inside its own fold, so the transcript must not
  // also hold a second one at the bottom of the column.
  assert.ok(
    transcript.includes('![...pendingTurns.values()].some((g) => g.running)') &&
      transcript.includes('activity={pending.running ? activity : null}'),
    'the trailing status line must stay off screen while a pending row carries it',
  );
});

test('a process row keeps its rule under the header in both folds', () => {
  // Same structure as the pending row: the rule is the stopwatch's separator, so an
  // opened fold hangs its steps below it and draws no second rule at their end --
  // the rule must not move (or double up) when the fold is toggled.
  const folds = [
    ["if (m.type === 'thought')", "if (m.type === 'tool_group')"],
    ["if (m.type === 'tool_group')", "if (m.type === 'assistant')"],
  ];
  for (const [start, end] of folds) {
    const block = transcript.slice(transcript.indexOf(start), transcript.indexOf(end));
    assert.equal(
      (block.match(/<div className="border-b border-line\/60 my-2\.5" \/>/g) ?? []).length,
      2,
      `${start} must draw the header rule once per fold and nothing at the end`,
    );
    assert.equal(
      block.includes('isLast'),
      false,
      'the end of the steps must not draw a second rule',
    );
    const opened = block.slice(block.indexOf(') : ('));
    const button = opened.indexOf('</button>');
    const rule = opened.indexOf('<div className="border-b border-line/60 my-2.5" />');
    const firstStep = opened.indexOf('onClick={() => handleToggleExpand(m.id)}');
    assert.ok(button !== -1, 'an opened fold must start with its header button');
    assert.ok(rule > button, 'the rule must sit under the opened header button');
    assert.ok(firstStep > rule, 'the steps must hang below the rule');
  }
  assert.equal(
    (transcript.match(/<div className="border-b border-line\/60 my-2\.5" \/>/g) ?? []).length,
    5,
    'the rule is drawn with a header only: a thought fold, a tool fold and the pending row',
  );
});

test('a thought or tool row owns the header once it lands', () => {
  assert.ok(
    transcript.includes('group.rows.forEach'),
    'a process row must take the header over from the pending row',
  );
  assert.ok(
    transcript.includes('g.rows.length === 0 && (g.anchor.work || g.running)'),
    'the pending row must be skipped for a turn that already has a process row',
  );
});