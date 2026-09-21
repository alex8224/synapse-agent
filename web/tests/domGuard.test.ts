import assert from 'node:assert/strict';
import { test } from 'node:test';
import { installDomMutationGuards } from '../src/domGuard.ts';

test('domGuard: safe removeChild handles normal, moved, and detached nodes', () => {
  // Polyfill a minimal DOM Node hierarchy for node:test runner
  class MockNode {
    parentNode: MockNode | null = null;
    children: MockNode[] = [];

    appendChild<T extends MockNode>(child: T): T {
      if (child.parentNode) {
        child.parentNode.removeChild(child);
      }
      child.parentNode = this;
      this.children.push(child);
      return child;
    }

    removeChild<T extends MockNode>(child: T): T {
      if (child.parentNode !== this) {
        throw new Error("Failed to execute 'removeChild' on 'Node': The node to be removed is not a child of this node.");
      }
      const idx = this.children.indexOf(child);
      if (idx !== -1) {
        this.children.splice(idx, 1);
      }
      child.parentNode = null;
      return child;
    }

    insertBefore<T extends MockNode>(newNode: T, refNode: MockNode | null): T {
      if (refNode && refNode.parentNode !== this) {
        throw new Error("Failed to execute 'insertBefore' on 'Node': The node before which the new node is to be inserted is not a child of this node.");
      }
      if (newNode.parentNode) {
        newNode.parentNode.removeChild(newNode);
      }
      newNode.parentNode = this;
      if (!refNode) {
        this.children.push(newNode);
      } else {
        const idx = this.children.indexOf(refNode);
        this.children.splice(idx, 0, newNode);
      }
      return newNode;
    }
  }

  // Setup global Node and window
  const originalGlobalNode = (globalThis as any).Node;
  const originalGlobalWindow = (globalThis as any).window;
  (globalThis as any).Node = MockNode;
  (globalThis as any).window = {};

  try {
    installDomMutationGuards();

    const parentA = new MockNode();
    const parentB = new MockNode();
    const child1 = new MockNode();
    const child2 = new MockNode();

    // 1. Normal removeChild
    parentA.appendChild(child1);
    assert.equal(child1.parentNode, parentA);
    const removed1 = parentA.removeChild(child1);
    assert.equal(removed1, child1);
    assert.equal(child1.parentNode, null);

    // 2. Detached node removeChild (would normally throw NotFoundError)
    assert.doesNotThrow(() => {
      const removedDetached = parentA.removeChild(child1);
      assert.equal(removedDetached, child1);
    });

    // 3. Moved node (e.g. extension moved child2 into parentB)
    parentA.appendChild(child2);
    parentB.appendChild(child2); // moved to parentB
    assert.equal(child2.parentNode, parentB);

    // React calls parentA.removeChild(child2)
    assert.doesNotThrow(() => {
      const removedMoved = parentA.removeChild(child2);
      assert.equal(removedMoved, child2);
      assert.equal(child2.parentNode, null);
      assert.equal(parentB.children.length, 0);
    });

    // 4. insertBefore with detached or moved referenceNode
    const ref = new MockNode();
    const incoming = new MockNode();
    parentB.appendChild(ref);

    // React calls parentA.insertBefore(incoming, ref) where ref is not child of parentA
    assert.doesNotThrow(() => {
      parentA.insertBefore(incoming, ref);
    });
    // incoming is inserted safely without throwing
    assert.ok(incoming.parentNode === parentA || incoming.parentNode === parentB);
  } finally {
    (globalThis as any).Node = originalGlobalNode;
    (globalThis as any).window = originalGlobalWindow;
  }
});
