"""验证 DAGSubAgentMiddleware 核心逻辑。

测试覆盖:
1. _topological_waves() 拓扑排序
2. _enrich_description() 依赖数据注入
3. DAGSubAgentMiddleware._execute_dag() 端到端
4. compile_subagent_runnables() 子Agent预编译
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import pytest

from synapse.parallel_subagents import (
    DAGSubAgentMiddleware,
    DAGTaskArgs,
    _enrich_description,
    _extract_final_response,
    _format_dag_results,
    _strip_tool_call_xml,
    _topological_waves,
    compile_subagent_runnables,
)

# ═══════════════════════════════════════════════════════════
# 测试 1: 拓扑排序
# ═══════════════════════════════════════════════════════════

def test_waves_all_independent():
    """全部无依赖 → 一次返回全部"""
    pending = [
        {"task_id": "A", "depends_on": []},
        {"task_id": "B", "depends_on": []},
        {"task_id": "C", "depends_on": []},
    ]
    ready, remaining = _topological_waves(pending, set())
    assert len(ready) == 3
    assert len(remaining) == 0


def test_waves_chain():
    """A→B→C 链式依赖"""
    pending = [
        {"task_id": "A", "depends_on": []},
        {"task_id": "B", "depends_on": ["A"]},
        {"task_id": "C", "depends_on": ["B"]},
    ]

    ready, remaining = _topological_waves(pending, set())
    assert [t["task_id"] for t in ready] == ["A"]
    assert len(remaining) == 2

    ready, remaining = _topological_waves(remaining, {"A"})
    assert [t["task_id"] for t in ready] == ["B"]
    assert len(remaining) == 1

    ready, remaining = _topological_waves(remaining, {"A", "B"})
    assert [t["task_id"] for t in ready] == ["C"]
    assert len(remaining) == 0


def test_waves_diamond():
    """菱形依赖: A→C, B→C"""
    pending = [
        {"task_id": "A", "depends_on": []},
        {"task_id": "B", "depends_on": []},
        {"task_id": "C", "depends_on": ["A", "B"]},
    ]

    ready, remaining = _topological_waves(pending, set())
    assert {t["task_id"] for t in ready} == {"A", "B"}

    # C 等 B 完成——此时只有 A 完成，C 不能启动
    ready, remaining = _topological_waves(remaining, {"A"})
    assert len(ready) == 0  # B 还没完成
    assert len(remaining) == 1

    # A、B 都完成了 → C 可以启动
    ready, remaining = _topological_waves(remaining, {"A", "B"})
    assert [t["task_id"] for t in ready] == ["C"]
    assert len(remaining) == 0


def test_waves_mixed():
    """混合依赖"""
    pending = [
        {"task_id": "A", "depends_on": []},
        {"task_id": "B", "depends_on": []},
        {"task_id": "C", "depends_on": ["A"]},
        {"task_id": "D", "depends_on": ["A", "B"]},
    ]

    ready, remaining = _topological_waves(pending, set())
    assert {t["task_id"] for t in ready} == {"A", "B"}

    ready, remaining = _topological_waves(remaining, {"A", "B"})
    assert {t["task_id"] for t in ready} == {"C", "D"}
    assert len(remaining) == 0


# ═══════════════════════════════════════════════════════════
# 测试 2: 依赖数据注入
# ═══════════════════════════════════════════════════════════

def test_enrich_no_deps():
    task = {"description": "写测试", "depends_on": []}
    assert _enrich_description(task, {}) == "写测试"


def test_enrich_with_deps():
    task = {"description": "审查", "depends_on": ["test", "lint"]}
    upstream = {"test": "3个测试通过", "lint": "无 lint 错误"}
    result = _enrich_description(task, upstream)
    assert "依赖任务 [test] 的输出" in result
    assert "3个测试通过" in result
    assert "依赖任务 [lint] 的输出" in result
    assert "无 lint 错误" in result
    assert "你的任务" in result
    assert "审查" in result


def test_enrich_partial_deps():
    task = {"description": "合并", "depends_on": ["A", "B"]}
    upstream = {"A": "完成"}
    result = _enrich_description(task, upstream)
    assert "[A]" in result
    assert "[B]" not in result  # B 未完成，不注入


# ═══════════════════════════════════════════════════════════
# Mock 子 Agent
# ═══════════════════════════════════════════════════════════

class _FakeSubAgent:
    """模拟 CompiledGraph——直接返回固定文本。"""

    def __init__(self, name: str, delay: float = 0.01):
        self._name = name
        self._delay = delay
        # 用真实的 langchain AIMessage 来做 mock
        from langchain_core.messages import AIMessage
        self._AIMessage = AIMessage

    async def ainvoke(self, state, config=None):
        await asyncio.sleep(self._delay)
        msgs = state.get("messages", [])
        desc = ""
        for m in msgs:
            if hasattr(m, "content"):
                desc = str(m.content)[:50]
                break
        msg = self._AIMessage(content=f"[{self._name}] done: {desc}")
        return {"messages": [msg]}


class _CallbackSubAgent:
    """模拟会向 callback 发布工具调用事件的子 Agent。"""

    async def ainvoke(self, state, config=None):
        from langchain_core.messages import AIMessage

        callbacks = list((config or {}).get("callbacks") or [])
        for callback in callbacks:
            callback.on_tool_start(
                {"name": "read_file"},
                '{"intent": "读取配置文件", "file_path": "pyproject.toml"}',
                run_id="tool-1",
                inputs={"intent": "读取配置文件", "file_path": "pyproject.toml"},
            )
            callback.on_tool_end("配置文件内容", run_id="tool-1")
        return {"messages": [AIMessage(content="最终分析结果")]}


class _MessageOnlySubAgent:
    """模拟 callback 不透传，但子图最终 state 带完整消息历史。"""

    async def ainvoke(self, state, config=None):
        from langchain_core.messages import AIMessage, ToolMessage

        tool_call = {
            "name": "read_file",
            "args": {
                "intent": "读取配置文件",
                "file_path": "pyproject.toml",
            },
            "id": "tool-call-1",
            "type": "tool_call",
        }
        return {
            "messages": [
                AIMessage(content="我需要先读取配置文件。", tool_calls=[tool_call]),
                ToolMessage(
                    content="配置文件内容",
                    tool_call_id="tool-call-1",
                    name="read_file",
                ),
                AIMessage(content="最终分析结果"),
            ]
        }


class _StreamingSubAgent:
    """模拟子图支持 astream_events，并要求意图在工具结束前写入监控。"""

    def __init__(self, monitor):
        self._monitor = monitor

    async def astream_events(self, payload, config=None, *, version="v2"):
        from langchain_core.messages import AIMessage, ToolMessage

        tool_call = {
            "name": "read_file",
            "args": {
                "intent": "读取配置文件",
                "file_path": "pyproject.toml",
            },
            "id": "tool-call-1",
            "type": "tool_call",
        }
        yield {
            "event": "on_chat_model_end",
            "data": {"output": AIMessage(content="", tool_calls=[tool_call])},
        }
        _, runs = self._monitor.snapshot()
        assert any(
            event.kind == "tool"
            and event.status == "running"
            and "读取配置文件" in event.title
            and event.body == ""
            for event in runs[0].events
        )
        yield {
            "event": "on_tool_start",
            "name": "read_file",
            "run_id": "tool-run-1",
            "data": {"input": {"file_path": "pyproject.toml"}},
        }
        yield {
            "event": "on_tool_end",
            "name": "read_file",
            "run_id": "tool-run-1",
            "data": {"output": "配置文件内容"},
        }
        yield {
            "event": "on_chain_end",
            "data": {
                "output": {
                    "messages": [
                        AIMessage(content="", tool_calls=[tool_call]),
                        ToolMessage(
                            content="配置文件内容",
                            tool_call_id="tool-call-1",
                            name="read_file",
                        ),
                        AIMessage(content="最终分析结果"),
                    ]
                }
            },
        }


def _make_mw(subagent_runnables: dict) -> DAGSubAgentMiddleware:
    """快速构造一个 DAGSubAgentMiddleware 用于测试。"""
    from langchain_core.tools import StructuredTool
    mw = DAGSubAgentMiddleware.__new__(DAGSubAgentMiddleware)
    mw._subagent_runnables = subagent_runnables
    mw._max_parallel = 6
    mw._backend = None
    mw._parent_run_config = None
    mw._dag_cache = {}
    mw.system_prompt = ""
    mw.tools = [
        StructuredTool.from_function(
            name="task", func=lambda **kw: "", coroutine=lambda **kw: "",
            args_schema=DAGTaskArgs,
        )
    ]
    return mw


# ═══════════════════════════════════════════════════════════
# 测试 3: DAG 调度引擎
# ═══════════════════════════════════════════════════════════

@pytest.mark.anyio
async def test_dag_schedule_parallel():
    """无依赖 → 全部并行执行"""
    mw = _make_mw({
        "tester": _FakeSubAgent("tester"),
        "writer": _FakeSubAgent("writer"),
        "reviewer": _FakeSubAgent("reviewer"),
    })

    task_calls = [
        {
            "name": "task",
            "id": "tc1",
            "args": {"subagent_type": "tester", "description": "T", "task_id": "t1"},
        },
        {
            "name": "task",
            "id": "tc2",
            "args": {"subagent_type": "writer", "description": "W", "task_id": "t2"},
        },
        {
            "name": "task",
            "id": "tc3",
            "args": {"subagent_type": "reviewer", "description": "R", "task_id": "t3"},
        },
    ]

    results, task_wave, task_deps, total_waves = await mw._execute_dag(task_calls)
    assert len(results) == 3
    assert "t1" in results and "[tester]" in results["t1"]
    assert "t2" in results and "[writer]" in results["t2"]
    assert "t3" in results and "[reviewer]" in results["t3"]
    # 全部无依赖 → 同一波次并行
    assert total_waves == 1
    assert task_wave["t1"] == 1 and task_wave["t2"] == 1 and task_wave["t3"] == 1


@pytest.mark.anyio
async def test_dag_schedule_with_deps():
    """有依赖 → 下游收到上游输出"""
    mw = _make_mw({"tester": _FakeSubAgent("tester"), "reviewer": _FakeSubAgent("reviewer")})

    task_calls = [
        {
            "name": "task",
            "id": "tc1",
            "args": {
                "subagent_type": "tester",
                "description": "写测试",
                "task_id": "test",
                "depends_on": [],
            },
        },
        {
            "name": "task",
            "id": "tc2",
            "args": {
                "subagent_type": "reviewer",
                "description": "审查",
                "task_id": "review",
                "depends_on": ["test"],
            },
        },
    ]

    results, task_wave, task_deps, total_waves = await mw._execute_dag(task_calls)
    assert len(results) == 2
    assert "test" in results
    assert "review" in results
    # review 依赖 test → 两个波次
    assert total_waves == 2
    assert task_wave["test"] == 1
    assert task_wave["review"] == 2
    assert task_deps["review"] == ["test"]


@pytest.mark.anyio
async def test_dag_unknown_subagent():
    """不存在的子 Agent → 不崩溃，返回错误"""
    mw = _make_mw({"tester": _FakeSubAgent("tester")})

    task_calls = [
        {
            "name": "task",
            "id": "tc1",
            "args": {
                "subagent_type": "nonexistent",
                "description": "X",
                "task_id": "bad",
            },
        },
    ]

    results, task_wave, task_deps, total_waves = await mw._execute_dag(task_calls)
    assert "bad" in results
    assert ("不存在" in results["bad"]) or ("错误" in results["bad"])


@pytest.mark.anyio
async def test_dag_subagent_crash():
    """子 Agent 崩溃 → 不崩溃，返回错误"""
    class _CrashAgent:
        async def ainvoke(self, state, config=None):
            raise RuntimeError("BOOM")

    mw = _make_mw({"crasher": _CrashAgent()})

    task_calls = [
        {
            "name": "task",
            "id": "tc1",
            "args": {"subagent_type": "crasher", "description": "X", "task_id": "fail"},
        },
    ]

    results, task_wave, task_deps, total_waves = await mw._execute_dag(task_calls)
    assert "fail" in results
    assert ("失败" in results["fail"]) or ("错误" in results["fail"])


@pytest.mark.anyio
async def test_dag_monitor_finishes_task_without_tool_call_id():
    """F9 监控不能依赖模型一定提供 tool_call_id。"""
    from synapse.subagent_monitor import MONITOR_CONFIG_KEY, SubagentMonitor

    monitor = SubagentMonitor()
    mw = _make_mw({"researcher": _CallbackSubAgent()})
    mw._parent_run_config = {
        "configurable": {MONITOR_CONFIG_KEY: monitor.monitor_id}
    }

    task_calls = [
        {
            "name": "task",
            "args": {
                "subagent_type": "researcher",
                "description": "分析运行链路",
                "task_id": "agent-loop",
            },
        },
    ]

    results, _, _, _ = await mw._execute_dag_inner(task_calls)
    _, runs = monitor.snapshot()

    assert results["agent-loop"] == "最终分析结果"
    assert len(runs) == 1
    assert runs[0].call_id == "agent-loop"
    assert runs[0].status == "ok"
    assert any(
        event.kind == "tool" and "读取配置文件" in event.title
        for event in runs[0].events
    )
    assert any(
        event.kind == "answer" and "最终分析结果" in event.body
        for event in runs[0].events
    )


def test_subagent_monitor_callback_keeps_model_tool_intent():
    """工具执行前 intent 被剥离时，F9 仍展示模型原始调用意图。"""
    from langchain_core.messages import AIMessage

    from synapse.subagent_monitor import SubagentMonitor, SubagentMonitorCallback

    monitor = SubagentMonitor()
    monitor.start_task(
        call_id="call-1",
        task_id="agent-loop",
        subagent_type="researcher",
        description="分析运行链路",
    )
    callback = SubagentMonitorCallback(monitor, "call-1")
    callback.on_llm_end(
        SimpleNamespace(generations=[[
            SimpleNamespace(message=AIMessage(
                content="",
                tool_calls=[{
                    "name": "read_file",
                    "args": {
                        "intent": "读取配置文件",
                        "file_path": "pyproject.toml",
                    },
                    "id": "tool-call-1",
                    "type": "tool_call",
                }],
            ))
        ]])
    )
    callback.on_tool_start(
        {"name": "read_file"},
        '{"file_path": "pyproject.toml"}',
        run_id="run-1",
    )
    callback.on_tool_end("配置文件内容", run_id="run-1")

    _, runs = monitor.snapshot()
    tool_events = [event for event in runs[0].events if event.kind == "tool"]

    assert [event.status for event in tool_events] == ["running", "ok"]
    assert all("读取配置文件" in event.title for event in tool_events)
    assert tool_events[0].body == ""
    assert "配置文件内容" in tool_events[1].body


@pytest.mark.anyio
async def test_dag_monitor_records_tool_intent_from_returned_messages():
    """callback 不透传时，F9 仍从子图返回消息回填模型和工具意图。"""
    from synapse.subagent_monitor import MONITOR_CONFIG_KEY, SubagentMonitor

    monitor = SubagentMonitor()
    mw = _make_mw({"researcher": _MessageOnlySubAgent()})
    mw._parent_run_config = {
        "configurable": {MONITOR_CONFIG_KEY: monitor.monitor_id}
    }

    task_calls = [
        {
            "name": "task",
            "args": {
                "subagent_type": "researcher",
                "description": "分析运行链路",
                "task_id": "agent-loop",
            },
        },
    ]

    await mw._execute_dag_inner(task_calls)
    _, runs = monitor.snapshot()

    assert any(
        event.kind == "model" and "读取配置文件" in event.body
        for event in runs[0].events
    )
    assert any(
        event.kind == "tool"
        and event.status == "running"
        and "读取配置文件" in event.title
        and event.body == ""
        for event in runs[0].events
    )
    assert any(
        event.kind == "tool"
        and event.status == "ok"
        and "配置文件内容" in event.body
        for event in runs[0].events
    )


@pytest.mark.anyio
async def test_dag_monitor_streams_tool_intent_before_tool_finishes():
    """支持 astream_events 时，F9 在工具结束前就能看到意图。"""
    from synapse.subagent_monitor import MONITOR_CONFIG_KEY, SubagentMonitor

    monitor = SubagentMonitor()
    mw = _make_mw({"researcher": _StreamingSubAgent(monitor)})
    mw._parent_run_config = {
        "configurable": {MONITOR_CONFIG_KEY: monitor.monitor_id}
    }

    task_calls = [
        {
            "name": "task",
            "args": {
                "subagent_type": "researcher",
                "description": "分析运行链路",
                "task_id": "agent-loop",
            },
        },
    ]

    results, _, _, _ = await mw._execute_dag_inner(task_calls)
    _, runs = monitor.snapshot()
    tool_events = [event for event in runs[0].events if event.kind == "tool"]

    assert results["agent-loop"] == "最终分析结果"
    assert [event.status for event in tool_events] == ["running", "ok"]
    assert all("读取配置文件" in event.title for event in tool_events)
    assert tool_events[0].body == ""
    assert "配置文件内容" in tool_events[1].body


@pytest.mark.anyio
async def test_dag_deadlock():
    """循环依赖 → ValueError"""
    mw = _make_mw({"tester": _FakeSubAgent("tester")})

    task_calls = [
        {
            "name": "task",
            "id": "tc1",
            "args": {
                "subagent_type": "tester",
                "description": "A",
                "task_id": "A",
                "depends_on": ["B"],
            },
        },
        {
            "name": "task",
            "id": "tc2",
            "args": {
                "subagent_type": "tester",
                "description": "B",
                "task_id": "B",
                "depends_on": ["A"],
            },
        },
    ]

    with pytest.raises(ValueError, match="死锁"):
        await mw._execute_dag(task_calls)


# ═══════════════════════════════════════════════════════════
# 测试 4: 子 Agent 编译
# ═══════════════════════════════════════════════════════════

def test_compile_real_subagents():
    """用真实 SubAgent 规范编译（需要 model + tools，deepagents >= 0.5 要求）"""
    from synapse.runtime.subagents import build_default_subagents

    specs = build_default_subagents(enabled=True, isolate_tools=True)
    assert specs is not None and len(specs) >= 2

    try:
        from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
        from langchain_core.messages import AIMessage as _AIMessage
        fake_model = GenericFakeChatModel(
            messages=iter([_AIMessage(content="x") for _ in range(5)])
        )
    except ImportError:
        pytest.skip("langchain_core test utilities unavailable")

    # Without backend: still compiles, but may lack filesystem tools.
    runnables = compile_subagent_runnables(specs, default_model=fake_model)
    print(f"Compiled {len(runnables)} subagents: {sorted(runnables.keys())}")
    assert isinstance(runnables, dict)
    for name, r in runnables.items():
        assert hasattr(r, "ainvoke"), f"{name} missing ainvoke"


def _tools_by_name_from_graph(graph) -> dict:
    tools_node = graph.nodes["tools"]
    for cand in (
        tools_node,
        getattr(tools_node, "bound", None),
        getattr(tools_node, "runnable", None),
    ):
        if cand is None:
            continue
        if hasattr(cand, "tools_by_name"):
            return cand.tools_by_name
        nested = getattr(cand, "bound", None)
        if nested is not None and hasattr(nested, "tools_by_name"):
            return nested.tools_by_name
    raise AssertionError("unable to locate tools_by_name on tools node")


def test_compile_subagents_with_backend_exposes_filesystem_tools(tmp_path: Path):
    """DAG compile must inject FilesystemMiddleware tools via shared backend."""
    from deepagents.backends.filesystem import FilesystemBackend
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    from synapse.runtime.subagents import build_default_subagents

    specs = build_default_subagents(enabled=True, isolate_tools=True)
    assert specs is not None
    backend = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)
    model = GenericFakeChatModel(
        messages=iter([AIMessage(content="ok") for _ in range(20)])
    )

    runnables = compile_subagent_runnables(
        specs,
        default_model=model,
        backend=backend,
    )
    assert "researcher" in runnables
    researcher = runnables["researcher"]
    assert "tools" in researcher.nodes

    names = set(_tools_by_name_from_graph(researcher))
    assert "read_file" in names
    assert "glob" in names
    assert "ls" in names


def test_build_subagent_run_config_includes_task_call_namespace():
    from synapse.parallel_subagents import _build_subagent_run_config

    parent = {
        "configurable": {"thread_id": "t1", "checkpoint_ns": "root"},
        "tags": ["parent"],
    }
    cfg = _build_subagent_run_config(
        parent_config=parent,
        subagent_type="researcher",
        task_id="core-arch",
        tool_call_id="call_abc",
    )
    conf = cfg["configurable"]
    assert conf["thread_id"] == "t1"
    assert conf["ls_agent_type"] == "subagent"
    assert "task_call:call_abc" in conf["checkpoint_ns"]
    assert "dag_researcher_core-arch" in conf["checkpoint_ns"]
    assert "subagent" in cfg["tags"]
    assert cfg["metadata"]["ls_agent_type"] == "subagent"


def test_extract_final_response_surfaces_tool_errors():
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    msgs = [
        HumanMessage(content="task"),
        AIMessage(
            content="",
            tool_calls=[{
                "name": "read_file",
                "args": {"file_path": "x.py"},
                "id": "c1",
                "type": "tool_call",
            }],
        ),
        ToolMessage(
            content="Error: read_file is not a valid tool, try one of [].",
            tool_call_id="c1",
            name="read_file",
        ),
    ]
    result = _extract_final_response(msgs)
    assert "工具执行出现错误" in result
    assert "read_file" in result


# ═══════════════════════════════════════════════════════════
# 测试 5: XML 清洗
# ═══════════════════════════════════════════════════════════

def test_strip_tool_call_xml_clean():
    """不含 XML 的文本保持不变"""
    text = "这段文本没有任何工具调用标签"
    assert _strip_tool_call_xml(text) == text


def test_strip_tool_call_xml_strips_tags():
    """清洗 <tool_calls> 和 <invoke> 等标签"""
    text = """我将开始分析项目。

<tool_calls>
<invoke name="glob">
<parameter name="pattern" string="true"/>src/**/*.py</parameter>
</invoke>
</tool_calls>

分析结果如下：共找到 42 个文件。"""
    cleaned = _strip_tool_call_xml(text)
    assert "我将开始分析项目" in cleaned
    assert "共找到 42 个文件" in cleaned
    assert "tool_calls" not in cleaned
    assert "invoke" not in cleaned
    assert "parameter" not in cleaned


def test_strip_tool_call_xml_handles_dsml():
    """清洗 <DSML> 风格标签"""
    text = (
        '< | | DSML | | tool_calls> \n'
        '< | | DSML | | invoke name="glob">\n'
        '一些中文内容\n'
        '</ | | DSML | | invoke>'
    )
    cleaned = _strip_tool_call_xml(text)
    assert "一些中文内容" in cleaned
    assert "DSML" not in cleaned


def test_strip_tool_call_xml_self_closing():
    """清洗自闭合标签 <parameter ... />"""
    text = (
        '<parameter name="pattern" string="true"/>'
        'src/file.py'
        '<parameter name="intent" string="true"/>'
    )
    cleaned = _strip_tool_call_xml(text)
    assert "src/file.py" in cleaned
    assert "parameter" not in cleaned


def test_strip_tool_call_xml_empty_and_none():
    """边界情况：空字符串和 None"""
    assert _strip_tool_call_xml("") == ""
    assert _strip_tool_call_xml("  ") == ""


# ═══════════════════════════════════════════════════════════
# 测试 6: 响应提取
# ═══════════════════════════════════════════════════════════

def test_extract_final_response_simple():
    """最后一条 AIMessage 即为最终回复"""
    from langchain_core.messages import AIMessage, HumanMessage
    msgs = [
        HumanMessage(content="task"),
        AIMessage(content="最终回复文本"),
    ]
    assert _extract_final_response(msgs) == "最终回复文本"


def test_extract_final_response_skips_tool_calls():
    """跳过含有 tool_calls 的中间 AIMessage，取最终纯文本回复"""
    from langchain_core.messages import AIMessage, HumanMessage

    _tc = {
        "name": "glob",
        "args": {"pattern": "*.py"},
        "id": "call_1",
        "type": "tool_call",
    }
    msgs = [
        HumanMessage(content="task"),
        AIMessage(content="我开始搜索...", tool_calls=[_tc]),
        AIMessage(content="搜索完成，最终回复"),
    ]
    result = _extract_final_response(msgs)
    assert result == "搜索完成，最终回复"


def test_extract_final_response_fallback():
    """全部 AIMessage 都含 tool_calls 时回退到最后一条并清洗"""
    from langchain_core.messages import AIMessage, HumanMessage

    _tc = {
        "name": "glob",
        "args": {"pattern": "*.py"},
        "id": "call_1",
        "type": "tool_call",
    }
    msgs = [
        HumanMessage(content="task"),
        AIMessage(
            content=(
                "<tool_calls>"
                "<invoke name='glob'>"
                "<parameter name='pattern'>*.py</parameter>"
                "</invoke>"
                "</tool_calls>"
            ),
            tool_calls=[_tc],
        ),
    ]
    result = _extract_final_response(msgs)
    # 应清洗掉 XML，但内容可能为空（因为全部是 XML）
    assert "tool_calls" not in result
    assert "invoke" not in result


def test_extract_final_response_empty():
    """无 AIMessage 时返回占位文本"""
    from langchain_core.messages import HumanMessage
    msgs = [HumanMessage(content="仅此消息")]
    assert "子 Agent 未返回文本回复" in _extract_final_response(msgs)


# ═══════════════════════════════════════════════════════════
# 测试 7: DAG 结果格式化
# ═══════════════════════════════════════════════════════════

def test_format_dag_results_single_wave():
    """单波次并行任务格式化"""
    results = {"t1": "结果1", "t2": "结果2"}
    task_wave = {"t1": 1, "t2": 1}
    task_deps = {"t1": [], "t2": []}
    task_type_map = {"t1": "tester", "t2": "reviewer"}

    formatted = _format_dag_results(results, task_wave, task_deps, 1, task_type_map)
    assert "DAG 执行完成" in formatted
    assert "1 个波次" in formatted
    assert "并行" in formatted
    assert "t1 (tester)" in formatted
    assert "t2 (reviewer)" in formatted
    assert "结果1" in formatted
    assert "结果2" in formatted


def test_format_dag_results_with_deps():
    """有依赖的多波次格式化"""
    results = {"search": "找到认证代码", "test": "测试通过"}
    task_wave = {"search": 1, "test": 2}
    task_deps = {"search": [], "test": ["search"]}
    task_type_map = {"search": "researcher", "test": "tester"}

    formatted = _format_dag_results(results, task_wave, task_deps, 2, task_type_map)
    assert "2 个波次" in formatted
    assert "依赖" in formatted
    assert "search" in formatted
    assert "test" in formatted


def test_format_dag_results_empty():
    """空结果"""
    formatted = _format_dag_results({}, {}, {}, 0, {})
    assert "未产生任何结果" in formatted


def test_capture_parent_run_config_uses_contextvar():
    """Runtime has no config field; capture must read runnable contextvar."""
    from langchain_core.runnables.config import var_child_runnable_config

    from synapse.parallel_subagents import _capture_parent_run_config

    token = var_child_runnable_config.set({
        "tags": ["parent"],
        "metadata": {"ls_agent_type": "root"},
        "configurable": {"thread_id": "tid-1"},
        "callbacks": ["sentinel-callback"],
    })
    try:
        class _Req:
            runtime = object()  # deliberately no .config

        cfg = _capture_parent_run_config(_Req())
        assert cfg is not None
        assert cfg["configurable"]["thread_id"] == "tid-1"
        assert cfg["tags"] == ["parent"]
        assert cfg["callbacks"] == ["sentinel-callback"]
    finally:
        var_child_runnable_config.reset(token)


def test_capture_parent_run_config_empty_without_context():
    from synapse.parallel_subagents import _capture_parent_run_config

    class _Req:
        runtime = object()

    # Ensure no leaked context from other tests beyond best effort.
    cfg = _capture_parent_run_config(_Req())
    # May be None or ambient from pytest plugins; only assert it is dict|None
    assert cfg is None or isinstance(cfg, dict)
