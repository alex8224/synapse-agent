/**
 * Source guards for the composer's keyboard and wire contracts.
 *
 * Three rules here are invisible until they are broken, and all three are
 * browser-side facts that no offline test can exercise, so they are pinned where
 * they live:
 *
 * - `Enter` must not be able to do two things at once.  Accepting an `@`
 *   suggestion and submitting the turn are both bound to Enter, and the ordering
 *   of those two branches is what decides whether a pick accidentally sends a
 *   half-written prompt.
 * - A pick must *replace* the typed `@` and its query.  Appending the pill would
 *   leave a stray `@` in the prompt, which is exactly the bug this feature was
 *   asked to avoid.
 * - An IME owns the keyboard while it is composing.  Without the guard, Enter
 *   commits a pinyin candidate *and* submits the turn.
 *
 * The wire contract is pinned from the other side: the composer may only reach
 * the store through `submitPrompt(text)` — the one string the runtime has always
 * accepted — so a rich draft can never smuggle markup or an image into the
 * prompt text.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));
const read = (...parts: string[]): string =>
  readFileSync(join(here, '..', 'src', ...parts), 'utf8');

const composer = read('components', 'composer', 'RichComposer.tsx');
const card = read('components', 'CommandInput.tsx');
const selection = read('components', 'composer', 'composerSelection.ts');

test('the IME owns the keyboard while it is composing', () => {
  assert.ok(
    composer.includes('event.nativeEvent.isComposing'),
    'a composing keydown must return before any composer shortcut',
  );
  const guard = composer.indexOf('isComposing');
  const enter = composer.indexOf("event.key === 'Enter'");
  assert.ok(guard !== -1 && enter !== -1 && guard < enter, 'the guard must run first');
});

test('Enter picks an open suggestion before it can submit', () => {
  const pick = composer.indexOf('pickMention(entries[activeIndex]');
  const submit = composer.indexOf('onSubmit(snapshot())');
  assert.ok(pick !== -1, 'Enter must be able to accept the active suggestion');
  assert.ok(submit !== -1, 'Enter must still submit when nothing is open');
  assert.ok(pick < submit, 'the suggestion branch must be tested first');
  // A pick consumes the event, so the submit branch below is unreachable for it.
  const branch = composer.slice(pick - 400, pick);
  assert.ok(branch.includes('preventDefault'), 'accepting a suggestion must consume Enter');
});

test('a pick replaces the typed @ instead of appending beside it', () => {
  assert.ok(
    composer.includes('range.deleteContents()'),
    'the `@` and the query must be deleted before the pill is inserted',
  );
  assert.ok(
    composer.includes('pendingCaretRef.current = seat'),
    'the place the `@` was deleted from must be handed to the placement effect',
  );
  assert.ok(
    composer.includes('anchor.insertNode(rendered)'),
    'the rendered pill must be moved to that place, not left where React appended it',
  );
  assert.ok(
    !composer.includes('insertNode(holder)'),
    'a placeholder node must not be left behind in the draft',
  );
  assert.ok(
    selection.includes('isRangeLive'),
    'a stale range must be re-checked before it is deleted through',
  );
  // A query is only ever a mention when it starts a token, so an email address
  // or a decorator in pasted code does not open the list.
  assert.ok(
    selection.includes("!/[\\s\\u200B]/.test(before[at - 1])"),
    'a mention must start at a token boundary',
  );
});

test('Shift+Enter breaks the line without submitting', () => {
  assert.ok(composer.includes("event.shiftKey"), 'the shift modifier must be honoured');
  assert.ok(
    composer.includes('insertLineBreakAtCaret(editor)'),
    'the break must be inserted explicitly, in the browser-kept shape',
  );
  // Scoped to the handler: the module's own documentation names both, and a
  // comment must not be able to satisfy an ordering rule about code.
  const handler = composer.slice(composer.indexOf('const onKeyDown'));
  const shift = handler.indexOf('event.shiftKey');
  const breakInsert = handler.indexOf('insertLineBreakAtCaret');
  assert.ok(shift < breakInsert, 'the line break must happen inside the Enter branch');
  assert.ok(
    handler.indexOf('insertLineBreakAtCaret') < handler.indexOf('onSubmit(snapshot())'),
    'the line-break branch must return before the submit branch',
  );
});

test('the editor never lets execCommand pick the DOM shape', () => {
  // `insertText` turns a newline into two block wrappers here (a blank line that
  // also reached the prompt as a second newline) and `insertLineBreak` produced
  // two wrappers as well, so every insertion goes through the explicit helpers.
  assert.ok(
    !composer.includes('document.execCommand('),
    'the composer must not call document.execCommand',
  );
  assert.ok(
    selection.includes('insertTextAtCaret'),
    'plain text must be inserted as one literal text node',
  );
  assert.ok(
    selection.includes('createTextNode(text)'),
    'the payload must stay literal text, never browser-chosen markup',
  );
  assert.ok(
    selection.includes('insertLineBreakAtCaret'),
    'a break must be inserted in the shape the browser keeps, not asked for',
  );
});

test('a whitespace-only paste is not content', () => {
  // A clipboard holding only a bitmap offers a bare newline as its text; that
  // used to leave a blank line above the image pasted next.
  assert.ok(
    composer.includes("text.trim() !== ''"),
    'whitespace-only clipboard text must be dropped instead of inserted',
  );
});

test('a pasted image pill lands at the caret', () => {
  assert.ok(
    composer.includes('anchor.insertNode(rendered)'),
    'the rendered pill must be moved to the caret, not left where React appended it',
  );
});

test('the arrows walk the list and Escape closes it', () => {
  for (const key of ['ArrowDown', 'ArrowUp', 'Escape']) {
    assert.ok(composer.includes(`'${key}'`), `${key} must be handled while the list is open`);
  }
  assert.ok(composer.includes("event.key === 'Tab'"), 'Tab must accept the active suggestion');
  // The editor keeps the focus: the active row is named, not focused.
  assert.ok(
    composer.includes('aria-activedescendant'),
    'the list must be driven by aria-activedescendant, not by moving focus',
  );
  assert.ok(
    composer.includes('onMouseDown={(event) => event.preventDefault()}'),
    'a pointer pick must not steal the selection the pick has to replace',
  );
});

test('the rich draft still reaches the runtime as one plain string', () => {
  // The card is the only thing that talks to the store, and it passes text.
  assert.ok(card.includes('submitPrompt(snapshot.text)'), 'the store gets the serialized text');
  assert.ok(
    !card.includes('attachment_refs'),
    'attachment refs stay the store\'s business: the composer never builds them',
  );
  // Every image route (paste, drop, picker) ends in the one upload path.
  const routes = card.match(/handleFiles\(/g) ?? [];
  assert.ok(routes.length >= 3, 'paste, drop and the picker must all route through handleFiles');
  assert.ok(
    card.includes('addAttachments(Array.from(files))'),
    'files must go to the store\'s own validation, never to a second pipeline',
  );
});
