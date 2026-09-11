import React, { useState } from 'react';
import { useConsoleStore } from '../stores/useConsoleStore';
import {
  PAIRING_CODE_LENGTH,
  normalizePairingCode,
  sanitizePairingCodeInput,
} from '../client/bootstrap';

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

  const ready = normalizePairingCode(code) !== null;
  const busy = pairingState === 'pairing' || pairingState === 'checking';

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    if (!ready || busy) return;
    void submitPairingCode(code);
  };

  return (
    <div className="bg-background text-on-background h-screen w-screen flex items-center justify-center font-sans">
      <form
        onSubmit={handleSubmit}
        className="w-full max-w-md border border-[#e5e7eb] rounded-lg bg-white p-6 shadow-sm"
      >
        <h1 className="text-sm font-semibold text-gray-900">Synapse Web 控制台配对</h1>
        <p className="mt-2 text-xs text-gray-500 leading-relaxed">
          {pairingState === 'checking'
            ? '正在检查本浏览器的控制台会话…'
            : '请输入 synapse-web-console 启动时在 stderr 打印的 8 位配对码。未完成配对前，控制台不会发起任何业务请求。'}
        </p>

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
          className="mt-4 w-full h-8 rounded bg-[#2563eb] text-white text-xs hover:bg-blue-700 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {busy ? '配对中…' : '配对'}
        </button>
      </form>
    </div>
  );
};
