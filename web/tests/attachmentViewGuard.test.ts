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
const richComposer = readFileSync(
  join(here, '..', 'src', 'components', 'composer', 'RichComposer.tsx'),
  'utf8',
);
const previewFlyout = readFileSync(
  join(here, '..', 'src', 'components', 'composer', 'ImagePreviewFlyout.tsx'),
  'utf8',
);

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
  // The pill keeps the name and the failure reason in its tooltip rather than
  // spending a row on them, and the file size is never printed inline.
  assert.ok(
    richComposer.includes('title={entry.error ??'),
    'the pill tooltip must carry the name and the failure reason',
  );
  assert.ok(
    !richComposer.includes('formatBytes'),
    'the composer must not print the file size inside the pill',
  );
});

test('hovering a composer pill reveals the enlarged copy', () => {
  assert.ok(preview.includes('thumbRef'), 'the thumbnail must be assigned the object URL');
  // The enlarged copy is a portalled flyout anchored to the thumbnail: inside the
  // editor's own scroller it would be clipped by the composer card.
  assert.ok(
    richComposer.includes('<ImagePreviewFlyout'),
    'hovering a pill must mount the enlarged copy',
  );
  assert.ok(
    richComposer.includes('onMouseEnter={(event) => onHover(event.currentTarget, entry)}'),
    'the hover target must be the pill',
  );
  assert.ok(
    previewFlyout.includes('createPortal') || previewFlyout.includes('<Portal'),
    'the enlarged copy must not be clipped by the editor',
  );
  assert.ok(
    previewFlyout.includes('anchor.getBoundingClientRect()'),
    'the enlarged copy must be anchored to the thumbnail, not to the card',
  );
});
