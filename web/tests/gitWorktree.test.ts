/**
 * Tests for the desktop shell's branch switch and worktree writes.
 *
 * Verifies:
 * - the `tauri_git_worktree_add` decoder accepts what the shell sends and
 *   refuses everything else (an extra key, a missing one, a non-boolean flag);
 * - the three writes are native-only, so a plain browser is told why instead of
 *   reaching for a runtime RPC that does not exist;
 * - each write names one command and one argument shape -- in particular
 *   `create_branch` stays snake_case, because the shell's command declares
 *   `rename_all = "snake_case"`.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import {
  GitHistoryUnavailableError,
  addGitWorktree,
  removeGitWorktree,
  switchGitBranch,
} from '../src/client/tauriGitFs.ts';
import { parseGitWorktreeAdd } from '../src/runtime-client/git.ts';

const here = dirname(fileURLToPath(import.meta.url));
const read = (relative: string): string => readFileSync(join(here, '..', 'src', relative), 'utf8');

interface Invocation {
  command: string;
  args: Record<string, unknown>;
}

function withTauri(handler: (command: string) => unknown): {
  calls: Invocation[];
  restore: () => void;
} {
  const calls: Invocation[] = [];
  const global = globalThis as unknown as { window?: unknown };
  const original = global.window;
  global.window = {
    __TAURI_INTERNALS__: {
      invoke: async (command: string, args: Record<string, unknown> = {}) => {
        calls.push({ command, args });
        return handler(command);
      },
    },
  };
  return {
    calls,
    restore: () => {
      global.window = original;
    },
  };
}

function withoutTauri(): () => void {
  const global = globalThis as unknown as { window?: unknown };
  const original = global.window;
  global.window = {};
  return () => {
    global.window = original;
  };
}

const ADD_PAYLOAD = {
  path: 'F:/work/synapse@feature-x',
  branch: 'feature/x',
  created_branch: true,
};

test('a created worktree decodes into camelCase fields', () => {
  const added = parseGitWorktreeAdd(ADD_PAYLOAD);
  assert.equal(added.path, 'F:/work/synapse@feature-x');
  assert.equal(added.branch, 'feature/x');
  assert.equal(added.createdBranch, true);
});

test('a malformed worktree payload is refused, not guessed at', () => {
  assert.throws(() => parseGitWorktreeAdd({ ...ADD_PAYLOAD, extra: 1 }), /unexpected keys/);
  // A missing key is the same refusal as an extra one: the key set must match
  // exactly, so a payload that lost a field is not half-decoded.
  assert.throws(() => parseGitWorktreeAdd({ path: 'p', branch: 'b' }), /unexpected keys/);
  assert.throws(() => parseGitWorktreeAdd({ ...ADD_PAYLOAD, path: null }), /must be a string/);
  assert.throws(
    () => parseGitWorktreeAdd({ ...ADD_PAYLOAD, created_branch: 'yes' }),
    /must be a boolean/,
  );
});

test('the branch and worktree writes are native-only', async () => {
  const restore = withoutTauri();
  try {
    await assert.rejects(() => switchGitBranch('main', false, '/w'), GitHistoryUnavailableError);
    await assert.rejects(() => addGitWorktree('main', true, '/w'), GitHistoryUnavailableError);
    await assert.rejects(() => removeGitWorktree('/w@main', false, '/w'), GitHistoryUnavailableError);
  } finally {
    restore();
  }
});

test('the three writes are named once, and the menu goes through the store', () => {
  // The menu is UI: it calls the store's named actions, never `invoke`, so the
  // console has exactly one place that knows a git command name.
  const menu = read('components/BranchMenu.tsx');
  assert.equal(menu.includes('tauriGitFs'), false, 'the menu must go through the store');
  assert.equal(/invoke[<(]/.test(menu), false, 'the menu must not invoke IPC itself');

  const store = read('stores/gitHistoryStore.ts');
  for (const action of ['switchBranch', 'createWorktree', 'removeWorktree']) {
    assert.ok(store.includes(`${action}:`), `the store must own ${action}`);
  }

  const bridge = read('client/tauriGitFs.ts');
  for (const command of [
    'tauri_git_switch_branch',
    'tauri_git_worktree_add',
    'tauri_git_worktree_remove',
  ]) {
    assert.equal(bridge.split(command).length - 1, 1, `${command} must be named once`);
  }
  // The shell derives the worktree directory from this convention, and the
  // sidebar folds projects by it; one token, two sides, so it is pinned here.
  assert.ok(
    read('components/SideBar.tsx').includes("lastIndexOf('@')"),
    'the sidebar folds worktrees by the same @ convention',
  );
});

test('each write names one command and its own argument shape', async () => {
  const shell = withTauri((command) => (command === 'tauri_git_worktree_add' ? ADD_PAYLOAD : null));
  try {
    await switchGitBranch('main', false, '/w');
    await switchGitBranch('topic', true, '/w');
    const added = await addGitWorktree('feature/x', true, '/w');
    await removeGitWorktree('F:/work/synapse@feature-x', false, '/w');
    await removeGitWorktree('F:/work/synapse@feature-x', true);

    assert.equal(added.path, 'F:/work/synapse@feature-x');
    assert.deepEqual(
      shell.calls.map((call) => call.command),
      [
        'tauri_git_switch_branch',
        'tauri_git_switch_branch',
        'tauri_git_worktree_add',
        'tauri_git_worktree_remove',
        'tauri_git_worktree_remove',
      ],
    );
    assert.deepEqual(shell.calls[0]?.args, { workspace: '/w', branch: 'main', create: false });
    assert.deepEqual(shell.calls[1]?.args, { workspace: '/w', branch: 'topic', create: true });
    // The shell's command is `rename_all = "snake_case"`, so this key is not
    // camelCased on the way out.
    assert.deepEqual(shell.calls[2]?.args, {
      workspace: '/w',
      branch: 'feature/x',
      create_branch: true,
    });
    assert.deepEqual(shell.calls[3]?.args, {
      workspace: '/w',
      path: 'F:/work/synapse@feature-x',
      force: false,
    });
    // No workspace given: the shell falls back to the one it was started in, so
    // the key is omitted rather than sent as an empty string.
    assert.deepEqual(shell.calls[4]?.args, {
      workspace: undefined,
      path: 'F:/work/synapse@feature-x',
      force: true,
    });
  } finally {
    shell.restore();
  }
});
