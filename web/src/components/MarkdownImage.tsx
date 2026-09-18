import React, { useEffect, useRef, useState } from 'react';
import { Image20Regular, ImageOff20Regular } from '@fluentui/react-icons';
import { useConsoleStore } from '../stores/useConsoleStore.ts';
import { toWorkspacePath } from '../markdown/filePaths.ts';
import { classifyImageSrc } from '../markdown/imageRefs.ts';
import { locateArtifact } from '../runtime-client/artifactLocate.ts';
import { FileRefButton } from './FileRefButton.tsx';
import { ImageLightbox } from './ImageLightbox.tsx';
import {
  MalformedArtifactError,
  artifactErrorMessage,
  artifactPreviewKind,
  createArtifactImageLoader,
  formatBytes,
} from '../client/artifacts.ts';
import type { ArtifactEntry, ArtifactImageLoader } from '../client/artifacts.ts';

/**
 * How long a reference must stay unchanged before the console reads it.
 *
 * The transcript re-renders on every streamed token.  A reference only becomes
 * an image node once its closing `)` has arrived, but the rest of the answer is
 * usually still streaming, so waiting for a short quiet period turns "one stat
 * per token" into "one read per finished reference".
 */
const RESOLVE_DELAY_MS = 250;

/** The wire message is generic; the reason lives in `service_code`. */
function describe(err: unknown): string {
  if (err instanceof MalformedArtifactError) return err.message;
  if (err instanceof Error && err.message) {
    const code = (err as { service_code?: string }).service_code ?? null;
    return artifactErrorMessage(code, err.message);
  }
  return String(err);
}

type ImageState =
  | { kind: 'resolving' }
  | { kind: 'ready'; entry: ArtifactEntry; url: string }
  | { kind: 'unavailable'; reason: string };

/** A chip used for a reference the console deliberately does not fetch. */
const RefusalChip: React.FC<{ text: string; note: string }> = ({ text, note }) => (
  <span className="inline-flex flex-wrap items-baseline gap-1">
    <ImageOff20Regular aria-hidden="true" className="text-amber-600" />
    <code className="rounded-control bg-sunken px-1 py-0.5 font-mono text-[0.85em] text-gray-600">
      {text}
    </code>
    <span className="text-[12px] text-amber-700">{note}</span>
  </span>
);

/**
 * One `![alt](src)` reference from a model answer.
 *
 * The source decides the behaviour, and only one of them ever touches the
 * runtime (`imageRefs.ts` holds the classification):
 *
 * - a local workspace path is stat-ed, size- and type-checked, read through the
 *   bounded image reader and shown from a blob URL this component owns; a click
 *   opens the shared `ImageLightbox` on the same URL, so zooming costs no extra
 *   read;
 * - a remote `http(s)` URL is *not* fetched -- that would tell a third party the
 *   answer was read -- so it stays a link the reader can follow deliberately;
 * - a `data:` payload or any other scheme is refused outright.
 *
 * Every refusal falls back to the file-reference button, so a reference is
 * always still actionable and never renders as a broken image.
 */
const MarkdownImageBody: React.FC<{ alt: string; src: string }> = ({ alt, src }) => {
  const client = useConsoleStore((s) => s.client);
  const currentSession = useConsoleStore((s) => s.currentSession);
  const workspacePath = useConsoleStore((s) => s.workspacePath);
  const paired = useConsoleStore((s) => s.pairingState === 'paired');

  const srcKind = classifyImageSrc(src);
  const path = srcKind === 'local' ? toWorkspacePath(src, workspacePath) : null;

  const [state, setState] = useState<ImageState>({ kind: 'resolving' });
  const [zoomed, setZoomed] = useState(false);

  const loaderRef = useRef<ArtifactImageLoader | null>(null);
  const session = { project_id: currentSession.project_id, thread_id: currentSession.thread_id };
  const sessionKey = `${session.project_id}/${session.thread_id}`;

  // One loader per rendered image: it owns the object URL, so unmounting (or a
  // session change) revokes the blob instead of leaking it.
  useEffect(() => {
    if (!client) return;
    const loader = createArtifactImageLoader({
      read: client,
      urls: {
        create: (bytes, mime) => URL.createObjectURL(new Blob([bytes.slice()], { type: mime })),
        revoke: (url) => URL.revokeObjectURL(url),
      },
    });
    loaderRef.current = loader;
    return () => {
      loaderRef.current = null;
      loader.dispose();
    };
  }, [client]);

  useEffect(() => {
    if (srcKind !== 'local' || path === null) return;
    if (!client || !paired) {
      setState({
        kind: 'unavailable',
        reason: paired ? '运行时客户端尚未就绪' : '控制台尚未与运行时配对',
      });
      return;
    }
    let cancelled = false;
    setState({ kind: 'resolving' });
    const timer = window.setTimeout(() => {
      void (async () => {
        const loader = loaderRef.current;
        if (loader === null) return;
        try {
          // A bare name is a real case (`![x](chart.png)`): locate it in the
          // workspace first, exactly as a transcript file click does.
          let target = path;
          if (!target.includes('/')) {
            const found = await locateArtifact(client, session, target).catch(() => null);
            if (found === null) {
              if (!cancelled) {
                setState({ kind: 'unavailable', reason: `工作区里找不到 ${target}` });
              }
              return;
            }
            target = found;
          }
          const entry = await client.statArtifact(session, target);
          if (cancelled) return;
          if (entry.kind === 'directory') {
            setState({ kind: 'unavailable', reason: `${target} 是目录，不是图片` });
            return;
          }
          if (artifactPreviewKind(entry.media_type, entry.path) !== 'image') {
            setState({
              kind: 'unavailable',
              reason: `${entry.media_type || '未知类型'} 不是可预览的图片`,
            });
            return;
          }
          const resource = await loader.load(entry, session);
          if (cancelled) return;
          if (resource.status === 'ready') {
            setState({ kind: 'ready', entry, url: resource.url });
          } else if (resource.status === 'refused') {
            setState({ kind: 'unavailable', reason: resource.reason });
          } else if (resource.status === 'error') {
            setState({ kind: 'unavailable', reason: describe(resource.error) });
          }
        } catch (err) {
          if (!cancelled) setState({ kind: 'unavailable', reason: describe(err) });
        }
      })();
    }, RESOLVE_DELAY_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
    // The session identity, not the object, is what a read depends on.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, paired, path, srcKind, sessionKey]);

  if (srcKind === 'remote') {
    return (
      <span className="inline-flex flex-wrap items-baseline gap-1">
        <Image20Regular aria-hidden="true" className="text-gray-500" />
        <a
          href={src}
          target="_blank"
          rel="noreferrer noopener"
          className="break-all text-blue-500 underline"
        >
          {alt || src}
        </a>
        <span className="text-[12px] text-gray-500">远程图片，未自动加载</span>
      </span>
    );
  }

  if (srcKind === 'inline') {
    return <RefusalChip text={alt || '图片'} note="内联 data: 图片不渲染" />;
  }

  if (srcKind === 'invalid' || path === null) {
    return <RefusalChip text={src} note="不是可渲染的图片引用" />;
  }

  if (state.kind === 'ready') {
    return (
      <span className="my-2 block">
        <button
          type="button"
          onClick={() => setZoomed(true)}
          title={`放大 ${state.entry.path}`}
          className="block max-w-full cursor-zoom-in"
        >
          <img
            src={state.url}
            alt={alt || state.entry.path}
            className="max-h-[420px] max-w-full rounded-control border border-line object-contain"
          />
        </button>
        <span className="mt-1 block font-mono text-[12px] text-gray-500">
          {state.entry.path} · {formatBytes(state.entry.size)}
        </span>
        {zoomed && (
          <ImageLightbox
            src={state.url}
            label={state.entry.path}
            meta={formatBytes(state.entry.size)}
            onClose={() => setZoomed(false)}
          />
        )}
      </span>
    );
  }

  if (state.kind === 'resolving') {
    return (
      <span className="my-2 inline-flex items-center gap-2 text-[13px] text-gray-500">
        <Image20Regular aria-hidden="true" />
        读取图片 {path}
      </span>
    );
  }

  return (
    <span className="my-2 inline-flex flex-wrap items-baseline gap-2 text-[13px]">
      <ImageOff20Regular aria-hidden="true" className="text-amber-600" />
      <FileRefButton text={path} />
      <span className="text-[12px] text-amber-700">{state.reason}</span>
    </span>
  );
};

/**
 * Memoized on the reference: a transcript row that re-renders for an unrelated
 * reason (a fold, an activity tick) must not re-resolve its image.
 */
export const MarkdownImage = React.memo(MarkdownImageBody);
