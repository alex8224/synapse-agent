/**
 * Source guards for the microphone button's boundaries.
 *
 * Speech input is the one composer feature that talks to a browser API with a
 * vendor prefix and a lifecycle of its own, so four things that would otherwise
 * rot silently are pinned here:
 *
 * - **One module owns the vendor global.**  `webkitSpeechRecognition` (and the
 *   unprefixed spelling) is named in `composer/speechInput.ts` only, so the
 *   prefix can never leak into the UI or into the protocol core, and the
 *   resolution rule stays the one the offline tests can exercise.
 * - **The protocol core stays host-agnostic.**  `src/runtime-client/` is checked
 *   without the DOM library, and no speech or capture API may appear there: the
 *   microphone is a browser affordance, not a wire capability.  (No runtime RPC,
 *   no contract regeneration, no settings change is part of this feature.)
 * - **The card owns no recognition lifecycle.**  `CommandInput.tsx` calls the hook
 *   and decides where a phrase lands; the session, its restarts and its errors
 *   live in `composer/useSpeechInput.ts`, which -- like the capture task and
 *   `codexUsage` -- is entry-local and reaches no store.
 * - **The separator rule and the insertion path each have one definition.**  A
 *   phrase joins the draft through `spokenTextToInsert` (defined once, in the pure
 *   module) and enters the editor through `insertSpokenAtCaret`, never through a
 *   second ad-hoc append.
 */
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));
const srcDir = join(here, '..', 'src');

/** Every TypeScript source under a directory, recursively. */
function sourceFiles(dir: string): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) found.push(...sourceFiles(full));
    else if (entry.name.endsWith('.ts') || entry.name.endsWith('.tsx')) found.push(full);
  }
  return found;
}

const read = (...parts: string[]): string =>
  readFileSync(join(here, '..', 'src', ...parts), 'utf8');

const speechCore = read('components', 'composer', 'speechInput.ts');
const speechHook = read('components', 'composer', 'useSpeechInput.ts');
const selection = read('components', 'composer', 'composerSelection.ts');
const composer = read('components', 'composer', 'RichComposer.tsx');
const card = read('components', 'CommandInput.tsx');
const menuManifest = read('components', 'composer', 'actions', 'manifest.ts');

test('exactly one module names the speech-recognition globals', () => {
  const owners = sourceFiles(srcDir)
    .filter((file) => /webkitSpeechRecognition/.test(readFileSync(file, 'utf8')))
    .map((file) => file.slice(srcDir.length + 1).replace(/\\/g, '/'));
  assert.deepEqual(owners, ['components/composer/speechInput.ts']);
});

test('the protocol core knows nothing about the microphone', () => {
  const files = sourceFiles(join(srcDir, 'runtime-client'));
  assert.ok(files.length > 10, 'the guard must actually scan the protocol core');
  for (const file of files) {
    const source = readFileSync(file, 'utf8');
    assert.equal(
      /SpeechRecognition|MediaRecorder|getUserMedia|mediaDevices/.test(source),
      false,
      `${file.slice(srcDir.length)} must stay host-agnostic`,
    );
  }
});

test('the card calls the hook and owns no recognition lifecycle', () => {
  assert.ok(card.includes('useSpeechInput('), 'the card must go through the hook');
  assert.equal(/new Recognition|webkitSpeechRecognition|onresult|\.continuous/.test(card), false,
    'no session, restart or result handling may live in the card');
  assert.ok(
    card.includes('onTranscript: (phrase) => composerRef.current?.insertSpoken(phrase)'),
    'a recognized phrase goes to the editor, never into the prompt text directly',
  );
  assert.ok(
    card.includes('aria-pressed={speech.listening}') &&
      card.includes('composer-speech-status'),
    'the listening state must be visible, not only in a tooltip',
  );
  assert.ok(
    card.includes('attachmentError ?? speech.error'),
    'a failed microphone must be reported in the card, not swallowed',
  );
});

test('the hook is entry-local and reaches no store', () => {
  assert.equal(/useConsoleStore|zustand/.test(speechHook), false);
  assert.ok(
    speechHook.includes('resolveSpeechRecognition('),
    'the hook must use the one resolver, not a global of its own',
  );
  assert.equal(/window\.SpeechRecognition/.test(speechHook), false, 'no second lookup path');
});

test('the microphone is a control of its own, not a row of the action menu', () => {
  const ids = readdirSync(join(srcDir, 'components', 'composer', 'actions'));
  assert.equal(
    ids.some((name) => /speech|microphone|voice/i.test(name)),
    false,
    'a menu row cannot paint "listening", so speech must not become one',
  );
  assert.equal(/speech|voice/i.test(menuManifest), false);
});

test('one rule decides the separator and one path inserts the phrase', () => {
  const definitions = sourceFiles(srcDir).filter((file) =>
    /export function spokenTextToInsert/.test(readFileSync(file, 'utf8')),
  );
  assert.equal(definitions.length, 1, 'the separator rule must have exactly one definition');
  assert.ok(
    speechCore.includes('export function spokenTextToInsert'),
    'and it belongs in the pure module the offline tests can reach',
  );
  assert.ok(
    selection.includes('spokenTextToInsert(') &&
      selection.includes('export function insertSpokenAtCaret'),
    'the caret helper applies that one rule',
  );
  assert.ok(
    composer.includes('insertSpokenAtCaret(editor, phrase)'),
    'the editor inserts a phrase through the caret helper',
  );
  assert.ok(
    composer.includes('insertSpoken: (phrase: string) => void'),
    'the handle the card calls is typed',
  );
});

test('the listening animation is decoration that yields to reduced motion', () => {
  const css = readFileSync(join(srcDir, 'index.css'), 'utf8');
  assert.ok(css.includes('@keyframes composer-speech-halo'), 'the halo must be a keyframe');
  assert.ok(css.includes('@keyframes composer-speech-bar'), 'the bars must be a keyframe');
  assert.ok(css.includes('@keyframes composer-speech-caret'), 'the caption caret must be a keyframe');
  assert.match(
    css,
    /\.composer-speech-on::after\s*\{[^}]*pointer-events:\s*none/,
    'the halo must not eat a click meant for the button',
  );
  // The halo is a pseudo-element, so it cannot widen the button: the control row's
  // geometry is pinned by the appearance acceptance run, and an expanding box that
  // participated in layout would break it.
  assert.ok(
    /\.composer-speech-on\s*\{[^}]*position:\s*relative/.test(css),
    'the halo is positioned against the button, not the row',
  );
  const reduceBlocks = css.split('@media (prefers-reduced-motion: reduce)').slice(1);
  assert.ok(
    reduceBlocks.some(
      (block) =>
        block.includes('.composer-speech-on::after') && block.includes('.composer-speech-bars > span'),
    ),
    'a pulse that keeps repeating after the reader asked for no motion must be switched off',
  );
  assert.ok(
    reduceBlocks.some((block) => block.includes('.composer-speech-caption-caret')),
    'a blinking caret is motion too',
  );
  assert.ok(
    card.includes('aria-hidden="true"') && card.includes('composer-speech-bars'),
    'the bars are decoration beside the status word, never announced twice',
  );
});

test('interim text is shown in the caption and never inserted into the draft', () => {
  // Two different sinks, pinned separately: the finalized phrase is the only thing
  // that reaches the editor, and the in-progress phrase is the only thing the
  // caption paints.  Inserting an interim result would put text under the caret
  // that the recognizer is still rewriting.
  assert.match(speechHook, /const phrases = finalTranscripts\(event\)/);
  assert.match(
    speechHook,
    /for \(const phrase of phrases\) transcriptRef\.current\(phrase\)/,
    'the insertion sink is fed by finalized phrases only',
  );
  assert.match(
    speechHook,
    /setInterim\(interimTranscript\(event\)\)/,
    'the caption sink is fed by the in-progress phrase only',
  );
  assert.equal(
    (card.match(/insertSpoken\(/g) ?? []).length,
    1,
    'the draft has exactly one entry point for speech',
  );
  assert.ok(card.includes('{speechCaption}'), 'the caption is painted, not inserted');
  assert.ok(card.includes('captionText(speech.interim)'), 'and it is bounded before painting');
  assert.equal(
    /insertSpoken\(speech\.interim\)|insertSpoken\(interim/.test(card),
    false,
    'interim text must never be inserted',
  );
  assert.ok(
    speechCore.includes('export const SPEECH_CAPTION_MAX_CHARS'),
    'the caption bound is a named constant, not a literal at the call site',
  );
});
