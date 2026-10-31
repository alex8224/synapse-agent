/**
 * Source guard for the sidebar's per-row "copy thread_id" action.
 *
 * The thread id is the console's own handle on a session -- a reader pastes it into
 * the CLI, an issue or another session -- and it used to be reachable only by
 * opening the header's session info, which also gets in the way of dragging the
 * window.  The row that names the session now carries the copy, next to the write
 * actions it already had.
 *
 * The rules pinned here are the ones a future edit is likely to break: the row must
 * copy *its own* id (never the open session's), the feedback must be per row and
 * must clear itself, and a refused clipboard must stay silent -- a copy that did
 * not happen is not a session-management failure worth a banner.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));
const read = (relative: string) => readFileSync(join(here, '..', 'src', relative), 'utf8');

const sidebar = read('components/SideBar.tsx');

test("the row copies its own thread id, not the open session's", () => {
  assert.ok(
    sidebar.includes('onClick={() => copyThreadId(sess.thread_id)}'),
    'the copy must be sent with the id the row already holds',
  );
  assert.equal(
    /copyThreadId\(currentSession|copyThreadId\(sessionKey/.test(sidebar),
    false,
    'the row must not copy whichever session happens to be open',
  );
  assert.ok(
    sidebar.includes('navigator.clipboard') && sidebar.includes('writeText(threadId)'),
    'the handler must write the id it was handed',
  );
});

test('the copied flag belongs to one row and clears itself', () => {
  assert.ok(
    sidebar.includes('const [copiedThreadId, setCopiedThreadId] = useState<string | null>(null)'),
    'one shared boolean would put the checkmark on the last-rendered row',
  );
  assert.ok(
    sidebar.includes('copiedThreadId === sess.thread_id'),
    'only the row that was copied may paint the checkmark',
  );
  assert.ok(
    sidebar.includes('COPIED_FLAG_MS'),
    'the feedback window must be a named constant, not a literal',
  );
  assert.ok(
    /clearTimeout\(copiedTimer\.current\)/.test(sidebar),
    'a second copy must restart the window instead of stacking timers',
  );
  assert.ok(
    sidebar.includes("title={copiedThreadId === sess.thread_id ? '已复制' : '复制 thread_id'}"),
    'the control must say what it does, and say it happened',
  );
});

test('the copy is a hover action and a refusal stays silent', () => {
  const copyButton = sidebar.slice(
    sidebar.indexOf('onClick={() => copyThreadId(sess.thread_id)}'),
    sidebar.indexOf('onClick={() => {\n                                  void forkRow('),
  );
  assert.ok(
    copyButton.includes('opacity-0 group-hover:opacity-100'),
    'the row surfaces its actions on hover, like the two next to it',
  );
  assert.ok(
    copyButton.includes('ui-icon-button ui-compact'),
    'it must be the shared compact control, not a one-off',
  );
  // A clipboard the origin does not grant is not a session-management failure.
  assert.ok(
    /\.catch\(\(\) => undefined\)/.test(sidebar),
    'a refused clipboard must not surface as an error',
  );
  assert.equal(
    /copyThreadId[\s\S]{0,400}setSessionActionError/.test(sidebar),
    false,
    'the copy must not borrow the failure banner',
  );
});
