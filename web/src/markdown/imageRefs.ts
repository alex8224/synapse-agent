/**
 * Where an image reference in a model answer points, and what the console is
 * allowed to do about it.
 *
 * The transcript renders untrusted model output, so a source is never handed
 * straight to `<img src>`:
 *
 * - `local` -- a workspace path.  It is read through the bounded artifact
 *   surface and displayed from a blob URL the console created itself, exactly
 *   like the file viewer's image stage.
 * - `remote` -- an `http(s)` URL.  It is *not* fetched: loading it would tell a
 *   third party that this answer was read, and no other remote image path
 *   exists in the console.  The reader gets a link to follow deliberately.
 * - `inline` -- a `data:` payload.  It is not a workspace file, and a
 *   `data:image/svg+xml` payload is a script-capable document, so it is dropped.
 * - `invalid` -- everything else (`javascript:`, `file:`, protocol-relative
 *   `//host/...`), which is never an image and never a link target either.
 *
 * Pure functions: no DOM, no client, no socket.
 */

/** What an image source is, before any policy is applied to it. */
export type ImageSrcKind = 'local' | 'remote' | 'inline' | 'invalid';

/** Any URL scheme; a Windows drive letter (`C:\`, `C:/`) is checked first. */
const SCHEME_RE = /^[a-z][a-z0-9+.-]*:/i;
const WINDOWS_DRIVE_RE = /^[A-Za-z]:[\\/]/;

/** Classify one image source.  Never throws, never reads anything. */
export function classifyImageSrc(raw: string): ImageSrcKind {
  const src = raw.trim();
  if (src === '') return 'invalid';
  // Protocol-relative: a remote URL whose scheme is inherited from the page.
  if (src.startsWith('//')) return 'invalid';
  if (/^data:/i.test(src)) return 'inline';
  if (/^https?:/i.test(src)) return 'remote';
  if (WINDOWS_DRIVE_RE.test(src)) return 'local';
  if (SCHEME_RE.test(src)) return 'invalid';
  return 'local';
}

/**
 * The source of a real image reference, or `null` when it may not be one.
 *
 * Used by the parser: a rejected source leaves the reference as plain text
 * instead of producing an image node, so `![x](data:...)` can never reach the
 * renderer at all.
 */
export function sanitizeImageSrc(raw: string): string | null {
  const kind = classifyImageSrc(raw);
  if (kind === 'invalid' || kind === 'inline') return null;
  return raw.trim();
}
