/**
 * The `@` catalog: what can be offered, and how a query ranks it.
 *
 * Two facts here are contracts rather than presentation.  A mention's `token` is
 * appended to the prompt verbatim, so it must stay in the one shape the agent is
 * told to expect (`@path`, `@skill:name`, `@context:name`) and must never be
 * derived from the visible label.  And the repository skills are mirrored by hand
 * — the Python side owns the real catalog and the console has no RPC for it — so
 * a skill added under `skills/` without a row here would silently disappear from
 * the flyout.  That mirror is checked against the checkout below.
 */
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import {
  CONTEXT_MENTIONS,
  fileMention,
  MENTION_GROUP_TITLE,
  MENTION_KIND_LABEL,
  rankMentions,
  SKILL_MENTIONS,
} from '../src/components/composer/mentionCatalog.ts';

const here = dirname(fileURLToPath(import.meta.url));

test('every mention token keeps the shape the agent is told to expect', () => {
  for (const entry of [...SKILL_MENTIONS, ...CONTEXT_MENTIONS]) {
    if (entry.kind === 'skill') {
      assert.match(entry.token, /^@skill:[a-z0-9-]+$/, `${entry.token} must be @skill:<name>`);
    } else {
      assert.match(entry.token, /^@context:[a-z_]+$/, `${entry.token} must be @context:<name>`);
    }
    // The token is what reaches the model, so a row with a missing or duplicated
    // one would send something the reader never saw.
    assert.ok(entry.token.length > 1, `${entry.id} needs a token`);
  }
  const tokens = [...SKILL_MENTIONS, ...CONTEXT_MENTIONS].map((entry) => entry.token);
  assert.equal(new Set(tokens).size, tokens.length, 'tokens must be unique');
});

test('a file mention serializes as its workspace path', () => {
  const entry = fileMention('src/synapse/app/agent.py');
  assert.equal(entry.kind, 'file');
  assert.equal(entry.token, '@src/synapse/app/agent.py');
  assert.equal(entry.label, 'agent.py');
  assert.equal(entry.detail, 'src/synapse/app/agent.py');
  // A path at the workspace root has no directory to strip.
  assert.equal(fileMention('README.md').label, 'README.md');
});

test('the shipped skills are all offered by the flyout', () => {
  // The console cannot read `skills/` at runtime, so this mirror is the only
  // thing keeping the two in step.  Adding a skill means adding a row here.
  const dir = join(here, '..', '..', 'skills');
  const names = readdirSync(dir, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => entry.name)
    .filter((name) => {
      try {
        readFileSync(join(dir, name, 'SKILL.md'), 'utf8');
        return true;
      } catch {
        return false;
      }
    })
    .sort();
  const offered = SKILL_MENTIONS.map((entry) => entry.label).sort();
  assert.deepEqual(offered, names, 'the skill mirror must match skills/<name>/SKILL.md');
});

test('a query ranks exact, prefix and substring matches ahead of the rest', () => {
  const ranked = rankMentions(SKILL_MENTIONS, 'cua-driver');
  assert.equal(ranked[0]?.label, 'cua-driver');

  const prefix = rankMentions(SKILL_MENTIONS, 'session');
  assert.ok(prefix.length >= 2, 'a prefix match must not collapse to one row');
  for (const entry of prefix) {
    assert.ok(entry.label.toLowerCase().includes('session') || entry.detail.includes('session'));
  }

  // The token is searchable too, so `@git` finds `git:diff` without its colon.
  const byToken = rankMentions(CONTEXT_MENTIONS, 'context:git');
  assert.equal(byToken.length, 1);
  assert.equal(byToken[0]?.label, 'git:diff');
});

test('an empty query offers everything and a miss offers nothing', () => {
  assert.equal(rankMentions(SKILL_MENTIONS, '').length, SKILL_MENTIONS.length);
  assert.equal(rankMentions(SKILL_MENTIONS, '').length, SKILL_MENTIONS.length);
  assert.deepEqual(rankMentions(SKILL_MENTIONS, 'zzzz-no-such-skill'), []);
});

test('every kind has a group title and a row label', () => {
  for (const kind of ['file', 'skill', 'context'] as const) {
    assert.ok(MENTION_GROUP_TITLE[kind].length > 0, `${kind} needs a group title`);
    assert.ok(MENTION_KIND_LABEL[kind].length > 0, `${kind} needs a row label`);
  }
});
