import React, { useEffect, useState, useCallback } from 'react';
import {
  ArrowClockwise20Regular,
  Checkmark20Regular,
  Copy20Regular,
  Dismiss20Regular,
  Globe20Regular,
  Phone20Regular,
} from '@fluentui/react-icons';
import {
  getRemoteStatus,
  toggleRemote,
  approveRequest,
  rejectRequest,
  revokeDevice,
  type RemoteStatusResponse,
} from '../client/remoteAccess.ts';

export const RemoteAccessSection: React.FC = () => {
  const [status, setStatus] = useState<RemoteStatusResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copiedUrl, setCopiedUrl] = useState<string | null>(null);

  const refreshStatus = useCallback(async () => {
    try {
      const data = await getRemoteStatus();
      setStatus(data);
      setError(null);
    } catch (err: unknown) {
      if (err instanceof Error) {
        setError(err.message);
      } else {
        setError(String(err));
      }
    }
  }, []);

  useEffect(() => {
    void refreshStatus();
    const timer = setInterval(() => {
      void refreshStatus();
    }, 3000);
    return () => clearInterval(timer);
  }, [refreshStatus]);

  const handleToggle = async (enabled: boolean) => {
    try {
      setLoading(true);
      await toggleRemote(enabled);
      await refreshStatus();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  const handleApprove = async (requestId: string) => {
    try {
      await approveRequest(requestId);
      await refreshStatus();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const handleReject = async (requestId: string) => {
    try {
      await rejectRequest(requestId);
      await refreshStatus();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const handleRevoke = async (deviceId: string) => {
    try {
      await revokeDevice(deviceId);
      await refreshStatus();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const copyToClipboard = (text: string) => {
    void navigator.clipboard.writeText(text);
    setCopiedUrl(text);
    setTimeout(() => setCopiedUrl(null), 2000);
  };

  return (
    <div className="space-y-4 text-xs">
      <div className="flex items-center justify-between border-b border-line pb-3">
        <div>
          <span className="font-semibold text-gray-900">允许局域网设备访问</span>
          <p className="mt-0.5 text-gray-500 text-[11px]">
            开启后，同一局域网下的手机、平板或其他电脑可通过浏览器访问并控制本项目。
          </p>
        </div>
        <label className="relative inline-flex cursor-pointer items-center">
          <input
            type="checkbox"
            checked={Boolean(status?.remote_enabled)}
            disabled={loading}
            onChange={(e) => void handleToggle(e.target.checked)}
            className="peer sr-only"
          />
          <div className="h-5 w-9 rounded-full bg-gray-200 peer-checked:bg-accent after:absolute after:left-[2px] after:top-[2px] after:h-4 after:w-4 after:rounded-full after:bg-surface after:transition-all after:content-[''] peer-checked:after:translate-x-full"></div>
        </label>
      </div>

      {error && (
        <div className="rounded border border-red-200 bg-red-50 p-2 font-mono text-[11px] text-red-700">
          {error}
        </div>
      )}

      {status?.remote_enabled && (
        <>
          <div className="rounded border border-line bg-surface p-3 space-y-2">
            <div className="flex items-center justify-between">
              <span className="font-semibold text-gray-800 flex items-center gap-1.5">
                <Globe20Regular className="text-accent-fg" />
                局域网直连地址
              </span>
              <button
                type="button"
                onClick={() => void refreshStatus()}
                className="text-[11px] text-gray-500 hover:text-gray-800 flex items-center gap-1"
              >
                <ArrowClockwise20Regular className="h-3.5 w-3.5" />
                刷新
              </button>
            </div>

            {status.lan_urls.length > 0 ? (
              <div className="space-y-1.5">
                {status.lan_urls.map((url) => (
                  <div
                    key={url}
                    className="flex items-center justify-between rounded border border-line bg-surface px-2.5 py-1.5 font-mono text-[11px]"
                  >
                    <span className="truncate text-gray-900">{url}</span>
                    <button
                      type="button"
                      onClick={() => copyToClipboard(url)}
                      className="ml-2 flex items-center gap-1 text-accent-fg hover:text-blue-500 shrink-0"
                    >
                      {copiedUrl === url ? (
                        <>
                          <Checkmark20Regular className="h-3.5 w-3.5 text-green-600" />
                          <span className="text-green-600">已复制</span>
                        </>
                      ) : (
                        <>
                          <Copy20Regular className="h-3.5 w-3.5" />
                          <span>复制</span>
                        </>
                      )}
                    </button>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-[11px] text-gray-500">
                未检测到活跃局域网网卡，请确保已连接 Wi-Fi 或本地网络。
              </p>
            )}

            {status.pairing_code && (
              <div className="mt-2 pt-2 border-t border-line flex items-center justify-between">
                <span className="text-gray-600">备用配对码：</span>
                <span className="font-mono font-bold tracking-wider text-sm text-accent-fg">
                  {status.pairing_code}
                </span>
              </div>
            )}
          </div>

          {status.pending_requests && status.pending_requests.length > 0 && (
            <div className="rounded border border-amber-300 bg-amber-50 p-3 space-y-2">
              <span className="font-semibold text-amber-900 flex items-center gap-1.5">
                <Phone20Regular className="text-amber-700" />
                待审批的连接申请 ({status.pending_requests.length})
              </span>
              <div className="space-y-2">
                {status.pending_requests.map((req) => (
                  <div
                    key={req.request_id}
                    className="flex items-center justify-between rounded border border-amber-200 bg-surface p-2.5 shadow-card"
                  >
                    <div>
                      <div className="font-medium text-gray-900">{req.name}</div>
                      <div className="text-[11px] text-gray-500 font-mono">
                        IP: {req.ip || '未知'} · 确认码:{' '}
                        <strong className="text-accent-fg">{req.code}</strong>
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      <button
                        type="button"
                        onClick={() => void handleApprove(req.request_id)}
                        className="rounded bg-accent px-2.5 py-1 text-[11px] font-medium text-on-accent hover:bg-accent/90 transition-colors flex items-center gap-1"
                      >
                        <Checkmark20Regular className="h-3.5 w-3.5" />
                        允许 (30天)
                      </button>
                      <button
                        type="button"
                        onClick={() => void handleReject(req.request_id)}
                        className="rounded border border-gray-300 px-2 py-1 text-[11px] text-gray-600 hover:bg-gray-100 transition-colors flex items-center gap-1"
                      >
                        <Dismiss20Regular className="h-3.5 w-3.5" />
                        拒绝
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          <div className="space-y-2">
            <span className="font-semibold text-gray-800">
              已受信任的设备 ({status.trusted_devices?.length || 0})
            </span>
            {status.trusted_devices && status.trusted_devices.length > 0 ? (
              <div className="divide-y divide-line rounded border border-line bg-surface">
                {status.trusted_devices.map((dev) => (
                  <div
                    key={dev.device_id}
                    className="flex items-center justify-between p-2.5 text-[11px]"
                  >
                    <div className="min-w-0 pr-2">
                      <div className="font-medium text-gray-900 truncate">{dev.name}</div>
                      <div className="text-gray-500 font-mono text-[10px]">
                        IP: {dev.ip || '未知'} · 最近活跃:{' '}
                        {dev.last_seen_at
                          ? new Date(dev.last_seen_at * 1000).toLocaleString()
                          : '刚连接'}
                      </div>
                    </div>
                    <button
                      type="button"
                      onClick={() => void handleRevoke(dev.device_id)}
                      className="rounded border border-red-200 px-2 py-0.5 text-red-600 hover:bg-red-50 transition-colors shrink-0"
                    >
                      移除
                    </button>
                  </div>
                ))}
              </div>
            ) : (
              <div className="rounded border border-dashed border-gray-200 p-4 text-center text-gray-400 text-[11px]">
                暂无已信任设备。手机扫码访问后点击申请，在此处点击允许即可信任。
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
};
