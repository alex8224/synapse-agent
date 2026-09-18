import React from 'react';

/**
 * The console's single `dangerouslySetInnerHTML` boundary.
 *
 * The transcript renders untrusted model output, and the rule everywhere else is
 * that Markdown becomes typed React nodes and never raw HTML.  Two renderers
 * cannot follow that rule because their output *is* markup:
 *
 * - KaTeX (`renderTex`, `trust: false`) -- TeX is parsed into a fixed node
 *   grammar, and the trusted commands that could emit author-controlled HTML or
 *   links stay disabled;
 * - mermaid, whose SVG is produced by `MermaidBlock` and passed through
 *   DOMPurify's SVG profile (plus mermaid's own `securityLevel: 'strict'`)
 *   before it reaches this component.
 *
 * Keeping the injection in one component makes the boundary reviewable and
 * lets `tests/markdownRenderGuard.test.ts` assert that nothing else in `src/`
 * injects markup.  Never pass raw model text here.
 */
export interface GeneratedHtmlProps {
  /** Markup produced by KaTeX or by the sanitized mermaid renderer. */
  html: string;
  /** Host element: `span` for inline math, `div` for blocks. */
  tag?: 'span' | 'div';
  className?: string;
  /** Plain-text tooltip; never markup. */
  title?: string;
}

export const GeneratedHtml: React.FC<GeneratedHtmlProps> = ({ html, tag = 'div', className, title }) => {
  const shared = { className, title, dangerouslySetInnerHTML: { __html: html } };
  return tag === 'span' ? <span {...shared} /> : <div {...shared} />;
};
