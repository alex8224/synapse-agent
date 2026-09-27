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
const localHook = read('components', 'composer', 'useLocalSpeechInput.ts');
const localAudio = read('components', 'composer', 'localSpeechAudio.ts');
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
      /SpeechRecognition|MediaRecorder|getUserMedia|mediaDevices|AudioContext|AudioWorklet/.test(
        source,
      ),
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

test('the local engine hook never names the Web Speech global', () => {
  // The local engine is a different API (mic capture + runtime RPC).  It shares
  // the controller *types* with the browser hook, never the vendor recognizer:
  // the one module that names the global stays `speechInput.ts`.
  assert.equal(
    /webkitSpeechRecognition|SpeechRecognition/.test(localHook),
    false,
    'the vendor prefix must not leak into the local engine',
  );
  assert.ok(
    localHook.includes("from './useSpeechInput.ts'"),
    'the shared controller types come from the browser hook, not a copy',
  );
  assert.equal(
    /new Recognition|onresult|\.continuous/.test(localHook),
    false,
    'no recognition session may live in the local hook',
  );
});

test('the streaming hook is handed a transport and opens no socket of its own', () => {
  // The daemon's offline engine and the desktop shell's cloud connection are two
  // ends behind one interface, so the capture code is written once.  What the hook
  // must never do is *decide* which end, or build one: that is `sttEngine`'s decision
  // and `sttTransport`'s job.
  assert.ok(
    localHook.includes('transport: SttTransport | null'),
    'the transport is handed in, not looked up',
  );
  assert.equal(
    /useConsoleStore\(|WebSocket|new SynapseRuntimeClient/.test(localHook),
    false,
    'the hook reads no store and opens no transport of its own',
  );
  assert.ok(localHook.includes('transport.begin()'), 'the dictation is opened by the transport');
  for (const verb of ['.append(', '.finish()', '.cancel()']) {
    assert.ok(
      localHook.includes(`dictation${verb}`),
      `the hook drives the dictation through the transport via ${verb}`,
    );
  }
  // The window follows the transport: a cloud engine is asked for 100-200 ms packets,
  // and that window -- not the relay -- is what shortens the wait for a word.
  assert.ok(
    localHook.includes('CLOUD_STT_CHUNK_SAMPLES'),
    'a cloud dictation uses the narrower chunk window',
  );
});

test('both speech ends live behind one transport interface', () => {
  const transport = read('components', 'composer', 'sttTransport.ts');
  const bridge = read('client', 'tauriStt.ts');
  assert.ok(
    transport.includes("readonly id: 'runtime' | 'tauri'"),
    'two ends, one shape the capture hook can be written against',
  );
  assert.ok(transport.includes('client.sttBegin('), 'the daemon end is the runtime RPC');
  assert.ok(transport.includes('sttCloudBegin('), 'the desktop end is the shell IPC');
  // The credential is the one thing that must not be reachable from this side: the
  // shell reads it from the user's speech config and never sends it back.
  assert.equal(/api[_-]?key/i.test(transport), false, 'no credential reaches the transport');
  assert.equal(/api[_-]?key/i.test(bridge), false, 'no credential crosses the IPC bridge');
  assert.ok(
    bridge.includes("isTauri()"),
    'the bridge refuses to run outside the desktop shell',
  );
});

test('the audio helpers stay DOM-free for the offline tests', () => {
  // The module must run under `node --test`, so it carries its own base64 codec
  // and touches no host capture API; the calls (not the prose) are what is pinned.
  assert.equal(
    /btoa\(|atob\(|AudioContext\(|MediaRecorder\(|getUserMedia\(/.test(localAudio),
    false,
    'the pure module must not reach for a host API',
  );
});

test('the engine and the platform together decide the route, not a local guess', () => {
  assert.ok(
    card.includes('useLocalSpeechInput('),
    'the card must be able to stream audio',
  );
  assert.ok(
    card.includes('.sttStatus('),
    'the card asks the runtime which engine is configured',
  );
  assert.match(card, /const route = speechRoute\(sttStatus, desktopShell\)/);
  assert.match(card, /const speech = route === 'browser' \? browserSpeech : localSpeech/);
  assert.ok(
    card.includes('usesLocalEngine(sttStatus)'),
    'local-only copy remains distinct from cloud provider routing',
  );
  assert.ok(
    card.includes('speechNotice'),
    'an engine that cannot run surfaces its reason in the card notice region',
  );
  // A runtime status alone is not enough to say an engine can run: a cloud engine
  // needs a handshake header only the desktop shell can set, so the platform has to
  // be consulted, and a cloud engine in a browser must say so rather than look idle.
  assert.ok(card.includes('isTauri'), 'the platform is part of the answer');
  assert.ok(
    card.includes("selectedProvider?.kind === 'cloud'"),
    'the unreachable-in-a-browser case is named in the notice',
  );
});

test('the microphone is what asks for the local build, and the wait is painted', () => {
  // Building the models costs over a minute on a CPU.  Asking for it on mount made
  // merely opening the console start a build nobody wanted, and while it ran the
  // reader could not switch engines at all -- so the trigger is the microphone, and
  // the wait is painted where it is paid.
  const warmCalls = card.match(/sttWarmUp\(/g) ?? [];
  assert.equal(warmCalls.length, 1, 'the build has exactly one trigger in the card');
  assert.ok(
    card.includes('!needsWarmUp(sttStatus)'),
    'the trigger asks the question itself instead of a mount-time effect',
  );
  // Every outcome still starts the dictation: nothing to build, build finished, and
  // build refused -- the last one so a refused build reports itself through the
  // engine's own error channel instead of leaving a button that does nothing.
  assert.equal(
    (card.match(/speech\.toggle\(\)/g) ?? []).length,
    3,
    'each of the three outcomes reaches the dictation',
  );
  assert.ok(card.includes('正在加载语音模型'), 'the wait is painted, not silent');
  assert.ok(
    card.includes('disabled={!speech.supported || warming}'),
    'a build in flight cannot start a dictation that would block behind it',
  );
  // A minute-long answer must not overwrite a newer choice: the warm-up re-reads the
  // status instead of publishing its own view.
  assert.ok(card.includes('setSttReadToken('), 'the status is re-read after the build');
  assert.equal(
    /view: warm/.test(card),
    false,
    'the warm-up answer must not be published as the current status',
  );
});

test('the engine can be changed from the settings dialog and takes effect live', () => {
  // The setting belongs to the runtime (it decides who transcribes the audio), so
  // the dialog writes it there -- and the composer is told to re-read rather than
  // being left to notice on its own, which would mean a page reload.
  const section = read('components', 'SpeechEngineSection.tsx');
  const dialog = read('components', 'SettingsDialog.tsx');
  const store = read('stores', 'useConsoleStore.ts');

  assert.ok(dialog.includes('<SpeechEngineSection />'), 'the dialog must offer the choice');
  assert.ok(section.includes('client.sttSetEngine('), 'the choice is a runtime write');
  assert.ok(section.includes('bumpSttRevision()'), 'and it signals the composer');
  assert.ok(store.includes('sttRevision'), 'the signal is a store counter');
  assert.ok(card.includes('sttRevision'), 'the card re-reads the status on that signal');
  assert.ok(
    section.includes('status.reason'),
    'an engine that cannot run says why instead of falling back silently',
  );
});
