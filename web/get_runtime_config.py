import json
from pathlib import Path

from synapse.integrations.mcp_client import load_mcp_server_configs
from synapse.models.registry import registry_from_settings
from synapse.settings import load_project_settings

workspace = Path(__file__).resolve().parent.parent
s = load_project_settings(workspace)
reg = registry_from_settings(s)
models = sorted(list(reg.list_names())) if reg else []
current_model = (
    getattr(s, "active_model", None)
    or (reg.default if reg else None)
    or s.model
    or "openai:deepseek-v4-flash"
)

mcp_servers = load_mcp_server_configs(path=s.mcp_config_path, json_blob=s.mcp_servers_json)
servers_list = [
    {
        "name": srv.name,
        "transport": srv.transport,
        "enabled": srv.enabled,
        "tool_prefix": srv.tool_prefix,
    }
    for srv in mcp_servers
]

allowed_think = list(reg.allowed_thinking_levels(current_model)) if reg and current_model else []
if not allowed_think:
    allowed_think = ['off', 'minimal', 'low', 'medium', 'high', 'max']

current_think = getattr(s, 'reasoning_effort', None) or 'high'

res = {
    'current_model': current_model,
    'available_models': models,
    'thinking_level': current_think,
    'thinking_levels': allowed_think,
    'mcp_servers': servers_list,
    'mcp_count': 0,
    'mcp_enabled': bool(getattr(s, 'enable_mcp', True)),
    'goal': getattr(s, 'active_goal', None) or '无活跃目标'
}
print(json.dumps(res))
