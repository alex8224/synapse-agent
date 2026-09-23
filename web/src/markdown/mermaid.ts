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

/**
 * What the renderer does with the SVG's *size*, and why it cannot leave it to
 * mermaid.
 *
 * mermaid's default is `useMaxWidth: true`: `calculateSvgSizeAttrs` gives the
 * root `<svg>` `width="100%"` plus an inline `style="max-width: {width}px"`.
 * An inline style outranks any stylesheet rule, so the diagram is always laid
 * out at the reading column's width -- a wide diagram is silently scaled down,
 * and the scrollable stage around it can never scroll.  The functions below
 * replace that with the diagram's own `viewBox` size, so the *stage* decides
 * how big the drawing is, and a viewer can zoom it.
 */

/** Intrinsic size of a rendered diagram, in SVG user units (`viewBox` units). */
export interface DiagramSize {
  width: number;
  height: number;
}

/**
 * The first `<svg>` start tag: mermaid's output is a single root element, and
 * every attribute rule below is scoped to it so the diagram body is untouched.
 */
const SVG_ROOT_RE = /<svg\b[^>]*>/i;
/**
 * Attribute matchers are anchored on whitespace, not `\b`: `\bwidth=` also
 * matches inside `stroke-width="2"`, which would strip a real attribute.
 */
const VIEW_BOX_RE = /\bviewBox\s*=\s*"([^"]*)"/i;
const WIDTH_ATTR_RE = /\swidth\s*=\s*"([^"]*)"/i;
const HEIGHT_ATTR_RE = /\sheight\s*=\s*"([^"]*)"/i;
const STYLE_ATTR_RE = /\sstyle\s*=\s*"([^"]*)"/i;
const MAX_WIDTH_DECL_RE = /^max-width\s*:/i;

/** A plain pixel length; `100%` and other units are not an intrinsic size. */
function pixelLength(raw: string | undefined): number {
  if (raw === undefined) return Number.NaN;
  const match = /^\s*(\d+(?:\.\d+)?)(?:px)?\s*$/.exec(raw);
  return match === null ? Number.NaN : Number(match[1]);
}

function usableSize(width: number, height: number): boolean {
  return Number.isFinite(width) && Number.isFinite(height) && width > 0 && height > 0;
}

/**
 * The diagram's own size: `viewBox` first (mermaid always sets it via
 * `setupGraphViewbox`), then explicit `width`/`height` attributes.
 *
 * `null` when the markup carries no size the viewer could work with -- the
 * card then keeps mermaid's own `width="100%"` behaviour, and the enlarge
 * button stays hidden rather than opening a viewer that cannot be fitted.
 */
export function svgIntrinsicSize(svg: string): DiagramSize | null {
  const tag = SVG_ROOT_RE.exec(svg);
  if (tag === null) return null;
  const box = VIEW_BOX_RE.exec(tag[0]);
  if (box !== null) {
    const parts = box[1].trim().split(/[\s,]+/).map(Number);
    if (parts.length === 4 && usableSize(parts[2], parts[3])) {
      return { width: parts[2], height: parts[3] };
    }
  }
  const width = pixelLength(WIDTH_ATTR_RE.exec(tag[0])?.[1]);
  const height = pixelLength(HEIGHT_ATTR_RE.exec(tag[0])?.[1]);
  return usableSize(width, height) ? { width, height } : null;
}

/** Round to a hundredth so a float `viewBox` cannot leak `1234.0000000002`. */
function round(value: number): number {
  return Math.round(value * 100) / 100;
}

export interface FrozenDiagram {
  /** The SVG with an explicit intrinsic size, or the input when it has none. */
  html: string;
  /** `null` when nothing could be measured, so no viewer should be offered. */
  size: DiagramSize | null;
}

/**
 * Give the root `<svg>` its intrinsic pixel size and drop mermaid's
 * `width="100%"` / inline `max-width`.
 *
 * Runs *after* sanitization, so what it writes is what gets injected.  Every
 * other attribute and every other style declaration on the root survives
 * (`overflow: visible` and friends), and the diagram body is never touched.
 */
export function freezeSvgSize(svg: string): FrozenDiagram {
  const size = svgIntrinsicSize(svg);
  const tag = SVG_ROOT_RE.exec(svg);
  if (size === null || tag === null) return { html: svg, size: null };
  const frozen = tag[0]
    .replace(STYLE_ATTR_RE, (_match, css: string) => {
      const kept = css
        .split(';')
        .map((declaration) => declaration.trim())
        .filter((declaration) => declaration !== '' && !MAX_WIDTH_DECL_RE.test(declaration));
      return kept.length === 0 ? '' : ` style="${kept.join('; ')}"`;
    })
    .replace(WIDTH_ATTR_RE, '')
    .replace(HEIGHT_ATTR_RE, '')
    .replace(/^<svg\b/i, `<svg width="${round(size.width)}" height="${round(size.height)}"`);
  // A function replacement: the markup must never be read as `$&` patterns.
  return { html: svg.replace(tag[0], () => frozen), size };
}
