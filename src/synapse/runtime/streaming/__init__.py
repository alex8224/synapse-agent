"""UI-independent streaming contracts and runtime-owned turn state."""

from synapse.runtime.streaming.accumulator import TurnAccumulator
from synapse.runtime.streaming.adapters import InstrumentedStreamSink
from synapse.runtime.streaming.events import (
    EVENT_VERSION,
    ActivityPayload,
    DiffPayload,
    PlanEntryPayload,
    PlanPayload,
    PlanRemovedPayload,
    SubagentStatusPayload,
    TextPayload,
    ToolBatchFinishedPayload,
    ToolBatchPayload,
    ToolCallPayload,
    ToolFinishedPayload,
    ToolItemPayload,
    ToolResultPayload,
    TurnEvent,
    TurnEventKind,
    TurnTerminalPayload,
    UsagePayload,
)
from synapse.runtime.streaming.protocol import (
    AgentEventSink,
    CallbackEventSink,
    CollectingEventSink,
    CompositeEventSink,
    NullEventSink,
)
from synapse.runtime.streaming.runtime import (
    checkpointer_supports_async,
    is_sync_only_checkpointer_error,
    iter_stream_events,
)
from synapse.runtime.streaming.tool_model import ToolItem

__all__ = [
    "EVENT_VERSION",
    "ActivityPayload",
    "DiffPayload",
    "PlanEntryPayload",
    "PlanPayload",
    "PlanRemovedPayload",
    "AgentEventSink",
    "CallbackEventSink",
    "CollectingEventSink",
    "CompositeEventSink",
    "InstrumentedStreamSink",
    "NullEventSink",
    "TextPayload",
    "ToolBatchFinishedPayload",
    "ToolBatchPayload",
    "ToolCallPayload",
    "ToolFinishedPayload",
    "ToolItem",
    "ToolItemPayload",
    "ToolResultPayload",
    "SubagentStatusPayload",
    "TurnAccumulator",
    "TurnEvent",
    "TurnEventKind",
    "TurnTerminalPayload",
    "UsagePayload",
    "checkpointer_supports_async",
    "is_sync_only_checkpointer_error",
    "iter_stream_events",
]
