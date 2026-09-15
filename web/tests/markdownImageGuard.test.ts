/**
 * Source guards for image rendering in the transcript.
 *
 * The parser test proves `![alt](src)` becomes an image node; these guards pin
 * down what the renderer is allowed to do with it.  They read the sources
 * instead of rendering, because the property that matters is a *shape* of the
 * code: which URL reaches an `<img>`, and which branch may touch the network.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const webRoot = join(here, '..');
const read = (relative: string): string => readFileSync(join(webRoot, relative), 'utf8');

const markdown = read('src/components/Markdown.tsx');
const image = read('src/components/MarkdownImage.tsx');
const parse = read('src/markdown/parse.ts');
const refs = read('src/markdown/imageRefs.ts');

/**
 * Code without its comments, for the "this module does not do X" assertions:
 * prose about a network call is not a network call.
 */
function code(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
}

const imageCode = code(image);
const refsCode = code(refs);

test('the markdown renderer routes an image span to MarkdownImage', () => {
  assert.ok(
    markdown.includes("span.type === 'image'"),
    'Markdown.tsx must branch on the image span',
  );
  assert.ok(markdown.includes('<MarkdownImage'), 'Markdown.tsx must render MarkdownImage');
  assert.ok(
    markdown.includes('src={span.src}'),
    'Markdown.tsx must pass the reference source through',
  );
});

test('the parser validates an image source through imageRefs', () => {
  assert.ok(
    parse.includes("import { sanitizeImageSrc } from './imageRefs.ts';"),
    'parse.ts must delegate source validation to imageRefs.ts',
  );
  assert.ok(
    parse.includes('sanitizeImageSrc(image[2])'),
    'parse.ts must validate the parsed target before building an image span',
  );
});

test('imageRefs keeps the classification in one pure place', () => {
  assert.ok(refs.includes('export function classifyImageSrc'));
  assert.ok(refs.includes('export function sanitizeImageSrc'));
  // No DOM and no network in the classifier.
  assert.ok(!/\b(document|window)\s*\./.test(refsCode), 'imageRefs.ts must stay DOM-free');
  assert.ok(!/\bfetch\s*\(/.test(refsCode), 'imageRefs.ts must never fetch');
});

test('only the resolved blob URL ever reaches an <img>', () => {
  const imgTags = image.match(/<img\b/g) ?? [];
  assert.equal(imgTags.length, 1, 'MarkdownImage.tsx must render exactly one <img>');
  assert.ok(image.includes('src={state.url}'), 'the <img> must use the resolved blob URL');
  assert.ok(
    !image.includes('src={src}'),
    'the raw reference source must never be handed to <img>',
  );
  assert.ok(
    image.indexOf("if (state.kind === 'ready')") < image.indexOf('<img'),
    'the <img> must sit behind the ready state',
  );
});

test('a remote source is linked, never fetched', () => {
  assert.ok(image.includes("srcKind === 'remote'"), 'the remote case must be handled');
  assert.ok(image.includes('href={src}'), 'a remote source must stay a real link');
  assert.ok(
    image.includes('rel="noreferrer noopener"'),
    'a remote link must not leak the referrer',
  );
  assert.ok(!/\bfetch\s*\(/.test(imageCode), 'MarkdownImage.tsx must never fetch');
  assert.ok(
    !/XMLHttpRequest|EventSource|WebSocket/.test(imageCode),
    'MarkdownImage.tsx must have no other network path',
  );
  assert.ok(
    image.indexOf("srcKind === 'remote'") < image.indexOf("if (state.kind === 'resolving')"),
    'the remote branch must be decided before anything is resolved',
  );
});

test('an inline data payload is refused, not rendered', () => {
  assert.ok(image.includes("srcKind === 'inline'"), 'the inline case must be handled');
  assert.ok(
    image.includes('内联 data: 图片不渲染'),
    'an inline payload must be refused with a visible reason',
  );
});

test('a refusal always falls back to the file reference button', () => {
  assert.ok(image.includes('<FileRefButton'), 'the fallback must be the shared file button');
  assert.ok(!image.includes('dangerouslySetInnerHTML'), 'no markup injection in this path');
});
