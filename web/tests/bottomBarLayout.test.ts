/**
 * Source guard for the bottom bar layout decision.
 *
 * The bar is a symmetric three-track grid (`1fr auto 1fr`) so the current-turn
 * telemetry sits in the exact horizontal centre; the left column carries
 * activity + MCP / goal (the model and reasoning pickers moved into the composer)
 * and the right track stays an empty spacer. This test pins that decision against
 * a future edit that would slide the telemetry to the right edge.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, '..', 'src', 'components', 'BottomBar.tsx'), 'utf8');

function classNamesOf(anchor: string): string {
  const index = source.indexOf(anchor);
  assert.ok(index >= 0, `BottomBar must contain ${anchor}`);
  const match = /className="([^"]*)"/.exec(source.slice(index));
  assert.ok(match, `no className found after ${anchor}`);
  return match[1];
}

test('the bottom bar keeps the symmetric three-track grid', () => {
  const footer = classNamesOf('<footer');
  assert.ok(
    footer.includes('grid-cols-[1fr_auto_1fr]'),
    'the footer must keep 1fr auto 1fr so the centre track stays centred',
  );
  assert.equal(footer.includes('justify-end'), false, 'the bar must not be right-aligned');
});

test('the bar never clips the popover it anchors', () => {
  // The MCP popover is `absolute bottom-8` inside this bar, so `overflow-hidden`
  // on the footer hid it completely: it mounted, and nothing was visible.  The
  // no-wrap rule that keeps the bar 28px tall must come from `whitespace-nowrap`
  // alone (growable labels truncate at their own `max-w`).
  const footer = source.slice(source.indexOf('<footer'), source.indexOf('</footer>'));
  assert.ok(footer.includes('whitespace-nowrap'), 'the bar must not wrap its labels');
  assert.equal(footer.includes('overflow-hidden'), false, 'the bar must not clip its popover');
  // The popover really is anchored inside the bar, which is what makes the rule
  // above necessary rather than cosmetic.
  assert.ok(source.includes('<McpPanel'), 'the MCP popover is anchored in the bar');
  const panel = readFileSync(join(here, '..', 'src', 'components', 'McpPanel.tsx'), 'utf8');
  assert.ok(panel.includes('absolute bottom-'), 'the popover must stay anchored to its trigger');
});

test('the telemetry block is centred, not pushed to the right edge', () => {
  const centre = classNamesOf('{/* Centre: all turn telemetry');
  assert.ok(centre.includes('justify-self-center'), 'the telemetry block must self-centre');
  assert.equal(centre.includes('ml-auto'), false);
  assert.equal(centre.includes('justify-end'), false);
});
