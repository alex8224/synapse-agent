/**
 * The microphone button's browser lifecycle.
 *
 * This hook owns the one thing the composer must not: a `SpeechRecognition`
 * session, which the browser starts, ends at every pause, and refuses to keep
 * open by itself.  The reader's *intent* (the button's state) is therefore what
 * lives here, not the session: a clean end, a `no-speech` or an `aborted` code
 * restarts it while the intent is still `on`, and only a fatal code or an
 * exhausted restart budget closes the microphone.  All of those decisions are the
 * pure rules in `speechInput.ts`; this module only wires them to the API.
 *
 * Deliberately entry-local, like `codexUsage` and the capture task: it reaches
 * neither the console store nor the runtime, so speech input is a browser-only
 * affordance that adds no wire surface and cannot outlive the card that shows it.
 * A recognized phrase is handed back as plain text -- where it lands in the draft
 * is the composer's decision, not this hook's.
 *
 * Two things leave this hook, and they are not interchangeable.  `onTranscript`
 * carries a *finalized* phrase, which the composer inserts.  `interim` carries
 * the phrase the recognizer is still revising, which the card may only *show*:
 * an interim result is rewritten as the recognizer changes its mind, so text that
 * unstable would fight the caret, the pills and undo if it were inserted, and a
 * reader typing at the same time would have their keystrokes overwritten.
 *
 * The recognition instance is created on the first activation rather than on
 * mount, so merely opening the console never asks for the microphone, and the
 * same instance is reused for every later session.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  SPEECH_LANGUAGE,
  SPEECH_RESTART_DELAY_MS,
  finalTranscripts,
  interimTranscript,
  resolveSpeechRecognition,
  speechErrorMessage,
  speechRestartDecision,
  type SpeechRecognitionConstructor,
  type SpeechRecognitionLike,
  type SpeechScope,
} from './speechInput.ts';

export interface SpeechInputOptions {
  /** One finalized phrase, in the order the recognizer produced it. */
  onTranscript: (phrase: string) => void;
}

export interface SpeechInputController {
  /** Whether this browser offers speech recognition at all. */
  supported: boolean;
  /** Whether the microphone is open (or being opened). */
  listening: boolean;
  /** Why the last attempt failed, or null; cleared when a new attempt starts. */
  error: string | null;
  /**
   * The phrase being revised right now, for the live caption only -- never for
   * insertion.  Empty whenever the recognizer has nothing in progress.
   */
  interim: string;
  /** Open the microphone, or close it if it is already open. */
  toggle: () => void;
}

/** The recognition constructor this browser offers, or null. */
function browserSpeechRecognition(): SpeechRecognitionConstructor | null {
  if (typeof window === 'undefined') return null;
  // The DOM library declares neither spelling, so `window` carries no property
  // the scope type names; the narrowing is explicit here (the one place the
  // global is read) instead of a cast hidden inside the resolver.
  const scope = window as unknown as SpeechScope;
  return resolveSpeechRecognition(scope);
}

/**
 * Start a session on an instance that already carries this hook's handlers.
 *
 * `start()` throws when a session is already live, which is exactly the state
 * that was wanted (a restart racing a slow `onend`), so the throw is absorbed
 * rather than reported: nothing is broken and there is nothing to undo.
 */
function startRecognition(recognition: SpeechRecognitionLike): void {
  try {
    recognition.start();
  } catch {
    // See above: a duplicate start is a no-op, not a failure.
  }
}

export function useSpeechInput({ onTranscript }: SpeechInputOptions): SpeechInputController {
  const [supported] = useState(() => browserSpeechRecognition() !== null);
  const [listening, setListening] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [interim, setInterim] = useState('');

  /** The reader's intent, which outlives the sessions the recognizer ends itself. */
  const intentRef = useRef<'off' | 'on'>('off');
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);
  /** Restarts since the last recognized phrase; the budget against a hot loop. */
  const restartsRef = useRef(0);
  /** The code that ended the last session, read back by `onend`. */
  const lastErrorRef = useRef<string | null>(null);
  const restartTimerRef = useRef<number | null>(null);
  /**
   * The phrase sink, in a ref: the recognition handlers are attached once, so a
   * re-rendered callback must not be captured by the closure that outlives it.
   */
  const transcriptRef = useRef(onTranscript);

  useEffect(() => {
    transcriptRef.current = onTranscript;
  }, [onTranscript]);

  const clearRestartTimer = useCallback(() => {
    if (restartTimerRef.current === null) return;
    window.clearTimeout(restartTimerRef.current);
    restartTimerRef.current = null;
  }, []);

  /** Open a session, creating the instance and its handlers on first use. */
  const startSession = useCallback((Recognition: SpeechRecognitionConstructor) => {
    let recognition = recognitionRef.current;
    if (recognition === null) {
      recognition = new Recognition();
      recognition.lang = SPEECH_LANGUAGE;
      // `continuous` keeps one session open across pauses instead of ending at
      // the first one, and `interimResults` is on so a pause cannot cut a phrase
      // in half.  The interim text itself is never inserted -- see
      // `finalTranscripts`.
      recognition.continuous = true;
      recognition.interimResults = true;
      recognition.onresult = (event) => {
        const phrases = finalTranscripts(event);
        if (phrases.length > 0) {
          restartsRef.current = 0;
          for (const phrase of phrases) transcriptRef.current(phrase);
        }
        // Published last, so a phrase that finalized in this same event is
        // inserted first and the caption never shows text the draft already has.
        setInterim(interimTranscript(event));
      };
      recognition.onerror = (event) => {
        lastErrorRef.current = event.error;
        const message = speechErrorMessage(event.error);
        if (message !== null) setError(message);
      };
      recognition.onend = () => {
        setInterim('');
        const endingError = lastErrorRef.current;
        lastErrorRef.current = null;
        if (intentRef.current === 'off') {
          setListening(false);
          return;
        }
        if (speechRestartDecision(endingError, restartsRef.current) === 'stop') {
          intentRef.current = 'off';
          setListening(false);
          return;
        }
        restartsRef.current += 1;
        // The recognizer ends a session at every pause; the reader's intent, not
        // the session, is what the button toggles, so the microphone is reopened
        // shortly after -- never immediately, so a service that fails at
        // `start()` is not polled in a tight loop.
        restartTimerRef.current = window.setTimeout(() => {
          restartTimerRef.current = null;
          const live = recognitionRef.current;
          if (intentRef.current === 'on' && live !== null) startRecognition(live);
        }, SPEECH_RESTART_DELAY_MS);
      };
      recognitionRef.current = recognition;
    }
    startRecognition(recognition);
  }, []);

  const toggle = useCallback(() => {
    if (intentRef.current === 'on') {
      intentRef.current = 'off';
      clearRestartTimer();
      setListening(false);
      // The caption is a live view of the session, so it goes with the session --
      // `onend` clears it too, but a stopped microphone must not leave a phrase
      // on screen that nothing is going to finalize.
      setInterim('');
      // `stop()` rather than `abort()`: a phrase the reader just finished is
      // still delivered as a final result before the session ends.
      recognitionRef.current?.stop();
      return;
    }
    const Recognition = browserSpeechRecognition();
    if (Recognition === null) return;
    setError(null);
    restartsRef.current = 0;
    lastErrorRef.current = null;
    intentRef.current = 'on';
    setListening(true);
    startSession(Recognition);
  }, [clearRestartTimer, startSession]);

  useEffect(
    () => () => {
      intentRef.current = 'off';
      clearRestartTimer();
      setInterim('');
      const recognition = recognitionRef.current;
      recognitionRef.current = null;
      // `abort()` on the way out: the card is going away, so a trailing phrase
      // has nowhere to land and waiting for it would keep the microphone open.
      recognition?.abort();
    },
    [clearRestartTimer],
  );

  return { supported, listening, error, interim, toggle };
}
