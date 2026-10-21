import { CONSOLE_CSRF_HEADER, CONSOLE_CSRF_HEADER_VALUE } from './bootstrap.ts';

export interface TrustedDevice {
  device_id: string;
  name: string;
  ip: string;
  user_agent: string;
  created_at: number;
  expires_at: number;
  last_seen_at: number;
}

export interface PendingRemoteRequest {
  request_id: string;
  device_id: string;
  name: string;
  code: string;
  ip: string;
  user_agent: string;
  created_at: number;
  expires_at: number;
  status: 'pending' | 'approved' | 'rejected';
}

export interface RemoteStatusResponse {
  allow_remote: boolean;
  remote_enabled: boolean;
  bound_port: number;
  lan_ips: string[];
  lan_urls: string[];
  pairing_code: string | null;
  trusted_devices: TrustedDevice[];
  pending_requests: PendingRemoteRequest[];
}

export interface RequestAccessResponse {
  request_id: string;
  device_id: string;
  name: string;
  code: string;
  status: string;
  expires_at: number;
}

export interface CheckAccessResponse {
  status: 'pending' | 'approved' | 'rejected';
  device_token?: string;
  code?: string;
  expires_at?: number;
  project?: {
    project_id: string;
    workspace_path: string;
    workspace_name: string | null;
    git_branch: string | null;
  };
}

const defaultHeaders = {
  [CONSOLE_CSRF_HEADER]: CONSOLE_CSRF_HEADER_VALUE,
  'Content-Type': 'application/json',
};

export async function getRemoteStatus(): Promise<RemoteStatusResponse> {
  const resp = await fetch('/api/remote/status', {
    method: 'GET',
    headers: { 'Cache-Control': 'no-store' },
  });
  if (!resp.ok) {
    throw new Error(`Failed to fetch remote status: HTTP ${resp.status}`);
  }
  return resp.json();
}

export async function toggleRemote(enabled: boolean): Promise<boolean> {
  const resp = await fetch('/api/remote/toggle', {
    method: 'POST',
    headers: defaultHeaders,
    body: JSON.stringify({ enabled }),
  });
  if (!resp.ok) {
    throw new Error(`Failed to toggle remote: HTTP ${resp.status}`);
  }
  const data = await resp.json();
  return Boolean(data.remote_enabled);
}

export async function requestAccess(deviceId: string, name: string): Promise<RequestAccessResponse> {
  const resp = await fetch('/api/remote/request-access', {
    method: 'POST',
    headers: defaultHeaders,
    body: JSON.stringify({ device_id: deviceId, name }),
  });
  if (!resp.ok) {
    throw new Error(`Failed to request access: HTTP ${resp.status}`);
  }
  return resp.json();
}

export async function checkAccess(requestId: string): Promise<CheckAccessResponse> {
  const resp = await fetch(`/api/remote/check-access?request_id=${encodeURIComponent(requestId)}`, {
    method: 'GET',
    headers: { 'Cache-Control': 'no-store' },
  });
  if (!resp.ok) {
    throw new Error(`Failed to check access: HTTP ${resp.status}`);
  }
  return resp.json();
}

export async function approveRequest(requestId: string): Promise<void> {
  const resp = await fetch('/api/remote/approve', {
    method: 'POST',
    headers: defaultHeaders,
    body: JSON.stringify({ request_id: requestId }),
  });
  if (!resp.ok) {
    throw new Error(`Failed to approve request: HTTP ${resp.status}`);
  }
}

export async function rejectRequest(requestId: string): Promise<void> {
  const resp = await fetch('/api/remote/reject', {
    method: 'POST',
    headers: defaultHeaders,
    body: JSON.stringify({ request_id: requestId }),
  });
  if (!resp.ok) {
    throw new Error(`Failed to reject request: HTTP ${resp.status}`);
  }
}

export async function revokeDevice(deviceId: string): Promise<void> {
  const resp = await fetch('/api/remote/revoke-device', {
    method: 'POST',
    headers: defaultHeaders,
    body: JSON.stringify({ device_id: deviceId }),
  });
  if (!resp.ok) {
    throw new Error(`Failed to revoke device: HTTP ${resp.status}`);
  }
}

export function getOrCreateDeviceId(): string {
  const key = 'synapse_client_device_id';
  let id = localStorage.getItem(key);
  if (!id) {
    id = 'dev_' + Math.random().toString(36).substring(2, 10) + Date.now().toString(36);
    localStorage.setItem(key, id);
  }
  return id;
}

export function detectDeviceName(): string {
  const ua = navigator.userAgent;
  if (/iPhone/i.test(ua)) return 'iPhone (Safari)';
  if (/iPad/i.test(ua)) return 'iPad (Safari)';
  if (/Android/i.test(ua)) return 'Android Device';
  if (/Macintosh/i.test(ua)) return 'Mac Web Browser';
  if (/Windows/i.test(ua)) return 'Windows Web Browser';
  if (/Linux/i.test(ua)) return 'Linux Web Browser';
  return 'Remote Web Client';
}
