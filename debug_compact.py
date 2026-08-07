"""Debug: why _find_summarization_middleware returns None with deepagents
0.6.12 + langchain 1.3.14."""

import functools
import inspect
from collections.abc import Mapping

from deepagents import create_deep_agent
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from synapse.runtime.context_compact import (
    _find_summarization_middleware,
    _iter_closure_values,
    force_compact_via_agent,
)


def main() -> None:
    model = FakeListChatModel(responses=["ok"])
    agent = create_deep_agent(
        model=model,
        system_prompt="test",
        tools=[],
        middleware=[],
        name="debug-agent",
    )
    print("=== agent.nodes keys ===")
    print(list(agent.nodes.keys()))
    node = agent.nodes.get("model")
    print("=== node type ===", type(node))
    bound = getattr(node, "bound", None)
    print("=== bound type ===", type(bound))
    if bound is not None:
        print("bound attrs:", [a for a in dir(bound) if not a.startswith("__")])
        mw = getattr(bound, "middleware", None)
        print("bound.middleware:", mw)
        func = getattr(bound, "func", None)
        afunc = getattr(bound, "afunc", None)
        print("bound.func:", type(func), func)
        print("bound.afunc:", type(afunc), afunc)
        print("=== closure scan (func) ===")
        names = []
        for c in _iter_closure_values(func):
            n = getattr(c, "name", None)
            nm = type(c).__name__
            if "Summar" in nm or n == "SummarizationMiddleware":
                names.append((nm, n, type(c)))
        print("func closure summarization hits:", names)
        print("=== closure scan (afunc) ===")
        names = []
        for c in _iter_closure_values(afunc):
            n = getattr(c, "name", None)
            nm = type(c).__name__
            if "Summar" in nm or n == "SummarizationMiddleware":
                names.append((nm, n, type(c)))
        print("afunc closure summarization hits:", names)
    print("=== _find_summarization_middleware ===")
    mw = _find_summarization_middleware(agent)
    print("found:", mw)

    # Also try find by walking compiled nodes bound objects recursively
    print("=== deep walk of model node bound ===")
    seen = set()

    def walk(v, depth=0, trail=""):
        if v is None or id(v) in seen or depth > 8:
            return
        seen.add(id(v))
        nm = type(v).__name__
        if "Summar" in nm or (hasattr(v, "name") and v.name == "SummarizationMiddleware"):
            print("HIT at depth", depth, trail, "->", type(v), "name=", getattr(v, "name", None))
        if isinstance(v, Mapping):
            for k, val in v.items():
                walk(val, depth + 1, trail + f".{{{k}}}")
        elif isinstance(v, (list, tuple)):
            for i, val in enumerate(v):
                walk(val, depth + 1, trail + f"[{i}]")
        elif inspect.isfunction(v) or inspect.ismethod(v):
            try:
                cv = inspect.getclosurevars(v)
            except (TypeError, ValueError):
                return
            for k, val in cv.nonlocals.items():
                walk(val, depth + 1, trail + f".{k}")
        elif isinstance(v, functools.partial):
            walk(v.func, depth + 1, trail + ".func")
            for i, a in enumerate(v.args):
                walk(a, depth + 1, trail + f".args[{i}]")

    walk(bound, trail="bound")
    print("=== force_compact_via_agent ===")
    ok, notes = force_compact_via_agent(agent, thread_id="debug-1")
    print(ok, notes)


if __name__ == "__main__":
    main()
