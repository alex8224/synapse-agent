/**
 * Offline tests for the speech-input rules.  Pure: no browser, no socket.
 *
 * The microphone itself cannot be exercised here -- the recognizer only exists in
 * a real browser -- so what is pinned is every *decision* the feature makes: which
 * constructor is used, which results are read, how a phrase joins the draft, what
 * a failure says, and when a session is reopened.  Where the browser API is
 * allowed to appear at all is pinned by `speechInputGuard.test.ts`.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  MAX_SPEECH_RESTARTS,
  SPEECH_CAPTION_MAX_CHARS,
  SPEECH_LANGUAGE,
  captionText,
  finalTranscripts,
  interimTranscript,
  isFatalSpeechError,
  resolveSpeechRecognition,
  speechErrorMessage,
  speechRestartDecision,
  spokenTextToInsert,
  type SpeechRecognitionResultEventLike,
  type SpeechRecognitionResultLike,
} from '../src/components/composer/speechInput.ts';

/** One result row, in the shape the recognizer delivers it. */
function result(transcript: string, isFinal: boolean): SpeechRecognitionResultLike {
  return { isFinal, length: 1, 0: { transcript } };
}

/** One result event, in the shape the recognizer delivers it. */
function event(
  resultIndex: number,
  rows: SpeechRecognitionResultLike[],
): SpeechRecognitionResultEventLike {
  return { resultIndex, results: rows };
}

test('the unprefixed constructor wins and the webkit spelling is the fallback', () => {
  const unprefixed = function Unprefixed(): void {};
  const prefixed = function Prefixed(): void {};
  assert.equal(resolveSpeechRecognition({ SpeechRecognition: unprefixed }), unprefixed);
  assert.equal(resolveSpeechRecognition({ webkitSpeechRecognition: prefixed }), prefixed);
  assert.equal(
    resolveSpeechRecognition({ SpeechRecognition: unprefixed, webkitSpeechRecognition: prefixed }),
    unprefixed,
    'Chrome ships both, and the standard name is the one to use',
  );
});

test('a browser without the API reports it instead of throwing', () => {
  assert.equal(resolveSpeechRecognition({}), null);
  assert.equal(resolveSpeechRecognition({ SpeechRecognition: 'nope' }), null);
  assert.equal(resolveSpeechRecognition({ webkitSpeechRecognition: undefined }), null);
});

test('only the results this event finalized are read', () => {
  const rows = [
    result('你好', true),
    result('世', false),
    result('世界', true),
  ];
  assert.deepEqual(finalTranscripts(event(2, rows)), ['世界']);
  assert.deepEqual(
    finalTranscripts(event(0, rows)),
    ['你好', '世界'],
    'an earlier final result is read once, when it is first delivered',
  );
});

test('an interim-only event inserts nothing and blank results are dropped', () => {
  assert.deepEqual(finalTranscripts(event(0, [result('你', false)])), []);
  assert.deepEqual(finalTranscripts(event(0, [result('   ', true)])), []);
  assert.deepEqual(finalTranscripts(event(0, [])), []);
});

test('the caption shows the phrase still being revised', () => {
  assert.equal(interimTranscript(event(0, [result('你', false)])), '你');
  assert.equal(
    interimTranscript(event(1, [result('你好', true), result('世界', false)])),
    '世界',
    'a phrase that just finalized belongs to the draft, not to the caption',
  );
  assert.equal(
    interimTranscript(event(0, [result('你', false), result('你好', false)])),
    '你好',
    'the newest unfinished phrase is the one being spoken',
  );
  assert.equal(interimTranscript(event(0, [result('你好', true)])), '');
  assert.equal(interimTranscript(event(0, [result('  ', false)])), '');
  assert.equal(interimTranscript(event(0, [])), '');
});

test('a long caption keeps its tail and says it was cut', () => {
  assert.equal(captionText('  你好  '), '你好');
  const exact = 'x'.repeat(SPEECH_CAPTION_MAX_CHARS);
  assert.equal(captionText(exact), exact, 'a caption at the bound is not truncated');
  const long = 'x'.repeat(SPEECH_CAPTION_MAX_CHARS + 40);
  const shown = captionText(long);
  assert.equal(shown.length, SPEECH_CAPTION_MAX_CHARS + 1, 'the ellipsis is the only extra mark');
  assert.equal(shown.slice(1), long.slice(-SPEECH_CAPTION_MAX_CHARS), 'the newest words survive');
  assert.ok(shown.startsWith('…'), 'a cut caption says so instead of starting mid-word');
});

test('a phrase joins the draft without inventing or eating a separator', () => {
  // The recognizer emits no space between Chinese characters and writes `，`/`。`
  // itself, so the separator rule is what keeps a Chinese phrase readable and two
  // English words from becoming one.  The rule answers with the *insertion*, so
  // the draft in front of the caret is never rebuilt here.
  assert.equal(spokenTextToInsert('', '你好'), '你好');
  assert.equal(spokenTextToInsert('帮我看一下', '这个函数'), '这个函数');
  assert.equal(spokenTextToInsert('看一下\n', '这个函数'), '这个函数');
  assert.equal(spokenTextToInsert('hello ', 'world'), 'world');
  assert.equal(spokenTextToInsert('hello', 'world'), ' world');
  assert.equal(spokenTextToInsert('hello', '你好'), ' 你好');
  assert.equal(spokenTextToInsert('你好', 'world'), ' world');
  assert.equal(spokenTextToInsert('const x = 1;', 'check this'), ' check this');
  assert.equal(spokenTextToInsert('你好', '，然后呢'), '，然后呢');
  assert.equal(spokenTextToInsert('hello', '。'), '。');
  assert.equal(spokenTextToInsert('@src/a.ts', '看一下'), ' 看一下');
});

test('an empty phrase inserts nothing at all', () => {
  assert.equal(spokenTextToInsert('hello', ''), '');
  assert.equal(spokenTextToInsert('hello', '   '), '');
});

test('a pause is not an error, and a refusal says what to do', () => {
  assert.equal(speechErrorMessage('no-speech'), null, 'a pause is the normal end of a session');
  assert.equal(speechErrorMessage('aborted'), null, 'the console stopped it itself');
  assert.match(speechErrorMessage('not-allowed') ?? '', /麦克风权限/);
  assert.match(speechErrorMessage('service-not-allowed') ?? '', /麦克风权限/);
  assert.match(speechErrorMessage('audio-capture') ?? '', /麦克风设备/);
  assert.match(speechErrorMessage('network') ?? '', /联网/);
  assert.match(speechErrorMessage('language-not-supported') ?? '', new RegExp(SPEECH_LANGUAGE));
  assert.match(speechErrorMessage('weird') ?? '', /weird/, 'an unknown code still names itself');
});

test('a fatal code stops the microphone and a pause reopens it', () => {
  assert.equal(speechRestartDecision(null, 0), 'restart');
  assert.equal(speechRestartDecision('no-speech', 3), 'restart');
  assert.equal(speechRestartDecision('aborted', 0), 'restart');
  for (const code of ['not-allowed', 'service-not-allowed', 'audio-capture', 'network']) {
    assert.equal(isFatalSpeechError(code), true, `${code} must not be retried`);
    assert.equal(speechRestartDecision(code, 0), 'stop');
  }
});

test('the restart budget stops a service that fails at every start', () => {
  assert.equal(speechRestartDecision(null, MAX_SPEECH_RESTARTS - 1), 'restart');
  assert.equal(speechRestartDecision(null, MAX_SPEECH_RESTARTS), 'stop');
});
