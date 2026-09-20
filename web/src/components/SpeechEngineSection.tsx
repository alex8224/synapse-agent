/**
 * The speech engine choice, and what the local engine needs to run.
 *
 * The setting lives in the runtime (it decides who transcribes the audio), so this
 * section is a runtime write, not a browser preference: choosing an engine calls
 * `runtime.stt.set_engine`, which persists the choice *and* applies it to the
 * running daemon in the same call.  The composer therefore only needs a signal to
 * re-read its status -- `bumpSttRevision` -- and the switch is live without a page
 * reload or a restart.
 *
 * The section is deliberately honest about the local engine: it shows the reason it
 * cannot run (a missing extra or model set) instead of hiding the option, because
 * "why did nothing change?" is the question a silent fallback would leave behind.
 */
import React, { useCallback, useEffect, useState } from 'react';
import { useConsoleStore } from '../stores/useConsoleStore.ts';
import { needsWarmUp, speechEngineErrorMessage } from './composer/sttEngine.ts';
import type { SttStatusView } from '../runtime-client/types.ts';

const ROW = 'flex items-baseline justify-between gap-3 border-b border-line/60 py-1.5 last:border-0';

const Row: React.FC<{ label: string; value: React.ReactNode }> = ({ label, value }) => (
  <div className={ROW}>
    <span className="shrink-0 text-sm text-gray-600">{label}</span>
    <span className="min-w-0 break-all text-right text-sm text-gray-900">{value}</span>
  </div>
);

export const SpeechEngineSection: React.FC = () => {
  const client = useConsoleStore((state) => state.client);
  const projectId = useConsoleStore((state) => state.currentSession.project_id);
  const threadId = useConsoleStore((state) => state.currentSession.thread_id);
  const bumpSttRevision = useConsoleStore((state) => state.bumpSttRevision);

  const [status, setStatus] = useState<SttStatusView | null>(null);
  const [modelDir, setModelDir] = useState('');
  // Keyed by provider id.  Never seeded from the status: the daemon does not send
  // the key back, so an empty box means "not typing a new one", not "none stored".
  const [keys, setKeys] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const publish = useCallback((view: SttStatusView) => {
    setStatus(view);
    setModelDir(view.model_dir);
  }, []);

  useEffect(() => {
    if (client === null) return;
    let cancelled = false;
    void client.sttStatus({ project_id: projectId, thread_id: threadId }).then(
      (view) => {
        if (!cancelled) publish(view);
      },
      () => {
        if (!cancelled) setStatus(null);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [client, projectId, threadId, publish]);

  /** One write, then the signal that makes the composer re-read. */
  const apply = useCallback(
    (engine: string, dir: string | null) => {
      if (client === null) return;
      setBusy(true);
      setError(null);
      void client.sttSetEngine({ project_id: projectId, thread_id: threadId }, engine, dir).then(
        (view) => {
          publish(view);
          setBusy(false);
          bumpSttRevision();
        },
        (failure: unknown) => {
          setBusy(false);
          setError(speechEngineErrorMessage(failure));
        },
      );
    },
    [client, projectId, threadId, publish, bumpSttRevision],
  );

  const warm = useCallback(() => {
    if (client === null) return;
    setBusy(true);
    setError(null);
    void client.sttWarmUp({ project_id: projectId, thread_id: threadId }).then(
      (view) => {
        publish(view);
        setBusy(false);
      },
      () => {
        setBusy(false);
        setError('加载本地语音模型失败');
      },
    );
  }, [client, projectId, threadId, publish]);

  /** Store one provider's key, then drop it from component state. */
  const saveKey = useCallback(
    (provider: string) => {
      if (client === null) return;
      const value = (keys[provider] ?? '').trim();
      if (value === '') return;
      setBusy(true);
      setError(null);
      void client.sttSetApiKey({ project_id: projectId, thread_id: threadId }, provider, value).then(
        (view) => {
          publish(view);
          // The value is the daemon's now; keeping it in the browser would be the
          // one place this console deliberately does not hold a credential.
          setKeys((current) => ({ ...current, [provider]: '' }));
          setBusy(false);
          bumpSttRevision();
        },
        (failure: unknown) => {
          setBusy(false);
          setError(speechEngineErrorMessage(failure));
        },
      );
    },
    [client, keys, projectId, threadId, publish, bumpSttRevision],
  );

  const engine = status?.engine ?? 'browser';
  const localState =
    status === null
      ? '读取中…'
      : !status.available
        ? (status.reason ?? '不可用')
        : status.loaded
          ? '可用（模型已加载）'
          : '可用（模型未加载）';

  return (
    <section className="mt-4">
      <h2 className="ui-section-label mb-2">语音输入</h2>
      <Row
        label="引擎"
        value={
          <span
            className="flex flex-wrap items-center justify-end gap-1"
            role="group"
            aria-label="语音引擎"
          >
            {/* The choices come from the runtime: adding an engine is a daemon-side
                change, and this screen never has to learn a new id. */}
            {(status?.providers ?? []).map((option) => (
              <button
                key={option.id}
                type="button"
                onClick={() => apply(option.id, modelDir.trim() === '' ? null : modelDir.trim())}
                disabled={busy || client === null}
                aria-pressed={engine === option.id}
                title={option.available ? option.detail : (option.reason ?? option.detail)}
                className={`ui-button border ${
                  engine === option.id ? 'ui-primary border-accent' : 'border-line bg-surface'
                }`}
              >
                {option.label}
              </button>
            ))}
          </span>
        }
      />
      {/* A provider that needs a credential gets a write-only field: the daemon
          reports whether one is configured and never sends the value back, so the
          box starts empty even when a key is already stored. */}
      {(status?.providers ?? [])
        .filter((option) => option.needs_key)
        .map((option) => (
          <Row
            key={option.id}
            label={`${option.label} API Key`}
            value={
              <span className="flex flex-wrap items-center justify-end gap-1">
                <span className="text-xs text-gray-600">
                  {option.key_configured ? '已配置' : '未配置'}
                </span>
                <input
                  type="password"
                  className="ui-field"
                  value={keys[option.id] ?? ''}
                  placeholder={option.key_configured ? '••••••（留空表示不改）' : '粘贴 API Key'}
                  aria-label={`${option.label} API Key`}
                  autoComplete="off"
                  onChange={(event) =>
                    setKeys((current) => ({ ...current, [option.id]: event.target.value }))
                  }
                />
                <button
                  type="button"
                  className="ui-button border border-line bg-surface"
                  disabled={busy || client === null || (keys[option.id] ?? '') === ''}
                  onClick={() => saveKey(option.id)}
                >
                  保存
                </button>
              </span>
            }
          />
        ))}
      <Row label="本地引擎" value={localState} />
      <Row
        label="模型目录"
        value={
          <span className="flex flex-wrap items-center justify-end gap-1">
            <input
              type="text"
              className="ui-field"
              value={modelDir}
              placeholder="默认 ~/.synapse/stt/models"
              aria-label="本地语音模型目录"
              onChange={(event) => setModelDir(event.target.value)}
              onBlur={() => {
                if (engine === 'local' && modelDir.trim() !== (status?.model_dir ?? '')) {
                  apply('local', modelDir.trim() === '' ? null : modelDir.trim());
                }
              }}
            />
            {status !== null && needsWarmUp(status) && (
              <button type="button" className="ui-button border border-line bg-surface" disabled={busy} onClick={warm}>
                加载模型
              </button>
            )}
          </span>
        }
      />
      <p className="mt-1 text-xs text-gray-600">
        本地引擎完全离线、不联网；首次使用需加载模型（约 1 分钟），之后常驻。
      </p>
      {error !== null && (
        <p role="alert" className="mt-1 text-xs text-red-700">
          {error}
        </p>
      )}
    </section>
  );
};
