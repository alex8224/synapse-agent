/**
 * The desktop shell's own cloud dictation, over Tauri IPC.
 *
 * The vendor authenticates the WebSocket *handshake* with headers, which a browser's
 * `WebSocket` cannot set -- so the socket lives in the Rust shell and the API key is
 * read there from the user's speech config.  Nothing in this module ever sees a
 * credential: every call carries audio or text only.
 *
 * Outside the desktop shell there is no bridge, so each call reports that plainly
 * instead of surfacing a Tauri IPC error.  The console's routing (`speechRoute`)
 * decides when this module may be used at all; a plain browser never gets here.
 */
import { invoke } from '@tauri-apps/api/core';
import { isTauri } from './tauri.ts';

/** Whether the cloud engine can run here, and why not when it cannot. */
export interface SttCloudStatus {
  available: boolean;
  keyConfigured: boolean;
  reason: string | null;
  sampleRate: number;
}

export interface SttCloudBegin {
  sessionId: number;
  sampleRate: number;
}

export interface SttCloudAppend {
  partial: string;
  finalized: string[];
  error: string | null;
}

export interface SttCloudFinish {
  finalized: string[];
}

export interface SttCloudCancel {
  cancelled: boolean;
}

/** The one reason a cloud dictation cannot start outside the desktop app. */
export const NOT_DESKTOP_REASON = '云端语音识别仅桌面端可用';

export async function sttCloudStatus(): Promise<SttCloudStatus | null> {
  if (!isTauri()) return null;
  return await invoke<SttCloudStatus>('stt_cloud_status');
}

export async function sttCloudBegin(): Promise<SttCloudBegin> {
  if (!isTauri()) throw new Error(NOT_DESKTOP_REASON);
  return await invoke<SttCloudBegin>('stt_cloud_begin');
}

export async function sttCloudAppend(
  sessionId: number,
  dataBase64: string,
): Promise<SttCloudAppend> {
  if (!isTauri()) throw new Error(NOT_DESKTOP_REASON);
  return await invoke<SttCloudAppend>('stt_cloud_append', { sessionId, dataBase64 });
}

export async function sttCloudFinish(sessionId: number): Promise<SttCloudFinish> {
  if (!isTauri()) throw new Error(NOT_DESKTOP_REASON);
  return await invoke<SttCloudFinish>('stt_cloud_finish', { sessionId });
}

export async function sttCloudCancel(sessionId: number): Promise<SttCloudCancel> {
  if (!isTauri()) throw new Error(NOT_DESKTOP_REASON);
  return await invoke<SttCloudCancel>('stt_cloud_cancel', { sessionId });
}
