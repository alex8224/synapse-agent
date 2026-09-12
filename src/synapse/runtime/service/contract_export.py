"""Render the runtime contract manifest and TypeScript types from the registry.

Everything here is derived: the registry is the authority and this module only
reflects it (dataclass fields, annotations, defaults) into two deterministic
artifacts.  Nothing in the contract layer reads these artifacts back at runtime.

The output must be byte-stable across machines and runs: no local paths, no
timestamps, no memory addresses, no iteration-order dependent output.  Both
renderers raise :class:`ContractExportError` instead of emitting a dangling
schema reference or an untyped ``any``.
"""

from __future__ import annotations

import dataclasses
import json
import re
from typing import Any, Final

from synapse.runtime.service import contract_registry as registry

__all__ = [
    "ContractExportError",
    "MANIFEST_RELATIVE_PATH",
    "TYPESCRIPT_RELATIVE_PATH",
    "manifest",
    "render_manifest_json",
    "render_typescript",
    "schema_names",
]

#: Repository-relative locations of the two generated artifacts.
MANIFEST_RELATIVE_PATH: Final = "src/synapse/runtime/service/contract_manifest.json"
TYPESCRIPT_RELATIVE_PATH: Final = "web/src/runtime-client/contract.generated.ts"

_TS_ATOMS: Final[dict[str, str]] = {
    "Any": "JsonValue",
    "JSONValue": "JsonValue",
    "None": "null",
    "bool": "boolean",
    "float": "number",
    "int": "number",
    "str": "string",
}

_TS_ALLOWED_TRANSPORT_TYPES: Final = re.compile(r"^[A-Za-z0-9_'<>.,|?\[\] {}]+$")


class ContractExportError(ValueError):
    """A registry declaration cannot be rendered without guessing."""


def _compact(
    entry: dict[str, object], *, omit_empty: tuple[str, ...] = ()
) -> dict[str, object]:
    """Drop absent metadata so the snapshot stays readable without losing facts."""
    return {
        key: value
        for key, value in entry.items()
        if not (key in omit_empty and (value is None or value == "" or value == [] or value == {}))
    }


def schema_names() -> frozenset[str]:
    """Return every declared schema name."""
    return frozenset(schema.name for schema in registry.SCHEMAS)


def _split_top_level(text: str, separator: str) -> list[str]:
    """Split *text* on *separator* outside brackets/quotes."""
    parts: list[str] = []
    depth = 0
    quote = ""
    current: list[str] = []
    for char in text:
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            continue
        if char in "'\"":
            quote = char
            current.append(char)
            continue
        if char in "[(":
            depth += 1
        elif char in "])":
            depth -= 1
        if char == separator and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return parts


def _ts_atom(atom: str, *, known: frozenset[str]) -> str:
    text = atom.strip()
    if not text:
        raise ContractExportError("empty type annotation")
    mapped = _TS_ATOMS.get(text)
    if mapped is not None:
        return mapped
    if text in known:
        return text
    raise ContractExportError(f"no TypeScript mapping for annotation {text!r}")


def _ts_sequence_item(annotation: str, *, known: frozenset[str]) -> str:
    """Render one sequence item, parenthesizing a union so the array stays an array.

    ``list[A | B]`` is an array of unions, so it renders as ``(A | B)[]``;
    without the parentheses ``A | B[]`` would read as "an ``A``, or an array of
    ``B``".  Only a *top-level* union of the annotation is wrapped, so a union
    nested inside the item (``list[dict[str, A | B]]``) keeps its bare form.
    """
    item = _ts_type(annotation, known=known)
    if len(_split_top_level(annotation, "|")) > 1:
        return f"({item})"
    return item


def _ts_type(annotation: str, *, known: frozenset[str]) -> str:
    """Map one Python annotation onto a TypeScript type, never onto ``any``."""
    text = annotation.strip()
    if not text:
        raise ContractExportError("empty type annotation")
    union = _split_top_level(text, "|")
    if len(union) > 1:
        return " | ".join(_ts_type(part, known=known) for part in union)
    sequence = re.fullmatch(r"(tuple|list|set|frozenset)\[(.*)\]", text, re.DOTALL)
    if sequence is not None:
        args = _split_top_level(sequence.group(2), ",")
        if len(args) == 2 and args[1].strip() == "...":
            return f"{_ts_sequence_item(args[0], known=known)}[]"
        if len(args) == 1:
            return f"{_ts_sequence_item(args[0], known=known)}[]"
        return "[" + ", ".join(_ts_type(arg, known=known) for arg in args) + "]"
    mapping = re.fullmatch(r"(dict|Mapping)\[(.*)\]", text, re.DOTALL)
    if mapping is not None:
        args = _split_top_level(mapping.group(2), ",")
        if len(args) != 2 or args[0].strip() != "str":
            raise ContractExportError(f"unsupported mapping annotation {text!r}")
        return f"Record<string, {_ts_type(args[1], known=known)}>"
    return _ts_atom(text, known=known)


def _transport_ts_type(declared: str, *, known: frozenset[str]) -> str:
    """Validate a hand-declared transport type expression."""
    text = declared.strip()
    if not text or _TS_ALLOWED_TRANSPORT_TYPES.fullmatch(text) is None:
        raise ContractExportError(f"transport type is not a plain TypeScript type: {declared!r}")
    for name in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text):
        if name in {"string", "number", "boolean", "JsonValue", "Record", "null", "T"}:
            continue
        if name in known:
            continue
        raise ContractExportError(f"transport type {declared!r} references unknown name {name!r}")
    return text


def _is_nullable(annotation: str) -> bool:
    if annotation.strip() == "None":
        return True
    return any(part.strip() == "None" for part in _split_top_level(annotation, "|"))


def _default_entry(field: dataclasses.Field[Any]) -> tuple[str, object]:
    """Return ``(default_kind, value)`` for one reflected field."""
    if field.default is not dataclasses.MISSING:
        value = field.default
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError):
            # A non-JSON default instance: record its type name, never a repr.
            return "opaque", type(value).__name__
        return "value", value
    if field.default_factory is not dataclasses.MISSING:
        factory = field.default_factory
        name = getattr(factory, "__name__", None)
        if not isinstance(name, str) or not name:
            name = type(factory).__name__
        return "factory", name
    return "none", None


def _reflected_field(
    schema: registry.SchemaDeclaration,
    field: dataclasses.Field[Any],
    *,
    known: frozenset[str],
) -> dict[str, object]:
    metadata = dict(schema.field_metadata).get(field.name, registry.FieldMetadata())
    annotation = field.type if isinstance(field.type, str) else str(field.type)
    default_kind, default = _default_entry(field)
    optional = schema.role == "request" and default_kind != "none"
    ts_type = metadata.ts_type or _ts_type(annotation, known=known)
    if metadata.wire == "unsupported":
        if not ts_type.endswith("[]"):
            raise ContractExportError(
                f"{schema.name}.{field.name}: an unsupported field must be a sequence"
            )
        ts_type = "never[]"
    entry: dict[str, object] = {
        "name": field.name,
        "python_type": annotation,
        "ts_type": ts_type,
        "required": not optional,
        "optional": optional,
        "nullable": _is_nullable(annotation),
        "wire": metadata.wire,
        "default": default,
        "default_kind": default_kind,
        "note": metadata.note,
    }
    if default_kind == "none":
        entry.pop("default")
        entry.pop("default_kind")
    return _compact(entry, omit_empty=("note",))


def _declared_field(
    field: registry.FieldDeclaration, *, known: frozenset[str]
) -> dict[str, object]:
    entry: dict[str, object] = {
        "name": field.name,
        "python_type": None,
        "ts_type": _transport_ts_type(field.type, known=known),
        "required": field.required,
        "optional": not field.required,
        "nullable": False,
        "wire": "json",
        "note": field.note,
    }
    return _compact(entry, omit_empty=("note",))


def _schema_entry(
    schema: registry.SchemaDeclaration, *, known: frozenset[str]
) -> dict[str, object]:
    if schema.origin == "python":
        if schema.dto is None or not dataclasses.is_dataclass(schema.dto):
            raise ContractExportError(f"{schema.name}: origin 'python' requires a dataclass")
        fields = [
            _reflected_field(schema, field, known=known) for field in dataclasses.fields(schema.dto)
        ]
    elif schema.origin == "transport":
        fields = [_declared_field(field, known=known) for field in schema.fields]
    else:
        raise ContractExportError(f"{schema.name}: unknown origin {schema.origin!r}")
    return _compact(
        {
            "origin": schema.origin,
            "role": schema.role,
            "type_params": list(schema.type_params),
            "fields": fields,
            "notes": list(schema.notes),
        },
        omit_empty=("type_params", "notes"),
    )


def _method_entry(method: registry.WireMethod) -> dict[str, object]:
    return _compact(
        {
            "method": method.method,
            "class": method.method_class,
            "service_method": method.service_method,
            "request": method.request,
            "result": method.result,
            "result_nullable": method.result_nullable,
            # A connection-state method has no authorization capability at all, so
            # the key is absent rather than null: ``null`` would read as "checked
            # and unauthorized" instead of "never checked".
            "capability": method.capability,
            "scope": method.scope,
            "scope_location": method.scope_location,
            "params_alias": method.params_alias,
            "result_alias": method.result_alias,
            "wire_defaults": dict(method.wire_defaults),
            "in_process": method.in_process,
            "notes": list(method.notes),
        },
        omit_empty=(
            "service_method",
            "capability",
            "result_alias",
            "params_alias",
            "in_process",
            "notes",
        ),
    )


def _event_entry(event: registry.EventDeclaration) -> dict[str, object]:
    return _compact({
        "kind": event.kind,
        "payload": event.payload,
        "status": event.status,
        "legacy": event.legacy,
        "transient": event.transient,
        "notes": list(event.notes),
    }, omit_empty=("notes",))


def _validate() -> frozenset[str]:
    """Fail loudly on a dangling reference instead of exporting a broken contract."""
    known = schema_names()
    names = [schema.name for schema in registry.SCHEMAS]
    if len(names) != len(set(names)):
        raise ContractExportError("duplicate schema name")
    method_names = [method.method for method in registry.WIRE_METHODS]
    if len(method_names) != len(set(method_names)):
        raise ContractExportError("duplicate wire method")
    for method in registry.WIRE_METHODS:
        for role in ("request", "result"):
            name = getattr(method, role)
            if name is not None and name not in known:
                raise ContractExportError(f"{method.method}: undeclared {role} schema {name!r}")
        if method.method_class == "service" and method.service_method is None:
            raise ContractExportError(f"{method.method}: a service method needs a port method")
        if method.method_class == "transport" and method.capability is not None:
            raise ContractExportError(f"{method.method}: a transport method has no capability")
        if method.method_class == "service" and method.capability is None:
            raise ContractExportError(f"{method.method}: a service method needs a capability")
        if method.scope in ("connection", "catalog"):
            # A connection-state method and a catalog-scoped method have no
            # per-request routing position: the former is bound to the
            # connection, the latter lets the server decide visibility.
            if method.scope_location is not None:
                raise ContractExportError(f"{method.method}: this scope has no scope path")
        elif method.scope_location is None:
            raise ContractExportError(f"{method.method}: missing scope location")
    kinds = [event.kind for event in registry.EVENTS]
    if len(kinds) != len(set(kinds)):
        raise ContractExportError("duplicate event kind")
    for event in registry.EVENTS:
        if event.payload != "str" and event.payload not in known:
            raise ContractExportError(f"{event.kind}: undeclared payload schema {event.payload!r}")
    for notification in registry.TRANSPORT_NOTIFICATIONS:
        if notification.params not in known:
            raise ContractExportError(
                f"{notification.method}: undeclared params schema {notification.params!r}"
            )
    for alias, target in registry.TYPE_ALIASES:
        if alias in known:
            raise ContractExportError(f"type alias {alias!r} shadows a schema")
        if target not in known:
            _transport_ts_type(target, known=known)
    for name in registry.STANDALONE_SCHEMAS:
        if name not in known:
            raise ContractExportError(f"standalone schema {name!r} is not declared")
    return known


def manifest() -> dict[str, object]:
    """Build the deterministic manifest document."""
    known = _validate()
    service_methods = sorted(
        (method for method in registry.WIRE_METHODS if method.method_class == "service"),
        key=lambda method: method.method,
    )
    transport_methods = sorted(
        (method for method in registry.WIRE_METHODS if method.method_class == "transport"),
        key=lambda method: method.method,
    )
    return {
        "contract_version": registry.CONTRACT_VERSION,
        "wire_version": registry.WIRE_VERSION,
        "event_version": registry.EVENT_VERSION,
        "methods": [_method_entry(method) for method in service_methods],
        "transport_methods": [_method_entry(method) for method in transport_methods],
        "transport_notifications": [
            _compact(
                {
                    "method": notification.method,
                    "params": notification.params,
                    "notes": list(notification.notes),
                },
                omit_empty=("notes",),
            )
            for notification in sorted(
                registry.TRANSPORT_NOTIFICATIONS, key=lambda item: item.method
            )
        ],
        "events": [
            _event_entry(event)
            for event in sorted(registry.EVENTS, key=lambda item: item.kind)
        ],
        "schemas": {
            schema.name: _schema_entry(schema, known=known)
            for schema in sorted(registry.SCHEMAS, key=lambda item: item.name)
        },
        "protocol_features": dict(sorted(registry.PROTOCOL_FEATURES.items())),
        "authorization_capabilities": sorted(registry.AUTHORIZATION_CAPABILITIES),
        "approval_decision_kinds": list(registry.APPROVAL_DECISION_KINDS),
        "standalone_schemas": sorted(registry.STANDALONE_SCHEMAS),
        "type_aliases": dict(sorted(registry.TYPE_ALIASES)),
        "limits": dict(sorted(registry.LIMITS.items())),
    }


def render_manifest_json() -> str:
    """Render the manifest as canonical JSON text (LF terminated)."""
    return json.dumps(manifest(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


_HEADER: Final = '''/**
 * GENERATED FILE - DO NOT EDIT.
 *
 * Authoritative source: src/synapse/runtime/service/contract_registry.py
 * Regenerate: uv run --no-sync python scripts/export_contract_manifest.py
 * Verify:     uv run --no-sync python scripts/export_contract_manifest.py --check
 *
 * contract_version={contract_version} wire_version="{wire_version}" \
event_version={event_version}
 *
 * Field comments record the *Python DTO* default, which is not a statement about
 * the wire rules: the wire defaults are declared per method in the manifest.
 * Nothing here carries UI-only data (consumer lists, timestamps, presentation
 * flags); unknown event kinds stay compatible through the fallback payload type.
 */'''


def _doc(lines: tuple[str, ...] | list[str], *, indent: str = "") -> list[str]:
    if not lines:
        return []
    block = [f"{indent}/**"]
    for line in lines:
        text = line.rstrip()
        block.append(f"{indent} * {text}".rstrip())
    block.append(f"{indent} */")
    return block


def _field_doc(field: dict[str, object]) -> tuple[str, ...]:
    header: list[str] = []
    if field["wire"] != "json":
        header.append(f"wire={field['wire']}")
    if field.get("default_kind") is not None:
        rendered = json.dumps(field["default"], ensure_ascii=False)
        header.append(f"python_default_kind={field['default_kind']} python_default={rendered}")
    lines: list[str] = []
    if header:
        lines.append(" ".join(header))
    note = field.get("note")
    if isinstance(note, str) and note:
        lines.append(note)
    return tuple(lines)


def _interface(
    name: str,
    entry: dict[str, object],
    *,
    type_params: str = "",
    extends: str = "",
) -> list[str]:
    notes = entry.get("notes")
    lines = _doc(notes if isinstance(notes, list) else [])
    suffix = f" extends {extends}" if extends else ""
    lines.append(f"export interface {name}{type_params}{suffix} {{")
    fields = entry.get("fields")
    if isinstance(fields, list):
        for field in fields:
            if not isinstance(field, dict):
                continue
            lines.extend(_doc(_field_doc(field), indent="  "))
            marker = "?" if field["optional"] else ""
            lines.append(f"  {field['name']}{marker}: {field['ts_type']};")
    lines.append("}")
    return lines


def _string_array(name: str, values: list[str]) -> list[str]:
    lines = [f"export const {name} = ["]
    lines.extend(f'  "{value}",' for value in values)
    lines.append("] as const;")
    return lines


def render_typescript() -> str:
    """Render the generated TypeScript contract module."""
    known = _validate()
    entries = {
        schema.name: _schema_entry(schema, known=known)
        for schema in sorted(registry.SCHEMAS, key=lambda item: item.name)
    }
    service_methods = sorted(
        (method for method in registry.WIRE_METHODS if method.method_class == "service"),
        key=lambda method: method.method,
    )
    all_methods = sorted(registry.WIRE_METHODS, key=lambda method: method.method)

    lines: list[str] = _HEADER.format(
        contract_version=registry.CONTRACT_VERSION,
        wire_version=registry.WIRE_VERSION,
        event_version=registry.EVENT_VERSION,
    ).splitlines()
    lines.append("")
    lines.append(f"export const CONTRACT_VERSION = {registry.CONTRACT_VERSION};")
    lines.append(f'export const WIRE_VERSION = "{registry.WIRE_VERSION}";')
    lines.append(f"export const EVENT_VERSION = {registry.EVENT_VERSION};")
    lines.append("")
    lines.append("export type JsonValue =")
    lines.append("  | null")
    lines.append("  | boolean")
    lines.append("  | number")
    lines.append("  | string")
    lines.append("  | JsonValue[]")
    lines.append("  | { [key: string]: JsonValue };")
    lines.append("")
    lines.extend(
        _doc(
            (
                f"The {len(all_methods)} wire methods: {len(service_methods)} service methods",
                "plus the connection-state methods runtime.protocol.negotiate and",
                "runtime.events.unwatch.",
            )
        )
    )
    lines.extend(_string_array("WIRE_METHODS", [method.method for method in all_methods]))
    lines.append("export type WireMethod = (typeof WIRE_METHODS)[number];")
    lines.append("")
    lines.extend(
        _doc(
            (
                "Protocol feature flags returned by runtime.protocol.negotiate.",
                "They are transport features only and never take part in ACL checks.",
            )
        )
    )
    lines.append("export const PROTOCOL_FEATURES = {")
    for feature, enabled in sorted(registry.PROTOCOL_FEATURES.items()):
        lines.append(f"  {feature}: {json.dumps(enabled)},")
    lines.append("} as const;")
    lines.append("export type ProtocolFeature = keyof typeof PROTOCOL_FEATURES;")
    lines.append("")
    lines.append(
        f"/** Authorization capabilities enforced by the ACL layer "
        f"({len(registry.AUTHORIZATION_CAPABILITIES)}). */"
    )
    lines.extend(
        _string_array(
            "AUTHORIZATION_CAPABILITIES", sorted(registry.AUTHORIZATION_CAPABILITIES)
        )
    )
    lines.append("export type AuthorizationCapability =")
    lines.append("  (typeof AUTHORIZATION_CAPABILITIES)[number];")
    lines.append("")
    lines.extend(_doc(("HITL decision kinds accepted by runtime.turn.approval.resume.",)))
    lines.extend(_string_array("APPROVAL_DECISION_KINDS", list(registry.APPROVAL_DECISION_KINDS)))
    lines.append("export type ApprovalDecisionKind = (typeof APPROVAL_DECISION_KINDS)[number];")
    lines.append("")
    lines.extend(_doc(("Server-to-client notification methods pushed outside a response.",)))
    lines.extend(
        _string_array(
            "TRANSPORT_NOTIFICATIONS",
            sorted(notification.method for notification in registry.TRANSPORT_NOTIFICATIONS),
        )
    )
    lines.append("")
    lines.extend(_doc(("Authorization capability per service method.",)))
    lines.append("export const WIRE_METHOD_CAPABILITIES: Partial<")
    lines.append("  Record<WireMethod, AuthorizationCapability>")
    lines.append("> = {")
    for method in service_methods:
        lines.append(f'  "{method.method}": "{method.capability}",')
    lines.append("};")
    lines.append("")
    lines.append("// --- DTOs, event payloads, and transport-only shapes ----------------------")
    lines.append("")
    for name in sorted(entries):
        entry = entries[name]
        type_params = entry.get("type_params")
        rendered_params = (
            "<" + ", ".join(type_params) + ">"
            if isinstance(type_params, list) and type_params
            else ""
        )
        lines.extend(_interface(name, entry, type_params=rendered_params))
        lines.append("")
    lines.append("// --- event kind -> payload union ------------------------------------------")
    lines.append("")
    lines.extend(
        _doc(
            (
                "Per-kind payload map.  An unknown kind falls back to JsonValue so a new",
                "server-side kind never breaks an older client.",
            )
        )
    )
    lines.append("export interface RuntimeEventPayloadMap {")
    for event in sorted(registry.EVENTS, key=lambda item: item.kind):
        payload = "string" if event.payload == "str" else event.payload
        lines.append(f"  {event.kind}: {payload};")
    lines.append("}")
    lines.append("export type RuntimeEventKind = keyof RuntimeEventPayloadMap;")
    lines.append("export type RuntimeEventPayloadUnion = RuntimeEventPayloadMap[RuntimeEventKind];")
    lines.append("")
    lines.extend(
        _string_array("RUNTIME_EVENT_KINDS", sorted(event.kind for event in registry.EVENTS))
    )
    lines.append("")
    lines.extend(
        _doc(
            (
                "Kinds frozen in v1 that the TUI and the web console deliberately do not",
                "render (ACP consumes them).",
            )
        )
    )
    lines.extend(
        _string_array(
            "RUNTIME_EVENT_UI_IGNORED_KINDS",
            sorted(event.kind for event in registry.EVENTS if event.status == "v1-ui-ignored"),
        )
    )
    lines.append("")
    lines.extend(
        _doc(
            (
                "A runtime event whose payload is narrowed by kind.  Use RuntimeEvent when",
                "the kind is unknown or not yet modelled.",
            )
        )
    )
    lines.append("export interface TypedRuntimeEvent<K extends RuntimeEventKind> {")
    lines.append("  sequence: number;")
    lines.append("  turn_sequence: number;")
    lines.append("  turn_id: string;")
    lines.append("  kind: K;")
    lines.append("  payload: RuntimeEventPayloadMap[K];")
    lines.append("  version: number;")
    lines.append("}")
    lines.append("")
    lines.append("// --- method Params / Result aliases ---------------------------------------")
    lines.append("")
    aliases: dict[str, str] = {}
    for method in all_methods:
        if method.params_alias and method.request:
            aliases[method.params_alias] = method.request
        if method.result_alias and method.result:
            aliases[method.result_alias] = method.result
    for alias, target in registry.TYPE_ALIASES:
        aliases[alias] = target
    for alias in sorted(aliases):
        lines.append(f"export type {alias} = {aliases[alias]};")
    lines.append("")
    return "\n".join(lines)
