"""Data models for reversible tool-output transformation."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class ContentType(StrEnum):
    SEARCH = "search"
    PATHS = "paths"
    LOG = "log"
    DIFF = "diff"
    GIT_SUMMARY = "git-summary"
    JSON = "json"
    CODE = "code"
    TEXT = "text"


@dataclass(frozen=True)
class Detection:
    content_type: ContentType
    confidence: float


@dataclass(frozen=True)
class TransformContext:
    tool_name: str
    status: str
    query: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    file_path: str = ""
    file_suffix: str = ""
    fresh_read_source: bool = True


@dataclass(frozen=True)
class CompressionStageEvent:
    """One observable stage in a tool-output compression decision."""

    phase: str
    algorithm: str
    applied: bool
    reason_code: str
    input_bytes: int = 0
    output_bytes: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TransformResult:
    content: str
    transformer: str
    content_type: ContentType
    critical_total: int
    critical_retained: int
    metadata: dict[str, Any] = field(default_factory=dict)
    stages: tuple[CompressionStageEvent, ...] = ()


@dataclass(frozen=True)
class ToolOutputRecord:
    ref: str
    thread_id: str
    checkpoint_ns: str
    tool_call_id: str
    tool_name: str
    status: str
    content: str
    size_bytes: int
    sha256: str
    created_at: str


@dataclass(frozen=True)
class ModelRequestCompressionEvent:
    """One model call's compression and provider-safety accounting."""

    request_id: str
    provider: str
    api_style: str
    auth_mode: str
    model: str
    input_tokens_before: int
    input_tokens_after: int
    provider_input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    uncached_input_tokens: int = 0
    output_tokens: int = 0
    tool_output_saved_tokens: int = 0
    prompt_saved_tokens: int = 0
    summarization_saved_tokens: int = 0
    total_saved_tokens: int = 0
    candidate_blocks: int = 0
    transformed_blocks: int = 0
    protected_tokens_by_reason: dict[str, int] = field(default_factory=dict)
    content_breakdown: dict[str, int] = field(default_factory=dict)
    opportunity_tokens_by_reason: dict[str, int] = field(default_factory=dict)
    turn_id: str = ""
    turn_index: int = 0
    model_call_index: int = 0
    live_zone_plan: list[dict[str, Any]] = field(default_factory=list)
    live_zone_tokens: dict[str, int] = field(default_factory=dict)
    wire_fingerprints: dict[str, Any] = field(default_factory=dict)
    cache_diagnostics: dict[str, Any] = field(default_factory=dict)
    tool_schema_profiles: list[dict[str, Any]] = field(default_factory=list)
    token_count_method: str = "langchain_approximate"
    duration_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        whole_denominator = self.input_tokens_after + self.total_saved_tokens
        new_input = self.uncached_input_tokens + self.cache_write_tokens
        data["whole_request_savings_ratio"] = (
            round(self.total_saved_tokens / whole_denominator, 4) if whole_denominator else 0.0
        )
        data["new_input_savings_ratio"] = (
            round(self.total_saved_tokens / (new_input + self.total_saved_tokens), 4)
            if new_input > 0
            else 0.0
        )
        return data


@dataclass(frozen=True)
class TransformEvent:
    content_type: str
    transformer: str
    outcome: str
    original_bytes: int
    visible_bytes: int
    duration_ms: float
    critical_total: int
    critical_retained: int
    ref_created: bool
    execution_path: str = "python_only"
    estimated_original_tokens: int = 0
    estimated_visible_tokens: int = 0
    decision: str = "transformed"
    reason_code: str = "compressed"
    reason_detail: str = ""
    eligible: bool = True
    source_kind: str = "tool-output"
    detection_confidence: float = 0.0
    threshold_bytes: int = 0
    tool_call_id: str = ""
    tool_name: str = ""
    status: str = "success"
    checkpoint_ns: str = ""
    message_id: str = ""
    algorithm_output_bytes: int = 0
    algorithm_output_tokens: int = 0
    token_count_method: str = "langchain_approximate"
    content_sha256: str = ""
    stages: tuple[CompressionStageEvent, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["saved_bytes"] = max(0, self.original_bytes - self.visible_bytes)
        data["estimated_saved_tokens"] = max(
            0, self.estimated_original_tokens - self.estimated_visible_tokens
        )
        data["savings_ratio"] = (
            round(1 - self.visible_bytes / self.original_bytes, 4) if self.original_bytes else 0.0
        )
        return data
