"""Runtime contract v1: registry authority, manifest, and generated TypeScript.

The gates here are deliberately independent of the registry whenever possible:
the port surface is read from ``service/ports.py``, the ACL mapping from an AST
scan of ``service/access.py``, the wire method table and feature flags from
``transport/protocol.py``, the event kinds from ``event_types.TurnEventKind``,
the wire defaults from real ``decode_params`` calls, and the web type names from
the committed web console sources.  The registry is then compared against all of
them, so a self-consistent but wrong registry cannot pass.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from synapse.runtime.service import contract_export, contract_registry
from synapse.runtime.service.access import ALL_RUNTIME_CAPABILITIES
from synapse.runtime.service.artifacts import (
    DEFAULT_CHUNK_BYTES,
    MAX_CHUNK_BYTES,
    MIN_CHUNK_BYTES,
)
from synapse.runtime.service.commands import ApprovalDecision
from synapse.runtime.service.event_types import TurnEvent, TurnEventKind
from synapse.runtime.service.events import (
    DEFAULT_MAX_EVENT_BYTES,
    DEFAULT_SCAN_LIMIT,
    MAX_EVENT_BYTES,
    MIN_EVENT_BYTES,
    EventFilter,
    RuntimeEvent,
)
from synapse.runtime.service.history import (
    HISTORY_LIMIT_DEFAULT,
    SESSION_LIST_LIMIT_DEFAULT,
)
from synapse.runtime.service.ports import AgentRuntimeService
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.transport import protocol

ROOT = Path(__file__).resolve().parents[1]
SERVICE_DIR = ROOT / "src" / "synapse" / "runtime" / "service"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "runtime_contract" / "v1"
SCRIPT = ROOT / "scripts" / "export_contract_manifest.py"

#: The shared, language-neutral fixture consumed by both the Python and the
#: TypeScript gates (it lives beside the v1 fixture directory, not inside it).
SHARED_FIXTURE = FIXTURES.parent / "fixtures.json"

#: ``SessionRef`` as it appears on the wire, used by the decoder probes.
_WIRE_REF = {"project_id": "p1", "thread_id": "t1"}

REF = SessionRef(project_id="p1", thread_id="t1")

#: The frozen v1 baseline, recorded literally.  Growth is asserted *against*
#: these sets, so an additive change must name itself in the additive sets below
#: instead of quietly rewriting the expected count in place.
V1_SERVICE_METHODS = frozenset(
    {
        "cancel_turn",
        "close_session",
        "get_runtime_config",
        "get_session",
        "get_session_goal",
        "list_artifacts",
        "list_sessions",
        "open_session",
        "pending_approval",
        "read_artifact",
        "read_events",
        "read_session_history",
        "rebind_session",
        "reconcile_session",
        "reload_mcp",
        "resume_turn",
        "set_project_thinking_level",
        "set_thinking_level",
        "stat_artifact",
        "steer_turn",
        "submit_turn",
        "watch_events",
    }
)
V1_WIRE_METHODS = frozenset(
    {
        "runtime.artifacts.list",
        "runtime.artifacts.read",
        "runtime.artifacts.stat",
        "runtime.config.get",
        "runtime.events.read",
        "runtime.events.unwatch",
        "runtime.events.watch",
        "runtime.protocol.negotiate",
        "runtime.project.thinking.set",
        "runtime.session.close",
        "runtime.session.get",
        "runtime.session.goal",
        "runtime.session.history",
        "runtime.session.list",
        "runtime.session.mcp.reload",
        "runtime.session.open",
        "runtime.session.rebind",
        "runtime.session.reconcile",
        "runtime.session.thinking.set",
        "runtime.turn.approval.get",
        "runtime.turn.approval.resume",
        "runtime.turn.cancel",
        "runtime.turn.steer",
        "runtime.turn.submit",
    }
)
V1_AUTHORIZATION_CAPABILITIES = frozenset(
    {
        "artifacts.list",
        "artifacts.read",
        "artifacts.stat",
        "events.read",
        "events.watch",
        "project.thinking",
        "session.close",
        "session.list",
        "session.mcp.reload",
        "session.open",
        "session.read",
        "session.rebind",
        "session.thinking",
        "turn.approval.read",
        "turn.approval.resume",
        "turn.cancel",
        "turn.steer",
        "turn.submit",
    }
)

#: Additive service port methods added after the v1 baseline (wire name -> port
#: method).  A new method must be listed here *and* in the wire baseline diff.
ADDITIVE_WIRE_METHODS = {
    "runtime.project.list": "list_projects",
    "runtime.session.create": "create_session",
    "runtime.session.rename": "rename_session",
    "runtime.session.delete": "delete_session",
    "runtime.session.search": "search_sessions",
    "runtime.session.goal.set": "set_session_goal",
    "runtime.session.goal.edit": "edit_session_goal",
    "runtime.session.goal.clear": "clear_session_goal",
    "runtime.session.goal.pause": "pause_session_goal",
    "runtime.session.goal.resume": "resume_session_goal",
    "runtime.attachments.begin": "begin_attachment",
    "runtime.attachments.append": "append_attachment_chunk",
    "runtime.attachments.finish": "finish_attachment",
    "runtime.attachments.abort": "abort_attachment",
    "runtime.attachments.stat": "stat_attachment",
    "runtime.attachments.read": "read_attachment",
    "runtime.git.status": "git_status",
    "runtime.git.diff": "git_diff",
    "runtime.workspace.revert": "revert_turn_change",
    "runtime.project.register": "register_project",
    "runtime.fs.list": "list_directories",
    "runtime.codex.usage.get": "get_codex_usage",
    "runtime.codex.reset_credits.get": "get_codex_reset_credits",
    "runtime.codex.reset_credits.consume": "consume_codex_reset",
    "runtime.apps.list": "list_external_apps",
    "runtime.workspace.open_external": "open_external",
    "runtime.skills.list": "list_skills",
}

#: Authorization capabilities added on top of the frozen v1 ACL surface.  Like the
#: wire-method set, growth is named here instead of rewriting the v1 baseline.
ADDITIVE_AUTHORIZATION_CAPABILITIES = frozenset(
    {
        "project.list",
        "session.create",
        "session.rename",
        "session.delete",
        "session.search",
        "session.goal",
        "attachments.read",
        "attachments.write",
        "git.status",
        "git.diff",
        "workspace.revert",
        "project.register",
        "fs.list",
        "codex.usage.read",
        "codex.reset.consume",
        "apps.list",
        "workspace.open_external",
        "skills.list",
    }
)

#: Web console type names added on top of the frozen v1 surface
#: (``tests/fixtures/runtime_contract/v1/web_console_types.json``).
ADDITIVE_WEB_CONSOLE_TYPES = frozenset(
    {
        "ListProjectsParams",
        "ProjectListItem",
        "ProjectListResult",
        "CreateSessionParams",
        "CreateSessionResult",
        "RenameSessionParams",
        "RenameSessionResult",
        "DeleteSessionParams",
        "DeleteSessionResult",
        "SearchSessionsParams",
        "SessionSearchResult",
        "SetSessionGoalParams",
        "EditSessionGoalParams",
        "ClearSessionGoalParams",
        "PauseSessionGoalParams",
        "ResumeSessionGoalParams",
        "SessionGoalResult",
        # Attachment upload/read DTOs added by the image-attachment slice.
        "AbortAttachmentCommand",
        "AbortAttachmentResult",
        "AppendAttachmentChunkCommand",
        "AppendAttachmentChunkResult",
        "AttachmentChunk",
        "AttachmentMetadata",
        "AttachmentRef",
        "BeginAttachmentCommand",
        "BeginAttachmentResult",
        "FinishAttachmentCommand",
        "FinishAttachmentResult",
        "HistoryAttachment",
        "ReadAttachmentQuery",
        "StatAttachmentQuery",
        # Project-registration and host-directory-browse DTOs (the "add project" flow).
        "RegisterProjectParams",
        "RegisterProjectResult",
        "ListDirectoriesParams",
        "ListDirectoriesResult",
        "DirectoryEntry",
        "DirectoryListing",
        # Reverting one file of one turn (the change cards' undo).
        "RevertTurnChangeCommand",
        "RevertTurnChangeResult",
        # Discoverable Agent Skills enumeration.
        "ListSkillsParams",
        "ListSkillsResult",
        "SkillEntry",
        "SkillListPage",
    }
)

#: In-process objects, implementation classes, and security assembly that must
#: never appear as a contract schema.
FORBIDDEN_SCHEMA_NAMES = frozenset(
    {
        "AgentRuntimeService",
        "AclAuthorizer",
        "AclGrant",
        "AccessRequest",
        "BrokerRecoveryState",
        "CatalogProjectProvider",
        "DaemonAuthorizer",
        "EventStream",
        "EventWatch",
        "LocalAgentRuntimeService",
        "LocalEventStream",
        "LocalEventWatch",
        "ManagerFactory",
        "Principal",
        "ProjectProvider",
        "RuntimeManagerRouter",
        "RuntimeProject",
        "SessionEventEnvelope",
        "SessionEventWindow",
        # Session management resolves its project settings and live manager
        # through an in-process context object and a metadata-store port; neither
        # may ever become a wire schema (they carry settings/managers, not data).
        "SessionProjectContext",
        "SessionMetadataStore",
        "SessionMetadataService",
        "SessionStoreMetadataStore",
        "Settings",
        "TurnHandle",
    }
)


def _schema(name: str) -> dict[str, Any]:
    entry = contract_export.manifest()["schemas"]
    assert isinstance(entry, dict)
    assert name in entry, f"schema {name!r} is not declared"
    value = entry[name]
    assert isinstance(value, dict)
    return value


def _field_names(name: str) -> list[str]:
    fields = _schema(name)["fields"]
    assert isinstance(fields, list)
    return [field["name"] for field in fields]


def _field(name: str, field: str) -> dict[str, Any]:
    fields = _schema(name)["fields"]
    assert isinstance(fields, list)
    for item in fields:
        if item["name"] == field:
            return item
    raise AssertionError(f"schema {name!r} has no field {field!r}")


def _port_methods() -> set[str]:
    """The public method surface of the service port, read from the protocol."""
    return {
        name
        for name, value in vars(AgentRuntimeService).items()
        if not name.startswith("_") and callable(value)
    }


def _registry_service_methods() -> dict[str, contract_registry.WireMethod]:
    methods = {
        method.service_method: method
        for method in contract_registry.WIRE_METHODS
        if method.method_class == "service"
    }
    assert None not in methods
    return methods  # type: ignore[return-value]


def _access_source_capability_map() -> dict[str, set[str]]:
    """Extract ``method -> {capability}`` from ``service/access.py`` via AST.

    This reads the ACL layer's own ``_authorize`` / ``authorize_project`` calls
    instead of trusting the registry, so the two must agree independently.

    Both ``def`` and ``async def`` are scanned: ``watch_events`` is deliberately
    synchronous (it returns an ``EventWatch`` lease instead of awaiting), so
    scanning only coroutines would silently drop its capability.  Helpers that
    perform no authorization of their own contribute nothing and are skipped.

    ``visible_project_ids`` is the third authorization entry point: it is the
    project-level visibility projection a catalog-scoped method is gated by, so
    it is scanned exactly like ``_authorize`` / ``authorize_project``.
    """
    tree = ast.parse((SERVICE_DIR / "access.py").read_text(encoding="utf-8"))
    constants = {
        node.targets[0].id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    mapping: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        found: set[str] = set()
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            if getattr(call.func, "attr", None) not in {
                "_authorize",
                "authorize_project",
                "visible_project_ids",
            }:
                continue
            for argument in call.args:
                if isinstance(argument, ast.Name) and argument.id in constants:
                    found.add(constants[argument.id])
        if found:
            mapping[node.name] = found
    return mapping


def _normalize(value: object) -> object:
    """Normalize a decoded DTO value for comparison against JSON defaults."""
    if isinstance(value, EventFilter):
        return {"kinds": sorted(value.kinds), "turn_ids": sorted(value.turn_ids)}
    if isinstance(value, (frozenset, set)):
        return sorted(value)
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    return value


def _decoded(method: str, params: dict[str, Any]) -> Any:
    return protocol.decode_params(method, params)


def _dispatch_service_methods() -> set[str]:
    """Service port methods invoked by ``transport/protocol.py``'s dispatch."""
    source = (ROOT / "src" / "synapse" / "runtime" / "transport" / "protocol.py").read_text(
        encoding="utf-8"
    )
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.AsyncFunctionDef) or node.name != "dispatch":
            continue
        for call in ast.walk(node):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "service"
            ):
                found.add(call.func.attr)
    return found


def _matches(annotation: str, value: object, *, schemas: dict[str, Any], path: str) -> None:
    """Assert one fixture value satisfies one declared field annotation."""
    text = annotation.strip()
    if "|" in text:
        parts = [part.strip() for part in text.split("|")]
        if value is None and "None" in parts:
            return
        failures: list[str] = []
        for part in parts:
            if part == "None":
                continue
            try:
                _matches(part, value, schemas=schemas, path=path)
                return
            except AssertionError as error:
                failures.append(str(error))
        raise AssertionError(f"{path}: {value!r} matches none of {parts}: {failures}")
    if text in {"Any", "JSONValue"}:
        assert isinstance(value, (type(None), bool, int, float, str, list, dict)), path
        return
    if text == "str":
        assert isinstance(value, str), f"{path}: expected str, got {value!r}"
        return
    if text == "int":
        assert isinstance(value, int) and not isinstance(value, bool), f"{path}: {value!r}"
        return
    if text == "float":
        assert isinstance(value, (int, float)) and not isinstance(value, bool), f"{path}: {value!r}"
        return
    if text == "bool":
        assert isinstance(value, bool), f"{path}: expected bool, got {value!r}"
        return
    sequence = re.fullmatch(r"(tuple|list|set|frozenset)\[(.*)\]", text, re.DOTALL)
    if sequence is not None:
        assert isinstance(value, list), f"{path}: expected a list, got {value!r}"
        inner = sequence.group(2)
        if inner.endswith(", ..."):
            inner = inner[: -len(", ...")]
        for index, item in enumerate(value):
            _matches(inner.strip(), item, schemas=schemas, path=f"{path}[{index}]")
        return
    mapping = re.fullmatch(r"(dict|Mapping)\[(.*)\]", text, re.DOTALL)
    if mapping is not None:
        assert isinstance(value, dict), f"{path}: expected an object, got {value!r}"
        for key, item in value.items():
            assert isinstance(key, str), f"{path}: non-string key"
            _matches(
                mapping.group(2).split(",", 1)[1].strip(),
                item,
                schemas=schemas,
                path=f"{path}.{key}",
            )
        return
    assert text in schemas, f"{path}: unknown annotation {annotation!r}"
    assert isinstance(value, dict), f"{path}: expected a nested object, got {value!r}"
    nested = schemas[text]["fields"]
    expected = {field["name"] for field in nested}
    assert set(value) == expected, f"{path}: {text} keys {sorted(value)} != {sorted(expected)}"
    for field in nested:
        _matches(
            field["python_type"],
            value[field["name"]],
            schemas=schemas,
            path=f"{path}.{field['name']}",
        )


def test_registry_covers_every_service_port_method() -> None:
    """The registry's service surface is exactly the service port's surface."""
    port_methods = _port_methods()
    # The frozen v1 baseline stays fully covered; growth is additive and named.
    assert V1_SERVICE_METHODS <= port_methods
    assert port_methods - V1_SERVICE_METHODS == set(ADDITIVE_WIRE_METHODS.values())
    assert set(_registry_service_methods()) == port_methods


def test_wire_method_table_is_the_v1_baseline_plus_named_additions() -> None:
    """The frozen 24 stay a subset; every addition is explicit and named."""
    declared = {method.method for method in contract_registry.WIRE_METHODS}
    assert V1_WIRE_METHODS <= declared
    assert declared - V1_WIRE_METHODS == set(ADDITIVE_WIRE_METHODS)
    assert protocol.METHODS == frozenset(declared)

    service = {m.method for m in contract_registry.WIRE_METHODS if m.method_class == "service"}
    transport = {m.method for m in contract_registry.WIRE_METHODS if m.method_class == "transport"}
    assert V1_WIRE_METHODS - {"runtime.protocol.negotiate", "runtime.events.unwatch"} <= service
    assert transport == {"runtime.protocol.negotiate", "runtime.events.unwatch"}
    assert service | transport == declared
    for method in contract_registry.WIRE_METHODS:
        if method.method_class == "service":
            assert method.capability in ALL_RUNTIME_CAPABILITIES
            assert method.request is not None and method.result is not None
        else:
            assert method.capability is None
            assert method.scope == "connection" or method.scope == "subscription"

    # Every additive wire method maps to the named port method it adds.
    for wire_method, port_method in ADDITIVE_WIRE_METHODS.items():
        entry = next(m for m in contract_registry.WIRE_METHODS if m.method == wire_method)
        assert entry.service_method == port_method

    # ``dispatch`` really invokes every declared service method except
    # ``watch_events``, which the websocket layer drives through a lease.
    dispatched = _dispatch_service_methods()
    assert dispatched == set(_registry_service_methods()) - {"watch_events"}


def test_capability_mapping_matches_the_acl_layer() -> None:
    """The declared capability per method equals the ACL layer's own check."""
    source = _access_source_capability_map()
    registry_methods = _registry_service_methods()
    assert set(source) == set(registry_methods)
    for name, method in registry_methods.items():
        assert source[name] == {method.capability}, name


def test_registry_capabilities_are_the_access_constant_set() -> None:
    """Authorization capabilities come from ``service/access.py``, not from here."""
    declared = {m.capability for m in contract_registry.WIRE_METHODS if m.capability}
    assert declared == set(ALL_RUNTIME_CAPABILITIES)
    assert contract_registry.AUTHORIZATION_CAPABILITIES == ALL_RUNTIME_CAPABILITIES
    # The frozen v1 baseline stays covered and the set only grew additively.
    assert V1_AUTHORIZATION_CAPABILITIES <= ALL_RUNTIME_CAPABILITIES
    assert ALL_RUNTIME_CAPABILITIES - V1_AUTHORIZATION_CAPABILITIES == set(
        ADDITIVE_AUTHORIZATION_CAPABILITIES
    )


def test_protocol_features_stay_separate_from_authorization_capabilities() -> None:
    """The 4 negotiated feature flags and the 18 ACL capabilities never mix."""
    assert protocol.CAPABILITIES == dict(contract_registry.PROTOCOL_FEATURES)
    assert len(contract_registry.PROTOCOL_FEATURES) == 4
    assert not set(contract_registry.PROTOCOL_FEATURES) & set(ALL_RUNTIME_CAPABILITIES)
    assert protocol.RUNTIME_WIRE_VERSION == contract_registry.WIRE_VERSION
    assert protocol.SUPPORTED_WIRE_VERSIONS == (contract_registry.WIRE_VERSION,)
    assert contract_registry.CONTRACT_VERSION == 1
    assert contract_registry.EVENT_VERSION == 1


def test_every_event_kind_declares_a_payload_schema() -> None:
    """All 25 kinds are declared, each with a resolvable payload schema."""
    kinds = {event.kind for event in contract_registry.EVENTS}
    assert kinds == {kind.value for kind in TurnEventKind}
    assert len(kinds) == 25
    schemas = contract_export.schema_names()
    for event in contract_registry.EVENTS:
        assert event.status in {"v1", "v1-ui-ignored"}
        if event.payload == "str":
            assert event.kind == "info"
        else:
            assert event.payload in schemas, event.kind
    legacy = {event.kind for event in contract_registry.EVENTS if event.legacy}
    assert legacy == {"tool_result"}
    transient = {event.kind for event in contract_registry.EVENTS if event.transient}
    assert transient == {"subagent_status_changed"}
    ignored = {event.kind for event in contract_registry.EVENTS if event.status == "v1-ui-ignored"}
    assert ignored == {"diff_updated", "plan_removed", "plan_updated"}


def test_event_payload_samples_match_the_declared_schema() -> None:
    """One committed sample per kind must satisfy the declared payload schema."""
    samples = json.loads((FIXTURES / "event_payload_samples.json").read_text(encoding="utf-8"))
    assert set(samples) == {event.kind for event in contract_registry.EVENTS}
    schemas = contract_export.manifest()["schemas"]
    assert isinstance(schemas, dict)
    for event in contract_registry.EVENTS:
        sample = samples[event.kind]
        if event.payload == "str":
            assert isinstance(sample, str), event.kind
            continue
        payload = schemas[event.payload]
        expected = {field["name"] for field in payload["fields"]}
        assert isinstance(sample, dict), event.kind
        assert set(sample) == expected, event.kind
        for field in payload["fields"]:
            _matches(
                field["python_type"],
                sample[field["name"]],
                schemas=schemas,
                path=f"{event.kind}.{field['name']}",
            )


def test_v1_key_fields_and_known_drift_fixes_are_frozen() -> None:
    """Freeze the fields the current web console types get wrong."""
    assert _field_names("RuntimeEvent") == [
        "sequence",
        "turn_sequence",
        "turn_id",
        "kind",
        "payload",
        "version",
    ]
    assert "timestamp" not in _field_names("RuntimeEvent")
    assert _field("RuntimeEvent", "payload")["ts_type"] == "JsonValue"
    assert "items" in _field_names("ToolBatchPayload")
    assert "accepted_at" not in _field_names("CommandReceipt")
    assert _field("CommandReceipt", "accepted")["required"] is True
    assert "opened_at" not in _field_names("OpenSessionResult")
    assert "description" not in _field_names("ApprovalActionView")
    assert "allowed_decisions" not in _field_names("ApprovalActionView")
    assert _field("SubmitTurnCommand", "attachments")["wire"] == "unsupported"
    assert _field("SubmitTurnCommand", "config_overrides")["wire"] == "restricted"
    assert _field("SubmitTurnCommand", "attachments")["ts_type"] == "never[]"
    assert _field("ApprovalDecision", "kind")["ts_type"] == "ApprovalDecisionKind"
    for kind in contract_registry.APPROVAL_DECISION_KINDS:
        ApprovalDecision(kind=kind)
    with pytest.raises(ValueError):
        ApprovalDecision(kind="not_a_decision")
    assert RuntimeEvent.__dataclass_fields__["turn_sequence"] is not None


def test_typescript_union_sequence_items_are_parenthesized() -> None:
    """``list[A | B]`` is an array of unions, never the ambiguous ``A | B[]``."""
    known = contract_export.schema_names()

    def render(annotation: str) -> str:
        return contract_export._ts_type(annotation, known=known)

    # A top-level union item is wrapped, in both the variadic and the single-arg
    # sequence forms.
    assert render("list[str | int]") == "(string | number)[]"
    assert render("tuple[str | None, ...]") == "(string | null)[]"
    assert render("set[str | int]") == "(string | number)[]"
    assert render("frozenset[EventCursor | None]") == "(EventCursor | null)[]"

    # Nested variants: the wrapper follows the item, so a nested sequence keeps
    # the array postfix outside its own parentheses.
    assert render("list[list[str | int]]") == "(string | number)[][]"
    assert render("tuple[list[str | int], ...]") == "(string | number)[][]"
    assert render("list[dict[str, list[str | int]]]") == "Record<string, (string | number)[]>[]"

    # A union nested *inside* the item needs no wrapper, and a top-level union is
    # still rendered unwrapped.
    assert render("list[str]") == "string[]"
    assert render("tuple[str, int]") == "[string, number]"
    assert render("list[dict[str, str | int]]") == "Record<string, string | number>[]"
    assert render("str | None") == "string | null"

    # No declared field's rendered type carries the ambiguous shape (the
    # hand-written recursive ``JsonValue`` alias legitimately unions an array).
    ambiguous = re.compile(r"\|\s*[A-Za-z_][A-Za-z0-9_]*\[\]")
    schemas = contract_export.manifest()["schemas"]
    assert isinstance(schemas, dict)
    offenders = [
        field["ts_type"]
        for schema in schemas.values()
        for field in schema["fields"]
        if ambiguous.search(str(field["ts_type"]))
    ]
    assert offenders == []


def test_schemas_never_expose_in_process_objects() -> None:
    """No runtime handle, implementation class, or security assembly is a schema."""
    names = contract_export.schema_names()
    assert not names & FORBIDDEN_SCHEMA_NAMES
    assert all(not name.startswith("_") for name in names)
    assert all("Local" not in name for name in names)
    document = contract_export.manifest()
    assert not set(document["schemas"]) & FORBIDDEN_SCHEMA_NAMES
    assert "WatchSpec" in names  # the transport-only watch shape is in, not out


def test_schema_inventory_is_complete_and_reachable() -> None:
    """Every method/event reference resolves, and every schema is reachable."""
    document = contract_export.manifest()
    schemas = document["schemas"]
    assert isinstance(schemas, dict)
    names = set(schemas)

    roots: set[str] = set()
    for group in ("methods", "transport_methods"):
        for method in document[group]:
            for key in ("request", "result"):
                if method.get(key):
                    roots.add(method[key])
                    assert method[key] in names, method["method"]
    for event in document["events"]:
        if event["payload"] != "str":
            roots.add(event["payload"])
            assert event["payload"] in names, event["kind"]
    for notification in document["transport_notifications"]:
        roots.add(notification["params"])
        assert notification["params"] in names, notification["method"]

    assert set(document["standalone_schemas"]) <= names
    # Declared standalone roots are roots as well: a nested type one of them
    # references must still resolve, or a schema could be declared, exported, and
    # never reachable from anywhere.
    reachable = set(roots) | set(document["standalone_schemas"])
    pending = list(reachable)
    while pending:
        current = pending.pop()
        for field in schemas[current]["fields"]:
            # A reflected field carries its Python annotation; a transport-only
            # field has no Python type at all and declares its reference in the
            # TypeScript type instead, so the walk has to read both.
            annotation = field.get("python_type") or field.get("ts_type")
            if not annotation:
                continue
            assert isinstance(annotation, str)
            for referenced in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", annotation):
                if referenced in names and referenced not in reachable:
                    reachable.add(referenced)
                    pending.append(referenced)
    orphans = names - reachable
    assert orphans == set()
    # every method, event, and notification actually needs a declared schema
    assert set(document["schemas"]) >= roots


def test_committed_artifacts_match_the_registry() -> None:
    """``--check`` fails on a missing or drifted artifact; here it must pass."""
    for relative in (
        contract_export.MANIFEST_RELATIVE_PATH,
        contract_export.TYPESCRIPT_RELATIVE_PATH,
    ):
        assert (ROOT / relative).is_file(), relative
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


#: A named type re-export: ``export type { A, B } from './contract.generated.ts';``.
_TS_NAMED_TYPE_REEXPORT = re.compile(
    r"export\s+type\s*\{([^}]*)\}\s*from\s*['\"]([^'\"]+)['\"]", re.DOTALL
)
#: A wildcard re-export: ``export * from '../runtime-client/types.ts';``.
_TS_STAR_REEXPORT = re.compile(r"export\s+\*\s+from\s*['\"]([^'\"]+)['\"]")
#: A hand-written declaration, which would duplicate a generated wire DTO.
_TS_LOCAL_INTERFACE = re.compile(r"^\s*export\s+interface\s+([A-Za-z0-9_]+)", re.MULTILINE)
_TS_LOCAL_TYPE_ALIAS = re.compile(r"^\s*export\s+type\s+([A-Za-z0-9_]+)\s*=", re.MULTILINE)


def _ts_local_type_declarations(module: Path) -> set[str]:
    """Type names ``module`` declares itself instead of re-exporting.

    A hand-written ``export interface`` / ``export type X =`` in a console
    module is a duplicate of a generated wire DTO, so it is exactly what the
    re-export gate forbids.
    """
    text = module.read_text(encoding="utf-8")
    return set(_TS_LOCAL_INTERFACE.findall(text)) | set(_TS_LOCAL_TYPE_ALIAS.findall(text))


def _ts_exported_type_names(module: Path, seen: frozenset[Path] = frozenset()) -> set[str]:
    """Resolve the type names ``module`` exports, following re-export chains.

    ``client/types.ts`` is a compatibility ``export *`` of
    ``runtime-client/types.ts``, which re-exports the generated contract by
    name; following both forms reads the frozen surface from the generated
    contract instead of from the declaration syntax the old scan matched.
    """
    if module in seen:
        return set()
    seen = seen | {module}
    exported = _ts_local_type_declarations(module)
    text = module.read_text(encoding="utf-8")
    for match in _TS_NAMED_TYPE_REEXPORT.finditer(text):
        exported.update(name.strip() for name in match.group(1).split(",") if name.strip())
    for match in _TS_STAR_REEXPORT.finditer(text):
        target = (module.parent / match.group(1)).resolve()
        exported |= _ts_exported_type_names(target, seen=seen)
    return exported


def _ts_reexport_targets(module: Path) -> set[Path]:
    """The relative modules ``module`` re-exports from, resolved to real paths."""
    text = module.read_text(encoding="utf-8")
    specifiers = [match.group(2) for match in _TS_NAMED_TYPE_REEXPORT.finditer(text)]
    specifiers += [match.group(1) for match in _TS_STAR_REEXPORT.finditer(text)]
    return {(module.parent / specifier).resolve() for specifier in specifiers}


def test_generated_typescript_covers_every_web_console_type() -> None:
    """Every type name the web console uses today resolves in the generated file.

    The console's own modules are re-export shims: ``client/types.ts`` is the
    legacy path that ``export *``s the shared core, and ``runtime-client/types.ts``
    re-exports the generated contract by name.  The gate resolves that chain and
    forbids a hand-written duplicate of a wire DTO, instead of scanning for
    ``export interface`` / ``export type`` bodies that no longer exist.
    """
    fixture = json.loads((FIXTURES / "web_console_types.json").read_text(encoding="utf-8"))
    frozen = set(fixture["types"])
    assert frozen
    sources = [ROOT / relative for relative in fixture["sources"]]

    # The frozen surface must come from the generated contract, never from a
    # hand-written copy: a duplicate DTO is exactly the drift this gate blocks.
    for source in sources:
        duplicated = _ts_local_type_declarations(source) & frozen
        assert not duplicated, (
            f"{source.relative_to(ROOT).as_posix()} hand-writes wire DTO(s) "
            f"{sorted(duplicated)}; re-export them from the generated contract"
        )

    # Resolve the re-export chain (`client/types.ts` -> `runtime-client/types.ts`
    # -> `contract.generated.ts`) rather than the declaration syntax.  The frozen
    # v1 surface stays a subset and every addition is named explicitly, so this
    # gate cannot be satisfied by silently rewriting the baseline fixture.
    current: set[str] = set()
    for source in sources:
        current |= _ts_exported_type_names(source)
    assert frozen <= current, "the frozen v1 web console type surface regressed"
    assert current - frozen == ADDITIVE_WEB_CONSOLE_TYPES, (
        "web console type names changed; name the addition instead of refreshing the "
        "v1 fixture"
    )

    # The legacy browser import path must keep resolving through the core module.
    legacy = ROOT / "web" / "src" / "client" / "types.ts"
    core = ROOT / "web" / "src" / "runtime-client" / "types.ts"
    assert core in _ts_reexport_targets(legacy), (
        "web/src/client/types.ts must re-export the shared core module"
    )

    generated = (ROOT / contract_export.TYPESCRIPT_RELATIVE_PATH).read_text(encoding="utf-8")
    exported = set(re.findall(r"export (?:interface|type|const) ([A-Za-z0-9_]+)", generated))
    # Every frozen name must exist in the generated artifact and in the core
    # re-export set, so the console surface can never point at a missing DTO.
    assert sorted(frozen - exported) == []
    assert sorted(frozen - _ts_exported_type_names(core)) == []
    assert "RuntimeEventPayloadMap" in generated
    assert "never[]" in generated
    # No UI-only field may be declared: a declared field renders as `name:` or
    # `name?:`.  The check is on the declaration, not on the bare word, because
    # the generated docs deliberately state that such a field is *absent* (the
    # manifest already carries that note).
    for absent_field in ("timestamp", "accepted_at", "opened_at"):
        assert re.search(rf"^\s*{absent_field}\??\s*:", generated, re.MULTILINE) is None
    assert ": any" not in generated


def test_generated_artifacts_are_deterministic_and_path_free() -> None:
    """Two renders are identical, and the output leaks no machine-local fact."""
    manifest_text = contract_export.render_manifest_json()
    assert manifest_text == contract_export.render_manifest_json()
    assert manifest_text == (ROOT / contract_export.MANIFEST_RELATIVE_PATH).read_text(
        encoding="utf-8"
    )
    assert "\r\n" not in manifest_text
    assert str(ROOT) not in manifest_text
    assert re.search(r"[A-Za-z]:[\\/]", manifest_text) is None
    assert "/home/" not in manifest_text and "/Users/" not in manifest_text
    assert "generated_at" not in manifest_text
    assert json.loads(manifest_text)["contract_version"] == 1

    typescript = contract_export.render_typescript()
    assert typescript == contract_export.render_typescript()
    assert typescript == (ROOT / contract_export.TYPESCRIPT_RELATIVE_PATH).read_text(
        encoding="utf-8"
    )
    assert "\r\n" not in typescript
    assert str(ROOT) not in typescript


def test_manifest_limits_track_the_contract_modules() -> None:
    """Bounded limits are read from the owning contract modules."""
    limits = contract_export.manifest()["limits"]
    assert limits["default_chunk_bytes"] == DEFAULT_CHUNK_BYTES == 65536
    assert limits["min_chunk_bytes"] == MIN_CHUNK_BYTES
    assert limits["max_chunk_bytes"] == MAX_CHUNK_BYTES
    assert limits["default_max_event_bytes"] == DEFAULT_MAX_EVENT_BYTES
    assert limits["max_event_bytes"] == MAX_EVENT_BYTES
    assert limits["min_event_bytes"] == MIN_EVENT_BYTES
    assert limits["default_scan_limit"] == DEFAULT_SCAN_LIMIT
    assert limits["history_limit_default"] == HISTORY_LIMIT_DEFAULT
    assert limits["default_session_list_limit"] == SESSION_LIST_LIMIT_DEFAULT


def test_declared_wire_defaults_match_the_decoder() -> None:
    """Each declared wire default is what ``decode_params`` really applies."""
    methods = {method.method: method for method in contract_registry.WIRE_METHODS}
    probes: tuple[tuple[str, dict[str, Any]], ...] = (
        ("runtime.session.list", {"project_id": "p1"}),
        ("runtime.session.history", {"session": _WIRE_REF}),
        ("runtime.events.read", {"session": _WIRE_REF}),
        ("runtime.events.watch", {"session": _WIRE_REF}),
        ("runtime.artifacts.list", {"session": _WIRE_REF}),
        ("runtime.artifacts.read", {"ref": {"session": _WIRE_REF, "path": "a.txt"}}),
        ("runtime.turn.cancel", {"session": _WIRE_REF, "expected_turn_id": "t1"}),
        ("runtime.session.close", {"session": _WIRE_REF}),
    )
    for method, params in probes:
        declared = dict(methods[method].wire_defaults)
        assert declared, method
        decoded = _decoded(method, params)
        for field, expected in declared.items():
            attribute = field if hasattr(decoded, field) else "event_filter"
            assert _normalize(getattr(decoded, attribute)) == expected, (method, field)


def test_submit_turn_wire_rules_match_the_declaration() -> None:
    """Attachments are unsupported; config_overrides is a restricted JSON object."""
    base = {"session": _WIRE_REF, "text": "hello"}
    assert _decoded("runtime.turn.submit", {**base, "attachments": []}).attachments == ()
    assert dict(_decoded("runtime.turn.submit", base).config_overrides) == {}
    with pytest.raises(protocol.ProtocolError):
        _decoded("runtime.turn.submit", {**base, "attachments": [{"path": "a.txt"}]})
    with pytest.raises(protocol.ProtocolError):
        _decoded("runtime.turn.submit", {**base, "attachments": "a.txt"})
    with pytest.raises(protocol.ProtocolError):
        _decoded("runtime.turn.submit", {**base, "config_overrides": []})
    command = _decoded("runtime.turn.submit", {**base, "config_overrides": {"model": "fast"}})
    assert dict(command.config_overrides) == {"model": "fast"}


def test_manifest_records_in_process_special_cases() -> None:
    """The in-process-only differences are declared, not implied."""
    document = contract_export.manifest()
    methods = {method["method"]: method for method in document["methods"]}
    transport = {method["method"]: method for method in document["transport_methods"]}
    assert "lease" in methods["runtime.events.watch"]["in_process"]
    assert methods["runtime.turn.submit"]["in_process"]
    assert methods["runtime.session.goal"]["result_nullable"] is True
    assert "capability" not in transport["runtime.protocol.negotiate"]
    for name in (
        "runtime.config.get",
        "runtime.events.watch",
        "runtime.protocol.negotiate",
        "runtime.session.history",
        "runtime.session.list",
        "runtime.session.reconcile",
        "runtime.turn.submit",
    ):
        entry = methods.get(name) or transport[name]
        assert entry["in_process"], name


def test_new_contract_modules_import_only_the_contract_layer() -> None:
    """The registry is importable without transport, settings, or implementations."""
    forbidden = (
        "synapse.runtime.transport",
        "synapse.runtime.service.local",
        "synapse.runtime.service.routing",
        "synapse.runtime.service.history_store",
        "synapse.runtime.sessions.runtime",
        "synapse.runtime.sessions.manager",
        "synapse.runtime.streaming",
        "synapse.runtime.tool_ignore",
        "synapse.ui",
        "synapse.cli",
        "synapse.acp",
        "synapse.settings",
        "langchain",
        "langgraph",
        "deepagents",
    )
    for name in ("contract_registry.py", "contract_export.py"):
        source = (SERVICE_DIR / name).read_text(encoding="utf-8")
        modules: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
        for module in sorted(modules):
            for prefix in forbidden:
                assert not (module == prefix or module.startswith(prefix + ".")), (name, module)
        # A generated artifact must never be read back at runtime.
        assert "read_text" not in source
        assert "open(" not in source
    registry_source = (SERVICE_DIR / "contract_registry.py").read_text(encoding="utf-8")
    assert "contract_manifest" not in registry_source
    assert "contract.generated" not in registry_source


def test_shared_fixture_is_a_real_projection_of_the_declared_schemas() -> None:
    """The cross-language fixture is re-derived, never compared against itself.

    ``tests/fixtures/runtime_contract/fixtures.json`` is consumed by the web
    TypeScript gate too, so it has to stay language-neutral JSON.  Every entry is
    rebuilt here from the real service projection (``_to_runtime_event`` for the
    events, ``_project_session`` for the session read) and then pushed through the
    real wire projection (``protocol.project_result``), so a fixture that merely
    agreed with a hand-written copy of itself could not pass.  The declared
    manifest schema is checked on top of that.
    """
    import dataclasses
    from datetime import datetime

    from synapse.runtime.service import event_types
    from synapse.runtime.service.events import project_payload
    from synapse.runtime.service.local import _project_session, _to_runtime_event
    from synapse.runtime.sessions.events import SessionEventEnvelope
    from synapse.runtime.sessions.runtime import SessionSnapshot, SessionStatus, SessionUsage

    fixture = json.loads(SHARED_FIXTURE.read_text(encoding="utf-8"))
    document = contract_export.manifest()
    schemas = document["schemas"]
    assert isinstance(schemas, dict)
    declared = {event["kind"]: event["payload"] for event in document["events"]}
    assert fixture["contract_version"] == contract_registry.CONTRACT_VERSION
    assert fixture["wire_version"] == contract_registry.WIRE_VERSION

    read = fixture["session_read"]
    thread_id = read["request"]["session"]["thread_id"]
    for sample in fixture["events"]:
        kind = sample["kind"]
        wire = sample["wire"]
        assert wire["kind"] == kind
        assert set(wire) == {
            "sequence",
            "turn_sequence",
            "turn_id",
            "kind",
            "payload",
            "version",
        }
        producer = sample["producer_payload"]
        if sample["payload_schema"] is None:
            # A future kind: no declared schema and no known enum member, and the
            # payload must survive the projection untouched so a client can keep
            # the fields it does not understand.
            assert kind not in declared
            assert kind not in {member.value for member in TurnEventKind}
            assert project_payload(producer) == wire["payload"]
            continue
        assert sample["payload_schema"] == declared[kind]
        payload = (
            producer
            if sample["payload_schema"] == "str"
            else getattr(event_types, sample["payload_schema"])(**producer)
        )
        envelope = SessionEventEnvelope(
            thread_id=thread_id,
            sequence=wire["sequence"],
            turn_id=wire["turn_id"],
            event=TurnEvent(
                version=wire["version"],
                thread_id=thread_id,
                turn_id=wire["turn_id"],
                sequence=wire["turn_sequence"],
                kind=TurnEventKind(kind),
                payload=payload,
            ),
        )
        assert protocol.project_result(_to_runtime_event(envelope)) == wire, kind
        _matches(sample["payload_schema"], wire["payload"], schemas=schemas, path=kind)

    # The request half of the session read is decoded by the real wire decoder.
    decoded = protocol.decode_params(read["method"], read["request"])
    assert dataclasses.asdict(decoded.session) == read["request"]["session"]
    assert decoded.session == REF
    snapshot = read["snapshot"]
    view = _project_session(
        SessionSnapshot(
            project_id=snapshot["project_id"],
            thread_id=snapshot["thread_id"],
            status=SessionStatus(snapshot["status"]),
            active_turn_id=snapshot["active_turn_id"],
            latest_sequence=snapshot["latest_sequence"],
            usage=SessionUsage(
                input_tokens=snapshot["input_tokens"],
                output_tokens=snapshot["output_tokens"],
                cache_tokens=snapshot["cache_tokens"],
            ),
            last_error=snapshot["last_error"],
            last_activity_at=datetime.fromisoformat(snapshot["last_activity_at"]),
            active_model=snapshot["active_model"],
            model=snapshot["model"],
        )
    )
    assert read["result_schema"] == "SessionView"
    assert protocol.project_result(view) == read["result"]
    _matches(read["result_schema"], read["result"], schemas=schemas, path="session_read.result")
