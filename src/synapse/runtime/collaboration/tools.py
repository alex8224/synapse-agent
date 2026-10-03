"""Agent-facing collaboration tools.

Both tools take their trusted identity from the frozen turn rather than from
model arguments: the project comes from the daemon that built the agent, the
session from the running turn's config, and the collaboration request being
answered from the delivery metadata the runtime attached. A model can therefore
choose *what* to say and which *allowed* session to contact, but it cannot invent
a sender, a request id or a work generation.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, Literal

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from synapse.runtime.collaboration.model import (
    CollaborationError,
    MessageKind,
    NotPermittedError,
    TargetUnavailableError,
)
from synapse.runtime.sessions.ref import SessionRef, SessionResolutionError, parse_global_id

__all__ = ['build_collaboration_tools']


def _configurable(runtime: Any) -> Mapping[str, Any]:
    config = getattr(runtime, 'config', None)
    if not isinstance(config, Mapping):
        return {}
    configurable = config.get('configurable')
    return configurable if isinstance(configurable, Mapping) else {}


def _thread_id(runtime: Any) -> str | None:
    value = _configurable(runtime).get('thread_id')
    return str(value) if value else None


def _delivery_metadata(runtime: Any) -> Mapping[str, Any]:
    value = _configurable(runtime).get('collaboration')
    return value if isinstance(value, Mapping) else {}


def _call_key(runtime: Any) -> str:
    """Stable per tool call, so a retry of one call never writes twice."""
    value = getattr(runtime, 'tool_call_id', None)
    return str(value) if value else uuid.uuid4().hex


def build_collaboration_tools(coordinator: Any, *, source_project: str) -> list[Any]:
    """Build the two collaboration tools for one project's agent.

    ``coordinator`` is the daemon's single coordinator; ``source_project`` is the
    project this agent was built for, so a turn can never claim to be another
    project's session.
    """

    def _source(runtime: Any) -> SessionRef | None:
        thread_id = _thread_id(runtime)
        if not thread_id:
            return None
        return SessionRef(source_project, thread_id)

    @tool
    def contact_session(
        runtime: ToolRuntime,
        target: str,
        request: str,
        expect: str = '',
        collaboration_id: str = '',
    ) -> str:
        """请另一个会话协作处理一件事，并把结论带回当前会话。

        用 `@会话` 选择器插入目标会话，或按 `<project_id>:<thread_id>` 填写。
        只发送本次请求需要的背景与交付要求，不会发送整个对话历史。
        调用后立即返回：不要等待、不要轮询对方，继续完成不依赖它的部分；
        对方完成后结论会自动回到本会话继续。

        Args:
            target: 目标会话，格式 `<project_id>:<thread_id>`。
            request: 请对方做什么，以及必要的背景与约束。
            expect: 希望对方返回什么结论，可留空。
            collaboration_id: 已有协作 ID；向多个会话交办同一工作时复用，留空则新建。
        """
        source = _source(runtime)
        if source is None:
            return 'failed: this turn has no session identity'
        if _delivery_metadata(runtime).get('role') == 'target':
            return 'failed: 被交办方不能隐式转派，请向指挥者回复或请求澄清'
        # The console inserts a `@session:<project_id>:<thread_id>` pill; the
        # same target may also arrive as a bare ref, so both are accepted.
        reference = target.strip()
        if reference.startswith('@session:'):
            reference = reference[len('@session:'):]
        try:
            parsed = parse_global_id(reference)
        except SessionResolutionError:
            return 'failed: target must be `<project_id>:<thread_id>`'
        if parsed == source:
            return 'failed: the target is the current session'
        body = request.strip()
        if not body:
            return 'failed: request must not be empty'
        if expect.strip():
            body = f'{body}\n\n期望交付：{expect.strip()}'
        try:
            group_id = collaboration_id or str(_delivery_metadata(runtime).get('scope_id', ''))
            scope = (coordinator.work(group_id, source) if group_id
                     else coordinator.current_work(source))
            created = coordinator.request(
                scope.id,
                source,
                parsed,
                operation_key=_call_key(runtime),
                body=body,
                anchor_turn_id='',
            )
        except CollaborationError as exc:
            return _failure(exc)
        return (
            f'accepted: 已请 {parsed.global_id} 协作（协作 {scope.id}，交办 {created.id}）。'
            '对方处理完成后结论会回到本会话；现在继续做不依赖它的部分，不要轮询等待。'
        )

    @tool
    def reply_to_collaboration(
        runtime: ToolRuntime,
        message: str,
        kind: Literal['clarification', 'supplement', 'reply', 'progress'],
    ) -> str:
        """回复当前轮次正在处理的协作消息。

        - 被请求方：`clarification` 提出澄清问题，`reply` 交付最终结论，
          `progress` 报告进展或阻塞（仅记录通知，不启动对方轮次）。
        - 发起方收到对方提问时：`supplement` 回答对方的问题。

        只在当前轮次确实在处理一条协作消息时可用，普通对话调用会失败。
        最终结论必须用 `reply` 交付，它才会回到发起方会话。

        Args:
            message: 回复内容；`reply` 时是给对方直接使用的结论。
            kind: `clarification` / `supplement` / `reply` / `progress`。
        """
        source = _source(runtime)
        metadata = _delivery_metadata(runtime)
        request_id = metadata.get('request_id')
        if source is None or not request_id:
            return 'failed: this turn is not handling a collaboration message'
        role = str(metadata.get('role', ''))
        allowed = {'clarification', 'reply', 'progress'} if role == 'target' else {'supplement'}
        if kind not in allowed:
            return f'failed: role {role!r} cannot send {kind!r}'
        body = message.strip()
        if not body:
            return 'failed: message must not be empty'
        delivery_id = str(metadata.get('delivery_id', ''))
        if not delivery_id:
            return 'failed: this turn has no collaboration delivery identity'
        try:
            coordinator.respond(
                str(request_id),
                source,
                kind=MessageKind(kind),
                body=body,
                operation_key=f'{delivery_id}:{_call_key(runtime)}',
                anchor_turn_id=str(metadata.get('anchor_turn_id', '')),
            )
        except CollaborationError as exc:
            return _failure(exc)
        return 'recorded: 已记录本次回复。'

    return [contact_session, reply_to_collaboration]


def _failure(exc: CollaborationError) -> str:
    """Turn a domain failure into something the model (and the reader) can act on.

    The type name alone is not actionable: "the deployment forbids this" and
    "that session is not reachable" need different next steps, so they are said
    in words while every other failure keeps its value-free type name.
    """
    if isinstance(exc, NotPermittedError):
        return 'failed: 当前部署不允许跨项目协作，请改在同一项目内选择目标会话。'
    if isinstance(exc, TargetUnavailableError):
        reason = str(exc).strip() or 'unknown'
        return f'failed: 目标会话现在无法访问（{reason}），请确认后再试。'
    return f'failed: {type(exc).__name__}'
