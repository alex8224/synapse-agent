/**
 * Recognise workspace file paths inside model prose and turn them into
 * workspace-relative POSIX paths the read-only artifact surface understands.
 *
 * Two pure steps, kept dependency-free so they are unit-tested directly:
 *
 * - `splitFileRefs` scans a run of plain text and returns the pieces that look
 *   like a path, so the renderer can make them clickable.  It is deliberately
 *   conservative: it only claims a token when the shape is unambiguous (an
 *   absolute path, or a path with a separator and a known extension, or a bare
 *   name with a known extension), so ordinary prose -- `e.g.`, `i.e.`, version
 *   numbers, domain names, times -- is never rewritten.
 * - `toWorkspacePath` normalises whatever the model wrote (Windows drive path,
 *   POSIX absolute path, or relative path, with an optional `:line:col` suffix)
 *   to the canonical relative POSIX path `runtime.artifacts.*` requires.  A
 *   path that cannot be mapped (contains `..`, or is the workspace root itself)
 *   returns `null` instead of a bogus target.
 *
 * Detection is syntactic and needs no workspace root, so the Markdown layer can
 * split spans without knowing which project is attached; the root is only
 * consulted when the user actually opens a path.
 */

/** One run of text, split into literal text and clickable file references. */
export interface FileRefPart {
  type: 'text' | 'file';
  text: string;
}

/** Characters that may appear inside a single path token. */
const TOKEN_CHAR_RE = /[A-Za-z0-9_./\\:@+~-]/;

/**
 * Extensions we are willing to treat as a file when the token carries no
 * separator (a bare `agent.py`).  A curated allow-list, not "anything with a
 * dot": that is what keeps `e.g`, `1.2.3`, `v1.2` and `example.com` as prose.
 */
const KNOWN_EXTENSIONS = new Set([
  // code
  'py', 'pyi', 'ts', 'tsx', 'js', 'jsx', 'mjs', 'cjs', 'rs', 'go', 'java', 'kt',
  'c', 'h', 'cc', 'cpp', 'hpp', 'cs', 'rb', 'php', 'swift', 'scala', 'lua', 'sh',
  'bash', 'zsh', 'ps1', 'bat', 'cmd', 'sql', 'graphql', 'proto', 'vue', 'svelte',
  // markup / text / config
  'txt', 'md', 'markdown', 'rst', 'log', 'csv', 'tsv', 'json', 'jsonl', 'yaml',
  'yml', 'toml', 'ini', 'cfg', 'conf', 'env', 'xml', 'html', 'htm', 'css', 'scss',
  'less', 'sass', 'diff', 'patch', 'lock', 'gitignore', 'gitattributes',
  'dockerignore', 'editorconfig', 'prettierrc', 'eslintrc', 'nvmrc',
  // documents / data
  'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'rtf', 'tex',
  // images / media
  'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp', 'ico', 'tiff', 'avif',
  'mp3', 'wav', 'ogg', 'mp4', 'mov', 'webm', 'mkv',
  // archives
  'zip', 'tar', 'gz', 'tgz', 'bz2', 'xz', '7z', 'rar',
]);

/** Extension-less files recognised by their (lower-cased) basename. */
const KNOWN_FILENAMES = new Set([
  'license', 'licence', 'notice', 'authors', 'contributors', 'codeowners',
  'readme', 'changelog', 'changes', 'contributing', 'install', 'copying',
  'dockerfile', 'containerfile', 'makefile', 'justfile', 'procfile', 'vagrantfile',
  'gemfile', 'rakefile', 'brewfile', 'cmakelists',
]);

/** Lower-cased extension of a basename; a dotfile is its own extension. */
function extensionOfName(base: string): string {
  const dot = base.lastIndexOf('.');
  if (dot < 0) return '';
  if (dot === 0) return base.slice(1).toLowerCase();
  return base.slice(dot + 1).toLowerCase();
}

/**
 * Drop a trailing `:line` or `:line:col` reference.
 *
 * A Windows drive letter (`C:`) is not a line number, so a single-letter prefix
 * before the colon keeps the token intact.
 */
export function stripLineColumn(token: string): string {
  const match = /^(.+?):(\d+)(?::(\d+))?$/.exec(token);
  if (match === null) return token;
  if (/^[A-Za-z]$/.test(match[1])) return token;
  return match[1];
}

/** Whether a token looks like a workspace file path worth linking. */
export function looksLikeFileRef(token: string): boolean {
  const value = stripLineColumn(token);
  if (value.length < 2) return false;
  // A URL is the link parser's job, never a file reference.
  if (value.includes('://')) return false;
  // `F:\dir\file` / `F:/dir/file`.
  if (/^[A-Za-z]:[\\/]/.test(value)) return value.length > 3;
  const hasSeparator = /[\\/]/.test(value);
  const base = value.split(/[\\/]/).pop() ?? value;
  const extension = extensionOfName(base);
  if (value.startsWith('/')) {
    return hasSeparator && (extension !== '' || KNOWN_FILENAMES.has(base.toLowerCase()));
  }
  if (hasSeparator) {
    if (base === '' || base === '.' || base === '..') return false;
    return extension !== '' || KNOWN_FILENAMES.has(base.toLowerCase());
  }
  // A bare name only counts when its extension is on the allow-list.
  return extension !== '' && KNOWN_EXTENSIONS.has(extension);
}

/** Trailing punctuation that belongs to the sentence, not to the path. */
const TRAILING_PUNCT_RE = /[.,;:)\]}>"'!?]+$/;

/**
 * Split plain text into literal text and clickable file references.
 *
 * A candidate is a maximal run of path characters bounded by non-path
 * characters; trailing sentence punctuation (and a trailing `:line:col`, which
 * stays part of the reference) is peeled back before the shape is judged.
 */
export function splitFileRefs(text: string): FileRefPart[] {
  const parts: FileRefPart[] = [];
  let plain = '';
  const flush = (): void => {
    if (plain !== '') {
      parts.push({ type: 'text', text: plain });
      plain = '';
    }
  };
  let i = 0;
  while (i < text.length) {
    if (!TOKEN_CHAR_RE.test(text[i])) {
      plain += text[i];
      i += 1;
      continue;
    }
    let end = i;
    while (end < text.length && TOKEN_CHAR_RE.test(text[end])) end += 1;
    const run = text.slice(i, end);
    const trailing = TRAILING_PUNCT_RE.exec(run)?.[0] ?? '';
    const value = run.slice(0, run.length - trailing.length);
    if (value !== '' && looksLikeFileRef(value)) {
      flush();
      parts.push({ type: 'file', text: value });
      plain += trailing;
    } else {
      plain += run;
    }
    i = end;
  }
  flush();
  return parts;
}

/**
 * Normalise a model-written path to the canonical relative POSIX path the
 * artifact surface accepts, or `null` when it cannot be mapped.
 *
 * `workspaceRoot` is the attached project's host path; a path that starts with
 * it becomes relative to it (a Windows root compares case-insensitively), and a
 * leading `/` is treated as the workspace root rather than an absolute host
 * path, matching how the model writes workspace paths.
 */
export function toWorkspacePath(raw: string, workspaceRoot: string): string | null {
  const stripped = stripLineColumn(raw.trim());
  if (stripped === '') return null;
  let path = stripped.replace(/\\/g, '/');
  const root = (workspaceRoot ?? '').replace(/\\/g, '/').replace(/\/+$/, '');
  if (root !== '') {
    const lower = /^[A-Za-z]:/.test(root);
    const norm = (value: string): string => (lower ? value.toLowerCase() : value);
    if (norm(path).startsWith(norm(root) + '/')) {
      path = path.slice(root.length + 1);
    } else if (norm(path) === norm(root)) {
      return null;
    }
  }
  path = path.replace(/^\/+/, '');
  const segments = path.split('/').filter((segment) => segment !== '' && segment !== '.');
  if (segments.length === 0) return null;
  if (segments.some((segment) => segment === '..')) return null;
  return segments.join('/');
}
