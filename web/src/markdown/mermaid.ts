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
 *
 * The palette this module feeds mermaid (`themeVariables`, below) reaches that
 * same `<style>`, but it is not a third channel: it is a fixed table in this
 * module, chosen by the console theme, never by the diagram.  A diagram cannot
 * name a variable, and the `themeCSS` channel is still closed -- the two
 * refusals above are what keep it closed.
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

/**
 * Which of the console's two palettes a diagram should be drawn with.
 *
 * mermaid bakes its palette into the SVG at render time -- its own `<style>`
 * carries literal fills and strokes -- so the diagram cannot follow the
 * console's `data-theme` through CSS variables the way the rest of the UI does.
 * It has to be told which palette to draw with, and re-drawn when the theme
 * changes.  `fluent-dark` is the only shipped dark theme.
 */
export type DiagramPalette = 'light' | 'dark';

/** The palette for a `data-theme` value (`null` is the light fallback). */
export function diagramPaletteFor(dataTheme: string | null): DiagramPalette {
  return dataTheme !== null && dataTheme.endsWith('-dark') ? 'dark' : 'light';
}

/**
 * The palette every diagram is drawn with, in both console themes.
 *
 * mermaid's own `default` palette: lavender node fills, purple borders, dark
 * labels.  A diagram is a drawing rather than a UI surface, so it does not have
 * to be repainted per theme -- and the reader asked for exactly this, the blocks
 * reading the same in dark as in light.  Only the connectors are palette
 * dependent, because this palette draws them for a light page (see
 * {@link MERMAID_THEME_VARIABLES}).
 *
 * This is deliberately *not* a palette derived from the console's colour tokens:
 * that was tried and made the light theme render dark (see `MermaidBlock`'s note
 * on the removed cache).
 */
export const MERMAID_THEME = 'default' as const;

/**
 * The ink for everything a diagram draws directly on the page.
 *
 * The `default` palette is drawn for a light page: it strokes its connectors and
 * writes the labels attached to them in `#333333` (and `black` for a few), which
 * is right on white and effectively invisible on `--surface` /
 * `--surface-canvas`.  This is the light grey mermaid's own dark theme uses for
 * the role.
 *
 * Note what this does *not* cover: anything drawn on a light block -- a node's
 * label, the text in a note or a sequence diagram's label box -- stays dark,
 * because the block behind it is still light.  That is why the overrides below
 * name roles one by one instead of re-tinting the palette.
 */
export const DIAGRAM_PAGE_INK_DARK = 'lightgrey';

/**
 * The connector width, in the SVG's own units.
 *
 * mermaid draws a flowchart's edges with `stroke-width: ${strokeWidth ?? 2}px`
 * and its own themes set `strokeWidth: 1`, so the edges are one unit wide.  The
 * diagram is then scaled down to fit the transcript column (`.mermaid-diagram svg
 * { max-width: 100% }`), which puts a 1-unit stroke on a fraction of a device
 * pixel: it anti-aliases to a faint grey line over the dark surfaces.  Two units
 * is what survives that downscale.
 */
export const DIAGRAM_EDGE_STROKE_WIDTH = 2;

/**
 * `themeVariables` per palette, on top of {@link MERMAID_THEME}.
 *
 * Light is an empty table on purpose: it is left exactly as mermaid ships it, and
 * an empty object is also what clears the dark overrides when the console
 * switches back (mermaid merges the config it is re-initialized with, so
 * omitting the key would keep the dark values).
 *
 * Every dark entry is a role the `default` palette resolves to a dark colour
 * *and* draws on the page rather than on a light block; no fill is ever
 * overridden, so the blocks stay identical to the light theme.
 *
 * They have to be listed one by one: mermaid's `calculate()` applies these
 * overrides *after* `updateColors()`, so a role the palette derives from
 * `lineColor` (`relationColor`, `transitionColor`, `defaultLinkColor`, ...) keeps
 * the dark value it was derived with.  Only `signalColor` and `signalTextColor`
 * are read back through `textColor`, the rest are pinned to `lineColor`, and
 * `loopTextColor` to `actorTextColor` -- three different dark sources.
 */
export const MERMAID_THEME_VARIABLES: Readonly<
  Record<DiagramPalette, Readonly<Record<string, string | number>>>
> = {
  light: {},
  dark: {
    // Flowchart: edges, the link default, and the arrowheads.
    lineColor: DIAGRAM_PAGE_INK_DARK,
    defaultLinkColor: DIAGRAM_PAGE_INK_DARK,
    arrowheadColor: DIAGRAM_PAGE_INK_DARK,
    // Sequence: message lines and their arrowheads (`signalColor`), the message
    // text (`signalTextColor`), and the alt/loop/section labels
    // (`loopTextColor`).  The lifelines stay the palette's own purple
    // (`actorLineColor`), and the label box's own text stays black on its light
    // chip (`labelTextColor`).
    signalColor: DIAGRAM_PAGE_INK_DARK,
    signalTextColor: DIAGRAM_PAGE_INK_DARK,
    loopTextColor: DIAGRAM_PAGE_INK_DARK,
    // State: the transitions, and the marker drawn for a special state.
    transitionColor: DIAGRAM_PAGE_INK_DARK,
    specialStateColor: DIAGRAM_PAGE_INK_DARK,
    // Class and ER: the relations between boxes.
    relationColor: DIAGRAM_PAGE_INK_DARK,
    emArrowhead: DIAGRAM_PAGE_INK_DARK,
    emRelationStroke: DIAGRAM_PAGE_INK_DARK,
    // Gantt: the task label drawn beside a bar rather than inside it.  The label
    // inside a bar is the palette's own (`taskTextColor`), and stays.
    taskTextOutsideColor: DIAGRAM_PAGE_INK_DARK,
    strokeWidth: DIAGRAM_EDGE_STROKE_WIDTH,
  },
};

/**
 * The theme the document root currently carries, as mermaid needs to see it.
 *
 * Read from the DOM rather than from the appearance store so the renderer and
 * its re-render trigger agree on one source of truth: the store's `appearance`
 * can still be `system`, while the resolved palette is what `data-theme` holds.
 * A missing document (SSR-less test, a torn-down root) falls back to light.
 */
export function currentDataTheme(): string | null {
  if (typeof document === 'undefined') return null;
  return document.documentElement.dataset.theme ?? null;
}

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
