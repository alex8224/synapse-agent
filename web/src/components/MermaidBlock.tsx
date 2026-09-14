import React, { useEffect, useRef, useState } from 'react';
import DOMPurify from 'dompurify';
import { CodeBlock } from './CodeBlock.tsx';
import { GeneratedHtml } from './GeneratedHtml.tsx';
import {
  MAX_DIAGRAM_CHARS,
  describeMermaidError,
  rejectionReason,
} from '../markdown/mermaid.ts';

type Phase =
  | { status: 'idle' }
  | { status: 'rendering' }
  | { status: 'ready'; svg: string }
  | { status: 'failed'; reason: string };

type MermaidApi = typeof import('mermaid')['default'];

/**
 * Diagram ids are global, not per component: mermaid scopes its theme CSS with
 * `#<id>` and writes that id onto the SVG, so two diagrams sharing an id would
 * produce duplicate DOM ids and CSS that matches both.
 */
let diagramSeq = 0;

/**
 * The mermaid runtime, loaded on the first diagram and configured once.
 *
 * `import('mermaid')` is dynamic on purpose: mermaid is a large dependency and
 * most transcripts contain no diagram, so it must stay out of the initial
 * bundle.  `securityLevel: 'strict'` keeps mermaid's own SVG sanitization on,
 * `htmlLabels: false` keeps labels out of `foreignObject`, and
 * `suppressErrorRendering` makes a bad diagram reject instead of injecting
 * mermaid's error graphic.
 */
let mermaidPromise: Promise<MermaidApi> | null = null;

function loadMermaid(): Promise<MermaidApi> {
  mermaidPromise ??= import('mermaid')
    .then((module) => {
      const api = module.default;
      api.initialize({
        startOnLoad: false,
        securityLevel: 'strict',
        htmlLabels: false,
        theme: 'default',
        fontFamily: 'inherit',
        suppressErrorRendering: true,
        maxTextSize: MAX_DIAGRAM_CHARS,
      });
      return api;
    })
    .catch((error: unknown) => {
      // A failed chunk load must not poison every later diagram: drop the cached
      // promise so the next diagram retries the import.
      mermaidPromise = null;
      throw error;
    });
  return mermaidPromise;
}

/**
 * Second sanitization layer over mermaid's own strict-mode output.
 *
 * The SVG is injected with `dangerouslySetInnerHTML`, so it goes through
 * DOMPurify's SVG profile as well: scripts, event handlers and `javascript:`
 * URLs are dropped.  The theme CSS mermaid keeps in a `<style>` element is
 * allowed (the diagram is unstyled without it) but its remote fetches are
 * neutralised: a `<style>` inside an inline SVG applies document-wide, so
 * `@import` / `url()` there would let a diagram phone home.  Every other channel
 * that can reach that CSS -- `%%{...}%%` directives and YAML frontmatter -- is
 * refused before rendering (see `markdown/mermaid.ts`).
 */
function sanitizeSvg(svg: string): string {
  const clean = DOMPurify.sanitize(svg, {
    USE_PROFILES: { svg: true, svgFilters: true },
    ADD_TAGS: ['style'],
    FORBID_TAGS: ['script', 'iframe', 'object', 'embed'],
  });
  return clean.replace(
    /(<style\b[^>]*>)([\s\S]*?)(<\/style>)/gi,
    (_match, open: string, css: string, close: string) =>
      `${open}${css.replace(/@import[^;]*;?/gi, '').replace(/url\s*\([^)]*\)/gi, 'none')}${close}`,
  );
}

export interface MermaidBlockProps {
  /** Diagram source, exactly as it appeared inside the fence. */
  code: string;
  /** True while the fence has not been closed yet. */
  streaming: boolean;
}

/**
 * A `mermaid` fence rendered as a diagram.
 *
 * Rendering is all-or-nothing and never silent: while the fence is open, or when
 * the diagram is rejected/unparsable, the block stays a code block with the
 * reason in its header.  mermaid draws into an off-screen container the console
 * owns, so a render can neither reflow the transcript nor leave stray nodes
 * behind -- the container is always mounted, and a missing one is a failure
 * rather than a silent fall back to `document.body`.
 */
export const MermaidBlock: React.FC<MermaidBlockProps> = ({ code, streaming }) => {
  // The last *completed* render, tagged with the source it came from.  The
  // displayed phase is then derived during render instead of being pushed into
  // state from the effect, so a new diagram never flashes the previous one.
  const [rendered, setRendered] = useState<{ source: string; phase: Phase } | null>(null);
  const [copied, setCopied] = useState(false);
  const hostRef = useRef<HTMLDivElement | null>(null);

  const source = code.trim();
  const rejected = rejectionReason(source);
  const phase: Phase = streaming
    ? { status: 'idle' }
    : rejected !== null
      ? { status: 'failed', reason: rejected }
      : rendered !== null && rendered.source === source
        ? rendered.phase
        : { status: 'rendering' };

  useEffect(() => {
    // `source` (not `code`) is the dependency: a whitespace-only edit does not
    // change the diagram, so it must not re-run the renderer.
    if (streaming || rejectionReason(source) !== null) return;
    const host = hostRef.current;
    let cancelled = false;
    const id = `synapse-mermaid-${(diagramSeq += 1)}`;
    void (async () => {
      try {
        if (host === null) throw new Error('渲染容器不可用');
        const mermaid = await loadMermaid();
        const { svg } = await mermaid.render(id, source, host);
        if (cancelled) return;
        setRendered({ source, phase: { status: 'ready', svg: sanitizeSvg(svg) } });
      } catch (error: unknown) {
        if (!cancelled) {
          setRendered({ source, phase: { status: 'failed', reason: describeMermaidError(error) } });
        }
      } finally {
        // mermaid leaves its own copy (and any error graphic) in the render
        // target; the injected SVG above is the only one that stays.
        if (host !== null) host.innerHTML = '';
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [source, streaming]);

  const copy = (): void => {
    const clipboard = typeof navigator === 'undefined' ? undefined : navigator.clipboard;
    if (!clipboard) return;
    void clipboard
      .writeText(code)
      .then(() => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1200);
      })
      .catch(() => undefined);
  };

  // Off-screen render target, always mounted so the ref is never stale.
  // `visibility: hidden` (not `display: none`) and fixed positioning keep it
  // measurable for mermaid's `getBBox` pass while leaving the transcript's
  // layout and scroll position untouched.
  const host = (
    <div
      ref={hostRef}
      aria-hidden="true"
      className="pointer-events-none fixed left-0 top-0 -z-10 opacity-0"
      style={{ visibility: 'hidden' }}
    />
  );

  if (phase.status !== 'ready') {
    const note =
      phase.status === 'rendering'
        ? '正在渲染图形…'
        : phase.status === 'failed'
          ? `图形渲染失败：${phase.reason}`
          : undefined;
    return (
      <>
        {host}
        <CodeBlock lang="mermaid" code={code} streaming={streaming} note={note} />
      </>
    );
  }

  return (
    <>
      {host}
      <div className="my-2 overflow-hidden rounded-md border border-gray-200 bg-surface">
        <div className="flex items-center justify-between border-b border-gray-200 bg-sunken px-2.5 py-1">
          <span className="font-mono text-[10px] uppercase tracking-wide text-gray-500">
            mermaid
          </span>
          <button
            type="button"
            onClick={copy}
            title="复制图形源码"
            className="font-mono text-[10px] text-gray-500 hover:text-gray-900 transition-colors cursor-pointer"
          >
            {copied ? '已复制' : '复制源码'}
          </button>
        </div>
        <div className="overflow-x-auto px-3 py-3">
          <GeneratedHtml html={phase.svg} className="mermaid-diagram" />
        </div>
      </div>
    </>
  );
};
