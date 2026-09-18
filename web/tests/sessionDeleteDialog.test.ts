/**
 * Source guard for the sidebar's delete confirmation.
 *
 * The confirmation used to be an inline strip at the foot of the sidebar, armed by
 * the trash icon of a row far above it, and that cost three things:
 *
 *  * the question sat ~500px -- and often a scroll -- below the session it was
 *    about, and named nothing about it, so two rows titled "新会话" were
 *    indistinguishable at the point of no return;
 *  * the result notice of the *previous* delete rendered in the same corner with
 *    the same amber styling, which made a finished delete read like a new prompt;
 *  * nothing moved the focus into it: no `dialog` role, no Escape, no focus return,
 *    so a keyboard user answered a question they were never placed in.
 *
 * It is a portalled modal dialog now, opened with the whole target (title and
 * thread id) and following the shared dialog contract.  This file pins that, so a
 * future edit cannot quietly turn it back into a strip in the footer.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));
const read = (relative: string) => readFileSync(join(here, '..', 'src', relative), 'utf8');

const sidebar = read('components/SideBar.tsx');
const dialog = read('components/SessionDeleteDialog.tsx');

test('the delete confirmation is a dialog of its own, not a strip in the sidebar foot', () => {
  assert.ok(sidebar.includes('<SessionDeleteDialog'), 'the sidebar must open the shared dialog');
  assert.equal(
    sidebar.includes('deletingThreadId'),
    false,
    'the sidebar must not keep a second, inline confirmation',
  );
  // The row hands over the whole target: the dialog is what names the session,
  // because the row itself can be behind the scrim by the time it is read.
  assert.ok(
    /openDelete\(\{\s*threadId: sess\.thread_id, title: sess\.title\s*\}\)/.test(sidebar),
    'the row must pass the title as well as the thread id',
  );
  assert.ok(
    sidebar.includes('aria-haspopup="dialog"'),
    'the trash icon must announce that it opens a dialog',
  );
});

test('the dialog follows the console dialog contract', () => {
  assert.ok(dialog.includes("from './Portal.tsx'"), 'it must portal, or the rail anchors it');
  assert.ok(dialog.includes('role="dialog"'), 'it must be a dialog');
  assert.ok(dialog.includes('aria-modal="true"'), 'it must be modal');
  assert.ok(
    dialog.includes('aria-labelledby') && dialog.includes('aria-describedby'),
    'its question and its consequence text must both be reachable',
  );
  assert.ok(dialog.includes('useDialogKeyboardNav'), 'the focus must enter the box');
  assert.ok(dialog.includes("event.key === 'Escape'"), 'Escape must close it');
  // Enter on a freshly opened confirmation must not delete anything: the initial
  // focus is the header's close control, and the destructive button never carries it.
  // `data-initial-focus` is spelled twice: once as the keyboard hook's selector and
  // once as the attribute on the control that must actually take the focus.  The
  // attribute is the last occurrence, and it has to sit between the close control
  // and the destructive button.
  const initial = dialog.lastIndexOf('data-initial-focus');
  const destructive = dialog.indexOf('ui-danger');
  const closeControl = dialog.indexOf('关闭删除确认');
  assert.ok(initial > -1, 'the dialog must declare its initial focus');
  assert.ok(
    closeControl > -1 && closeControl < initial,
    'the initial focus must be the close control',
  );
  assert.ok(initial < destructive, 'and the destructive button must not be it');
  assert.equal(
    dialog.slice(destructive).includes('data-initial-focus'),
    false,
    'the destructive button must not take the initial focus',
  );
  // A two-word decision needs no third control saying "no": the close control, the
  // scrim and Escape all dismiss the box.
  assert.equal(
    /取消/.test(dialog),
    false,
    'the dialog must not carry a cancel button',
  );
});

test('the dialog says the conversation goes with the record, irreversibly', () => {
  // The delete purges the thread from the checkpoint store, the transcript
  // projection, the search index and its snapshots, so the body must say so --
  // the old copy promised the history stayed, which is now the one outcome the
  // server no longer produces.
  assert.ok(dialog.includes('全部对话历史'), 'the body must say the history goes too');
  assert.ok(dialog.includes('不可恢复'), 'and that it cannot be undone');
  assert.ok(
    dialog.includes('全文检索索引'),
    'and name the index, which is what made a deleted session searchable',
  );
  assert.equal(
    /仍保留在磁盘上/.test(dialog),
    false,
    'the confirmation must not promise a retention that no longer happens',
  );
  assert.equal(
    /已删除/.test(dialog),
    false,
    'the confirmation must not report a result it does not have yet',
  );
});

test('a refused delete keeps the dialog open with the reason inline', () => {
  assert.ok(
    sidebar.includes('if (accepted) setDeleting(null)'),
    'only an accepted delete may close the confirmation',
  );
  assert.ok(dialog.includes('role="alert"'), 'the reason belongs inside the open dialog');
  // While the dialog owns the failure, the sidebar banner stands down, so one
  // error is not painted twice.
  assert.ok(
    sidebar.includes('deleting === null && (sessionActionError !== null'),
    'the footer banner must stand down while the dialog is open',
  );
  assert.ok(
    sidebar.includes('dismissSessionAlert();'),
    'opening the confirmation must clear the previous action alert',
  );
});
