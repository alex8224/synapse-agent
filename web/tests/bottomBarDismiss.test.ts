/**
 * Source guard for the popover dismissal contract.
 *
 * Every popover must close as soon as it loses focus — a click anywhere outside
 * its own trigger+panel, or Escape — so each one has to live inside its own
 * ref'd wrapper, which is what the outside-click handler tests against.  The MCP
 * popover and the F1 help overlay are in the status bar; the model and reasoning
 * pickers moved into the composer's control row and keep the same contract.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, '..', 'src', 'components', 'BottomBar.tsx'), 'utf8');
const controls = readFileSync(
  join(here, '..', 'src', 'components', 'ModelControls.tsx'),
  'utf8',
);

test('every popover sits in a ref-wrapped trigger container', () => {
  assert.ok(source.includes('ref={mcpRef}'), 'the MCP wrapper must stay in the bar');
  assert.equal(
    source.split('className="relative" ref={').length - 1,
    1,
    'the bar may only keep the MCP popover now that the pickers moved out',
  );
  for (const ref of ['modelRef', 'thinkingRef']) {
    assert.ok(
      controls.includes(`ref={${ref}}`),
      `${ref} must wrap its trigger+panel in the composer`,
    );
  }
});

test('a click outside the open popover closes it', () => {
  for (const [name, file] of [
    ['the bar', source],
    ['the composer controls', controls],
  ] as const) {
    assert.ok(
      file.includes("document.addEventListener('mousedown'"),
      `${name} must listen for outside clicks while a popover is open`,
    );
    assert.ok(
      file.includes('ref.current?.contains(target)'),
      `${name} must keep clicks inside the popover open`,
    );
    assert.ok(
      file.includes('if (!inside) closeOthers()'),
      `${name} must close the open popover on an outside click`,
    );
  }
});

test('Escape closes the popovers and the help overlay', () => {
  const escape = source.indexOf("e.key === 'Escape'");
  assert.ok(escape >= 0, 'Escape must be handled by the bar');
  const tail = source.slice(escape, escape + 400);
  assert.ok(tail.includes('closeOthers()'), 'Escape must close the popovers');
  assert.ok(tail.includes('setShowHelp(false)'), 'Escape must close the help overlay');
  // The composer's own popovers dismiss on Escape as well.
  assert.ok(/key === 'Escape'/.test(controls), 'Escape must reach the composer popovers');
  assert.ok(controls.includes('closeOthers()'));
});
