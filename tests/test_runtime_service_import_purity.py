"""Import-closure purity gates for the Agent Runtime Service contract layer.

Each purity check spawns a fresh interpreter so it observes the true module
closure: importing a pure DTO submodule must not load the implementation
modules, the session execution stack, the workspace ignore matcher, the
streaming runtime, or the agent execution packages.  Object-identity checks
confirm the lazy package re-exports and the ``streaming.events`` re-exports
still resolve to the very same objects as the owning contract module.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

PURE_MODULES = [
    "synapse.runtime.service.access",
    "synapse.runtime.service.artifacts",
    "synapse.runtime.service.commands",
    "synapse.runtime.service.errors",
    "synapse.runtime.service.event_types",
    "synapse.runtime.service.events",
    "synapse.runtime.service.history",
    "synapse.runtime.service.ports",
    "synapse.runtime.service.project_list",
    "synapse.runtime.service.queries",
    "synapse.runtime.service.recovery",
    "synapse.runtime.service.runtime_config",
    "synapse.runtime.service.session_management",
]

FORBIDDEN_IMPLEMENTATION_PREFIXES = (
    "synapse.runtime.service.local",
    "synapse.runtime.service.routing",
    "synapse.runtime.service.history_store",
    "synapse.runtime.sessions.runtime",
    "synapse.runtime.sessions.manager",
    "synapse.runtime.sessions.persistence",
    "synapse.runtime.tool_ignore",
    "synapse.runtime.agent_loop",
    "synapse.runtime.streaming",
    "langchain",
    "langgraph",
    "deepagents",
)


def _run(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )


def _imported_modules(target: str) -> set[str]:
    proc = _run(
        "import importlib, json, sys\n"
        f"importlib.import_module({target!r})\n"
        "print(json.dumps(sorted(sys.modules)))\n"
    )
    assert proc.returncode == 0, f"import {target!r} failed:\n{proc.stderr}"
    return set(json.loads(proc.stdout.strip().splitlines()[-1]))


def _matches(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(module == prefix or module.startswith(prefix + ".") for prefix in prefixes)


@pytest.mark.parametrize("target", PURE_MODULES)
def test_pure_contract_import_does_not_load_implementation(target: str) -> None:
    loaded = _imported_modules(target)
    leaked = sorted(
        module for module in loaded if _matches(module, FORBIDDEN_IMPLEMENTATION_PREFIXES)
    )
    assert leaked == [], f"{target} leaked implementation modules: {leaked}"


def test_bare_service_package_import_is_lazy() -> None:
    loaded = _imported_modules("synapse.runtime.service")
    leaked = sorted(
        module for module in loaded if _matches(module, FORBIDDEN_IMPLEMENTATION_PREFIXES)
    )
    assert leaked == [], f"package import leaked implementation modules: {leaked}"


def test_sessions_ref_import_does_not_load_execution_stack() -> None:
    loaded = _imported_modules("synapse.runtime.sessions.ref")
    leaked = sorted(
        module
        for module in loaded
        if _matches(
            module,
            (
                "synapse.runtime.sessions.runtime",
                "synapse.runtime.sessions.manager",
                "synapse.runtime.sessions.persistence",
                "synapse.runtime.agent_loop",
            ),
        )
    )
    assert leaked == [], f"sessions.ref leaked execution modules: {leaked}"


def test_package_lazy_reexport_still_resolves_implementation() -> None:
    proc = _run(
        "import json, sys\n"
        "from synapse.runtime.service import ArtifactRef, LocalAgentRuntimeService\n"
        "print(json.dumps({\n"
        "    'svc': LocalAgentRuntimeService.__name__,\n"
        "    'ref': ArtifactRef.__name__,\n"
        "    'local_loaded': 'synapse.runtime.service.local' in sys.modules,\n"
        "}))\n"
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["svc"] == "LocalAgentRuntimeService"
    assert payload["ref"] == "ArtifactRef"
    assert payload["local_loaded"] is True


def test_service_package_reexports_match_submodule_objects() -> None:
    import synapse.runtime.service as service
    from synapse.runtime.service.artifacts import ArtifactRef
    from synapse.runtime.service.events import RuntimeEvent
    from synapse.runtime.service.local import LocalAgentRuntimeService

    assert service.ArtifactRef is ArtifactRef
    assert service.RuntimeEvent is RuntimeEvent
    assert service.LocalAgentRuntimeService is LocalAgentRuntimeService
    assert set(service.__all__) == set(service._LAZY_EXPORTS)


def test_streaming_events_reexports_contract_objects() -> None:
    from synapse.runtime.service import event_types
    from synapse.runtime.streaming import events as streaming_events

    assert streaming_events.EVENT_VERSION == event_types.EVENT_VERSION
    assert streaming_events.TurnEventKind is event_types.TurnEventKind
    assert streaming_events.TurnEvent is event_types.TurnEvent
    assert streaming_events.TextPayload is event_types.TextPayload
    assert streaming_events.ToolItemPayload is event_types.ToolItemPayload
