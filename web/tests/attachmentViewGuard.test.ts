/**
 * Source guards for how transcript images are displayed.
 *
 * A screenshot must stay readable: the thumbnail keeps its aspect ratio instead
 * of being cropped into a square, and clicking it opens the full-size view.  Both
 * are JSX facts, so they are pinned here the way the MCP panel guard pins its own
 * contract.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));
const read = (name: string) => readFileSync(join(here, '..', 'src', 'components', name), 'utf8');

const thumb = read('AttachmentThumb.tsx');
const preview = read('AttachmentPreview.tsx');
const lightbox = read('ImageLightbox.tsx');
const composer = readFileSync(join(here, '..', 'src', 'components', 'CommandInput.tsx'), 'utf8');

test('images keep their aspect ratio instead of being cropped into a square', () => {
  for (const [name, source] of [
    ['AttachmentThumb.tsx', thumb],
    ['AttachmentPreview.tsx', preview],
  ] as const) {
    assert.ok(!source.includes('object-cover'), `${name} must not crop the image`);
    assert.ok(source.includes('object-contain'), `${name} must preserve the aspect ratio`);
  }
});

test('a transcript image opens a full-size view', () => {
  assert.ok(thumb.includes('<ImageLightbox'), 'the thumbnail must be able to open the lightbox');
  assert.ok(thumb.includes('cursor-zoom-in'), 'the thumbnail must look clickable');
  assert.ok(thumb.includes('onClick={() => setOpen(true)}'), 'a click must open the lightbox');
  // The enlarged view reuses the blob the thumbnail already resolved: no second
  // read, and the loader keeps ownership of the URL.
  assert.ok(thumb.includes('src={ready.url}'), 'the lightbox must reuse the resolved URL');
});

test('the lightbox is a dialog with three dismissal paths', () => {
  assert.ok(lightbox.includes('role="dialog"'), 'the enlarged view must be a dialog');
  assert.ok(/event\.key === 'Escape'/.test(lightbox), 'Escape must close it');
  assert.ok(lightbox.includes('onClick={onClose}'), 'the backdrop must close it');
  assert.ok(lightbox.includes('stopPropagation'), 'a click inside the panel must not close it');
  assert.ok(lightbox.includes('fixed inset-0'), 'the overlay must not be clipped by the transcript');
});

test('a composer row is the image, not a file-name chip', () => {
  assert.ok(
    !composer.includes('>{entry.name}</span>'),
    'the composer must not print the file name next to the thumbnail',
  );
  assert.ok(
    !composer.includes("tabular-nums text-gray-400"),
    'the composer must not print the file size next to the thumbnail',
  );
  // The name stays reachable without occupying the row.
  assert.ok(composer.includes('title={entry.error ??'), 'the chip tooltip must carry the name');
});

test('hovering a composer row reveals the enlarged copy', () => {
  assert.ok(preview.includes('group relative'), 'the hover target must be the chip wrapper');
  assert.ok(preview.includes('group-hover:block'), 'hovering must reveal the enlarged copy');
  assert.ok(
    preview.includes('hidden w-max'),
    'the enlarged copy must be hidden until hover, not a modal',
  );
  // Both <img> nodes share the single object URL of the blob.
  assert.ok(preview.includes('thumbRef'), 'the chip image must be assigned the URL');
  assert.ok(preview.includes('zoomRef'), 'the enlarged copy must be assigned the same URL');
});
