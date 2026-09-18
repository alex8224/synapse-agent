/**
 * Source guards for the composer's image paths.
 *
 * A pasted screenshot must behave exactly like a picked or dropped file — one
 * `handleFiles` path, one validation pipeline — and the composer must show the
 * picked image *before* the turn is submitted.  Both are wiring facts that only
 * exist in the JSX, so they are pinned here the same way the MCP panel guard
 * pins its own contract.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));
const composer = readFileSync(join(here, '..', 'src', 'components', 'CommandInput.tsx'), 'utf8');
const richComposer = readFileSync(
  join(here, '..', 'src', 'components', 'composer', 'RichComposer.tsx'),
  'utf8',
);
const preview = readFileSync(join(here, '..', 'src', 'components', 'AttachmentPreview.tsx'), 'utf8');
const bar = readFileSync(join(here, '..', 'src', 'components', 'BottomBar.tsx'), 'utf8');

test('the model and reasoning pickers live in the composer, not the status bar', () => {
  assert.ok(composer.includes('<ModelControls />'), 'the composer control row must host them');
  assert.ok(
    !bar.includes('切换模型 (F2)'),
    'the status bar must not keep a model trigger',
  );
  assert.ok(!bar.includes('推理等级'), 'the status bar must not keep a reasoning trigger');
  // They configure the next turn, so they sit in the same row as the send button.
  const row = composer.slice(composer.indexOf('{/* Control row'));
  assert.ok(row.includes('<ModelControls />'), 'the pickers belong to the control row');
  assert.ok(row.includes('<ArrowUp20Regular'), 'and the primary action is in that row too');
});

test('a pasted image goes through the same path as a picked one', () => {
  assert.ok(composer.includes('onPaste='), 'the composer must handle paste');
  assert.ok(
    /onPaste=\{[\s\S]*?handleFiles\(/.test(composer),
    'a pasted image must be routed to handleFiles, not a second upload path',
  );
  assert.ok(
    /onPaste=\{[\s\S]*?clipboardData\?\.files/.test(composer),
    'the paste handler must read the clipboard files',
  );
  // A text paste must keep its default behaviour: the handler returns before
  // preventDefault when the clipboard carries no file.
  const handler = composer.slice(composer.indexOf('onPaste='));
  assert.ok(
    handler.indexOf('return;') < handler.indexOf('preventDefault()'),
    'a file-less paste must return before preventDefault',
  );
});

test('the composer previews the pick before submit', () => {
  // The pending image is rendered as an inline pill inside the editor, so the
  // preview lives in the rich composer rather than in the card that hosts it.
  assert.ok(
    richComposer.includes('<AttachmentPreview'),
    'each pending row must render the preview',
  );
  assert.ok(
    richComposer.includes('source={entry.source}'),
    'the preview must render the local pick, not a remote read',
  );
});

test('the preview creates one object URL per blob and revokes it on cleanup', () => {
  assert.ok(preview.includes('URL.createObjectURL'), 'the preview must build a local object URL');
  assert.ok(preview.includes('URL.revokeObjectURL'), 'the preview must release it again');
  // Created once per blob, never per render: hovering must not leak a URL.
  assert.ok(
    /urlRef\.current \?\?= URL\.createObjectURL\(blob\)/.test(preview),
    'the URL must be created once per blob, not per render',
  );
  assert.ok(
    /useEffect\(\s*\(\) => \(\) => \{[\s\S]*?revokeObjectURL/.test(preview),
    'the revoke must run as the effect cleanup, so unmount and submit both release it',
  );
  // A non-Blob source (an injected double) must degrade to the icon, never throw.
  assert.ok(preview.includes('instanceof Blob'), 'a non-Blob source must be tolerated');
});
