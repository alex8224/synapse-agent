/**
 * Project Tasks & Backlog Tab for the Right Auxiliary Dock.
 *
 * Implements:
 * - Project-level backlog inspection and management across sessions
 * - Visual task cards with status columns (To Do / In Progress / Blocked / Done)
 * - Multi-select: a selection is arranged onto one shared session or onto one
 *   session per task (both are background rows; neither runs anything)
 */
import {
  Add16Regular,
  ArrowClockwise16Regular,
  ArrowCircleRight16Regular,
  Delete16Regular,
  Dismiss16Regular,
  Edit16Regular,
  Play16Regular,
  TaskListSquareLtr16Regular,
  Warning16Regular,
} from '@fluentui/react-icons';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useShallow } from 'zustand/react/shallow';
import {
  ASSIGNED_CHIP,
  PRIORITY_COLORS,
  PRIORITY_LABELS,
  STATUS_COLORS,
  STATUS_LABELS,
  TASK_PRIORITIES,
  TASK_STATUSES,
  draftFromTask,
  emptyTaskDraft,
  formatTaskTime,
  groupTasksByStatus,
  isTaskTitleValid,
  parseTaskLabels,
  taskDraftChanges,
  type ProjectTaskView,
  type TaskArrangeMode,
  type TaskDraft,
  type TaskPriority,
  type TaskStatus,
} from '../../stores/taskView.ts';
import { sessionKey } from '../../stores/sessionViews.ts';
import { useConsoleStore } from '../../stores/useConsoleStore.ts';
import type { RightDockContext, RightDockTabDefinition } from './contract.ts';

/**
 * The project/auth boundary one board interaction belongs to: the active project,
 * the runtime client serving it and the pairing that produced it.  Any of them
 * moving means the console has left the backlog the interaction started on.
 */
function currentBoardScope() {
  const state = useConsoleStore.getState();
  return {
    projectId: state.activeProjectId,
    client: state.client,
    pairing: state.pairingState,
  };
}
type BoardScope = ReturnType<typeof currentBoardScope>;

export const TasksContent: React.FC<{ context: RightDockContext }> = () => {
  const {
    activeProjectId,
    client,
    pairingState,
    projectTasks,
    projectTasksLoading,
    projectTasksError,
    loadProjectTasks,
    createProjectTask,
    updateProjectTask,
    deleteProjectTask,
    arrangeTasks,
    switchSession,
    executeTask,
    backgroundViews,
    currentSession,
    runtimeStatus,
  } = useConsoleStore(
    useShallow((state) => ({
      activeProjectId: state.activeProjectId,
      client: state.client,
      pairingState: state.pairingState,
      projectTasks: state.projectTasks,
      projectTasksLoading: state.projectTasksLoading,
      projectTasksError: state.projectTasksError,
      loadProjectTasks: state.loadProjectTasks,
      createProjectTask: state.createProjectTask,
      updateProjectTask: state.updateProjectTask,
      deleteProjectTask: state.deleteProjectTask,
      arrangeTasks: state.arrangeTasks,
      switchSession: state.switchSession,
      executeTask: state.executeTask,
      backgroundViews: state.backgroundViews,
      currentSession: state.currentSession,
      runtimeStatus: state.runtimeStatus,
    })),
  );

  const [filterStatus, setFilterStatus] = useState<string>('all');
  const [busyTaskId, setBusyTaskId] = useState<string | null>(null);
  /** Ticked cards.  Board-local: a selection is a reading gesture, not session state. */
  const [selectedIds, setSelectedIds] = useState<ReadonlySet<string>>(() => new Set());
  /** True while an arrange run (session creation + binding) is in flight. */
  const [arranging, setArranging] = useState(false);
  /** The last arrange run's own confirmation line; cleared by the next action. */
  const [notice, setNotice] = useState<string | null>(null);
  /**
   * The create/edit form is *one* form in two modes: `draft` holds whichever
   * fields are on screen, `editingTask` keeps the opening snapshot, and
   * `formMode` decides which handler a submit runs.  Opening one mode replaces
   * the other's draft outright, so a half-typed create never bleeds into an edit
   * and vice versa -- and only one of them is ever in flight.
   */
  const [formMode, setFormMode] = useState<'create' | 'edit' | null>(null);
  const [editingTask, setEditingTask] = useState<ProjectTaskView | null>(null);
  const [draft, setDraft] = useState<TaskDraft>(() => emptyTaskDraft());
  /** The form's own error line: a failed validation or the store's last write error. */
  const [formError, setFormError] = useState<string | null>(null);
  /** True while a create/edit submit is in flight; fences the form and every mutation. */
  const [savingForm, setSavingForm] = useState(false);
  /**
   * Same-tick re-entry guard for the submit.  React state lags a double click by
   * one render, so `savingForm` alone cannot stop two submits in one tick.
   */
  const formSubmittingRef = useRef(false);
  /** The batch-status target picked in the toolbar select; `''` means "not chosen". */
  const [batchStatus, setBatchStatus] = useState<TaskStatus | ''>('');

  /**
   * Monotonic generation for the board-local gestures above.  The store already
   * fences its own backlog writes by project/client/pairing, but a draft, a
   * selection, a filter, a busy flag and a notice have no such owner: a switch
   * must abandon them so the next project never inherits the last one's half-typed
   * form, ticked cards or confirmation line.  Bumping this is also what stops a
   * callback that lands *after* the boundary moved from writing into the project
   * that replaced it -- the store cannot see those local writes at all.
   */
  const lifecycleRef = useRef(0);

  /** Drop every board-local gesture and fence the async work that carried it. */
  const resetBoard = useCallback(() => {
    lifecycleRef.current += 1;
    setFormMode(null);
    setEditingTask(null);
    setDraft(emptyTaskDraft());
    setFormError(null);
    setSavingForm(false);
    formSubmittingRef.current = false;
    setBatchStatus('');
    setFilterStatus('all');
    setBusyTaskId(null);
    setSelectedIds(new Set());
    setArranging(false);
    setNotice(null);
  }, []);

  /**
   * Watch the boundary *synchronously*: a project switch, a re-pairing or a
   * logout moves `activeProjectId` / `client` / `pairingState`, and each must
   * reset the board in the same tick the store moves -- keying an effect on those
   * values would leave one render of the old project's draft on screen. Unmount
   * invalidates the generation and fences any batch still looping; already-sent
   * RPCs are not recalled and no agent turn is cancelled.
   */
  useEffect(() => {
    let last = currentBoardScope();
    const unsubscribe = useConsoleStore.subscribe(() => {
      const next = currentBoardScope();
      if (
        next.projectId === last.projectId
        && next.client === last.client
        && next.pairing === last.pairing
      ) {
        return;
      }
      last = next;
      resetBoard();
    });
    return () => {
      unsubscribe();
      lifecycleRef.current += 1;
    };
  }, [resetBoard]);

  /**
   * Whether an operation started at `generation` on `scope` may still touch the
   * board: same local generation *and* same project/client/pairing.  The
   * generation is what makes an A -> B -> A round trip safe -- the project id and
   * client are identical across it, so only the generation can tell the visits
   * apart.
   */
  const scopeCurrent = useCallback((generation: number, scope: BoardScope): boolean => {
    if (generation !== lifecycleRef.current) return false;
    const now = currentBoardScope();
    return (
      now.projectId === scope.projectId
      && now.client === scope.client
      && now.pairing === scope.pairing
    );
  }, []);

  useEffect(() => {
    // Reload whenever the boundary moves: another project, or the same project
    // served by a new runtime client (re-pairing) whose backlog we have not read.
    if (activeProjectId && client && pairingState === 'paired') {
      void loadProjectTasks();
    }
  }, [activeProjectId, client, pairingState, loadProjectTasks]);

  /**
   * True while any board mutation is in flight: an arrange run, one card's write,
   * or a form submit.  Every control that could start a *second* write is
   * disabled by this, so a save can never be double-submitted from another button.
   */
  const busy = busyTaskId !== null || arranging || savingForm;

  /** Open the create form, discarding whatever the edit form was holding. */
  const openCreate = (): void => {
    if (busy) return;
    setFormMode('create');
    setEditingTask(null);
    setDraft(emptyTaskDraft());
    setFormError(null);
  };

  /** Open the edit form prefilled from the saved row (full description, no truncation). */
  const openEdit = (task: ProjectTaskView): void => {
    if (busy) return;
    setFormMode('edit');
    // Compare with the opening snapshot, not a later refresh: unchanged fields
    // in an old draft must not overwrite another client's edits.
    setEditingTask({ ...task, labels: [...task.labels] });
    setDraft(draftFromTask(task));
    setFormError(null);
  };

  /** Close the form and drop its draft.  Sends no RPC. */
  const closeForm = (): void => {
    if (formSubmittingRef.current) return;
    setFormMode(null);
    setEditingTask(null);
    setDraft(emptyTaskDraft());
    setFormError(null);
  };

  /** The header's 新建 control: a second press closes the create form. */
  const toggleCreate = (): void => {
    if (busy) return;
    if (formMode === 'create') closeForm();
    else openCreate();
  };

  /**
   * Submit whichever form is open.
   *
   * The payload is resolved *before* the saving flag is raised, so a no-op edit
   * (nothing changed) closes without an RPC, and a row that vanished reports
   * without a write.  Only the changed fields are sent -- never the snapshot's
   * status or session binding -- and the generation/scope guard drops a result
   * that lands after the board moved, so a late save can neither close nor clear
   * the form of the project that replaced it.
   */
  const handleFormSubmit = async (e: React.FormEvent): Promise<void> => {
    e.preventDefault();
    if (formSubmittingRef.current || busy || formMode === null) return;
    if (!isTaskTitleValid(draft.title)) {
      setFormError(draft.title.trim() ? '标题不能超过 200 个字符' : '标题不能为空');
      return;
    }

    let run: () => Promise<boolean>;
    if (formMode === 'create') {
      const labels = parseTaskLabels(draft.labels);
      run = () => createProjectTask(
        draft.title.trim(),
        draft.description.trim() || undefined,
        draft.priority,
        labels.length > 0 ? labels : undefined,
      );
    } else {
      const task = projectTasks.find((t) => t.id === editingTask?.id);
      if (!task || !editingTask) {
        setFormError('待办已不存在，请刷新后重试');
        return;
      }
      const changes = taskDraftChanges(editingTask, draft);
      if (Object.keys(changes).length === 0) {
        // Nothing changed: closing is the honest no-op.  No RPC, and no echoing
        // the snapshot's status / session back over a concurrent change.
        closeForm();
        return;
      }
      const taskId = task.id;
      run = () => updateProjectTask(taskId, changes);
    }

    const generation = lifecycleRef.current;
    const scope = currentBoardScope();
    formSubmittingRef.current = true;
    setSavingForm(true);
    setFormError(null);
    try {
      const ok = await run();
      if (!scopeCurrent(generation, scope)) return;
      if (ok) {
        formSubmittingRef.current = false;
        closeForm();
        return;
      }
      // Keep the draft so the reader can retry; surface the store's own message
      // (e.g. an explicit not_found) rather than pretending the save landed.
      setFormError(
        useConsoleStore.getState().projectTasksError
          || (formMode === 'create' ? '创建失败，请重试' : '保存失败，请重试'),
      );
    } finally {
      if (scopeCurrent(generation, scope)) {
        formSubmittingRef.current = false;
        setSavingForm(false);
      }
    }
  };

  const handleStatusChange = async (task: ProjectTaskView, nextStatus: TaskStatus) => {
    if (busy || nextStatus === task.status) return;
    const generation = lifecycleRef.current;
    setBusyTaskId(task.id);
    try {
      // The select is controlled by the row, so a failure leaves the store
      // untouched and React restores the server value -- no fake success here.
      await updateProjectTask(task.id, { status: nextStatus });
    } finally {
      // Only the run that still owns the board may clear its own busy marker.
      if (generation === lifecycleRef.current) setBusyTaskId(null);
    }
  };

  const handleDelete = async (taskId: string) => {
    const generation = lifecycleRef.current;
    setBusyTaskId(taskId);
    try {
      await deleteProjectTask(taskId);
      if (generation !== lifecycleRef.current) return;
      setSelectedIds((prev) => {
        if (!prev.has(taskId)) return prev;
        const next = new Set(prev);
        next.delete(taskId);
        return next;
      });
    } finally {
      if (generation === lifecycleRef.current) setBusyTaskId(null);
    }
  };

  /**
   * Arrange a set of tasks onto sessions.
   *
   * This is deliberately *not* an execution: the store only creates the sessions
   * (`runtime.session.create` is metadata-only) and binds the tasks to them, so
   * nothing starts running and the console does not switch away from what the
   * reader is looking at.  Work starts when they open a session and send a message.
   */
  const handleArrange = async (tasks: readonly ProjectTaskView[], mode: TaskArrangeMode) => {
    if (tasks.length === 0 || arranging) return;
    const generation = lifecycleRef.current;
    const scope = currentBoardScope();
    setArranging(true);
    setNotice(null);
    try {
      const result = await arrangeTasks(tasks, mode);
      // A run the boundary overtook reports `{bound:0, sessions:[]}`; that is not
      // "no sessions available", so publishing the empty-run notice (or clearing
      // the new project's selection) would be a lie about the project now shown.
      if (!scopeCurrent(generation, scope)) return;
      if (result.bound === 0) {
        setNotice('未能安排：没有可用会话');
        return;
      }
      const where = result.sessions.length > 0 ? `${result.sessions.length} 个新会话` : '既有会话';
      setNotice(`已安排 ${result.bound} 项到${where}（未执行，可「前往会话」后再发起）`);
      setSelectedIds(new Set());
    } finally {
      if (generation === lifecycleRef.current) setArranging(false);
    }
  };

  /** Open the session a task is bound to.  Navigation only -- opening never runs it. */
  const handleOpenSession = (task: ProjectTaskView) => {
    if (task.assigned_thread_id) void switchSession(task.assigned_thread_id);
  };

  const isSessionRunning = (threadId: string | null) => {
    if (!threadId) return false;
    if (currentSession.project_id === activeProjectId && currentSession.thread_id === threadId && runtimeStatus === 'running') {
      return true;
    }
    const bg = backgroundViews[sessionKey({ project_id: activeProjectId, thread_id: threadId })];
    return bg?.runtimeStatus === 'running';
  };

  const handleExecute = async (task: ProjectTaskView) => {
    if (busyTaskId !== null || arranging) return;
    const generation = lifecycleRef.current;
    const scope = currentBoardScope();
    setBusyTaskId(task.id);
    setNotice(null);
    try {
      const ok = await executeTask(task);
      if (!scopeCurrent(generation, scope)) return;
      if (ok) {
        setNotice(`已启动待办「${task.title}」的执行会话（后台运行中）`);
      }
    } finally {
      if (generation === lifecycleRef.current) setBusyTaskId(null);
    }
  };

  const handleBatchExecute = async (tasks: readonly ProjectTaskView[]) => {
    if (tasks.length === 0 || busyTaskId !== null || arranging) return;
    const generation = lifecycleRef.current;
    const scope = currentBoardScope();
    setBusyTaskId('batch');
    setNotice(null);
    let count = 0;
    try {
      for (const task of tasks) {
        if (!scopeCurrent(generation, scope)) return;
        const ok = await executeTask(task);
        if (ok) count += 1;
      }
      if (scopeCurrent(generation, scope)) {
        setNotice(`已批量启动 ${count} 项待办的执行会话（后台运行中）`);
        setSelectedIds(new Set());
      }
    } finally {
      if (generation === lifecycleRef.current) setBusyTaskId(null);
    }
  };

  const handleBatchStatus = async (status: TaskStatus) => {
    const targets = projectTasks.filter((task) => selectedIds.has(task.id));
    if (targets.length === 0) return;
    const generation = lifecycleRef.current;
    const scope = currentBoardScope();
    setBusyTaskId('batch');
    try {
      for (const task of targets) {
        // Check *before* issuing: the store action reads the active project at
        // call time, so after a switch it would send this old task to the new
        // project.  The guard must run here, not just on the returned result.
        if (!scopeCurrent(generation, scope)) return;
        const ok = await updateProjectTask(task.id, { status });
        // A stale write is dropped by the store (`false`); stop the loop so the
        // rest of the old project's selection is never sent to the new one.
        // This also respects a refused write within the same project.
        if (!ok) return;
      }
      if (scopeCurrent(generation, scope)) setBatchStatus('');
    } finally {
      if (generation === lifecycleRef.current) setBusyTaskId(null);
    }
  };

  const handleBatchDelete = async () => {
    const targets = projectTasks.filter((task) => selectedIds.has(task.id));
    if (targets.length === 0) return;
    const generation = lifecycleRef.current;
    const scope = currentBoardScope();
    setBusyTaskId('batch');
    try {
      for (const task of targets) {
        if (!scopeCurrent(generation, scope)) return;
        const ok = await deleteProjectTask(task.id);
        if (!ok) return;
      }
      // Clearing the selection belongs to the run that still owns the board: a
      // stale delete must not wipe the new project's ticks.
      if (!scopeCurrent(generation, scope)) return;
      setSelectedIds(new Set());
    } finally {
      if (generation === lifecycleRef.current) setBusyTaskId(null);
    }
  };

  const toggleSelected = (taskId: string): void => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(taskId)) next.delete(taskId);
      else next.add(taskId);
      return next;
    });
  };

  const grouped = groupTasksByStatus(projectTasks);
  const visibleTasks =
    filterStatus === 'all'
      ? projectTasks
      : projectTasks.filter((t) => t.status === filterStatus);
  // The toolbar acts on the whole selection, cards the filter hides included.
  const selectedTasks = projectTasks.filter((task) => selectedIds.has(task.id));
  const allVisibleSelected =
    visibleTasks.length > 0 && visibleTasks.every((task) => selectedIds.has(task.id));

  const toggleSelectAllVisible = (): void => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      for (const task of visibleTasks) {
        if (allVisibleSelected) next.delete(task.id);
        else next.add(task.id);
      }
      return next;
    });
  };

  /**
   * One chip, two states.  The selected one is the accent *fill* with its own
   * label role on top (a theme decides both, so it stays legible either way);
   * the rest are a subtle surface the theme's hover step already provides.
   */
  const filterChipClass = (active: boolean): string =>
    `rounded-full px-2.5 py-0.5 text-[11px] whitespace-nowrap transition ${
      active
        ? 'bg-accent font-medium text-on-accent'
        : 'bg-surface-hover text-gray-600 hover:text-gray-900'
    }`;

  return (
    <div className="flex h-full flex-col overflow-hidden p-3 text-xs font-sans">
      {/* 顶部工具栏 */}
      <div className="mb-3 flex items-center justify-between gap-2 border-b border-line/60 pb-2">
        <div className="flex items-center gap-1.5 font-semibold text-gray-900">
          <TaskListSquareLtr16Regular className="text-base text-accent-fg" />
          <span>项目待办 ({projectTasks.length})</span>
        </div>
        <div className="flex items-center gap-1">
          <button
            type="button"
            title="刷新待办"
            onClick={() => void loadProjectTasks()}
            disabled={projectTasksLoading}
            className="ui-icon-button ui-compact text-gray-500"
          >
            <ArrowClockwise16Regular className={projectTasksLoading ? 'animate-spin' : ''} />
          </button>
          <button
            type="button"
            onClick={toggleCreate}
            disabled={busy}
            className="ui-button ui-compact ui-primary gap-1 px-2 text-[11px] font-medium"
          >
            <Add16Regular />
            <span>新建</span>
          </button>
        </div>
      </div>

      {projectTasksError && (
        <div className="mb-2 flex items-center gap-1.5 rounded border border-red-500/30 bg-red-500/10 p-2 text-[11px] text-red-700">
          <Warning16Regular />
          <span>{projectTasksError}</span>
        </div>
      )}

      {/*
        新建/编辑表单：同一个表单，两种模式。`max-h` + 内部滚动让长描述可读却不会把
        右栏撑高；描述框本身也限高滚动，所以完整描述不再被卡片上的三行截断。
      */}
      {formMode !== null && (
        <form
          onSubmit={handleFormSubmit}
          className="fluent-scrollbar mb-3 max-h-[60vh] overflow-y-auto rounded-card border border-line bg-surface p-3 shadow-card transition"
        >
          <div className="mb-2 flex items-center justify-between text-xs font-medium text-gray-900">
            <span>{formMode === 'create' ? '新建项目待办' : '编辑待办'}</span>
            <button
              type="button"
              onClick={closeForm}
              disabled={savingForm}
              title={formMode === 'create' ? '取消新建' : '取消编辑'}
              className="ui-icon-button ui-compact text-gray-500"
            >
              <Dismiss16Regular />
            </button>
          </div>
          <fieldset disabled={busy} className="min-w-0 space-y-2">
            <div>
              <input
                type="text"
                value={draft.title}
                onChange={(e) => setDraft((d) => ({ ...d, title: e.target.value }))}
                placeholder="待办标题..."
                aria-label={formMode === 'create' ? '待办标题' : '编辑待办标题'}
                className="ui-field w-full text-xs"
                autoFocus
                required
              />
              {draft.title.trim() && !isTaskTitleValid(draft.title) && (
                <p role="alert" className="mt-1 text-[11px] text-red-600">
                  标题不能超过 200 个字符
                </p>
              )}
            </div>
            <div>
              <textarea
                value={draft.description}
                onChange={(e) => setDraft((d) => ({ ...d, description: e.target.value }))}
                placeholder="详细描述或技术细节 (可选)..."
                rows={4}
                aria-label={formMode === 'create' ? '待办描述' : '编辑待办描述'}
                className="ui-field fluent-scrollbar max-h-40 w-full resize-none overflow-y-auto text-xs"
              />
            </div>
            <div className="flex items-center gap-2">
              <select
                value={draft.priority}
                onChange={(e) => setDraft((d) => ({ ...d, priority: e.target.value as TaskPriority }))}
                aria-label="待办优先级"
                className="ui-field ui-compact text-[11px]"
              >
                {TASK_PRIORITIES.map((p) => (
                  <option key={p} value={p}>{PRIORITY_LABELS[p]}</option>
                ))}
              </select>
              <input
                type="text"
                value={draft.labels}
                onChange={(e) => setDraft((d) => ({ ...d, labels: e.target.value }))}
                placeholder="标签 (逗号分隔)"
                aria-label="待办标签"
                className="ui-field ui-compact min-w-0 flex-1 text-[11px]"
              />
            </div>
            {formError && (
              <p role="alert" className="text-[11px] text-red-600">{formError}</p>
            )}
            <div className="flex justify-end gap-1.5 pt-1">
              <button
                type="button"
                onClick={closeForm}
                disabled={savingForm}
                className="ui-button ui-compact text-[11px]"
              >
                取消
              </button>
              <button
                type="submit"
                disabled={busy || !isTaskTitleValid(draft.title)}
                className="ui-button ui-compact ui-primary px-3 text-[11px] font-medium"
              >
                {formMode === 'create' ? '保存待办' : '保存修改'}
              </button>
            </div>
          </fieldset>
        </form>
      )}

      {/* 状态过滤选择器 */}
      <div className="mb-2 flex items-center gap-1 overflow-x-auto no-scrollbar py-0.5">
        <button
          type="button"
          onClick={() => setFilterStatus('all')}
          className={filterChipClass(filterStatus === 'all')}
        >
          全部 ({projectTasks.length})
        </button>
        {TASK_STATUSES.map((st) => (
          <button
            key={st}
            type="button"
            onClick={() => setFilterStatus(st)}
            className={filterChipClass(filterStatus === st)}
          >
            {st === 'todo' && '待办'}
            {st === 'in_progress' && '进行中'}
            {st === 'blocked' && '受阻'}
            {st === 'done' && '已完成'} ({grouped[st]?.length ?? 0})
          </button>
        ))}
        <button
          type="button"
          onClick={toggleSelectAllVisible}
          className="ml-auto shrink-0 rounded-full px-2 py-0.5 text-[11px] text-gray-500 hover:text-gray-900 transition"
        >
          {allVisibleSelected ? '取消全选' : '全选'}
        </button>
      </div>

      {/*
        多选工具条。

        “安排”只把待办绑定到会话上：会话由 `runtime.session.create` 建立（仅写入元数据，
        不会打开运行时、不会跑任何一轮），当前会话也不会被切走。要真正开始干活，需要
        “前往会话”后自己发一条消息。
      */}
      {selectedTasks.length > 0 && (
        <div className="mb-2 flex flex-wrap items-center gap-1.5 rounded-card border border-accent-fg/40 bg-accent/5 p-2">
          <span className="font-medium text-gray-900">已选 {selectedTasks.length} 项</span>
          <div className="ml-auto flex flex-wrap items-center gap-1">
            <button
              type="button"
              disabled={busy}
              onClick={() => void handleArrange(selectedTasks, 'shared')}
              title="为整批待办复用/新建一个会话并绑定（不会自动执行，也不会切换当前会话）"
              className="ui-button ui-compact ui-primary text-[11px]"
            >
              安排到一个会话
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => void handleArrange(selectedTasks, 'parallel')}
              title="为每个待办各自准备一个并行会话并绑定（不会自动执行）"
              className="ui-button ui-compact text-[11px]"
            >
              每项并行会话
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => void handleBatchExecute(selectedTasks)}
              title="为选中的待办直接一键启动执行会话（生成提示词并在后台开始运行）"
              className="ui-button ui-compact text-[11px]"
            >
              批量执行
            </button>
            {/*
              四个状态都可达，但不堆四个按钮挤爆卡片：一个原生 select 选择目标，
              再由「应用」提交。选择本身不发 RPC，所以误选不会写任何东西。
            */}
            <select
              value={batchStatus}
              onChange={(e) => setBatchStatus(e.target.value as TaskStatus | '')}
              disabled={busy}
              aria-label="批量设置状态"
              title="选择要批量设置的目标状态"
              className="ui-field ui-compact text-[11px]"
            >
              <option value="">批量状态…</option>
              {TASK_STATUSES.map((st) => (
                <option key={st} value={st}>{STATUS_LABELS[st]}</option>
              ))}
            </select>
            <button
              type="button"
              disabled={busy || batchStatus === ''}
              onClick={() => {
                if (batchStatus !== '') void handleBatchStatus(batchStatus);
              }}
              aria-label="应用批量状态"
              title="把所选待办批量改为目标状态"
              className="ui-button ui-compact text-[11px]"
            >
              应用
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => void handleBatchDelete()}
              className="ui-button ui-compact text-[11px] text-red-600"
            >
              删除
            </button>
            <button
              type="button"
              onClick={() => setSelectedIds(new Set())}
              className="ui-button ui-compact text-[11px]"
            >
              取消选择
            </button>
          </div>
        </div>
      )}

      {notice && (
        <p className="mb-2 text-[11px] text-gray-500" role="status">
          {notice}
        </p>
      )}

      {/* 待办事项列表卡片容器 */}
      <div className="flex-1 overflow-y-auto space-y-2 pr-0.5">
        {visibleTasks.length === 0 ? (
          <div className="flex flex-col items-center justify-center p-8 text-center text-gray-500">
            <TaskListSquareLtr16Regular className="text-3xl mb-2 opacity-50" />
            <p className="text-xs">暂无待办事项</p>
            <p className="mt-1 text-[11px] text-gray-500">
              点击上方“新建”开始为当前项目规划任务。
            </p>
          </div>
        ) : (
          visibleTasks.map((task) => {
            const isBusy = busyTaskId === task.id;
            const isSelected = selectedIds.has(task.id);
            const stColor = STATUS_COLORS[task.status as TaskStatus] || STATUS_COLORS.todo;
            const prioColor =
              PRIORITY_COLORS[task.priority as TaskPriority] || PRIORITY_COLORS.medium;

            return (
              <div
                key={task.id}
                className={`group relative rounded-card border bg-surface/70 p-2.5 shadow-card transition ${
                  isSelected ? 'border-accent-fg/60' : 'border-line/70 hover:border-line'
                }`}
              >
                <div className="flex items-start gap-2">
                  <input
                    type="checkbox"
                    className="ui-check mt-0.5"
                    checked={isSelected}
                    disabled={isBusy}
                    onChange={() => toggleSelected(task.id)}
                    aria-label={`选择待办：${task.title}`}
                  />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-1.5 flex-wrap">
                      <span
                        className={`rounded border px-1.5 py-0.5 text-[10px] font-medium ${stColor.bg} ${stColor.text} ${stColor.border}`}
                      >
                        {STATUS_LABELS[task.status as TaskStatus] || task.status}
                      </span>
                      <span
                        className={`rounded px-1.5 py-0.5 text-[10px] ${prioColor.bg} ${prioColor.text}`}
                      >
                        {PRIORITY_LABELS[task.priority as TaskPriority] || task.priority}
                      </span>
                      {task.assigned_thread_id && (
                        <span className={`rounded border px-1 py-0.5 text-[9px] ${ASSIGNED_CHIP}`}>
                          会话中
                        </span>
                      )}
                    </div>
                    <h4 className="mt-1 text-xs font-semibold text-gray-900 break-words">
                      {task.title}
                    </h4>
                    {task.description && (
                      <p className="mt-0.5 text-[11px] text-gray-500 whitespace-pre-wrap break-words line-clamp-3">
                        {task.description}
                      </p>
                    )}
                    {task.labels && task.labels.length > 0 && (
                      <div className="mt-1.5 flex flex-wrap gap-1">
                        {task.labels.map((lbl) => (
                          <span
                            key={lbl}
                            className="rounded bg-surface-hover px-1 py-0.5 text-[10px] text-gray-500"
                          >
                            #{lbl}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>

                  {/* 删除按钮 */}
                  <button
                    type="button"
                    title="删除待办"
                    onClick={() => handleDelete(task.id)}
                    disabled={busy}
                    className="opacity-0 group-hover:opacity-100 p-1 text-gray-400 hover:text-red-600 transition"
                  >
                    <Delete16Regular />
                  </button>
                </div>

                {/* 卡片底栏动作栏 */}
                <div className="mt-2.5 flex flex-wrap items-center justify-between gap-x-2 gap-y-1.5 border-t border-line/40 pt-2 text-[10px] text-gray-500">
                  <span className="shrink-0">{formatTaskTime(task.updated_at_ms)}</span>
                  <div className="flex flex-wrap items-center gap-1.5">
                    {/*
                      编辑/详情入口：始终可见、可键盘聚焦，aria-label 带任务标题以区分卡片。
                      点开的是同一个表单，以已保存值预填。
                    */}
                    <button
                      type="button"
                      onClick={() => openEdit(task)}
                      disabled={busy}
                      aria-label={`编辑待办：${task.title}`}
                      title={`编辑待办：${task.title}`}
                      className="flex items-center gap-0.5 rounded px-1.5 py-0.5 text-gray-600 hover:bg-surface-hover hover:text-gray-900 transition disabled:opacity-50"
                    >
                      <Edit16Regular className="text-xs" />
                      <span>编辑</span>
                    </button>
                    {/*
                      四状态选择器：任意状态到任意状态（done -> todo 即重新打开）。controlled
                      value 取自服务端返回的行，写入失败时 React 会把原生 select 恢复到原值，
                      所以不会出现假更新。
                    */}
                    <select
                      value={TASK_STATUSES.includes(task.status as TaskStatus) ? task.status : 'todo'}
                      onChange={(e) => void handleStatusChange(task, e.target.value as TaskStatus)}
                      disabled={busy}
                      aria-label={`待办状态：${task.title}`}
                      title={`待办状态：${task.title}`}
                      className="rounded border border-line bg-surface px-1 py-0.5 text-[10px] text-gray-600 transition disabled:opacity-50"
                    >
                      {TASK_STATUSES.map((st) => (
                        <option key={st} value={st}>{STATUS_LABELS[st]}</option>
                      ))}
                    </select>
                    {/*
                      已绑定：只切换视图，不执行；未绑定：为该待办准备一个后台会话并绑定，
                      同样不会自动执行，也不会把当前会话切走。
                    */}
                    {task.assigned_thread_id ? (
                      <div className="flex items-center gap-1">
                        <button
                          type="button"
                          onClick={() => void handleExecute(task)}
                          disabled={busy || isSessionRunning(task.assigned_thread_id)}
                          title={isSessionRunning(task.assigned_thread_id) ? "该待办关联的会话正在运行中" : "在关联会话中一键开始执行该待办（后台运行）"}
                          className="flex items-center gap-1 rounded bg-accent/10 px-2 py-0.5 font-medium text-accent-fg hover:bg-accent/20 transition disabled:opacity-50"
                        >
                          <Play16Regular className="text-xs" />
                          <span>{isSessionRunning(task.assigned_thread_id) ? '执行中' : '执行'}</span>
                        </button>
                        <button
                          type="button"
                          onClick={() => handleOpenSession(task)}
                          title="打开该待办所在的会话（仅切换视图）"
                          className="flex items-center gap-1 rounded bg-surface px-2 py-0.5 font-medium text-gray-700 border border-line/60 hover:bg-surface-hover transition"
                        >
                          <ArrowCircleRight16Regular className="text-xs" />
                          <span>前往会话</span>
                        </button>
                      </div>
                    ) : (
                      <button
                        type="button"
                        onClick={() => void handleExecute(task)}
                        disabled={busy}
                        title="为该待办创建会话并直接一键开始执行（生成结构化提示词并在后台开始运行）"
                        className="flex items-center gap-1 rounded bg-accent/10 px-2 py-0.5 font-medium text-accent-fg hover:bg-accent/20 transition"
                      >
                        <Play16Regular className="text-xs" />
                        <span>执行待办</span>
                      </button>
                    )}
                  </div>
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
};

export const tasksTab: RightDockTabDefinition = {
  id: 'tasks',
  label: '待办',
  order: 25,
  Icon: TaskListSquareLtr16Regular,
  badge: () => {
    const tasks = useConsoleStore.getState().projectTasks;
    const pendingCount = tasks.filter((t) => t.status === 'in_progress' || t.status === 'todo').length;
    return pendingCount > 0 ? String(pendingCount) : null;
  },
  badgeVariant: 'accent',
  Content: TasksContent,
};
