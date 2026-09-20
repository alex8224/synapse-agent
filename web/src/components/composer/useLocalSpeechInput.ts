/**
 * The microphone button's *local* lifecycle: capture audio in the browser and
 * stream it to the runtime's own speech-to-text engine.
 *
 * This is the local counterpart of `useSpeechInput`: it exposes the very same
 * controller shape (`supported` / `listening` / `error` / `interim` / `toggle`)
 * and the same options shape (`onTranscript`), so the card can pick one engine
 * and treat both identically.  What differs is where the audio goes: instead of
 * a browser-owned recognition session that ends at every pause, this hook opens
 * the microphone with `getUserMedia`, converts the stream to 16 kHz mono
 * int16 PCM and streams ~600 ms chunks through `runtime.stt.begin / append /
 * finish / cancel` on the console's runtime client.
 *
 * The client comes from `useConsoleStore` (`s.client` + `s.currentSession`),
 * exactly the way `GitExplorer` and `ArtifactsPanel` reach it -- the hook adds
 * no store state of its own, opens no socket, and never reads a global of its
 * own.  The card decides *which* engine runs; this hook only runs when asked.
 *
 * Two sinks, not interchangeable (same as the browser engine): `interim` is the
 * latest provisional `partial`, which the caption may only *show*, and each
 * `finalized` sentence is authoritative and goes to `onTranscript`.  On a
 * deliberate stop the carry-over tail is flushed and `runtime.stt.finish` is
 * called, so the reader's last sentence is corrected and returned; on unmount
 * the dictation is cancelled, because there is nowhere left to deliver it.
 *
 * Degrades instead of throwing: a missing `getUserMedia` / `AudioContext`, or a
 * refused `runtime.stt.begin`, turns into `supported: false` plus a readable
 * `error`.
 */
import { useCallback, useEffect, useRef, useState } from 'react';

import { useConsoleStore } from '../../stores/useConsoleStore';
import type { SessionRef } from '../../client/types.ts';
import type { SynapseRuntimeClient } from '../../client/SynapseRuntimeClient.ts';
import type { SpeechInputController, SpeechInputOptions } from './useSpeechInput.ts';
import {
  createSampleBatcher,
  pcmChunkToBase64,
  resampleTo16k,
  type SampleBatcher,
} from './localSpeechAudio.ts';

/** Samples per `ScriptProcessorNode` callback (~85 ms at 48 kHz). */
const SCRIPT_PROCESSOR_BUFFER = 4096;

/** One live capture: the audio graph, its batcher and its append ordering. */
interface ActiveCapture {
  client: SynapseRuntimeClient;
  session: SessionRef;
  stream: MediaStream;
  context: AudioContext;
  source: MediaStreamAudioSourceNode;
  processor: ScriptProcessorNode;
  gain: GainNode;
  batcher: SampleBatcher;
  /** Serializes `runtime.stt.append` so chunks reach the runtime in order. */
  appendChain: Promise<void>;
  /** Set once teardown starts: no late callback may append into a flush. */
  stopping: boolean;
}

/** Whether this browser can capture audio at all, and why not when it cannot. */
function detectAudioCapture(): { available: boolean; reason: string | null } {
  if (
    typeof navigator === 'undefined' ||
    navigator.mediaDevices === undefined ||
    typeof navigator.mediaDevices.getUserMedia !== 'function'
  ) {
    return { available: false, reason: '当前浏览器不支持麦克风采集（getUserMedia）' };
  }
  if (typeof AudioContext === 'undefined') {
    return { available: false, reason: '当前浏览器不支持 Web Audio（AudioContext）' };
  }
  return { available: true, reason: null };
}

/** A permission refusal is a reader decision, not a missing capability. */
function isPermissionError(error: unknown): boolean {
  const name = (error as { name?: unknown } | null)?.name;
  return name === 'NotAllowedError' || name === 'SecurityError';
}

/** A readable reason for a failed capture, from a DOM error or an RPC error. */
function localSpeechErrorMessage(error: unknown): string {
  const name = (error as { name?: unknown } | null)?.name;
  if (name === 'NotAllowedError') return '麦克风权限被拒绝，无法使用本地语音输入';
  if (name === 'NotFoundError') return '没有可用的麦克风设备';
  if (name === 'NotReadableError') return '麦克风被其他程序占用';
  const message = (error as { message?: unknown } | null)?.message;
  if (typeof message === 'string' && message !== '') return message;
  if (typeof name === 'string' && name !== '') return name;
  return '本地语音输入失败';
}

/**
 * Build the capture `AudioContext`.
 *
 * The requested 16 kHz is a hint most desktops ignore; a build that refuses the
 * option outright falls back to its own default rate, and `resampleTo16k`
 * handles whichever rate comes back.
 */
function createAudioContext(sampleRate: number): AudioContext {
  try {
    return new AudioContext({ sampleRate });
  } catch {
    return new AudioContext();
  }
}

export function useLocalSpeechInput(options: SpeechInputOptions): SpeechInputController {
  const client = useConsoleStore((state) => state.client);
  const projectId = useConsoleStore((state) => state.currentSession.project_id);
  const threadId = useConsoleStore((state) => state.currentSession.thread_id);

  // The capability check runs once (a lazy initializer): a browser does not grow
  // `getUserMedia` while the card is mounted.  It is also the first error.
  const [capability] = useState(detectAudioCapture);
  const [supported, setSupported] = useState(capability.available);
  const [listening, setListening] = useState(false);
  const [error, setError] = useState<string | null>(capability.reason);
  const [interim, setInterim] = useState('');

  const mountedRef = useRef(true);
  const intentRef = useRef<'off' | 'on'>('off');
  const captureRef = useRef<ActiveCapture | null>(null);

  // The sinks and the client are read through refs so the async capture chain
  // never closes over a stale render (the same reason `useSpeechInput` keeps its
  // `onTranscript` in a ref).
  const optionsRef = useRef(options);
  useEffect(() => {
    optionsRef.current = options;
  }, [options]);
  const clientRef = useRef(client);
  useEffect(() => {
    clientRef.current = client;
  }, [client]);
  const sessionRef = useRef<SessionRef>({ project_id: projectId, thread_id: threadId });
  useEffect(() => {
    sessionRef.current = { project_id: projectId, thread_id: threadId };
  }, [projectId, threadId]);

  /** Deliver each authoritative sentence to the one insertion sink. */
  const publishFinalized = useCallback((phrases: readonly string[]) => {
    for (const phrase of phrases) {
      if (phrase.trim() !== '') optionsRef.current.onTranscript(phrase);
    }
  }, []);

  /**
   * Stream one chunk and publish what it produced.
   *
   * A `partial` is only shown while the capture is live; a `finalized` sentence
   * is authoritative and is delivered even during the finishing flush.
   */
  const deliver = useCallback(
    async (capture: ActiveCapture, dataBase64: string): Promise<void> => {
      const result = await capture.client.sttAppend(capture.session, dataBase64);
      if (!capture.stopping) setInterim(result.partial);
      publishFinalized(result.finalized);
    },
    [publishFinalized],
  );

  /**
   * Tear the capture down, then either finish (deliver the last sentence) or
   * cancel (drop it).
   *
   * The audio graph is stopped *before* the flush so no late callback appends
   * into a dictation that is already being closed.  Every step is best-effort:
   * a teardown must never throw, whatever the browser or the socket does.
   */
  const teardown = useCallback(
    async (capture: ActiveCapture, mode: 'finish' | 'cancel'): Promise<void> => {
      if (capture.stopping) return;
      capture.stopping = true;
      try {
        capture.processor.onaudioprocess = null;
      } catch {
        // The node was already detached; nothing to stop.
      }
      try {
        capture.processor.disconnect();
      } catch {
        // Already disconnected.
      }
      try {
        capture.source.disconnect();
      } catch {
        // Already disconnected.
      }
      try {
        capture.gain.disconnect();
      } catch {
        // Already disconnected.
      }
      for (const track of capture.stream.getTracks()) {
        try {
          track.stop();
        } catch {
          // Already stopped.
        }
      }
      try {
        if (mode === 'finish') {
          const tail = capture.batcher.flush();
          if (tail !== null && tail.length > 0) {
            const dataBase64 = pcmChunkToBase64(tail);
            capture.appendChain = capture.appendChain.then(() => deliver(capture, dataBase64));
          }
          await capture.appendChain;
          const result = await capture.client.sttFinish(capture.session);
          publishFinalized(result.finalized);
        } else {
          await capture.appendChain.catch(() => undefined);
          await capture.client.sttCancel(capture.session);
        }
      } catch (err) {
        setError(localSpeechErrorMessage(err));
      } finally {
        try {
          await capture.context.close();
        } catch {
          // Already closed.
        }
      }
    },
    [deliver, publishFinalized],
  );

  /** Report a live failure and drop the capture without delivering anything. */
  const reportError = useCallback(
    (err: unknown) => {
      setError(localSpeechErrorMessage(err));
      intentRef.current = 'off';
      setListening(false);
      setInterim('');
      const capture = captureRef.current;
      if (capture !== null) {
        captureRef.current = null;
        void teardown(capture, 'cancel');
      }
    },
    [teardown],
  );

  /** One Web Audio callback: resample, batch, and queue each full chunk. */
  const handleAudio = useCallback(
    (capture: ActiveCapture, event: AudioProcessingEvent) => {
      if (capture.stopping) return;
      const samples = event.inputBuffer.getChannelData(0);
      const mono16k = resampleTo16k(samples, capture.context.sampleRate);
      for (const chunk of capture.batcher.push(mono16k)) {
        const dataBase64 = pcmChunkToBase64(chunk);
        capture.appendChain = capture.appendChain
          .then(() => deliver(capture, dataBase64))
          .catch((err) => reportError(err));
      }
    },
    [deliver, reportError],
  );

  /** Open the microphone and start streaming to the runtime. */
  const start = useCallback(async (): Promise<void> => {
    const activeClient = clientRef.current;
    const session = sessionRef.current;
    if (activeClient === null || session.thread_id === '') {
      setSupported(false);
      setError('尚未连接运行时，本地语音输入不可用');
      return;
    }
    const capability = detectAudioCapture();
    if (!capability.available) {
      setSupported(false);
      setError(capability.reason);
      return;
    }

    setError(null);
    intentRef.current = 'on';
    setListening(true);
    let stream: MediaStream | null = null;
    let context: AudioContext | null = null;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (intentRef.current !== 'on' || !mountedRef.current) {
        for (const track of stream.getTracks()) track.stop();
        return;
      }
      const begin = await activeClient.sttBegin(session);
      if (intentRef.current !== 'on' || !mountedRef.current) {
        for (const track of stream.getTracks()) track.stop();
        await activeClient.sttCancel(session).catch(() => undefined);
        return;
      }

      context = createAudioContext(begin.sample_rate);
      const source = context.createMediaStreamSource(stream);
      const processor = context.createScriptProcessor(SCRIPT_PROCESSOR_BUFFER, 1, 1);
      // A muted gain keeps the processor's callback firing without routing the
      // microphone back to the speakers.
      const gain = context.createGain();
      gain.gain.value = 0;
      const capture: ActiveCapture = {
        client: activeClient,
        session,
        stream,
        context,
        source,
        processor,
        gain,
        batcher: createSampleBatcher(),
        appendChain: Promise.resolve(),
        stopping: false,
      };
      processor.onaudioprocess = (event) => handleAudio(capture, event);
      source.connect(processor);
      processor.connect(gain);
      gain.connect(context.destination);
      captureRef.current = capture;

      // The reader may have toggled off while `begin` was in flight.
      if (intentRef.current !== 'on' || !mountedRef.current) {
        captureRef.current = null;
        await teardown(capture, 'cancel');
      }
    } catch (err) {
      if (stream !== null) {
        for (const track of stream.getTracks()) track.stop();
      }
      if (context !== null) {
        try {
          await context.close();
        } catch {
          // Already closed.
        }
      }
      // A refused `begin` (or any setup failure that is not a permission
      // decision) means the local engine cannot run here at all.
      if (!isPermissionError(err)) setSupported(false);
      reportError(err);
    }
  }, [handleAudio, reportError, teardown]);

  const toggle = useCallback(() => {
    if (intentRef.current === 'on') {
      intentRef.current = 'off';
      setListening(false);
      setInterim('');
      const capture = captureRef.current;
      if (capture !== null) {
        captureRef.current = null;
        // A deliberate stop flushes and finishes, so the last sentence is
        // corrected and returned rather than dropped.
        void teardown(capture, 'finish');
      }
      return;
    }
    void start();
  }, [start, teardown]);

  useEffect(
    () => {
      // Re-armed on every setup so StrictMode's mount -> unmount -> mount keeps
      // the flag correct; a stale `false` would make the next `start` bail out.
      mountedRef.current = true;
      return () => {
        mountedRef.current = false;
        intentRef.current = 'off';
        const capture = captureRef.current;
        if (capture !== null) {
          captureRef.current = null;
          // The card is going away, so there is nowhere for a trailing sentence
          // to land: cancel instead of finishing.
          void teardown(capture, 'cancel');
        }
      };
    },
    [teardown],
  );

  return { supported, listening, error, interim, toggle };
}
