"""P0 ACP SDK/schema baseline and dependency-boundary tests."""

from __future__ import annotations

import ast
import importlib.metadata
import json
import os
import subprocess
import sys
from pathlib import Path

import acp
import acp.schema as schema

ROOT = Path(__file__).parents[1]
RUNTIME_ROOT = ROOT / "src" / "synapse" / "runtime"
ACP_VERSION = "0.12.0"


def test_official_acp_sdk_baseline_is_locked() -> None:
    assert importlib.metadata.version("agent-client-protocol") == ACP_VERSION
    assert acp.PROTOCOL_VERSION == 1
    assert acp.AGENT_METHODS["initialize"] == "initialize"
    assert acp.AGENT_METHODS["session_new"] == "session/new"
    assert acp.AGENT_METHODS["session_prompt"] == "session/prompt"
    assert acp.AGENT_METHODS["session_cancel"] == "session/cancel"


def test_agent_and_client_method_registries_cover_p0_matrix() -> None:
    expected_agent = {
        "initialize",
        "session_new",
        "session_prompt",
        "session_cancel",
        "session_load",
        "session_list",
        "session_delete",
        "session_close",
        "session_resume",
        "session_fork",
        "session_set_config_option",
    }
    expected_client = {
        "session_update",
        "session_request_permission",
        "fs_read_text_file",
        "fs_write_text_file",
        "terminal_create",
        "terminal_output",
        "terminal_wait_for_exit",
        "terminal_kill",
        "terminal_release",
    }
    assert expected_agent <= acp.AGENT_METHODS.keys()
    assert expected_client <= acp.CLIENT_METHODS.keys()


def test_schema_uses_snake_case_python_fields_and_camel_case_wire_aliases() -> None:
    assert "protocol_version" in schema.InitializeResponse.model_fields
    assert schema.InitializeResponse.model_fields["protocol_version"].alias == "protocolVersion"
    assert "session_id" in schema.NewSessionResponse.model_fields
    assert schema.NewSessionResponse.model_fields["session_id"].alias == "sessionId"
    assert schema.ToolCallStart.model_fields["tool_call_id"].alias == "toolCallId"


def test_schema_golden_initialize_messages_are_valid_single_line_json() -> None:
    fixture = ROOT / "tests" / "fixtures" / "acp" / "initialize.jsonl"
    lines = fixture.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line in lines:
        assert line
        assert "\n" not in line
        payload = json.loads(line)
        assert payload["jsonrpc"] == "2.0"
    request_payload = json.loads(lines[0])["params"]
    response_payload = json.loads(lines[1])["result"]
    request = schema.InitializeRequest.model_validate(request_payload)
    response = schema.InitializeResponse.model_validate(response_payload)
    assert request.protocol_version == 1
    assert response.protocol_version == 1


def test_runtime_source_does_not_import_acp_adapter_or_ui() -> None:
    violations: list[str] = []
    for path in RUNTIME_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name == "acp" or name.startswith("acp.") or name == "synapse.acp":
                    violations.append(f"{path}:{node.lineno}:{name}")
                if name == "textual" or name.startswith("textual.") or name.startswith(
                    "synapse.ui"
                ):
                    violations.append(f"{path}:{node.lineno}:{name}")
    assert violations == []


def test_runtime_import_does_not_load_acp_or_ui() -> None:
    code = (
        "import sys;"
        "import synapse.runtime.agent_loop.turn;"
        "import synapse.runtime.streaming.runtime;"
        "import synapse.runtime.streaming.parser;"
        "bad = [m for m in sys.modules if m == 'acp' or m.startswith('acp.')];"
        "assert not bad, bad"
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=ROOT,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


