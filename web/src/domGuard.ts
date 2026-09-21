/**
 * Monkey-patches `Node.prototype.removeChild` and `Node.prototype.insertBefore`
 * to protect React reconciliation against desynchronization caused by external
 * DOM mutations (e.g. browser extensions like Dark Reader, Chrome translation,
 * readability tools) and `contenteditable` browser operations.
 *
 * Background:
 * When React unmounts or moves a Fiber node during its commit phase, it calls
 * `hostParent.removeChild(childNode)`. If an external script, browser editing
 * command, or Range operation moved or removed `childNode`, calling native
 * `removeChild` throws a fatal `NotFoundError: Failed to execute 'removeChild' on 'Node':
 * The node to be removed is not a child of this node.`
 * In React 19, an uncaught error in commit phase unmounts the entire root, causing
 * an unrecoverable blank white screen.
 *
 * Guarantees:
 * - If `child.parentNode === this`: executes native `removeChild` unchanged.
 * - If `child.parentNode !== this` and `child.parentNode` exists: delegates deletion
 *   to `child.parentNode` so the node is safely detached without throwing.
 * - If `child.parentNode` is null (already detached): returns `child` gracefully without throwing.
 * - For `insertBefore`: if `referenceNode` is no longer a child of `this`, falls back
 *   to inserting into `referenceNode.parentNode` if present, or `this.appendChild(newNode)`.
 */
export function installDomMutationGuards(): void {
  if (typeof window === 'undefined' || typeof Node === 'undefined' || !Node.prototype) {
    return;
  }

  const originalRemoveChild = Node.prototype.removeChild;
  Node.prototype.removeChild = function <T extends Node>(child: T): T {
    if (!child) return child;
    if (child.parentNode !== this) {
      if (child.parentNode) {
        return child.parentNode.removeChild(child) as T;
      }
      return child;
    }
    return originalRemoveChild.call(this, child) as T;
  };

  const originalInsertBefore = Node.prototype.insertBefore;
  Node.prototype.insertBefore = function <T extends Node>(newNode: T, referenceNode: Node | null): T {
    if (referenceNode && referenceNode.parentNode !== this) {
      if (referenceNode.parentNode) {
        return referenceNode.parentNode.insertBefore(newNode, referenceNode) as T;
      }
      return this.appendChild(newNode) as T;
    }
    return originalInsertBefore.call(this, newNode, referenceNode) as T;
  };
}
