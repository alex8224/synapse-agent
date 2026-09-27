/**
 * Where a dictation's audio goes.
 *
 * The console has exactly two PCM transports and they are not interchangeable:
 *
 * - `runtime` -- the daemon's offline engine, over
 *   `runtime.stt.begin / append / finish / cancel`;
 * - `tauri` -- the desktop shell's own connection to a hosted cloud engine, over the
 *   `stt_cloud_*` commands.  The vendor authenticates the WebSocket handshake with
 *   headers, which a browser cannot set, so this transport only exists in the
 *   desktop app.
 *
 * Both expose the same three verbs, so the capture hook never learns which one it was
 * handed and no capture code is duplicated per engine.  *Which* transport to build is
 * `sttEngine.speechRoute`'s decision; this module only implements the two ends.
 */
import type { SessionRef } from '../../client/types.ts';
import type { SynapseRuntimeClient } from '../../client/SynapseRuntimeClient.ts';
import {
  sttCloudAppend,
  sttCloudBegin,
  sttCloudCancel,
  sttCloudFinish,
} from '../../client/tauriStt.ts';

/** One chunk's worth of progress, in the shape both ends already produce. */
export interface SttAppendOutcome {
  partial: string;
  finalized: string[];
  /** A transport-level failure that belongs to this dictation, or null. */
  error: string | null;
}

/** One open dictation: the three verbs, and nothing about where it runs. */
export interface SttSessionHandle {
  append(dataBase64: string): Promise<SttAppendOutcome>;
  finish(): Promise<{ finalized: string[] }>;
  cancel(): Promise<void>;
}

export interface SttTransport {
  /** Which end this is: the runtime daemon, or the desktop shell. */
  readonly id: 'runtime' | 'tauri';
  /** Why this transport cannot start here, or null when it can. */
  readonly unavailable: string | null;
  /** Open a dictation; `sampleRate` is the rate every later chunk must be encoded at. */
  begin(): Promise<{ sampleRate: number; session: SttSessionHandle }>;
}

/**
 * The daemon's engine, for one session.
 *
 * `unavailable` mirrors the check the capture hook used to make itself: without a
 * connected client or a resolved thread there is no session to dictate into.
 */
export function runtimeSttTransport(
  client: SynapseRuntimeClient,
  session: SessionRef,
): SttTransport {
  const unavailable = session.thread_id === '' ? '尚未连接运行时，语音输入不可用' : null;
  return {
    id: 'runtime',
    unavailable,
    async begin() {
      const started = await client.sttBegin(session);
      return {
        sampleRate: started.sample_rate,
        session: {
          append: (dataBase64) => client.sttAppend(session, dataBase64),
          finish: () => client.sttFinish(session),
          cancel: async () => {
            await client.sttCancel(session);
          },
        },
      };
    },
  };
}

/** The desktop shell's cloud engine.  No credential crosses this boundary. */
export function tauriSttTransport(): SttTransport {
  return {
    id: 'tauri',
    unavailable: null,
    async begin() {
      const started = await sttCloudBegin();
      return {
        sampleRate: started.sampleRate,
        session: {
          append: (dataBase64) => sttCloudAppend(started.sessionId, dataBase64),
          finish: () => sttCloudFinish(started.sessionId),
          cancel: async () => {
            await sttCloudCancel(started.sessionId);
          },
        },
      };
    },
  };
}
