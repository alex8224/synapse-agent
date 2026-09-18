/**
 * Policy for the `mermaid` fences the transcript is willing to draw.
 *
 * Kept apart from the React component so the rules stay pure and unit-testable
 * under `node --test` (the component itself is `.tsx` and needs a DOM): what is
 * refused, why, and how a renderer failure is described to the reader.
 *
 * The two refusals below are a security boundary, not a formatting preference:
 * mermaid compiles both channels into a `<style>` element inside the SVG it
 * returns, and that SVG is injected into the document.  A `<style>` in an inline
 * SVG applies document-wide, and DOMPurify does not parse CSS, so anything the
 * diagram text can put there can reach the whole console (verified: frontmatter
 * `themeCSS` survives into the injected `<style>`, including remote `url()`).
 */

/** Larger diagrams are shown as source: typesetting them would stall a frame. */
export const MAX_DIAGRAM_CHARS = 20_000;

/**
 * mermaid directive opener (`%%{init: ...}%%`, `%%{config: ...}%%`).
 *
 * `sanitizeDirective` only checks that the CSS is brace-balanced, so `themeCSS`
 * and `fontFamily` arrive verbatim.
 */
export const DIRECTIVE_RE = /%%\s*\{/;

/**
 * YAML frontmatter opener, matching the position mermaid itself accepts
 * (`frontMatterRegex` is anchored at the start of the diagram).
 *
 * Frontmatter is the second, independent config channel: it is merged into the
 * same config object as the directives, so refusing only `%%{...}%%` would leave
 * this one open.
 */
export const FRONTMATTER_RE = /^[^\S\n]*---[ \t]*\r?\n/;

/** Longest error text shown in the code-block header before it is clipped. */
export const MAX_REASON_CHARS = 160;

/** Why this source is never handed to mermaid, or `null` when it is. */
export function rejectionReason(source: string): string | null {
  if (source === '') return '图形定义为空';
  if (source.length > MAX_DIAGRAM_CHARS) return `图形定义超过 ${MAX_DIAGRAM_CHARS} 字符`;
  if (DIRECTIVE_RE.test(source)) return '图形含 mermaid 指令（%%{...}%%），不渲染';
  if (FRONTMATTER_RE.test(source)) return '图形含 YAML frontmatter 配置（---），不渲染';
  return null;
}

/**
 * One-line, length-capped description of a renderer failure.
 *
 * mermaid's parse errors are multi-line and embed the offending source, which
 * would flood a code-block header; the first non-empty line is the useful part.
 */
export function describeMermaidError(error: unknown): string {
  const raw =
    error instanceof Error ? error.message : typeof error === 'string' ? error : '未知错误';
  const firstLine =
    raw.split('\n').map((line) => line.trim()).find((line) => line !== '') ?? '未知错误';
  return firstLine.length > MAX_REASON_CHARS
    ? `${firstLine.slice(0, MAX_REASON_CHARS)}…`
    : firstLine;
}
