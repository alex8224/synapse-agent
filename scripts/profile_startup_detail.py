import time
from pathlib import Path

from coding_agent.config import load_settings
from coding_agent.models_registry import registry_from_settings

s = load_settings(workspace=Path.cwd())
reg = registry_from_settings(s)

t0 = time.perf_counter()
from coding_agent.llm_openai_compat import enable_openai_compat_reasoning_patch

print(f"import patch {(time.perf_counter()-t0)*1000:.1f}ms")

t0 = time.perf_counter()
enable_openai_compat_reasoning_patch()
print(f"enable patch {(time.perf_counter()-t0)*1000:.1f}ms")

t0 = time.perf_counter()
from langchain.chat_models import init_chat_model

print(f"import init_chat_model {(time.perf_counter()-t0)*1000:.1f}ms")

t0 = time.perf_counter()
import langchain_openai

print(f"import langchain_openai {(time.perf_counter()-t0)*1000:.1f}ms")

t0 = time.perf_counter()
model = reg.build_chat_model(
    reg.default,
    fallback_api_key=s.openai_api_key or s.anthropic_api_key,
    fallback_base_url=s.openai_base_url,
    fallback_enable_thinking=s.enable_thinking,
    fallback_reasoning_effort=s.reasoning_effort or "high",
    fallback_parallel_tool_calls=s.parallel_tool_calls,
)
print(f"build_chat_model {(time.perf_counter()-t0)*1000:.1f}ms -> {type(model)}")

t0 = time.perf_counter()
from coding_agent.mcp_client import load_mcp_server_configs, load_mcp_tools

servers = load_mcp_server_configs(workspace=s.workspace)
print(f"mcp configs {(time.perf_counter()-t0)*1000:.1f}ms n={len(servers)}")

for srv in servers:
    t0 = time.perf_counter()
    r = load_mcp_tools([srv], enabled=True)
    print(
        f"mcp connect {srv.name} {(time.perf_counter()-t0)*1000:.1f}ms "
        f"tools={r.tool_names} warn={r.warnings}"
    )
