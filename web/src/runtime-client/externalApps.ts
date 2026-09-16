/**
 * Strict decoders and pure helpers for the host's external-program surface
 * (`runtime.apps.list` / `runtime.workspace.open_external`).
 *
 * Same rule as the git and artifact decoders: only the declared keys are accepted,
 * every value is type-checked, and anything else raises instead of reaching the UI
 * as a half-shaped object.  Pure and dependency-free so it runs under `node --test`.
 *
 * The helpers below are the console's *presentation* policy, not the wire's: which
 * application the menu recommends for a path, which one the reader's remembered
 * choice points at, and what a named refusal should say.  The host decides what can
 * actually be started; nothing here can widen that set.
 */

export type ExternalAppKind = 'editor' | 'viewer' | 'terminal' | 'shell' | 'system';
export type ExternalAppIconKind = 'glyph' | 'data_url';
export type OpenExternalMode = 'open' | 'reveal';

/** The id the host gives the operating system's own file association. */
export const SYSTEM_APP_ID = 'system';

/** How many applications the menu will show for one path before it stops grouping. */
export const RECOMMENDED_LIMIT = 3;

export interface ExternalAppIconView {
  kind: ExternalAppIconKind;
  value: string;
}

export interface ExternalAppView {
  id: string;
  name: string;
  shortName: string;
  kind: ExternalAppKind;
  /** The extensions the application claims, without a leading dot. */
  extensions: string[];
  icon: ExternalAppIconView;
  isSystemDefault: boolean;
  available: boolean;
}

export interface ExternalAppsView {
  apps: ExternalAppView[];
  /** True when the host found more applications than one page holds. */
  truncated: boolean;
}

export interface OpenExternalResultView {
  opened: boolean;
  appId: string;
  mode: OpenExternalMode;
}

export class MalformedExternalAppsError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'MalformedExternalAppsError';
  }
}

const APP_KINDS: readonly ExternalAppKind[] = [
  'editor',
  'viewer',
  'terminal',
  'shell',
  'system',
];
const ICON_KINDS: readonly ExternalAppIconKind[] = ['glyph', 'data_url'];
const MODES: readonly OpenExternalMode[] = ['open', 'reveal'];

function asRecord(value: unknown, what: string): Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new MalformedExternalAppsError(`${what} must be an object`);
  }
  return value as Record<string, unknown>;
}

function exactKeys(record: Record<string, unknown>, keys: readonly string[], what: string): void {
  const actual = Object.keys(record).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length || actual.some((key, i) => key !== expected[i])) {
    throw new MalformedExternalAppsError(`${what} has unexpected keys`);
  }
}

function text(value: unknown, what: string): string {
  if (typeof value !== 'string') {
    throw new MalformedExternalAppsError(`${what} must be a string`);
  }
  return value;
}

function flag(value: unknown, what: string): boolean {
  if (typeof value !== 'boolean') {
    throw new MalformedExternalAppsError(`${what} must be a boolean`);
  }
  return value;
}

function oneOf<T extends string>(value: unknown, allowed: readonly T[], what: string): T {
  const found = allowed.find((candidate) => candidate === value);
  if (found === undefined) {
    throw new MalformedExternalAppsError(`${what} is not a known value`);
  }
  return found;
}

function appIcon(value: unknown, what: string): ExternalAppIconView {
  const record = asRecord(value, what);
  exactKeys(record, ['kind', 'value'], what);
  return {
    kind: oneOf(record.kind, ICON_KINDS, `${what}.kind`),
    value: text(record.value, `${what}.value`),
  };
}

function app(value: unknown, what: string): ExternalAppView {
  const record = asRecord(value, what);
  exactKeys(
    record,
    ['id', 'name', 'short_name', 'kind', 'extensions', 'icon', 'is_system_default', 'available'],
    what,
  );
  const extensions = record.extensions;
  if (!Array.isArray(extensions)) {
    throw new MalformedExternalAppsError(`${what}.extensions must be an array`);
  }
  return {
    id: text(record.id, `${what}.id`),
    name: text(record.name, `${what}.name`),
    shortName: text(record.short_name, `${what}.short_name`),
    kind: oneOf(record.kind, APP_KINDS, `${what}.kind`),
    extensions: extensions.map((entry, index) =>
      text(entry, `${what}.extensions[${index}]`),
    ),
    icon: appIcon(record.icon, `${what}.icon`),
    isSystemDefault: flag(record.is_system_default, `${what}.is_system_default`),
    available: flag(record.available, `${what}.available`),
  };
}

/** Decode one page of the host's application catalog (`runtime.apps.list`). */
export function parseExternalAppPage(value: unknown): ExternalAppsView {
  const record = asRecord(value, 'external app page');
  exactKeys(record, ['apps', 'truncated'], 'external app page');
  const apps = record.apps;
  if (!Array.isArray(apps)) {
    throw new MalformedExternalAppsError('external app page.apps must be an array');
  }
  return {
    apps: apps.map((entry, index) => app(entry, `external app page.apps[${index}]`)),
    truncated: flag(record.truncated, 'external app page.truncated'),
  };
}

/** Decode the outcome of one launch (`runtime.workspace.open_external`). */
export function parseOpenExternalResult(value: unknown): OpenExternalResultView {
  const record = asRecord(value, 'open external result');
  exactKeys(record, ['opened', 'app_id', 'mode'], 'open external result');
  return {
    opened: flag(record.opened, 'open external result.opened'),
    appId: text(record.app_id, 'open external result.app_id'),
    mode: oneOf(record.mode, MODES, 'open external result.mode'),
  };
}

/** The lower-case extension of a workspace path (`.tsx`), or `''` when it has none. */
export function extensionOf(path: string): string {
  const name = path.slice(path.lastIndexOf('/') + 1);
  const dot = name.lastIndexOf('.');
  if (dot <= 0) return '';
  return name.slice(dot).toLowerCase();
}

function claims(app: ExternalAppView, extension: string): boolean {
  if (extension === '') return false;
  const bare = extension.startsWith('.') ? extension.slice(1) : extension;
  return app.extensions.some((entry) => entry.toLowerCase() === bare);
}

/**
 * The applications whose own table claims this path's extension, most specific
 * first.  The system association is never "recommended": it is always offered
 * separately, and a claim on an extension is what makes an application relevant.
 */
export function recommendedApps(apps: readonly ExternalAppView[], path: string): ExternalAppView[] {
  const extension = extensionOf(path);
  return apps.filter((app) => !app.isSystemDefault && claims(app, extension));
}

/** Every other application, in the host's own order (editors, then the association). */
export function otherApps(apps: readonly ExternalAppView[], path: string): ExternalAppView[] {
  const recommended = new Set(recommendedApps(apps, path).map((app) => app.id));
  return apps.filter((app) => !recommended.has(app.id));
}

/**
 * The application the menu shows in the trigger.
 *
 * In order: the reader's own choice for this extension, the application they used
 * last (only while it claims this extension, so a `.txt` reader does not follow them
 * into a `.tsx` file), what the host recommends for the extension, and finally the
 * system association.  The host remains the authority on what may be started; this
 * only decides which of its applications the trigger names.
 */
export function preferredApp(
  apps: readonly ExternalAppView[],
  path: string,
  rememberedId: string | null,
  lastUsedId: string | null = null,
): ExternalAppView | null {
  if (apps.length === 0) return null;
  const remembered = apps.find((app) => app.id === rememberedId);
  if (remembered !== undefined) return remembered;
  const lastUsed = apps.find((app) => app.id === lastUsedId);
  if (lastUsed !== undefined && claims(lastUsed, extensionOf(path))) return lastUsed;
  const recommended = recommendedApps(apps, path)[0];
  if (recommended !== undefined) return recommended;
  return apps.find((app) => app.isSystemDefault) ?? apps[0];
}

/**
 * What a named refusal should say.
 *
 * The host answers with a `service_code` that names the condition, so the console
 * can explain it instead of showing a bare failure.  An unknown code falls back to
 * the host's own message, which is already path-free.
 */
export function describeOpenExternalFailure(
  code: string | undefined,
  message: string,
  appName: string,
): string {
  switch (code) {
    case 'external_app_path_invalid':
      return '文件路径不合法，无法用外部程序打开。';
    case 'external_app_outside_workspace':
      return '该路径不在工作区内，控制台不会把它交给外部程序。';
    case 'external_app_file_missing':
      return '文件已不在工作区（可能已被删除或重命名），无法用外部程序打开。';
    case 'external_app_unknown':
      return `宿主上没有名为 ${appName} 的应用，请重新选择。`;
    case 'external_app_launch_failed':
      return `启动 ${appName} 失败：宿主拒绝了这次调用。`;
    case 'external_apps_unavailable':
    case 'external_app_unavailable':
    case 'external_app_workspace_unavailable':
      return '宿主当前无法打开外部程序（工作区或桌面会话不可用）。';
    case 'permission_denied':
      return '没有用外部程序打开文件的权限。';
    default:
      return message === '' ? `打开 ${appName} 失败。` : message;
  }
}
