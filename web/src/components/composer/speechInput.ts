/**
 * Speech-to-text input for the composer: the pure half.
 *
 * The microphone button recognizes speech with the browser's own Web Speech API
 * (Chrome and Edge; free, no API key, at the price of sending the audio to the
 * browser vendor's service).  This module holds every rule that decides what a
 * recognized phrase *does*, and it is deliberately free of React, of the DOM and
 * of the console store, so `node --test` can exercise it:
 *
 * - **`resolveSpeechRecognition`** takes the scope as a parameter instead of
 *   reading `window`, so the presence or absence of either spelling is testable,
 *   and no other module in `src/**` ever names the constructor.
 * - **`spokenTextToInsert`** is the one rule that decides the separator.  The
 *   recognizer writes no space between Chinese characters and emits `，`/`。`
 *   itself, so a space is inserted only around ASCII words and never in front of
 *   closing punctuation -- otherwise a Chinese phrase would arrive as `， 好的`
 *   and two English ones as `helloworld`.
 * - **`finalTranscripts`** reads only the results the browser has *finalized*
 *   and only from `resultIndex` (a continuous session keeps every earlier result
 *   in the list, so re-reading them would duplicate the draft).
 * - **`interimTranscript`** / **`captionText`** are what the reader sees *while*
 *   speaking: the recognizer's not-yet-final phrase, bounded so a long sentence
 *   cannot grow the card without limit.  The caption is deliberately the only
 *   place interim text appears -- see `useSpeechInput`.
 * - **`speechErrorMessage`** maps the browser's error codes onto what the reader
 *   can act on, and answers `null` for the codes that are not worth a notice.
 * - **`speechRestartDecision`** owns the "a session ends at every pause" rule:
 *   the reader's *intent* keeps the microphone open, not the session, and the
 *   restart budget keeps a service that fails instantly from becoming a hot loop.
 */

/** The recognition language.  Chinese first; mixed input is transcribed as Chinese. */
export const SPEECH_LANGUAGE = 'zh-CN';

/** Consecutive restarts without a single recognized phrase before giving up. */
export const MAX_SPEECH_RESTARTS = 8;

/** Delay before a restarted session, so a failing service is not polled hot. */
export const SPEECH_RESTART_DELAY_MS = 250;

/**
 * The Web Speech API's shapes, written out minimally.
 *
 * The DOM library does not declare `SpeechRecognition` (it is still absent in
 * TypeScript's own `lib.dom.d.ts`), and this module needs exactly the fields it
 * reads -- nothing here is a copy of the vendor's full interface.  The names end
 * in `Like` to say so.
 */
export interface SpeechRecognitionAlternativeLike {
  readonly transcript: string;
}

export interface SpeechRecognitionResultLike {
  readonly isFinal: boolean;
  readonly length: number;
  readonly [index: number]: SpeechRecognitionAlternativeLike | undefined;
}

export interface SpeechRecognitionResultListLike {
  readonly length: number;
  readonly [index: number]: SpeechRecognitionResultLike | undefined;
}

export interface SpeechRecognitionResultEventLike {
  /** The first result this event changed; everything before it was delivered. */
  readonly resultIndex: number;
  readonly results: SpeechRecognitionResultListLike;
}

export interface SpeechRecognitionErrorEventLike {
  /** A Web Speech API error code (`not-allowed`, `no-speech`, ...). */
  readonly error: string;
}

export interface SpeechRecognitionLike {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  start: () => void;
  stop: () => void;
  abort: () => void;
  onresult: ((event: SpeechRecognitionResultEventLike) => void) | null;
  onerror: ((event: SpeechRecognitionErrorEventLike) => void) | null;
  onend: (() => void) | null;
}

export type SpeechRecognitionConstructor = new () => SpeechRecognitionLike;

/** The global object, narrowed to the two spellings the API is shipped under. */
export interface SpeechScope {
  SpeechRecognition?: unknown;
  webkitSpeechRecognition?: unknown;
}

/**
 * The recognition constructor this scope offers, or null when it offers none.
 *
 * Chrome ships both spellings today; older builds only `webkitSpeechRecognition`,
 * and Firefox ships neither.  Nothing else in `src/**` may name these globals:
 * one module owning the lookup is what keeps the vendor prefix out of the UI.
 */
export function resolveSpeechRecognition(scope: SpeechScope): SpeechRecognitionConstructor | null {
  const candidate = scope.SpeechRecognition ?? scope.webkitSpeechRecognition;
  return typeof candidate === 'function' ? (candidate as SpeechRecognitionConstructor) : null;
}

/**
 * The finalized phrases one result event carries, in order.
 *
 * Interim results are skipped on purpose: the console inserts a phrase into the
 * draft the reader is editing, and text that is rewritten on every keystroke of
 * the recognizer cannot be edited, cannot sit beside a pill, and cannot be
 * undone as one unit.  Whitespace-only results are dropped rather than inserted
 * as a blank line.
 */
export function finalTranscripts(event: SpeechRecognitionResultEventLike): string[] {
  const phrases: string[] = [];
  const results = event.results;
  const from = Math.max(0, Math.min(event.resultIndex, results.length));
  for (let index = from; index < results.length; index += 1) {
    const result = results[index];
    if (result === undefined || !result.isFinal) continue;
    const transcript = (result[0]?.transcript ?? '').trim();
    if (transcript !== '') phrases.push(transcript);
  }
  return phrases;
}

/**
 * The phrase the recognizer is still revising, or '' when it has none.
 *
 * An event can carry several results at once (a phrase that just finalized
 * followed by the next one in progress), so the *newest* unfinished one is the
 * caption: the earlier ones are either already in the draft or already gone.
 * This is the only reader of interim results in the feature.
 */
export function interimTranscript(event: SpeechRecognitionResultEventLike): string {
  const results = event.results;
  const from = Math.max(0, Math.min(event.resultIndex, results.length));
  let interim = '';
  for (let index = from; index < results.length; index += 1) {
    const result = results[index];
    if (result === undefined || result.isFinal) continue;
    const transcript = (result[0]?.transcript ?? '').trim();
    if (transcript !== '') interim = transcript;
  }
  return interim;
}

/** Longest caption kept; beyond this the oldest words are dropped. */
export const SPEECH_CAPTION_MAX_CHARS = 120;

/**
 * The caption as it is painted.
 *
 * Truncation keeps the *tail*: a dictation caption is read at its end, and the
 * words at the front are the ones the recognizer is most likely to have already
 * revised.  The leading ellipsis says that something was cut rather than letting
 * a sentence appear to start mid-word.
 */
export function captionText(interim: string): string {
  const text = interim.trim();
  if (text.length <= SPEECH_CAPTION_MAX_CHARS) return text;
  return `…${text.slice(-SPEECH_CAPTION_MAX_CHARS)}`;
}

const ASCII_WORD = /[A-Za-z0-9]/;

/**
 * Punctuation the recognizer emits attached to its phrase, so a space must never
 * be inserted in front of it (`hello` + `，` is not `hello ，`).
 */
const CLOSING_PUNCTUATION = /[，。！？；：、）】》」』”’…,.!?;:)\]}]/;

/**
 * The exact text to insert for one phrase, given what precedes the caret.
 *
 * `before` is the text immediately before the insertion point (not the whole
 * draft: a phrase typed into the middle of a paragraph must be spaced against
 * *its* neighbours).  The rule is symmetric and only ever adds one leading space:
 * a Chinese phrase follows the draft directly, an ASCII word is separated from
 * an ASCII word, and a phrase that starts with closing punctuation gets nothing.
 * The returned value is what goes into the editor -- the draft itself is never
 * rebuilt here, so a phrase cannot disturb text the reader already wrote.
 */
export function spokenTextToInsert(before: string, transcript: string): string {
  const phrase = transcript.trim();
  if (phrase === '') return '';
  const tail = before.slice(-1);
  if (tail === '' || /\s/.test(tail)) return phrase;
  const head = phrase.slice(0, 1);
  if (CLOSING_PUNCTUATION.test(head)) return phrase;
  return ASCII_WORD.test(tail) || ASCII_WORD.test(head) ? ` ${phrase}` : phrase;
}

/**
 * Why the microphone could not be used, or null when there is nothing to report.
 *
 * `no-speech` and `aborted` are silent: the first is the normal end of a pause
 * (the session is restarted) and the second is the console's own `stop()`.  Every
 * other code names the situation and what to do about it, because a silent
 * failure on a microphone button reads as a broken button.
 */
export function speechErrorMessage(code: string): string | null {
  switch (code) {
    case 'no-speech':
    case 'aborted':
      return null;
    case 'not-allowed':
    case 'service-not-allowed':
      return '麦克风权限被拒绝：请在浏览器的站点设置里允许麦克风后重试';
    case 'audio-capture':
      return '没有可用的麦克风设备：请检查系统输入设备后重试';
    case 'network':
      return '语音识别服务连接失败：浏览器识别需要联网，请检查网络后重试';
    case 'language-not-supported':
      return `当前浏览器不支持 ${SPEECH_LANGUAGE} 的语音识别`;
    default:
      return `语音识别失败（${code}）`;
  }
}

/**
 * Codes that must not be retried.
 *
 * A denied permission, a missing device, a broken service or an unsupported
 * language will fail again identically; restarting would only loop.  Everything
 * else (`no-speech`, `aborted`, and a clean end) is the ordinary end of a
 * session and is restarted while the reader still wants the microphone.
 */
const FATAL_SPEECH_ERRORS = new Set([
  'not-allowed',
  'service-not-allowed',
  'audio-capture',
  'network',
  'language-not-supported',
]);

/** Whether a session that failed with this code should stay closed. */
export function isFatalSpeechError(code: string): boolean {
  return FATAL_SPEECH_ERRORS.has(code);
}

/** Whether the recognizer should be started again after a session ended. */
export type SpeechRestartDecision = 'restart' | 'stop';

/**
 * Whether to open a new session after the previous one ended.
 *
 * `error` is the code of the failure that ended it, or null for a clean end.
 * `consecutiveRestarts` counts the restarts since the last recognized phrase: the
 * budget is what stops a recognizer that fails on `start()` from spinning.
 */
export function speechRestartDecision(
  error: string | null,
  consecutiveRestarts: number,
): SpeechRestartDecision {
  if (error !== null && isFatalSpeechError(error)) return 'stop';
  return consecutiveRestarts >= MAX_SPEECH_RESTARTS ? 'stop' : 'restart';
}
