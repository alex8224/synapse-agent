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
  // The header is a stopwatch only while the runtime says a turn is running: an
  // unfinished thought row is not the same thing (a dropped connection leaves one).
  assert.ok(
    transcript.includes('runtimeStatus: state.runtimeStatus'),
    'the transcript must read the running latch off the store',
  );
  assert.ok(
    transcript.includes("if (runtimeStatus !== 'running') return null;"),
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
    /Math\.floor\(\(Date\.now\(\) - startedAt\) \/ 1000\)/.test(transcript),
    'the tick must read the elapsed time off the wall clock',
  );
});

test('the final elapsed time is frozen per turn', () => {
  // The turn's entry is left in place when it stops, so the header keeps the number
  // it counted to instead of dropping back to the durations summed off its rows.
  assert.ok(
    transcript.includes('turnSeconds.get(currentTurnKey)'),
    'a turn this console watched run must report its stopwatch',
  );
  assert.ok(
    transcript.includes('prev.get(runningTurnKey) === seconds ? prev'),
    'a tick that did not change the second must not re-render the transcript',
  );
  assert.ok(
    transcript.includes('useState<ReadonlyMap<string, number>>'),
    'the elapsed time must be remembered per turn',
  );
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
    transcript.includes('pendingTurns.has(m.id)'),
    'the pending row must be rendered under its own user row',
  );
  assert.ok(
    transcript.includes('m.id === runningTurnKey'),
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

test('a thought or tool row owns the header once it lands', () => {
  assert.ok(
    transcript.includes("m.type === 'thought' || m.type === 'tool_group'"),
    'a process row must take the header over from the pending row',
  );
  assert.ok(
    transcript.includes('if (!hasProcessRow && (turnSeconds.has(m.id) || m.id === runningTurnKey))'),
    'the pending row must be skipped for a turn that already has a process row',
  );
});