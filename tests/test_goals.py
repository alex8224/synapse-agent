"""长程目标（goal）子系统测试：存储 / 运行时 / 工具 / 命令 / 引导。

覆盖移植自 Codex ``ext/goal`` 的核心语义：
- 每 thread 一个持久化 goal，未完成目标不可被新目标静默替换；
- token/时间用量记账与预算耗尽自动置 budget_limited；
- 状态推进（pause/resume/complete/blocked/usage_limited）；
- ``/goal`` 命令与 ``gooooal`` 别名；
- 工具参数校验与引导提示生成。
"""

from __future__ import annotations

import threading

import pytest

from synapse.commands.goal import GOAL_USAGE, handle_goal, is_goal_command
from synapse.goals.model import (
    MAX_GOAL_OBJECTIVE_CHARS,
    ThreadGoal,
    ThreadGoalStatus,
    goal_token_delta,
    validate_goal_budget,
    validate_goal_objective,
)
from synapse.goals.runtime import GoalService, reset_goal_service
from synapse.goals.steering import (
    GOAL_STEER_PREFIX,
    budget_limit_prompt,
    continuation_for_status,
    continuation_prompt,
    objective_updated_prompt,
)
from synapse.goals.store import GoalStore


@pytest.fixture()
def store(tmp_path):
    s = GoalStore(tmp_path / "sessions.sqlite")
    yield s
    s.close()


@pytest.fixture()
def service(tmp_path):
    svc = GoalService(GoalStore(tmp_path / "sessions.sqlite"))
    yield svc
    try:
        svc.store.close()
    except Exception:  # noqa: BLE001
        pass


def make_goal(**overrides) -> ThreadGoal:
    base = dict(
        thread_id="t1",
        goal_id="g1",
        objective="finish the thing",
        status=ThreadGoalStatus.ACTIVE,
        token_budget=None,
        tokens_used=0,
        time_used_seconds=0,
        created_at_ms=1,
        updated_at_ms=1,
    )
    base.update(overrides)
    return ThreadGoal(**base)


# ---------------------------------------------------------------------------
# 存储层
# ---------------------------------------------------------------------------
class TestGoalStore:
    def test_insert_and_get(self, store):
        goal = store.insert("t1", "objective A")
        assert goal is not None
        assert goal.status == ThreadGoalStatus.ACTIVE
        assert store.get("t1") == goal
        assert store.get("missing") is None

    def test_insert_refuses_when_unfinished_goal_exists(self, store):
        store.insert("t1", "first")
        assert store.insert("t1", "second") is None
        goal = store.get("t1")
        assert goal is not None
        assert goal.objective == "first"

    def test_insert_replaces_terminal_goal(self, store):
        store.insert("t1", "first", status=ThreadGoalStatus.COMPLETE)
        second = store.insert("t1", "second")
        assert second is not None
        assert second.objective == "second"
        # 未完成状态不可替换
        store.insert("t1", "third")
        assert store.insert("t1", "fourth") is None

    def test_update_partial_and_expected_goal_id(self, store):
        goal = store.insert("t1", "objective")
        assert goal is not None
        updated = store.update("t1", objective="new objective")
        assert updated is not None
        assert updated.objective == "new objective"
        assert updated.tokens_used == 0
        # expected_goal_id 不匹配时拒绝更新
        assert store.update("t1", status=ThreadGoalStatus.COMPLETE, expected_goal_id="nope") is None
        assert store.get("t1").status == ThreadGoalStatus.ACTIVE  # type: ignore[union-attr]

    def test_clear(self, store):
        goal = store.insert("t1", "objective")
        assert goal is not None
        cleared = store.clear("t1")
        assert cleared is not None
        assert store.get("t1") is None
        assert store.clear("t1") is None

    def test_account_usage_budget_limit(self, store):
        goal = store.insert("t1", "objective", token_budget=100)
        assert goal is not None
        updated = store.account_usage("t1", time_delta_seconds=10, token_delta=50)
        assert updated is not None
        assert updated.tokens_used == 50
        assert updated.time_used_seconds == 10
        assert updated.status == ThreadGoalStatus.ACTIVE
        updated = store.account_usage("t1", time_delta_seconds=0, token_delta=60)
        assert updated is not None
        assert updated.status == ThreadGoalStatus.BUDGET_LIMITED
        # budget_limited 后当前回合剩余时间仍累计（新回合不再自动开始）
        frozen = store.account_usage("t1", time_delta_seconds=5, token_delta=10)
        assert frozen is not None
        assert frozen.tokens_used == 120
        assert frozen.time_used_seconds == 15

    def test_account_usage_respects_expected_goal_id(self, store):
        goal = store.insert("t1", "objective")
        assert goal is not None
        updated = store.account_usage(
            "t1", time_delta_seconds=3, token_delta=7, expected_goal_id="other"
        )
        assert updated is None
        unchanged = store.get("t1")
        assert unchanged is not None
        assert unchanged.tokens_used == 0

    def test_account_usage_ignores_complete_unless_allowed(self, store):
        goal = store.insert("t1", "objective", status=ThreadGoalStatus.COMPLETE)
        assert goal is not None
        assert store.account_usage("t1", time_delta_seconds=5, token_delta=1) is None
        assert store.account_usage(
            "t1", time_delta_seconds=5, token_delta=1, allow_complete=True
        ) is not None


# ---------------------------------------------------------------------------
# 运行时 / 服务
# ---------------------------------------------------------------------------
class TestGoalService:
    def test_set_goal_and_replace_semantics(self, service):
        goal, error = service.set_goal("t1", "objective A")
        assert goal is not None and error is None
        _, error = service.set_goal("t1", "objective B")
        assert error is not None and "unfinished" in error
        goal, error = service.set_goal("t1", "objective B", replace=True)
        assert goal is not None and error is None
        assert goal.objective == "objective B"

    def test_pause_resume(self, service):
        service.set_goal("t1", "objective")
        goal, _ = service.pause_goal("t1")
        assert goal is not None
        assert goal.status == ThreadGoalStatus.PAUSED
        goal, _ = service.resume_goal("t1")
        assert goal is not None
        assert goal.status == ThreadGoalStatus.ACTIVE

    def test_late_turn_end_does_not_clear_newer_turn(self, service):
        service.set_goal("t1", "objective")
        service.on_turn_start("t1", "old")
        service.on_turn_start("t1", "new")

        goal = service.on_turn_end("t1", turn_id="old")

        assert goal is not None
        assert goal.status == ThreadGoalStatus.ACTIVE
        assert service.runtime("t1").current_turn_id() == "new"

    def test_late_turn_abort_does_not_clear_newer_turn(self, service):
        service.set_goal("t1", "objective")
        service.on_turn_start("t1", "old")
        service.on_turn_start("t1", "new")

        service.on_turn_abort("t1", "old")

        assert service.runtime("t1").current_turn_id() == "new"
        assert service.runtime("t1")._wall_clock_started_at is not None  # noqa: SLF001

    def test_clear_goal(self, service):
        service.set_goal("t1", "objective")
        goal, error = service.clear_goal("t1")
        assert goal is not None and error is None
        assert service.get("t1") is None

    def test_mark_status_complete(self, service):
        service.set_goal("t1", "objective")
        goal, _ = service.mark_status("t1", ThreadGoalStatus.COMPLETE)
        assert goal is not None
        assert goal.status == ThreadGoalStatus.COMPLETE
        # 完成后可设置新目标
        goal, error = service.set_goal("t1", "next")
        assert goal is not None and error is None

    def test_on_model_call_usage_accounting(self, service):
        service.set_goal("t1", "objective", token_budget=1000)
        service.on_model_call_begin("t1")
        service.on_model_call_end("t1", input_tokens=300, output_tokens=100)
        goal = service.get("t1")
        assert goal is not None
        assert goal.tokens_used == 400  # 300 input + 100 output
        assert goal.time_used_seconds >= 0

    def test_budget_hit_via_model_calls(self, service):
        service.set_goal("t1", "objective", token_budget=500)
        service.on_model_call_begin("t1")
        service.on_model_call_end("t1", input_tokens=600, output_tokens=0)
        goal = service.get("t1")
        assert goal is not None
        assert goal.status == ThreadGoalStatus.BUDGET_LIMITED

    def test_on_turn_end_finishes_turn(self, service):
        service.set_goal("t1", "objective")
        service.on_model_call_begin("t1")
        service.on_model_call_end("t1", input_tokens=50, output_tokens=10)
        goal = service.on_turn_end("t1")
        assert goal is not None
        assert goal.tokens_used == 60
        # 回合结束后新的模型调用重新开始记账
        service.on_model_call_begin("t1")
        service.on_model_call_end("t1", input_tokens=100, output_tokens=0)
        assert service.get("t1").tokens_used == 160  # type: ignore[union-attr]

    def test_on_turn_end_usage_limit_error(self, service):
        service.set_goal("t1", "objective")
        goal = service.on_turn_end("t1", usage_limit_error=True)
        assert goal is not None
        assert goal.status == ThreadGoalStatus.USAGE_LIMITED

    def test_on_turn_end_returns_paused_goal_snapshot(self, service):
        service.set_goal("t1", "objective")
        service.pause_goal("t1")

        goal = service.on_turn_end("t1")

        assert goal is not None
        assert goal.status == ThreadGoalStatus.PAUSED

    def test_mark_status_accepts_explicit_terminal_update_from_paused_goal(self, service):
        service.set_goal("t1", "objective")
        service.pause_goal("t1")

        goal, error = service.mark_status("t1", ThreadGoalStatus.COMPLETE)

        assert error is None
        assert goal is not None
        assert goal.status == ThreadGoalStatus.COMPLETE

    def test_mark_status_ignores_stale_goal_id(self, service):
        first, _ = service.set_goal("t1", "first")
        assert first is not None
        service.mark_status("t1", ThreadGoalStatus.COMPLETE)
        second, _ = service.set_goal("t1", "second")
        assert second is not None

        goal, error = service.mark_status(
            "t1",
            ThreadGoalStatus.BLOCKED,
            expected_goal_id=first.goal_id,
        )

        assert error is None
        assert goal is not None
        assert goal.goal_id == second.goal_id
        assert goal.status == ThreadGoalStatus.ACTIVE

    def test_listener_notified(self, service):
        events: list[tuple[str, ThreadGoal | None]] = []

        def listener(thread_id: str, goal: ThreadGoal | None) -> None:
            events.append((thread_id, goal))

        service.add_listener(listener)
        service.set_goal("t1", "objective")
        service.clear_goal("t1")
        service.remove_listener(listener)
        service.set_goal("t1", "again")
        assert len(events) == 2
        assert events[0][1] is not None and events[1][1] is None


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
class TestGoalTools:
    def _invoke(self, fn, **kwargs):
        thread_id = "tool-t1"

        class _Runtime:
            config = {"configurable": {"thread_id": thread_id}}

        # StructuredTool 不可直接调用；fn.func 是原函数（含 runtime 注入参数）。
        return fn.func(_Runtime(), **kwargs)

    def test_tools_use_service(self, tmp_path):
        from synapse.goals.runtime import init_goal_service
        from synapse.goals.tools import build_goal_tools

        reset_goal_service()
        try:
            init_goal_service(tmp_path / "sessions.sqlite")
            tools = build_goal_tools()
            by_name = {t.name: t for t in tools}
            assert set(by_name) == {"get_goal", "create_goal", "update_goal"}

            out = self._invoke(by_name["create_goal"], objective="build a thing")
            assert "goal created" in out
            out = self._invoke(by_name["get_goal"])
            assert "build a thing" in out
            # 重复创建被拒绝
            out = self._invoke(by_name["create_goal"], objective="another")
            assert "unfinished goal" in out
            # 更新为 complete 后允许新目标
            out = self._invoke(by_name["update_goal"], status="complete")
            assert "complete" in out
            out = self._invoke(by_name["create_goal"], objective="next")
            assert "goal created" in out
            # 校验：空 objective / 非法预算
            out = self._invoke(by_name["create_goal"], objective="   ")
            assert "must not be empty" in out
            out = self._invoke(by_name["create_goal"], objective="x", token_budget=-5)
            assert "positive" in out
        finally:
            reset_goal_service()

    def test_tool_thread_id_from_runtime(self, tmp_path):
        from synapse.goals.runtime import get_goal_service, init_goal_service
        from synapse.goals.tools import build_goal_tools

        reset_goal_service()
        try:
            init_goal_service(tmp_path / "sessions.sqlite")
            tools = {t.name: t for t in build_goal_tools()}

            class _Runtime:
                config = {"configurable": {"thread_id": "thread-42"}}

            out = tools["create_goal"].func(_Runtime(), objective="task")
            assert "goal created" in out
            goal = get_goal_service().get("thread-42")
            assert goal is not None and goal.objective == "task"
        finally:
            reset_goal_service()


# ---------------------------------------------------------------------------
# /goal 命令
# ---------------------------------------------------------------------------
class TestGoalCommand:
    def test_goal_alias(self):
        assert is_goal_command("/goal")
        assert is_goal_command("/gooooal")
        assert is_goal_command("/gooal")
        assert not is_goal_command("/goalx")
        assert not is_goal_command("/goala")
        assert not is_goal_command("/model")

    def test_set_and_show(self, service):
        # service 注册为进程单例（命令入口走 get_goal_service）
        import synapse.goals.runtime as runtime_mod

        original = runtime_mod._service
        runtime_mod._service = service
        try:
            result = handle_goal(["do the thing"], thread_id="t1")
            assert result.handled and not result.error
            assert any("do the thing" in line for line in result.lines)
            result = handle_goal([], thread_id="t1")
            assert any("Status: active" in line for line in result.lines)
        finally:
            runtime_mod._service = original

    def test_clear_pause_resume(self, service):
        import synapse.goals.runtime as runtime_mod

        original = runtime_mod._service
        runtime_mod._service = service
        try:
            handle_goal(["task"], thread_id="t1")
            result = handle_goal(["pause"], thread_id="t1")
            assert any("paused" in line for line in result.lines)
            assert result.cancel_active_turn is True
            result = handle_goal(["resume"], thread_id="t1")
            assert any("resumed" in line for line in result.lines)
            assert result.cancel_active_turn is False
            result = handle_goal(["clear"], thread_id="t1")
            assert any("cleared" in line for line in result.lines)
            result = handle_goal(["clear"], thread_id="t1")
            assert result.error and "no goal" in result.lines[0]
        finally:
            runtime_mod._service = original

    def test_usage_when_no_thread(self, service):
        import synapse.goals.runtime as runtime_mod

        original = runtime_mod._service
        runtime_mod._service = service
        try:
            result = handle_goal(["task"], thread_id=None)
            assert GOAL_USAGE in result.lines
        finally:
            runtime_mod._service = original


# ---------------------------------------------------------------------------
# 模型校验 / 引导 / 展示
# ---------------------------------------------------------------------------
class TestGoalHelpers:
    def test_validate_objective(self):
        assert validate_goal_objective("ok") is None
        assert validate_goal_objective("   ") is not None
        assert validate_goal_objective("x" * (MAX_GOAL_OBJECTIVE_CHARS + 1)) is not None

    def test_validate_budget(self):
        assert validate_goal_budget(None) is None
        assert validate_goal_budget(100) is None
        assert validate_goal_budget(0) is not None
        assert validate_goal_budget(-1) is not None

    def test_goal_token_delta(self):
        assert goal_token_delta(100, 20) == 120
        assert goal_token_delta(100, 20, cache_read_tokens=40) == 80
        assert goal_token_delta(10, 2, cache_read_tokens=50) == 2

    def test_steering_prompts_contain_objective(self):
        goal = make_goal(token_budget=1000, tokens_used=200)
        assert "finish the thing" in continuation_prompt(goal)
        assert "800" in continuation_prompt(goal)  # remaining tokens
        assert "finish the thing" in budget_limit_prompt(goal)
        assert "finish the thing" in objective_updated_prompt(goal)

    def test_continuation_for_status(self):
        active = make_goal(status=ThreadGoalStatus.ACTIVE)
        assert continuation_for_status(active) is not None
        assert continuation_for_status(make_goal(status=ThreadGoalStatus.COMPLETE)) is None
        budgeted = make_goal(status=ThreadGoalStatus.BUDGET_LIMITED)
        assert "budget" in (continuation_for_status(budgeted) or "")

    def test_goal_steer_prefix(self):
        assert GOAL_STEER_PREFIX == "[goal continuation]"

    def test_runtime_concurrency(self, service):
        """并发记账不丢更新（基本冒烟）。"""
        service.set_goal("t1", "objective", token_budget=10_000)
        errors: list[Exception] = []

        def worker(n: int) -> None:
            try:
                for _ in range(20):
                    service.on_model_call_begin("t1")
                    service.on_model_call_end("t1", input_tokens=10, output_tokens=0)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        goal = service.get("t1")
        assert goal is not None
        assert goal.tokens_used == 20 * 4 * 10


# ---------------------------------------------------------------------------
# 端到端：真实 deepagents 图 + goal middleware/工具
# ---------------------------------------------------------------------------
def test_tui_maybe_continue_goal_schedules_continuation(tmp_path) -> None:
    """``/goal <objective>`` 设置成功后应调度续跑回合（idle + active）。"""
    import types
    from types import SimpleNamespace

    from synapse.goals.runtime import GoalService
    from synapse.goals.steering import GOAL_STEER_PREFIX
    from synapse.goals.store import GoalStore
    from synapse.runtime.steer import SteerQueue
    from synapse.ui.turn.controller import TurnController

    svc = GoalService(GoalStore(tmp_path / "sessions.sqlite"))
    svc.set_goal("t1", "objective")
    queue = SteerQueue()

    fake = object.__new__(TurnController)
    fake._app = SimpleNamespace(
        _busy=False,
        settings=SimpleNamespace(goal_auto_continue=True),
        agent=SimpleNamespace(_coding_goal_service=svc),
        thread_id="t1",
        _turn_steer_queue=lambda: queue,
    )

    bound = types.MethodType(TurnController.maybe_continue_goal, fake)
    assert bound() is True
    assert queue.peek_count() == 1
    assert str(queue.peek_items()[0]).startswith(GOAL_STEER_PREFIX)

    # 已存在未消费的 goal continuation 时不重复推送
    assert bound() is False
    assert queue.peek_count() == 1

    # 非 active 目标不续跑
    svc.pause_goal("t1")
    queue2 = SteerQueue()
    fake._app._turn_steer_queue = lambda: queue2
    assert bound() is False
    assert queue2.peek_count() == 0


def test_goal_listener_schedules_continuation_from_ui_thread(tmp_path) -> None:
    """listener 在 UI 线程同步触发（call_from_thread 抛 RuntimeError）时仍能调度续跑。

    这是 ``/goal <objective>`` 的真实路径：slash 处理在 UI 线程同步调用
    service.set_goal -> notify -> listener。
    """
    import types
    from types import SimpleNamespace

    from synapse.goals.runtime import GoalService
    from synapse.goals.steering import GOAL_STEER_PREFIX
    from synapse.goals.store import GoalStore
    from synapse.runtime.steer import SteerQueue
    from synapse.ui.tui import CodingAgentApp

    svc = GoalService(GoalStore(tmp_path / "sessions.sqlite"))

    class _FakeApp:
        def __init__(self) -> None:
            self._busy = False
            self.thread_id = "t1"
            self.settings = SimpleNamespace(goal_auto_continue=True)
            self.queue = SteerQueue()
            self.agent = SimpleNamespace(
                _coding_goal_service=svc, _coding_steer_queue=self.queue
            )
            self._current_goal: object | None = None
            self._bottombar = SimpleNamespace(refresh=lambda: None)
            self.deferred: list[tuple[object, ...]] = []
            self.ui_thread_calls = 0

        def _turn_steer_queue(self) -> SteerQueue:
            return self.queue

        def _schedule_followup_steer(self, queue: SteerQueue) -> bool:
            return True

        def call_from_thread(self, callback, *args, **kwargs) -> object:  # noqa: ANN001
            # 模拟 Textual：从 UI 线程调用必须抛 RuntimeError。
            raise RuntimeError("The `call_from_thread` method must run in a different thread")

    fake = object.__new__(CodingAgentApp)
    fake.__dict__.update(_FakeApp().__dict__)
    # 把真实方法绑定到 fake 上
    fake._turn_steer_queue = types.MethodType(CodingAgentApp._turn_steer_queue, fake)
    fake._maybe_continue_goal = types.MethodType(CodingAgentApp._maybe_continue_goal, fake)
    fake._schedule_followup_steer = lambda queue: True  # 不依赖 Textual 运行时
    fake._bind_goal_listener = types.MethodType(CodingAgentApp._bind_goal_listener, fake)

    fake._bind_goal_listener()
    svc.set_goal("t1", "objective")  # notify -> listener（模拟 UI 线程，RuntimeError 回退同步）

    assert fake.queue.peek_count() == 1
    assert str(fake.queue.peek_items()[0]).startswith(GOAL_STEER_PREFIX)


def test_goal_end_to_end_with_real_agent(tmp_path) -> None:
    """真实 agent 图上：middleware 记账钩子不报错、工具可注入、/goal 可管理。"""
    from deepagents import create_deep_agent
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.checkpoint.memory import MemorySaver

    from synapse.goals.middleware import build_goal_middleware
    from synapse.goals.runtime import get_goal_service, init_goal_service
    from synapse.goals.tools import build_goal_tools

    reset_goal_service()
    try:
        init_goal_service(tmp_path / "sessions.sqlite")

        class _ToolBindableFakeModel(FakeMessagesListChatModel):
            def bind_tools(  # noqa: ANN001
                self, tools, **kwargs: object
            ) -> _ToolBindableFakeModel:
                return self

        agent = create_deep_agent(
            model=_ToolBindableFakeModel(responses=[AIMessage(content="ok")]),
            checkpointer=MemorySaver(),
            tools=build_goal_tools(),
            middleware=[build_goal_middleware(enabled=True)],
            subagents=[],
        )
        agent.invoke(
            {"messages": [HumanMessage(content="do the thing")]},
            {"configurable": {"thread_id": "e2e-1"}},
        )
        # middleware 钩子运行后，该 thread 有活跃记账回合
        rt = get_goal_service().runtime("e2e-1")
        assert rt.turn_active()
        # 工具 + 命令可管理目标
        svc = get_goal_service()
        goal, error = svc.set_goal("e2e-1", "long objective")
        assert goal is not None and error is None
        goal, error = svc.pause_goal("e2e-1")
        assert goal is not None and goal.status == ThreadGoalStatus.PAUSED
        goal, error = svc.resume_goal("e2e-1")
        assert goal is not None and goal.status == ThreadGoalStatus.ACTIVE
        goal = svc.on_turn_end("e2e-1")  # 回合结束结算
        assert goal is not None and goal.status == ThreadGoalStatus.ACTIVE
        assert not rt.turn_active()
    finally:
        reset_goal_service()


# ---------------------------------------------------------------------------
# TUI：ESC interrupt 暂停 active goal / 切换会话刷新 goal 状态（对齐 Codex）
# ---------------------------------------------------------------------------
def test_esc_interrupt_pauses_active_goal(tmp_path) -> None:
    """ESC 取消回合时，当前 thread 的 active goal 被置为 paused。

    对齐 Codex ``pause_active_goal_for_interrupt``：goal 保持 ACTIVE 会让
    自动续跑在取消后重新拉起回合，置为 paused 才能真正停住 loop。
    """
    import types
    from types import SimpleNamespace

    from synapse.ui.tui import CodingAgentApp

    svc = GoalService(GoalStore(tmp_path / "sessions.sqlite"))
    svc.set_goal("t1", "objective")

    fake = object.__new__(CodingAgentApp)
    fake.agent = SimpleNamespace(_coding_goal_service=svc)
    fake.thread_id = "t1"

    fake._pause_goal_for_interrupt = types.MethodType(
        CodingAgentApp._pause_goal_for_interrupt, fake
    )
    fake._pause_goal_for_interrupt()

    goal = svc.get("t1")
    assert goal is not None
    assert goal.status == ThreadGoalStatus.PAUSED


def test_esc_interrupt_keeps_terminal_goal_status(tmp_path) -> None:
    """ESC 取消不会把已完成/受阻/受限的 goal 误改回 paused。"""
    import types
    from types import SimpleNamespace

    from synapse.ui.tui import CodingAgentApp

    svc = GoalService(GoalStore(tmp_path / "sessions.sqlite"))
    svc.set_goal("t1", "objective")
    svc.mark_status("t1", ThreadGoalStatus.COMPLETE)

    fake = object.__new__(CodingAgentApp)
    fake.agent = SimpleNamespace(_coding_goal_service=svc)
    fake.thread_id = "t1"

    fake._pause_goal_for_interrupt = types.MethodType(
        CodingAgentApp._pause_goal_for_interrupt, fake
    )
    fake._pause_goal_for_interrupt()

    goal = svc.get("t1")
    assert goal is not None
    assert goal.status == ThreadGoalStatus.COMPLETE


def test_apply_ok_result_reloads_goal_after_thread_switch(tmp_path) -> None:
    """sessions dialog 切换会话后，bottombar goal 状态必须刷新（对齐 Codex）。"""
    import types

    from synapse.commands.result import SlashResult
    from synapse.ui.tui import CodingAgentApp

    svc = GoalService(GoalStore(tmp_path / "sessions.sqlite"))
    svc.set_goal("t1", "objective")
    calls: list[str] = []

    fake = object.__new__(CodingAgentApp)
    fake.thread_id = "t1"
    fake._current_goal = svc.get("t1")
    fake._bottombar = types.SimpleNamespace(refresh=lambda: None)
    fake.agent = types.SimpleNamespace(_coding_goal_service=svc)
    fake._load_current_goal = types.MethodType(CodingAgentApp._load_current_goal, fake)
    fake._apply_ok_result = types.MethodType(CodingAgentApp._apply_ok_result, fake)
    # 其余 UI 方法依赖 Textual 运行时（size/reactive），替换为记录调用，
    # 聚焦验证 thread 切换后 goal 状态刷新这一核心逻辑。
    fake._reset_session_token_chrome = lambda: calls.append("reset_chrome")
    fake._reload_tool_output_stats = lambda: calls.append("reload_stats")
    fake._schedule_transcript_reset = lambda **kwargs: calls.append("reset_transcript")
    fake._emit_system_lines = lambda *args, **kwargs: calls.append("emit_lines")
    fake._reload_session_title = lambda: calls.append("reload_title")
    fake._refresh_topbar = lambda: calls.append("refresh_topbar")
    fake._refresh_codex_usage = lambda **kwargs: calls.append("refresh_codex")

    fake._apply_ok_result(SlashResult(thread_id="t2", clear_log=True))

    assert fake.thread_id == "t2"
    # 空会话没有 goal：_current_goal 被清除，bottombar 隐藏 goal 状态。
    assert fake._current_goal is None
    assert "reset_chrome" in calls
    assert "reload_stats" in calls


def test_turn_done_cancel_consumes_cancel_event(tmp_path) -> None:
    """ESC 取消确认后应重置 event 并丢弃本轮尚未执行的 followup。"""
    import types
    from types import SimpleNamespace

    from synapse.runtime.steer import SteerQueue
    from synapse.ui.tui import CodingAgentApp

    queue = SteerQueue()
    queue.push("stale followup")
    fake = object.__new__(CodingAgentApp)
    fake._skip_steer_followup = True
    fake._cancel_event = threading.Event()
    fake._cancel_event.set()
    fake._busy = True
    fake._active_steer_queue = queue
    fake.agent = SimpleNamespace()
    fake._sync_prompt_placeholder = lambda: None
    fake._on_steer_items_changed = lambda *args, **kwargs: None
    fake._commit_live_tools_to_log = lambda: None
    fake.clear_stream = lambda: None
    fake.set_activity = lambda *args, **kwargs: None
    fake._refresh_git_chrome = lambda: None
    fake._clear_subagent_status = lambda: None
    fake.query_one = lambda *args, **kwargs: SimpleNamespace(focus=lambda: None)
    fake._clear_turn_context = lambda: None
    fake._bind_steer_queue = lambda: None

    fake._turn_done = types.MethodType(CodingAgentApp._turn_done, fake)
    fake._turn_done()

    assert fake._skip_steer_followup is False
    assert not fake._cancel_event.is_set()
    assert queue.peek_count() == 0


def test_scheduled_followup_keeps_cancel_event_from_scheduling_time() -> None:
    """ESC 后即使当前 cancel event 被重置，已排队的 followup 也不能启动。

    ``call_after_refresh`` 延迟执行回调；取消完成路径会为后续新回合创建新的
    event。回调必须检查调度时的旧 event，否则会漏掉已经发生的 ESC。
    """
    import types

    from synapse.runtime.steer import SteerQueue
    from synapse.ui.tui import CodingAgentApp

    queue = SteerQueue()
    queue.push("pending followup")
    scheduled: list[tuple[object, tuple[object, ...]]] = []
    calls: list[str] = []

    fake = object.__new__(CodingAgentApp)
    fake._busy = False
    fake._cancel_event = threading.Event()
    fake._skip_steer_followup = False
    fake._sync_prompt_placeholder = lambda: None
    fake.call_after_refresh = lambda callback, *args: scheduled.append(
        (callback, args)
    ) or True
    fake._turn_done = lambda: calls.append("turn_done")
    fake._maybe_followup_steer = lambda _queue: calls.append("followup_started")
    fake._schedule_followup_steer = types.MethodType(
        CodingAgentApp._schedule_followup_steer, fake
    )
    fake._start_followup_steer = types.MethodType(
        CodingAgentApp._start_followup_steer, fake
    )

    cancelled_turn_event = fake._cancel_event
    assert fake._schedule_followup_steer(queue) is True
    cancelled_turn_event.set()  # ESC arrives before call_after_refresh runs
    fake._cancel_event = threading.Event()  # _turn_done prepares future turns

    callback, args = scheduled.pop()
    callback(*args)

    assert calls == ["turn_done"]
    assert fake._skip_steer_followup is True


def test_schedule_followup_deduplicates_same_queue() -> None:
    """goal listener 与 turn_done 同时调度时，同一队列只能有一个延迟回调。"""
    import types
    from types import SimpleNamespace

    from synapse.runtime.steer import SteerQueue
    from synapse.ui.turn.controller import TurnController

    queue = SteerQueue()
    queue.push("[goal continuation]\ncontinue")
    scheduled: list[tuple[object, ...]] = []
    app = SimpleNamespace(
        _cancel_event=threading.Event(),
        _busy=False,
        _sync_prompt_placeholder=lambda: None,
        call_after_refresh=lambda *args: scheduled.append(args) or True,
    )
    controller = object.__new__(TurnController)
    controller._app = app
    controller.schedule_followup_steer = types.MethodType(
        TurnController.schedule_followup_steer, controller
    )

    assert controller.schedule_followup_steer(queue) is True
    assert controller.schedule_followup_steer(queue) is True

    assert len(scheduled) == 1


def test_esc_cancels_pending_goal_followup_while_runtime_is_idle(tmp_path) -> None:
    """前一回合已 IDLE、goal followup 尚未启动时，ESC 仍应暂停整个 goal。"""
    import types
    from types import SimpleNamespace

    from synapse.runtime.steer import SteerQueue
    from synapse.ui.tui import CodingAgentApp

    svc = GoalService(GoalStore(tmp_path / "sessions.sqlite"))
    svc.set_goal("t1", "objective")
    queue = SteerQueue()
    queue.push("[goal continuation]\ncontinue")
    events: list[str] = []

    fake = SimpleNamespace(
        agent=SimpleNamespace(_coding_goal_service=svc),
        thread_id="t1",
        _turn=SimpleNamespace(busy=False, cancel=lambda reason: False),
        _busy=True,  # call_after_refresh 中待启动的 followup
        _compacting_context=False,
        _cancel_event=threading.Event(),
        screen=object(),
        set_activity=lambda *args, **kwargs: None,
        append_event=lambda message, style: events.append(message),
    )
    fake._pause_goal_for_interrupt = types.MethodType(
        CodingAgentApp._pause_goal_for_interrupt, fake
    )
    fake.action_cancel_run = types.MethodType(CodingAgentApp.action_cancel_run, fake)

    fake.action_cancel_run()

    goal = svc.get("t1")
    assert goal is not None and goal.status == ThreadGoalStatus.PAUSED
    assert fake._cancel_event.is_set()
    assert events == ["已暂停当前 goal。"]


def test_start_followup_drops_goal_continuation_paused_after_scheduling(tmp_path) -> None:
    """goal 在调度后被暂停时，延迟回调不能再拉起新 turn。"""
    import types
    from types import SimpleNamespace

    from synapse.runtime.steer import SteerQueue
    from synapse.ui.turn.controller import TurnController

    svc = GoalService(GoalStore(tmp_path / "sessions.sqlite"))
    svc.set_goal("t1", "objective")
    queue = SteerQueue()
    queue.push("[goal continuation]\ncontinue")
    svc.pause_goal("t1")
    calls: list[str] = []

    fake_app = SimpleNamespace(
        agent=SimpleNamespace(_coding_goal_service=svc),
        thread_id="t1",
        _cancel_event=threading.Event(),
        _skip_steer_followup=False,
        _turn_done=lambda: calls.append("turn_done"),
    )
    controller = object.__new__(TurnController)
    controller._app = fake_app
    controller.start_followup_steer = types.MethodType(
        TurnController.start_followup_steer, controller
    )

    controller.start_followup_steer(queue)

    assert calls == ["turn_done"]
    assert queue.peek_count() == 0
    assert fake_app._skip_steer_followup is True


def test_pause_does_not_overwrite_concurrent_complete(service, monkeypatch) -> None:
    """ESC pause 与 update_goal complete 竞态时，终态不能被改回 paused。"""
    service.set_goal("t1", "objective")
    runtime = service.runtime("t1")
    original_update = service.store.update

    def racing_update(thread_id: str, **kwargs):  # noqa: ANN003
        if kwargs.get("expected_status") == ThreadGoalStatus.ACTIVE:
            original_update(thread_id, status=ThreadGoalStatus.COMPLETE)
        return original_update(thread_id, **kwargs)

    monkeypatch.setattr(service.store, "update", racing_update)

    goal = runtime.pause()

    assert goal is not None
    assert goal.status == ThreadGoalStatus.COMPLETE


def test_goal_resume_after_esc_pause_schedules_continuation(tmp_path) -> None:
    """ESC 暂停的 goal 经 resume 后恢复 ACTIVE，续跑可再次调度。"""
    import types
    from types import SimpleNamespace

    from synapse.goals.runtime import GoalService
    from synapse.goals.steering import GOAL_STEER_PREFIX
    from synapse.goals.store import GoalStore
    from synapse.runtime.steer import SteerQueue
    from synapse.ui.tui import CodingAgentApp

    svc = GoalService(GoalStore(tmp_path / "sessions.sqlite"))
    svc.set_goal("t1", "objective")
    svc.pause_goal("t1")  # 模拟 ESC interrupt 置为 paused

    queue = SteerQueue()
    fake = object.__new__(CodingAgentApp)
    fake._busy = False
    fake.settings = SimpleNamespace(goal_auto_continue=True)
    fake.agent = SimpleNamespace(_coding_goal_service=svc)
    fake.thread_id = "t1"
    fake._active_steer_queue = None
    fake._turn_steer_queue = lambda: queue
    fake._schedule_followup_steer = lambda q: True

    goal, error = svc.resume_goal("t1")
    assert goal is not None and error is None
    assert goal.status == ThreadGoalStatus.ACTIVE

    bound = types.MethodType(CodingAgentApp._maybe_continue_goal, fake)
    assert bound() is True
    assert queue.peek_count() == 1
    assert str(queue.peek_items()[0]).startswith(GOAL_STEER_PREFIX)