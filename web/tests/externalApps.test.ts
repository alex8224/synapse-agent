/**
 * Contract tests for the host's external-program surface: the two strict decoders,
 * the pure presentation helpers, and the two `SynapseRuntimeClient` methods that
 * carry them over a real (injected) socket.
 *
 * The rules pinned here are the ones that keep the menu honest:
 *
 *  - only the declared keys are accepted, so an unexpected key (a path, a command
 *    line) never reaches the UI as if the host had sent it;
 *  - the launch request carries an application *id* the host published and a
 *    workspace-relative path, and an absent id stays absent instead of being
 *    invented by the console;
 *  - "recommended" means "the application's own table claims this extension", so the
 *    menu cannot promote an application the host did not claim;
 *  - a named refusal is worded from its `service_code`.
 *
 * No host, daemon or process is involved: the transport is a fake socket.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { SynapseRuntimeClient } from '../src/runtime-client/SynapseRuntimeClient.ts';
import type { SocketLike } from '../src/runtime-client/SynapseRuntimeClient.ts';
import {
  MalformedExternalAppsError,
  SYSTEM_APP_ID,
  describeOpenExternalFailure,
  extensionOf,
  otherApps,
  parseExternalAppPage,
  parseOpenExternalResult,
  preferredApp,
  recommendedApps,
} from '../src/runtime-client/externalApps.ts';
import type { ExternalAppView } from '../src/runtime-client/externalApps.ts';

const SESSION = { project_id: 'proj', thread_id: 'thr' };

const VSCODE = {
  id: 'vscode',
  name: 'Visual Studio Code',
  short_name: 'VS Code',
  kind: 'editor',
  extensions: ['tsx', 'ts', 'json'],
  icon: { kind: 'glyph', value: 'vscode' },
  is_system_default: false,
  available: true,
};

const NOTEPAD = {
  id: 'notepad',
  name: '记事本（Windows）',
  short_name: '记事本',
  kind: 'viewer',
  extensions: ['txt', 'log'],
  icon: { kind: 'glyph', value: 'notepad' },
  is_system_default: false,
  available: true,
};

const SYSTEM = {
  id: SYSTEM_APP_ID,
  name: '系统默认应用',
  short_name: '系统默认',
  kind: 'system',
  extensions: [],
  icon: { kind: 'glyph', value: 'system' },
  is_system_default: true,
  available: true,
};

const PAGE = { apps: [VSCODE, NOTEPAD, SYSTEM], truncated: false };

/** The decoded views, so the helpers are exercised on the shapes the UI really sees. */
const VIEWS: ExternalAppView[] = parseExternalAppPage(PAGE).apps;

// --- the decoders -----------------------------------------------------------

test('a declared catalog decodes field by field', () => {
  const page = parseExternalAppPage(PAGE);
  assert.equal(page.truncated, false);
  assert.equal(page.apps.length, 3);
  assert.equal(page.apps[0].shortName, 'VS Code');
  assert.deepEqual(page.apps[0].extensions, ['tsx', 'ts', 'json']);
  assert.deepEqual(page.apps[0].icon, { kind: 'glyph', value: 'vscode' });
  assert.equal(page.apps[2].isSystemDefault, true);
});

test('an unexpected key is rejected instead of forwarded', () => {
  for (const bad of [
    { ...PAGE, extra: 1 },
    { apps: [{ ...VSCODE, executable: 'C:/tools/code.exe' }], truncated: false },
    { apps: [{ ...VSCODE, icon: { kind: 'glyph', value: 'vscode', path: 'C:/x' } }], truncated: false },
  ]) {
    assert.throws(() => parseExternalAppPage(bad), MalformedExternalAppsError);
  }
});

test('a wrongly typed catalog is rejected', () => {
  for (const bad of [
    null,
    [],
    { apps: 'nope', truncated: false },
    { apps: [], truncated: 'no' },
    { apps: [{ ...VSCODE, kind: 'browser' }], truncated: false },
    { apps: [{ ...VSCODE, extensions: [1] }], truncated: false },
    { apps: [{ ...VSCODE, icon: { kind: 'file', value: 'x' } }], truncated: false },
  ]) {
    assert.throws(() => parseExternalAppPage(bad), MalformedExternalAppsError);
  }
});

test('a launch result decodes field by field', () => {
  assert.deepEqual(parseOpenExternalResult({ opened: true, app_id: 'vscode', mode: 'open' }), {
    opened: true,
    appId: 'vscode',
    mode: 'open',
  });
  assert.equal(parseOpenExternalResult({ opened: true, app_id: 'x', mode: 'reveal' }).mode, 'reveal');
  for (const bad of [
    { opened: true, app_id: 'x' },
    { opened: true, app_id: 'x', mode: 'execute' },
    { opened: 'yes', app_id: 'x', mode: 'open' },
  ]) {
    assert.throws(() => parseOpenExternalResult(bad), MalformedExternalAppsError);
  }
});

// --- the presentation helpers ----------------------------------------------

test('the extension is read from the file name only', () => {
  assert.equal(extensionOf('web/src/components/GitExplorer.tsx'), '.tsx');
  assert.equal(extensionOf('Makefile'), '');
  assert.equal(extensionOf('src/.gitignore'), '');
  assert.equal(extensionOf('archive.tar.gz'), '.gz');
});

test('recommended applications are the ones claiming the extension', () => {
  const apps = VIEWS;
  assert.deepEqual(
    recommendedApps(apps, 'src/app.tsx').map((app) => app.id),
    ['vscode'],
  );
  // The system association is never "recommended": it is always offered separately.
  assert.deepEqual(recommendedApps(apps, 'notes.txt').map((app) => app.id), ['notepad']);
  assert.deepEqual(recommendedApps(apps, 'README'), []);
});

test('every application appears exactly once across the two groups', () => {
  const apps = VIEWS;
  const path = 'src/app.tsx';
  const ids = [...recommendedApps(apps, path), ...otherApps(apps, path)].map((app) => app.id);
  assert.deepEqual([...ids].sort(), ['notepad', SYSTEM_APP_ID, 'vscode']);
  assert.equal(new Set(ids).size, ids.length);
});

test('the trigger prefers the remembered application, then the claim, then the system one', () => {
  const apps = VIEWS;
  assert.equal(preferredApp(apps, 'src/app.tsx', null)?.id, 'vscode');
  assert.equal(preferredApp(apps, 'src/app.tsx', 'notepad')?.id, 'notepad');
  assert.equal(preferredApp(apps, 'src/app.tsx', 'gone')?.id, 'vscode');
  assert.equal(preferredApp(apps, 'README', null)?.id, SYSTEM_APP_ID);
  assert.equal(preferredApp([], 'README', null), null);
});

test('the application used last wins only while it claims the extension', () => {
  const apps = VIEWS;
  // A .txt reader who just used Notepad keeps seeing Notepad.
  assert.equal(preferredApp(apps, 'notes.txt', null, 'notepad')?.id, 'notepad');
  // The reader's explicit choice for the extension still beats it.
  assert.equal(preferredApp(apps, 'notes.txt', 'vscode', 'notepad')?.id, 'vscode');
  // Notepad does not claim .tsx, so it must not follow the reader into this file.
  assert.equal(preferredApp(apps, 'src/app.tsx', null, 'notepad')?.id, 'vscode');
  // A remembered application that is gone falls back to the claim, not to a crash.
  assert.equal(preferredApp(apps, 'src/app.tsx', 'gone', 'also-gone')?.id, 'vscode');
});

test('a named refusal is worded from its service code', () => {
  assert.match(describeOpenExternalFailure('external_app_file_missing', '', 'VS Code'), /已不在工作区/);
  assert.match(describeOpenExternalFailure('external_app_outside_workspace', '', 'VS Code'), /不在工作区内/);
  assert.match(describeOpenExternalFailure('external_app_unknown', '', 'Zed'), /Zed/);
  assert.match(describeOpenExternalFailure('external_app_launch_failed', '', 'Zed'), /Zed/);
  assert.match(describeOpenExternalFailure('permission_denied', '', 'Zed'), /权限/);
  assert.equal(describeOpenExternalFailure('something_else', 'host said no', 'Zed'), 'host said no');
  assert.match(describeOpenExternalFailure(undefined, '', 'Zed'), /Zed/);
});

// --- the transport ----------------------------------------------------------

interface SentFrame {
  id: number;
  method: string;
  params: any;
}

const CAPABILITIES = {
  legacy_v1: true,
  raw_cursor: true,
  watch_resume: true,
  approval_resume: true,
};

/** Fake transport: answers the handshake, then one scripted reply per method. */
class AppsSocket implements SocketLike {
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: any }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: ((ev?: { code?: number; reason?: string }) => void) | null = null;
  sent: SentFrame[] = [];
  replies: Record<string, unknown> = {};

  send(data: string): void {
    const request = JSON.parse(data) as SentFrame;
    this.sent.push(request);
    this.push({
      jsonrpc: '2.0',
      id: request.id,
      meta: { wire_version: '1' },
      result:
        request.method === 'runtime.protocol.negotiate'
          ? { wire_version: '1', supported_versions: ['1'], capabilities: CAPABILITIES }
          : this.replies[request.method],
    });
  }

  push(frame: unknown): void {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }

  serverOpen(): void {
    this.readyState = 1;
    this.onopen?.();
  }

  close(): void {
    this.readyState = 3;
  }
}

async function openClient(replies: Record<string, unknown>): Promise<{
  client: SynapseRuntimeClient;
  socket: AppsSocket;
}> {
  const socket = new AppsSocket();
  socket.replies = replies;
  const client = new SynapseRuntimeClient({ url: 'ws://core', socketFactory: () => socket });
  const connecting = client.connect();
  socket.serverOpen();
  await connecting;
  return { client, socket };
}

test('the catalog read sends no params and decodes the page', async () => {
  const { client, socket } = await openClient({ 'runtime.apps.list': PAGE });
  const page = await client.listExternalApps();

  assert.equal(page.apps.length, 3);
  const frame = socket.sent.find((entry) => entry.method === 'runtime.apps.list');
  assert.ok(frame, 'the catalog read must be sent');
  assert.deepEqual(frame.params, {});
});

test('a launch sends exactly the declared params and omits an absent app id', async () => {
  const { client, socket } = await openClient({
    'runtime.workspace.open_external': { opened: true, app_id: 'vscode', mode: 'open' },
  });

  const result = await client.openExternal({ session: SESSION, path: 'src/app.tsx', appId: 'vscode' });
  assert.deepEqual(result, { opened: true, appId: 'vscode', mode: 'open' });

  const frames = socket.sent.filter((entry) => entry.method === 'runtime.workspace.open_external');
  assert.equal(frames.length, 1);
  assert.deepEqual(frames[0].params, {
    session: SESSION,
    path: 'src/app.tsx',
    app_id: 'vscode',
  });
  assert.equal('mode' in frames[0].params, false, 'an absent mode must stay absent');

  await client.openExternal({ session: SESSION, path: 'a.txt', mode: 'reveal' });
  const second = socket.sent.filter((entry) => entry.method === 'runtime.workspace.open_external')[1];
  assert.deepEqual(second.params, { session: SESSION, path: 'a.txt', mode: 'reveal' });
  assert.equal('app_id' in second.params, false);
});

test('a malformed reply is reported as a malformed payload, not as a launch', async () => {
  const { client } = await openClient({
    'runtime.workspace.open_external': { opened: true, app_id: 'vscode' },
  });
  await assert.rejects(
    () => client.openExternal({ session: SESSION, path: 'a.txt' }),
    MalformedExternalAppsError,
  );
});
