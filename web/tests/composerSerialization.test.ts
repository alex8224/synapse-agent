/**
 * The composer's document model and its projection onto the wire contract.
 *
 * The serializer is the one place where the rich editor can break the runtime:
 * everything else is presentation, but a wrong projection changes what the model
 * is asked to do.  These cases pin the mapping — text stays text, a line break
 * becomes `\n`, a mention becomes its token, an image becomes an attachment ref
 * and *no* text — plus the two ways it could silently go wrong: reading a pill's
 * own markup as prompt text, and gluing two pasted lines into one.
 *
 * `composerDocument.ts` is deliberately DOM-typed (it is a browser module), so
 * this test runs it against a minimal stand-in for the handful of DOM facts it
 * uses: node types, `childNodes`, `nodeValue`, `tagName` and `getAttribute`.
 * That keeps the test offline, which is what the repository's `*.test.ts` net
 * is for; real caret/selection behaviour is covered by the browser verify
 * scripts instead.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  CARET_ANCHOR,
  isSnapshotEmpty,
  PILL_ATTRIBUTE,
  PILL_ID_ATTRIBUTE,
  serializeComposer,
  type ComposerRegistry,
} from '../src/components/composer/composerDocument.ts';

const TEXT = 3;
const ELEMENT = 1;

class FakeText {
  nodeType = TEXT;
  nodeValue: string;

  constructor(nodeValue: string) {
    this.nodeValue = nodeValue;
  }
}

class FakeElement {
  nodeType = ELEMENT;
  childNodes: Array<FakeText | FakeElement> = [];
  attrs = new Map<string, string>();
  tagName: string;

  constructor(tagName: string) {
    this.tagName = tagName;
  }

  append(...nodes: Array<FakeText | FakeElement>): this {
    this.childNodes.push(...nodes);
    return this;
  }

  setAttribute(name: string, value: string): void {
    this.attrs.set(name, value);
  }

  getAttribute(name: string): string | null {
    return this.attrs.get(name) ?? null;
  }

  hasAttribute(name: string): boolean {
    return this.attrs.has(name);
  }
}

/** A pill element as the editor stamps it. */
function pill(pillId: string, tag = 'SPAN'): FakeElement {
  const element = new FakeElement(tag);
  element.setAttribute(PILL_ATTRIBUTE, '');
  element.setAttribute(PILL_ID_ATTRIBUTE, pillId);
  return element;
}

function registryOf(
  rows: Array<{ pillId: string; kind: 'file' | 'skill' | 'context' | 'image'; token?: string; localId?: string }>,
): ComposerRegistry {
  const pills = new Map();
  for (const row of rows) {
    pills.set(
      row.pillId,
      row.kind === 'image'
        ? { kind: 'image' as const, pillId: row.pillId, localId: row.localId ?? 'att-1' }
        : {
            kind: row.kind,
            pillId: row.pillId,
            token: row.token ?? '@x',
            label: row.token ?? 'x',
            detail: '',
          },
    );
  }
  return { pills };
}

test('plain text and line breaks project onto the prompt verbatim', () => {
  const root = new FakeElement('DIV')
    .append(new FakeText('第一行'))
    .append(new FakeElement('BR'))
    .append(new FakeText('第二行'));
  const snapshot = serializeComposer(root as unknown as Node, registryOf([]));
  assert.equal(snapshot.text, '第一行\n第二行');
  assert.deepEqual(snapshot.imageLocalIds, []);
});

test('a caret anchor never reaches the prompt', () => {
  // The editor parks the caret in a zero-width-space text node so the next
  // keystroke lands *after* a pasted pill instead of in the text before it; the
  // anchor is an insertion point, never content.
  const node = pill('pill-img');
  const root = new FakeElement('DIV')
    .append(new FakeText('这是截图'))
    .append(node)
    .append(new FakeText(`${CARET_ANCHOR}后`));
  const snapshot = serializeComposer(
    root as unknown as Node,
    registryOf([{ pillId: 'pill-img', kind: 'image', localId: 'att-9' }]),
  );
  assert.equal(snapshot.text, '这是截图后');
  assert.deepEqual(snapshot.imageLocalIds, ['att-9']);
  // An anchor on its own is not content: the composer stays empty.
  const anchorOnly = new FakeElement('DIV').append(new FakeText(CARET_ANCHOR));
  assert.equal(isSnapshotEmpty(serializeComposer(anchorOnly as unknown as Node, registryOf([]))), true);
});

test('a mention pill becomes its token, never its own markup', () => {
  const node = pill('pill-1');
  // What the reader sees inside the pill: label plus a remove affordance.
  node.append(new FakeText('agent.py')).append(new FakeText('✕'));
  const root = new FakeElement('DIV')
    .append(new FakeText('见 '))
    .append(node)
    .append(new FakeText(' 的实现'));
  const snapshot = serializeComposer(
    root as unknown as Node,
    registryOf([{ pillId: 'pill-1', kind: 'file', token: '@src/synapse/app/agent.py' }]),
  );
  assert.equal(snapshot.text, '见 @src/synapse/app/agent.py 的实现');
});

test('skills and context keep their own token form', () => {
  const root = new FakeElement('DIV')
    .append(pill('p-skill'))
    .append(new FakeText(' 配合 '))
    .append(pill('p-ctx'));
  const snapshot = serializeComposer(
    root as unknown as Node,
    registryOf([
      { pillId: 'p-skill', kind: 'skill', token: '@skill:cua-driver' },
      { pillId: 'p-ctx', kind: 'context', token: '@context:git_diff' },
    ]),
  );
  assert.equal(snapshot.text, '@skill:cua-driver 配合 @context:git_diff');
});

test('an image contributes an attachment ref and no prompt text at all', () => {
  const node = pill('pill-img');
  node.append(new FakeText('screenshot.png')).append(new FakeText('42%'));
  const root = new FakeElement('DIV')
    .append(new FakeText('看这张图'))
    .append(node)
    .append(new FakeText('谢谢'));
  const snapshot = serializeComposer(
    root as unknown as Node,
    registryOf([{ pillId: 'pill-img', kind: 'image', localId: 'att-7' }]),
  );
  assert.equal(snapshot.text, '看这张图谢谢');
  assert.deepEqual(snapshot.imageLocalIds, ['att-7']);
});

test('image refs keep document order, so a submit sends what was arranged', () => {
  const root = new FakeElement('DIV')
    .append(pill('a'))
    .append(new FakeText(' 中间 '))
    .append(pill('b'));
  const snapshot = serializeComposer(
    root as unknown as Node,
    registryOf([
      { pillId: 'a', kind: 'image', localId: 'att-1' },
      { pillId: 'b', kind: 'image', localId: 'att-2' },
    ]),
  );
  assert.deepEqual(snapshot.imageLocalIds, ['att-1', 'att-2']);
});

test('a pasted block wrapper separates its line from the next sibling', () => {
  const pasted = new FakeElement('DIV').append(new FakeText('粘贴的一段'));
  const root = new FakeElement('DIV').append(pasted).append(new FakeText('继续输入'));
  const snapshot = serializeComposer(root as unknown as Node, registryOf([]));
  assert.equal(snapshot.text, '粘贴的一段\n继续输入');
});

test('a trailing block wrapper does not add a phantom newline', () => {
  const pasted = new FakeElement('DIV').append(new FakeText('最后一行'));
  const root = new FakeElement('DIV').append(new FakeText('开头')).append(pasted);
  const snapshot = serializeComposer(root as unknown as Node, registryOf([]));
  assert.equal(snapshot.text, '开头最后一行');
});

test('an unknown pill is dropped instead of leaking its label', () => {
  const node = pill('ghost');
  node.append(new FakeText('不该出现'));
  const root = new FakeElement('DIV').append(node);
  const snapshot = serializeComposer(root as unknown as Node, registryOf([]));
  assert.equal(snapshot.text, '');
});

test('an empty editor is empty, but a lone pill is not', () => {
  assert.equal(isSnapshotEmpty(serializeComposer(null, registryOf([]))), true);
  const blank = new FakeElement('DIV').append(new FakeText('\n'));
  assert.equal(isSnapshotEmpty(serializeComposer(blank as unknown as Node, registryOf([]))), true);

  const withImage = new FakeElement('DIV').append(pill('only'));
  assert.equal(
    isSnapshotEmpty(
      serializeComposer(withImage as unknown as Node, registryOf([
        { pillId: 'only', kind: 'image', localId: 'att-1' },
      ])),
    ),
    false,
  );

  const withMention = new FakeElement('DIV').append(pill('only'));
  assert.equal(
    isSnapshotEmpty(
      serializeComposer(withMention as unknown as Node, registryOf([
        { pillId: 'only', kind: 'file', token: '@a.ts' },
      ])),
    ),
    false,
  );
});
