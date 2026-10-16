/**
 * Project Task (Backlog) View Model and Utilities.
 */
import type { ProjectTaskView } from '../runtime-client/contract.generated.ts';

export type { ProjectTaskView };

export type TaskStatus = 'todo' | 'in_progress' | 'blocked' | 'done';
export type TaskPriority = 'low' | 'medium' | 'high' | 'critical';

export const TASK_STATUSES: readonly TaskStatus[] = ['todo', 'in_progress', 'blocked', 'done'] as const;

export const STATUS_LABELS: Record<TaskStatus, string> = {
  todo: '待办 (To Do)',
  in_progress: '进行中 (In Progress)',
  blocked: '受阻 (Blocked)',
  done: '已完成 (Done)',
};

export const PRIORITY_LABELS: Record<TaskPriority, string> = {
  low: '低 (Low)',
  medium: '中 (Medium)',
  high: '高 (High)',
  critical: '紧急 (Critical)',
};

export const TASK_PRIORITIES: readonly TaskPriority[] = [
  'low',
  'medium',
  'high',
  'critical',
] as const;

/**
 * The backend's title ceiling (`src/synapse/tasks/model.py`).
 *
 * The form mirrors only the title cap, and only so an obviously-too-long title
 * does not spend a round trip to be refused.  Nothing here truncates or rewrites
 * what the reader typed; other field limits are enforced by the backend.
 */
export const TASK_TITLE_MAX_CHARS = 200;

/**
 * Codepoint length (`[...value].length`), not UTF-16 code units, so a title with
 * astral characters is measured the way the Python backend measures it (`len`).
 * `String.prototype.length` would over-count a surrogate pair and could reject a
 * value the backend would accept.
 */
export function codePointLength(value: string): number {
  return [...value].length;
}

/** The four editable fields of a task's create/edit draft (labels kept as raw text). */
export interface TaskDraft {
  title: string;
  description: string;
  priority: TaskPriority;
  labels: string;
}

/**
 * Only the fields an edit actually changed.  Status and the session binding are
 * deliberately absent: the card's own status selector owns the former and arrange
 * owns the latter, so an edit can never echo a stale snapshot over a concurrent
 * change.
 */
export interface TaskDraftChanges {
  title?: string;
  description?: string;
  priority?: TaskPriority;
  labels?: string[];
}

/** A blank draft; also what the form resets to on close. */
export function emptyTaskDraft(): TaskDraft {
  return { title: '', description: '', priority: 'medium', labels: '' };
}

/** Prefill a draft from the *saved* task, so reopening an edit shows stored values. */
export function draftFromTask(task: ProjectTaskView): TaskDraft {
  const priority = task.priority as TaskPriority;
  return {
    title: task.title,
    description: task.description ?? '',
    priority: TASK_PRIORITIES.includes(priority) ? priority : 'medium',
    labels: (task.labels ?? []).join(', '),
  };
}

/**
 * Canonical label list, shared by the create and edit forms: split on either
 * script's comma or whitespace, then trim, lowercase and de-duplicate.  An empty
 * result is the legitimate "no labels" value, not a failure.
 */
export function parseTaskLabels(input: string): string[] {
  const seen = new Set<string>();
  const labels: string[] = [];
  for (const part of input.split(/[,，\s]+/)) {
    const label = part.trim().toLowerCase();
    if (label === '' || seen.has(label)) continue;
    seen.add(label);
    labels.push(label);
  }
  return labels;
}

/** A title is valid when its trimmed form is non-empty and within the backend cap. */
export function isTaskTitleValid(title: string): boolean {
  const trimmed = title.trim();
  return trimmed.length > 0 && codePointLength(trimmed) <= TASK_TITLE_MAX_CHARS;
}

function sameLabels(a: readonly string[], b: readonly string[]): boolean {
  const left = a.map((label) => label.trim().toLowerCase()).filter(Boolean);
  const right = b.map((label) => label.trim().toLowerCase()).filter(Boolean);
  return left.length === right.length && left.every((label, index) => label === right[index]);
}

/**
 * The changed subset of `draft` relative to the task when the editor opened.
 * Comparing against that snapshot lets a submit send only touched fields: an untouched
 * field, status or session binding is never echoed back over a background change
 * that landed while the form was open. Clearing a description or the
 * labels yields `''` / `[]` (a real change), never `undefined`.
 */
export function taskDraftChanges(task: ProjectTaskView, draft: TaskDraft): TaskDraftChanges {
  const changes: TaskDraftChanges = {};
  const title = draft.title.trim();
  if (title !== task.title) changes.title = title;
  if (draft.description !== task.description) changes.description = draft.description;
  if (draft.priority !== task.priority) changes.priority = draft.priority;
  // Existing backend tags can contain spaces or commas. Leaving the prefilled
  // field alone must preserve them, not reinterpret them as new delimiters.
  if (draft.labels !== (task.labels ?? []).join(', ')) {
    const labels = parseTaskLabels(draft.labels);
    if (!sameLabels(labels, task.labels ?? [])) changes.labels = labels;
  }
  return changes;
}

/**
 * The chip palette, named through the theme contract (`src/index.css`).
 *
 * Every colour here is a step the contract replaces per theme, so a chip is a
 * tint of whatever surface it sits on.  Two rules make it legible in both:
 *
 * - the fill is the family's `500` step at 15% (a tint of the surface), and
 * - the label is the step that stays legible on *both* surfaces.  For the
 *   families whose dark theme repoints their `700` step to a light value
 *   (gray / amber / emerald / red) that is `700`; blue is the exception, because
 *   its `600`/`700` steps are the dark theme's brand *background* roles (a deep
 *   navy that is unreadable as text), so blue text takes `500`, the brand
 *   *foreground* role.
 *
 * Tailwind's own families (`zinc`, `indigo`, `orange`, ...) are deliberately not
 * used: they resolve to fixed values the theme cannot reach, which is what made
 * the board unreadable in the dark theme.
 */
export const STATUS_COLORS: Record<TaskStatus, { bg: string; text: string; border: string }> = {
  todo: {
    bg: 'bg-gray-500/15',
    text: 'text-gray-700',
    border: 'border-gray-500/30',
  },
  in_progress: {
    bg: 'bg-blue-500/15',
    text: 'text-blue-500',
    border: 'border-blue-500/30',
  },
  blocked: {
    bg: 'bg-amber-500/15',
    text: 'text-amber-700',
    border: 'border-amber-500/30',
  },
  done: {
    bg: 'bg-emerald-500/15',
    text: 'text-emerald-700',
    border: 'border-emerald-500/30',
  },
};

export const PRIORITY_COLORS: Record<TaskPriority, { bg: string; text: string }> = {
  low: { bg: 'bg-gray-500/15', text: 'text-gray-700' },
  medium: { bg: 'bg-blue-500/15', text: 'text-blue-500' },
  high: { bg: 'bg-amber-500/15', text: 'text-amber-700' },
  critical: { bg: 'bg-red-500/15', text: 'text-red-700' },
};

/**
 * The "bound to a session" mark: the one chip that says *where* the task is
 * being worked on, so it takes a hue no status or priority uses (purple is the
 * theme's remaining tinted family, and it is replaced by both themes).
 */
export const ASSIGNED_CHIP = 'bg-purple-500/15 text-purple-600 border-purple-500/30';

export function groupTasksByStatus(
  tasks: readonly ProjectTaskView[],
): Record<TaskStatus, ProjectTaskView[]> {
  const grouped: Record<TaskStatus, ProjectTaskView[]> = {
    todo: [],
    in_progress: [],
    blocked: [],
    done: [],
  };

  for (const task of tasks) {
    const st = (task.status as TaskStatus) in grouped ? (task.status as TaskStatus) : 'todo';
    grouped[st].push(task);
  }

  return grouped;
}

/** How a multi-selection is arranged onto sessions (`arrangeTasks`). */
export type TaskArrangeMode = 'shared' | 'parallel';

/** One session's worth of an arrange run; a `null` thread id needs a new session. */
export interface TaskArrangeGroup {
  threadId: string | null;
  tasks: ProjectTaskView[];
}

/** What an arrange run did, for the board's own confirmation line. */
export interface TaskArrangeResult {
  /** Tasks now bound to a session (newly bound, or already there). */
  bound: number;
  /** Thread ids of the sessions the selection was arranged onto. */
  sessions: string[];
}

/**
 * Group a selection into the sessions an arrange run binds it to.
 *
 * The grouping *is* the policy, so it lives here and is testable without a
 * runtime: a `null` thread id means "this group still needs a session".
 *
 * - `shared` yields exactly one group.  It reuses the session the selection
 *   already shares -- pressing arrange twice is then a no-op -- while a selection
 *   spread over several sessions is re-bound onto one fresh session.
 * - `parallel` yields one group per task, each reusing that task's own session
 *   when it already has one, so only the unbound tasks open a session.
 */
export function planTaskArrange(
  tasks: readonly ProjectTaskView[],
  mode: TaskArrangeMode,
): TaskArrangeGroup[] {
  if (mode === 'shared') {
    const boundIds = [
      ...new Set(
        tasks
          .map((task) => task.assigned_thread_id)
          .filter((id): id is string => id !== null && id !== ''),
      ),
    ];
    return [{ threadId: boundIds.length === 1 ? boundIds[0] : null, tasks: [...tasks] }];
  }
  return tasks.map((task) => ({
    threadId: task.assigned_thread_id !== null && task.assigned_thread_id !== ''
      ? task.assigned_thread_id
      : null,
    tasks: [task],
  }));
}

export function formatTaskTime(timestampMs: number): string {
  if (!timestampMs) return '';
  const date = new Date(timestampMs);
  return date.toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}
