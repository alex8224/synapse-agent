"""Transcript render state owned by ``TranscriptController``."""

from __future__ import annotations

from dataclasses import dataclass, field

from synapse.ui.timeline import ToolItem
from synapse.ui.tool_blocks import ToolGroupBlock
from synapse.ui.transcript_blocks import AnswerBlock, ThoughtBlock
from synapse.ui.user_turn_block import UserTurnBlock


@dataclass
class TranscriptState:
    """Mounted transcript bookkeeping for live streaming, turns and tools."""

    user_turns: list[UserTurnBlock] = field(default_factory=list)
    thought_blocks: list[ThoughtBlock] = field(default_factory=list)
    tool_blocks: list[ToolGroupBlock] = field(default_factory=list)
    live_stream_block: ThoughtBlock | AnswerBlock | None = None
    live_stream_kind: str | None = None
    live_tool_block: ToolGroupBlock | None = None
    in_tool_rail: bool = False
    pending_answer_divider: bool = False
    last_tool_items: list[ToolItem] = field(default_factory=list)
    last_tool_summary: str = ""
    live_tool_items: list[ToolItem] = field(default_factory=list)
    live_tool_summary: str = ""
    last_answer_text: str = ""
    last_thought_body: str = ""
    last_thought_elapsed: float = 0.0
    thought_expanded: bool = False
