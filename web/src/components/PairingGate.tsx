import React, { useEffect, useState, useRef } from 'react';
import { useConsoleStore } from '../stores/useConsoleStore';
import {
  PAIRING_CODE_LENGTH,
  normalizePairingCode,
  sanitizePairingCodeInput,
} from '../client/bootstrap';
import {
  requestAccess,
  checkAccess,
  getOrCreateDeviceId,
  detectDeviceName,
} from '../client/remoteAccess.ts';

/**
 * Pairing gate (phase-5 A1).
 *
 * The console renders this instead of the workspace whenever the browser is not
 * authenticated.  Only a code from the host stderr line can move it to the
 * paired state, and no runtime socket exists before that.
 */
export const PairingGate: React.FC = () => {
  const pairingState = useConsoleStore((s) => s.pairingState);
  const pairingError = useConsoleStore((s) => s.pairingError);
  const submitPairingCode = useConsoleStore((s) => s.submitPairingCode);
  const [code, setCode] = useState('');
  const [mode, setMode] = useState<'code' | 'request'>('code');
  const [pendingReqId, setPendingReqId] = useState<string | null>(null);
  const [pendingCode, setPendingCode] = useState<string | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const [isRequesting, setIsRequesting] = useState(false);
  const checkTimerRef = useRef<number | null>(null);

  const ready = normalizePairingCode(code) !== null;
  const busy = pairingState === 'pairing' || pairingState === 'checking';

  // Check URL parameters for ?code=XXXXXXXX (direct QR code scan)
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const urlCode = params.get('code');
    if (urlCode && pairingState === 'unpaired') {
      const sanitized = sanitizePairingCodeInput(urlCode);
      if (normalizePairingCode(sanitized)) {
        setCode(sanitized);
        void submitPairingCode(sanitized);
      }
    }
  }, [pairingState, submitPairingCode]);

  // Polling check access status when in pending request state
  useEffect(() => {
    if (!pendingReqId) return;
    const poll = async () => {
      try {
        const res = await checkAccess(pendingReqId);
        if (res.status === 'approved') {
          if (checkTimerRef.current) clearInterval(checkTimerRef.current);
          window.location.reload();
        } else if (res.status === 'rejected') {
          if (checkTimerRef.current) clearInterval(checkTimerRef.current);
          setRequestError('电脑端已拒绝本次连接申请。');
          setPendingReqId(null);
        }
      } catch {
        // transient error, continue polling
      }
    };

    checkTimerRef.current = window.setInterval(() => {
      void poll();
    }, 2000);

    return () => {
      if (checkTimerRef.current) clearInterval(checkTimerRef.current);
    };
  }, [pendingReqId]);

  const handleRequestAccess = async () => {
    try {
      setIsRequesting(true);
      setRequestError(null);
      const deviceId = getOrCreateDeviceId();
      const deviceName = detectDeviceName();
      const res = await requestAccess(deviceId, deviceName);
      setPendingReqId(res.request_id);
      setPendingCode(res.code);
      setMode('request');
    } catch (err: unknown) {
      setRequestError(
        err instanceof Error
          ? err.message
          : '发起申请失败，请确认电脑端是否已开启“允许局域网设备访问”'
      );
    } finally {
      setIsRequesting(false);
    }
  };

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    if (!ready || busy) return;
    void submitPairingCode(code);
  };

  return (
    <div className="bg-background text-on-background h-screen w-screen flex items-center justify-center font-sans">
      <form
        onSubmit={handleSubmit}
        className="w-full max-w-md border border-line rounded-card bg-surface p-6 shadow-card mx-4"
      >
        <h1 className="text-sm font-semibold text-gray-900">Synapse Web 控制台配对</h1>
        <p className="mt-2 text-xs text-gray-500 leading-relaxed">
          {pairingState === 'checking'
            ? '正在检查本浏览器的控制台会话…'
            : '请输入 synapse-web-console 启动时在 stderr 打印的 8 位配对码。未完成配对前，控制台不会发起任何业务请求。'}
        </p>

        {mode === 'request' && pendingReqId ? (
          <div className="mt-4 rounded border border-blue-200 bg-blue-50 p-4 text-center">
            <div className="text-xs text-blue-900 font-medium">已向电脑发送连接申请</div>
            <div className="mt-2 text-2xl font-mono font-bold tracking-widest text-accent-fg">
              {pendingCode}
            </div>
            <p className="mt-2 text-[11px] text-blue-500 leading-relaxed">
              请在电脑端 GUI 的【设置 -&gt; 远程访问】中核对上方确认码并点击【允许】。批准后将自动进入控制台。
            </p>
            <div className="mt-4 flex items-center justify-center gap-2">
              <div className="h-2 w-2 rounded-full bg-accent animate-ping" />
              <span className="text-[11px] text-gray-500">等待电脑端授权中…</span>
            </div>
            <button
              type="button"
              onClick={() => {
                setPendingReqId(null);
                setMode('code');
              }}
              className="mt-4 text-[11px] text-gray-500 hover:text-gray-800 underline"
            >
              切换回输入配对码
            </button>
          </div>
        ) : (
          <>
            <div className="mt-4 flex rounded border border-gray-200 p-0.5 bg-gray-50 text-xs">
              <button
                type="button"
                onClick={() => setMode('code')}
                className={`flex-1 py-1 rounded text-center transition-colors ${
                  mode === 'code'
                    ? 'bg-surface font-medium text-gray-900 shadow-card'
                    : 'text-gray-500 hover:text-gray-900'
                }`}
              >
                输入配对码
              </button>
              <button
                type="button"
                onClick={() => void handleRequestAccess()}
                disabled={isRequesting}
                className={`flex-1 py-1 rounded text-center transition-colors ${
                  mode === 'request'
                    ? 'bg-surface font-medium text-gray-900 shadow-card'
                    : 'text-gray-500 hover:text-gray-900'
                }`}
              >
                {isRequesting ? '正在发起…' : '申请电脑授权'}
              </button>
            </div>

            {mode === 'code' && (
              <>
        <label className="mt-4 block text-[11px] font-mono text-gray-500" htmlFor="pairing-code">
          PAIRING CODE
        </label>
        <input
          id="pairing-code"
          name="pairing-code"
          type="text"
          autoComplete="off"
          autoFocus
          spellCheck={false}
          value={code}
          maxLength={PAIRING_CODE_LENGTH}
          onChange={(event) => setCode(sanitizePairingCodeInput(event.target.value))}
          placeholder="XXXXXXXX"
          className="mt-1 w-full px-3 py-2 border border-gray-200 rounded font-mono tracking-[0.3em] text-center text-sm focus:outline-none focus:border-blue-500"
        />

        {pairingError && (
          <div
            role="alert"
            className="mt-3 rounded border border-red-200 bg-red-50 p-2 text-[11px] text-red-700 font-mono break-words"
          >
            {pairingError}
          </div>
        )}

        <button
          type="submit"
          disabled={!ready || busy}
          className="mt-4 w-full h-8 rounded bg-accent text-on-accent text-xs hover:bg-blue-700 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {busy ? '配对中…' : '配对'}
        </button>
              </>
            )}
          </>
        )}

        {requestError && (
          <div
            role="alert"
            className="mt-3 rounded border border-red-200 bg-red-50 p-2 text-[11px] text-red-700 font-mono break-words"
          >
            {requestError}
          </div>
        )}
      </form>
    </div>
  );
};
