"""Goal 运行时：进程内记账状态 + 跨层服务注册表。

对应 Codex ``ext/goal`` 的 ``GoalAccountingState``（turn 基线、墙钟、预算
报告去重）与 ``GoalService``（外部 set/clear/status 操作）。middleware 在
模型调用/工具调用边界结算，TUI 在回合结束时触发最终结算与自动继续。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from synapse.goals.model import ThreadGoal, ThreadGoalStatus, goal_token_delta
from synapse.goals.store import GoalStore

# 对外变更通知（TUI bottombar 等）签名：callback(thread_id, goal)
GoalListener = Callable[[str, ThreadGoal | None], None]


@dataclass
class _TurnAccounting:
    turn_id: str
    last_accounted_tokens: int = 0
    current_tokens: int = 0
    goal_id: str | None = None


class GoalRuntime:
    """单线程记账状态：记录当前回合 token 增量与目标墙钟。"""

    def __init__(self, thread_id: str, store: GoalStore) -> None:
        self.thread_id = thread_id
        self._store = store
        self._lock = threading.RLock()
        self._turn: _TurnAccounting | None = None
        self._wall_clock_goal_id: str | None = None
        self._wall_clock_started_at: float | None = None
        self._budget_reported_goal_id: str | None = None

    # ------------------------------------------------------------------
    # 回合生命周期
    # ------------------------------------------------------------------
    def start_turn(self, turn_id: str, token_usage: int = 0) -> None:
        """回合开始：记录基线 token 用量与墙钟起点。"""
        with self._lock:
            self._turn = _TurnAccounting(
                turn_id=turn_id,
                last_accounted_tokens=max(0, int(token_usage)),
                current_tokens=max(0, int(token_usage)),
            )
            self._wall_clock_goal_id = self._active_goal_id_locked()
            if self._wall_clock_goal_id is not None:
                self._wall_clock_started_at = time.monotonic()

    def record_token_usage(
        self, turn_id: str, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0
    ) -> None:
        """模型响应后累计 token（按 goal 计费口径折算，增量累加）。"""
        with self._lock:
            if self._turn is None or self._turn.turn_id != turn_id:
                return
            delta = goal_token_delta(input_tokens, output_tokens, cache_read_tokens)
            self._turn.current_tokens += max(0, delta)

    def mark_turn_goal_active(self, goal_id: str) -> None:
        """当前回合关联到指定 goal：重置 token 基线，避免计入创建前的用量。"""
        with self._lock:
            if self._turn is not None:
                self._turn.goal_id = goal_id
                self._turn.last_accounted_tokens = self._turn.current_tokens
            self._wall_clock_goal_id = goal_id
            self._wall_clock_started_at = time.monotonic()
            self._budget_reported_goal_id = None

    def mark_idle_goal_active(self, goal_id: str) -> None:
        with self._lock:
            self._wall_clock_goal_id = goal_id
            self._wall_clock_started_at = time.monotonic()
            self._budget_reported_goal_id = None

    def clear_active_goal(self) -> None:
        with self._lock:
            if self._turn is not None:
                self._turn.goal_id = None
            self._wall_clock_goal_id = None
            self._wall_clock_started_at = None
            self._budget_reported_goal_id = None

    def finish_turn(self, turn_id: str) -> None:
        with self._lock:
            if self._turn is not None and self._turn.turn_id == turn_id:
                self._turn = None
            self._wall_clock_started_at = None

    def finish_active_turn(self) -> None:
        with self._lock:
            self._turn = None
            self._wall_clock_started_at = None

    def turn_active(self) -> bool:
        with self._lock:
            return self._turn is not None

    def current_turn_id(self) -> str | None:
        with self._lock:
            return self._turn.turn_id if self._turn is not None else None

    # ------------------------------------------------------------------
    # 结算
    # ------------------------------------------------------------------
    def account_progress(
        self,
        *,
        allow_complete: bool = False,
        allow_stopped: bool = False,
        expected_goal_id: str | None = None,
    ) -> tuple[ThreadGoal | None, bool]:
        """把当前回合累计的 token/时间增量写入存储。

        返回 (更新后的 goal, 本次是否触发 budget_limited)。
        """
        with self._lock:
            turn = self._turn
            if turn is None:
                return self._account_wall_clock_only(
                    allow_complete=allow_complete, allow_stopped=allow_stopped
                )
            token_delta = max(0, turn.current_tokens - turn.last_accounted_tokens)
            time_delta = self._wall_clock_delta_locked()
            goal_id = turn.goal_id or self._wall_clock_goal_id
            if token_delta == 0 and time_delta == 0:
                return (self._store.get(self.thread_id), False)
            before = self._store.get(self.thread_id)
            expected = expected_goal_id or goal_id
            goal = self._store.account_usage(
                self.thread_id,
                time_delta_seconds=time_delta,
                token_delta=token_delta,
                allow_complete=allow_complete,
                allow_stopped=allow_stopped,
                expected_goal_id=expected,
            )
            turn.last_accounted_tokens = turn.current_tokens
            if time_delta > 0:
                self._reset_wall_clock_locked()
            budget_hit = bool(
                before is not None
                and before.status == ThreadGoalStatus.ACTIVE
                and goal is not None
                and goal.status == ThreadGoalStatus.BUDGET_LIMITED
            )
            return (goal, budget_hit)

    def _account_wall_clock_only(
        self, *, allow_complete: bool, allow_stopped: bool
    ) -> tuple[ThreadGoal | None, bool]:
        time_delta = self._wall_clock_delta_locked()
        if time_delta == 0:
            return (self._store.get(self.thread_id), False)
        before = self._store.get(self.thread_id)
        goal = self._store.account_usage(
            self.thread_id,
            time_delta_seconds=time_delta,
            token_delta=0,
            allow_complete=allow_complete,
            allow_stopped=allow_stopped,
            expected_goal_id=self._wall_clock_goal_id,
        )
        self._reset_wall_clock_locked()
        budget_hit = bool(
            before is not None
            and before.status == ThreadGoalStatus.ACTIVE
            and goal is not None
            and goal.status == ThreadGoalStatus.BUDGET_LIMITED
        )
        return (goal, budget_hit)

    # ------------------------------------------------------------------
    # 状态推进
    # ------------------------------------------------------------------
    def stop_for_error(self, usage_limit: bool) -> ThreadGoal | None:
        """回合终局错误：usage 超限 -> usage_limited，其他 -> blocked。"""
        status = ThreadGoalStatus.USAGE_LIMITED if usage_limit else ThreadGoalStatus.BLOCKED
        goal = self._store.update(self.thread_id, status=status)
        self.clear_active_goal()
        return goal

    def pause(self) -> ThreadGoal | None:
        goal = self._store.update(self.thread_id, status=ThreadGoalStatus.PAUSED)
        self.clear_active_goal()
        return goal

    def resume(self) -> ThreadGoal | None:
        goal = self._store.get(self.thread_id)
        if goal is None:
            return None
        if goal.status not in {
            ThreadGoalStatus.PAUSED,
            ThreadGoalStatus.BLOCKED,
            ThreadGoalStatus.USAGE_LIMITED,
            ThreadGoalStatus.BUDGET_LIMITED,
        }:
            return goal
        updated = self._store.update(
            self.thread_id,
            status=ThreadGoalStatus.ACTIVE,
            expected_goal_id=goal.goal_id,
        )
        if updated is not None:
            self.mark_idle_goal_active(updated.goal_id)
        return updated

    def mark_budget_reported(self, goal_id: str) -> bool:
        """首次对同一 goal 报告预算耗尽时返回 True（去重）。"""
        with self._lock:
            if self._budget_reported_goal_id == goal_id:
                return False
            self._budget_reported_goal_id = goal_id
            return True

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _active_goal_id_locked(self) -> str | None:
        goal = self._store.get(self.thread_id)
        if goal is not None and goal.status == ThreadGoalStatus.ACTIVE:
            return goal.goal_id
        return None

    def _wall_clock_delta_locked(self) -> int:
        if self._wall_clock_started_at is None:
            return 0
        return max(0, int(time.monotonic() - self._wall_clock_started_at))

    def _reset_wall_clock_locked(self) -> None:
        self._wall_clock_started_at = time.monotonic()


class GoalService:
    """进程级 goal 注册表：提供 slash/TUI/工具/middleware 共享入口。

    每个 thread 一个 :class:`GoalRuntime`；goal 变化时通知监听者
    （TUI bottombar 刷新）。
    """

    def __init__(self, store: GoalStore) -> None:
        self.store = store
        self._runtimes: dict[str, GoalRuntime] = {}
        self._lock = threading.RLock()
        self._listeners: list[GoalListener] = []

    # ------------------------------------------------------------------
    # 注册表
    # ------------------------------------------------------------------
    def runtime(self, thread_id: str | None) -> GoalRuntime:
        thread_id = str(thread_id or "")
        with self._lock:
            rt = self._runtimes.get(thread_id)
            if rt is None:
                rt = GoalRuntime(thread_id, self.store)
                self._runtimes[thread_id] = rt
            return rt

    def add_listener(self, listener: GoalListener) -> None:
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def remove_listener(self, listener: GoalListener) -> None:
        with self._lock:
            self._listeners = [cb for cb in self._listeners if cb is not listener]

    def notify(self, thread_id: str, goal: ThreadGoal | None) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(thread_id, goal)
            except Exception:  # noqa: BLE001 - 通知是尽力而为
                pass

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get(self, thread_id: str | None) -> ThreadGoal | None:
        if not thread_id:
            return None
        return self.store.get(str(thread_id))

    # ------------------------------------------------------------------
    # 外部操作（slash 命令 / TUI）
    # ------------------------------------------------------------------
    def set_goal(
        self,
        thread_id: str | None,
        objective: str,
        *,
        token_budget: int | None = None,
        replace: bool = False,
    ) -> tuple[ThreadGoal | None, str | None]:
        """设置新目标。已有未完成目标时：replace=True 替换，否则返回错误。"""
        if not thread_id:
            return None, "session must start before setting a goal"
        existing = self.store.get(str(thread_id))
        if existing is not None and not existing.status.is_replaceable():
            if not replace:
                return None, (
                    "there is already an unfinished goal; "
                    "finish it first or use /goal clear or /goal edit"
                )
            self.store.clear(str(thread_id))
            self.notify(str(thread_id), None)
        goal = self.store.insert(
            str(thread_id),
            objective,
            status=ThreadGoalStatus.ACTIVE,
            token_budget=token_budget,
        )
        if goal is None:
            return None, "cannot create a goal in this thread"
        self.runtime(str(thread_id)).mark_turn_goal_active(goal.goal_id)
        self.notify(str(thread_id), goal)
        return goal, None

    def edit_goal(
        self, thread_id: str | None, objective: str
    ) -> tuple[ThreadGoal | None, str | None]:
        if not thread_id:
            return None, "session must start before editing a goal"
        goal = self.store.update(str(thread_id), objective=objective)
        if goal is None:
            return None, "no goal is currently set"
        self.notify(str(thread_id), goal)
        return goal, None

    def clear_goal(self, thread_id: str | None) -> tuple[ThreadGoal | None, str | None]:
        if not thread_id:
            return None, "session must start before clearing a goal"
        goal = self.store.clear(str(thread_id))
        if goal is None:
            return None, "no goal is currently set"
        self.runtime(str(thread_id)).clear_active_goal()
        self.notify(str(thread_id), None)
        return goal, None

    def pause_goal(self, thread_id: str | None) -> tuple[ThreadGoal | None, str | None]:
        if not thread_id:
            return None, "session must start before pausing a goal"
        goal = self.runtime(str(thread_id)).pause()
        if goal is None:
            return None, "no goal is currently set"
        self.notify(str(thread_id), goal)
        return goal, None

    def resume_goal(self, thread_id: str | None) -> tuple[ThreadGoal | None, str | None]:
        if not thread_id:
            return None, "session must start before resuming a goal"
        goal = self.runtime(str(thread_id)).resume()
        if goal is None:
            return None, "no goal is currently set"
        self.notify(str(thread_id), goal)
        return goal, None

    def mark_status(
        self, thread_id: str | None, status: ThreadGoalStatus
    ) -> tuple[ThreadGoal | None, str | None]:
        """系统侧状态推进（budget_limited 等），供工具完成时调用。"""
        if not thread_id:
            return None, "missing thread id"
        goal = self.store.update(str(thread_id), status=status)
        if goal is None:
            return None, "no goal is currently set"
        if status in {
            ThreadGoalStatus.PAUSED,
            ThreadGoalStatus.BLOCKED,
            ThreadGoalStatus.USAGE_LIMITED,
            ThreadGoalStatus.BUDGET_LIMITED,
            ThreadGoalStatus.COMPLETE,
        }:
            self.runtime(str(thread_id)).clear_active_goal()
        self.notify(str(thread_id), goal)
        return goal, None

    # ------------------------------------------------------------------
    # 回合钩子（middleware / TUI 调用）
    # ------------------------------------------------------------------
    def on_model_call_begin(self, thread_id: str | None) -> None:
        """模型调用前确保该 thread 有活跃回合（无则新建）。"""
        if not thread_id:
            return
        rt = self.runtime(str(thread_id))
        if not rt.turn_active():
            rt.start_turn(f"{thread_id}:{time.monotonic_ns()}")

    def on_model_call_end(
        self,
        thread_id: str | None,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
    ) -> None:
        """模型响应后：累计 usage 并结算一次进度。"""
        if not thread_id:
            return
        rt = self.runtime(str(thread_id))
        if not rt.turn_active():
            rt.start_turn(f"{thread_id}:{time.monotonic_ns()}")
        rt.record_token_usage(
            rt.current_turn_id() or "",
            input_tokens,
            output_tokens,
            cache_read_tokens,
        )
        try:
            goal, budget_hit = rt.account_progress()
            if goal is not None:
                self.notify(str(thread_id), goal)
        except Exception:  # noqa: BLE001 - 记账失败不阻断主流程
            pass

    def on_turn_start(self, thread_id: str | None, turn_id: str, token_usage: int = 0) -> None:
        if not thread_id:
            return
        self.runtime(str(thread_id)).start_turn(turn_id, token_usage)

    def on_token_usage(
        self,
        thread_id: str | None,
        turn_id: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
    ) -> None:
        if not thread_id:
            return
        self.runtime(str(thread_id)).record_token_usage(
            turn_id, input_tokens, output_tokens, cache_read_tokens
        )

    def on_tool_finish(self, thread_id: str | None) -> None:
        """工具调用结束：结算一次进度（token + 时间）。"""
        if not thread_id:
            return
        try:
            goal, budget_hit = self.runtime(str(thread_id)).account_progress()
            if goal is not None:
                self.notify(str(thread_id), goal)
        except Exception:  # noqa: BLE001 - 记账失败不阻断主流程
            pass

    def on_turn_end(
        self,
        thread_id: str | None,
        *,
        usage_limit_error: bool = False,
    ) -> ThreadGoal | None:
        """回合结束：最终结算；终局错误时推进状态。返回当前 goal。"""
        if not thread_id:
            return None
        rt = self.runtime(str(thread_id))
        if usage_limit_error:
            goal = rt.stop_for_error(usage_limit=True)
        else:
            try:
                goal, _ = rt.account_progress()
            except Exception:  # noqa: BLE001
                goal = self.store.get(str(thread_id))
        rt.finish_active_turn()
        if goal is not None:
            self.notify(str(thread_id), goal)
        return goal


# ---------------------------------------------------------------------------
# 进程级单例（agent 构建时初始化；工具/middleware/TUI 共享）
# ---------------------------------------------------------------------------
_service: GoalService | None = None
_service_lock = threading.RLock()


def init_goal_service(sessions_path: Any) -> GoalService:
    """初始化（或复用）进程级 GoalService。"""
    global _service
    with _service_lock:
        if _service is None:
            _service = GoalService(GoalStore(str(sessions_path)))
        return _service


def get_goal_service() -> GoalService | None:
    return _service


def reset_goal_service() -> None:
    """测试用：清空单例。"""
    global _service
    with _service_lock:
        if _service is not None:
            try:
                _service.store.close()
            except Exception:  # noqa: BLE001
                pass
            _service = None
