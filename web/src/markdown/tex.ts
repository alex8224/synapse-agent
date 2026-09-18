/**
 * KaTeX rendering for model-authored math.
 *
 * TeX arrives as untrusted model output, so the renderer runs with
 * `trust: false` (no `\href`, `\includegraphics`, `\htmlClass`, `\htmlData`,
 * ...), `throwOnError: false` (a malformed formula renders as a visible error
 * node instead of throwing in the middle of a transcript) and a bounded macro
 * expansion budget, so a macro bomb cannot stall the tab.
 *
 * The module is deliberately DOM-free -- `renderToString` needs no document --
 * so the wrapper stays unit-testable under `node --test`.
 */
import katex from 'katex';

/** Class KaTeX puts on the node it renders when it cannot parse the input. */
const ERROR_MARKER = 'class="katex-error"';

export interface TexResult {
  /** KaTeX markup (visual HTML + MathML); safe to inject as-is. */
  html: string;
  /** True when KaTeX fell back to rendering the raw source. */
  failed: boolean;
}

/** Options every formula is rendered with; see the module comment. */
const OPTIONS = {
  throwOnError: false,
  trust: false,
  strict: false,
  maxExpand: 1000,
} as const;

/**
 * Render one TeX fragment.
 *
 * Never throws: an input KaTeX cannot even build an error node for (or a
 * `ParseError` it re-raises) comes back as `{ html: '', failed: true }`, and the
 * caller shows the source instead.
 */
export function renderTex(tex: string, displayMode: boolean): TexResult {
  try {
    const html = katex.renderToString(tex, { ...OPTIONS, displayMode });
    return { html, failed: html.includes(ERROR_MARKER) };
  } catch {
    // Explicit degradation boundary: KaTeX raising means "no formula", not a
    // broken transcript, so the caller falls back to the labelled source.
    return { html: '', failed: true };
  }
}
