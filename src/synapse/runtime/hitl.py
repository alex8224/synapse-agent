"""Human-in-the-loop interrupt detection and resume helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PendingAction:
    """One tool call waiting for human decision."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    allowed_decisions: list[str] = field(default_factory=lambda: ["approve", "reject"])


@dataclass
class PendingInterrupt:
    """Graph is paused for HITL."""

    actions: list[PendingAction] = field(default_factory=list)
    raw: Any = None


def _as_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        try:
            data = obj.model_dump()
            if isinstance(data, dict):
                return data
        except Exception:  # noqa: BLE001
            pass
    out: dict[str, Any] = {}
    for key in (
        "name",
        "args",
        "description",
        "action_request",
        "review_config",
        "value",
        "action",
    ):
        if hasattr(obj, key):
            out[key] = getattr(obj, key)
    return out


def _parse_action_request(item: Any) -> PendingAction | None:
    data = _as_dict(item)
    # Nested shapes: {action_request: {name, args, description}, review_config: {...}}
    ar = data.get("action_request") or data.get("action") or data
    ar = _as_dict(ar) if not isinstance(ar, dict) else ar
    name = str(ar.get("name") or data.get("name") or "").strip()
    if not name:
        return None
    args = ar.get("args") if isinstance(ar.get("args"), dict) else {}
    desc = str(ar.get("description") or data.get("description") or "")
    rc = data.get("review_config") or {}
    rc = _as_dict(rc) if not isinstance(rc, dict) else rc
    allowed = list(rc.get("allowed_decisions") or ["approve", "reject"])
    return PendingAction(name=name, args=args or {}, description=desc, allowed_decisions=allowed)


def extract_pending_interrupt(agent: Any, config: dict[str, Any]) -> PendingInterrupt | None:
    """Return pending HITL interrupt for the thread, if any."""
    if agent is None:
        return None
    get_state = getattr(agent, "get_state", None)
    if not callable(get_state):
        return None
    try:
        state = get_state(config)
    except Exception:  # noqa: BLE001
        return None

    actions: list[PendingAction] = []
    raw_items: list[Any] = []

    # Preferred: state.interrupts (LangGraph >= 0.2)
    interrupts = getattr(state, "interrupts", None) or ()
    for item in interrupts:
        raw_items.append(item)
        value = getattr(item, "value", item)
        # HITLRequest: {action_requests: [...], review_configs: [...]}
        vdict = _as_dict(value)
        reqs = vdict.get("action_requests") or vdict.get("actionRequests")
        if isinstance(reqs, list) and reqs:
            reviews = vdict.get("review_configs") or vdict.get("reviewConfigs") or []
            for i, req in enumerate(reqs):
                packed = {"action_request": req}
                if i < len(reviews):
                    packed["review_config"] = reviews[i]
                act = _parse_action_request(packed)
                if act:
                    actions.append(act)
            continue
        act = _parse_action_request(value)
        if act:
            actions.append(act)

    # Fallback: tasks with interrupts
    if not actions:
        tasks = getattr(state, "tasks", None) or ()
        for task in tasks:
            tintr = getattr(task, "interrupts", None) or ()
            for item in tintr:
                raw_items.append(item)
                value = getattr(item, "value", item)
                vdict = _as_dict(value)
                reqs = vdict.get("action_requests") or []
                if isinstance(reqs, list) and reqs:
                    for req in reqs:
                        act = _parse_action_request({"action_request": req})
                        if act:
                            actions.append(act)
                else:
                    act = _parse_action_request(value)
                    if act:
                        actions.append(act)

    if not actions and not raw_items:
        # Graph may still be mid-node with next != ()
        nxt = getattr(state, "next", None) or ()
        if not nxt:
            return None
        # No parseable actions
        return None

    if not actions:
        return PendingInterrupt(actions=[], raw=raw_items or interrupts)

    return PendingInterrupt(actions=actions, raw=raw_items or interrupts)


def format_interrupt_lines(pending: PendingInterrupt) -> list[str]:
    """Human-readable pending approvals."""
    if not pending.actions:
        return [
            "approval required (unparsed interrupt)",
            "use /approve or /reject after inspecting agent state",
        ]
    lines = [f"approval required for {len(pending.actions)} tool call(s):"]
    for i, act in enumerate(pending.actions, 1):
        args_preview = str(act.args)
        if len(args_preview) > 120:
            args_preview = args_preview[:119] + "…"
        lines.append(f"  {i}. {act.name}  args={args_preview}")
        if act.description:
            one = " ".join(act.description.split())
            if len(one) > 140:
                one = one[:139] + "…"
            lines.append(f"     {one}")
    lines.append("decide: /approve  |  /reject [reason]")
    return lines


def build_decisions(
    pending: PendingInterrupt,
    *,
    action: str,
    message: str | None = None,
) -> list[dict[str, Any]]:
    """Build resume decisions list matching pending action count."""
    kind = (action or "approve").strip().casefold()
    if kind not in {"approve", "reject"}:
        kind = "approve"
    decisions: list[dict[str, Any]] = []
    n = max(1, len(pending.actions))
    for _ in range(n):
        if kind == "approve":
            decisions.append({"type": "approve"})
        else:
            d: dict[str, Any] = {"type": "reject"}
            if message:
                d["message"] = message
            else:
                d["message"] = "Rejected by user via /reject"
            decisions.append(d)
    return decisions


def build_resume_payload(decisions: list[dict[str, Any]]) -> Any:
    """LangGraph Command(resume=...) payload for HITL middleware."""
    from langgraph.types import Command

    return Command(resume={"decisions": decisions})


def has_pending_interrupt(agent: Any, config: dict[str, Any]) -> bool:
    pending = extract_pending_interrupt(agent, config)
    return pending is not None and bool(pending.actions or pending.raw)
