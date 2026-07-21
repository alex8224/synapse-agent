"""Measure deferred startup phases."""

from __future__ import annotations

import time
from pathlib import Path


def main() -> None:
    t0 = time.perf_counter()
    from coding_agent.config import load_settings

    settings = load_settings(workspace=Path.cwd())
    print(f"{(time.perf_counter()-t0)*1000:8.1f} ms load_settings")

    t1 = time.perf_counter()
    from coding_agent.agent import attach_mcp_to_agent, build_coding_agent

    print(f"{(time.perf_counter()-t1)*1000:8.1f} ms import agent module")

    t2 = time.perf_counter()
    agent = build_coding_agent(settings, project_root=Path.cwd(), load_mcp=False)
    print(
        f"{(time.perf_counter()-t2)*1000:8.1f} ms build load_mcp=False "
        f"mcp_attached={getattr(agent,'_coding_mcp_attached',None)}"
    )

    t3 = time.perf_counter()
    agent2 = attach_mcp_to_agent(settings, agent, project_root=Path.cwd())
    print(
        f"{(time.perf_counter()-t3)*1000:8.1f} ms attach_mcp "
        f"tools={getattr(build_coding_agent,'last_mcp_tool_names',[])}"
    )
    print(f"{(time.perf_counter()-t0)*1000:8.1f} ms TOTAL")
    _ = agent2


if __name__ == "__main__":
    main()
