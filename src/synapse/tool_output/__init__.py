"""Reversible tool-output transformation domain."""

from synapse.tool_output.detection import detect_content_type
from synapse.tool_output.metrics import clear_metrics_notifier, set_metrics_notifier
from synapse.tool_output.models import (
    CompressionStageEvent,
    ContentType,
    Detection,
    ModelRequestCompressionEvent,
    ToolOutputRecord,
    TransformContext,
    TransformEvent,
    TransformResult,
)
from synapse.tool_output.pipeline import ToolOutputTransformPipeline
from synapse.tool_output.repository import ToolOutputRepository, content_to_text
from synapse.tool_output.transformers import (
    CodeTransformer,
    DiffTransformer,
    GenericTransformer,
    GitSummaryTransformer,
    JsonTransformer,
    LogTransformer,
    NativeTransformer,
    PathListTransformer,
    SearchTransformer,
    ToolOutputTransformer,
    load_native_transformers,
    load_transformer_plugins,
)

__all__ = [
    "CodeTransformer",
    "CompressionStageEvent",
    "ContentType",
    "Detection",
    "DiffTransformer",
    "GenericTransformer",
    "GitSummaryTransformer",
    "JsonTransformer",
    "LogTransformer",
    "ModelRequestCompressionEvent",
    "NativeTransformer",
    "PathListTransformer",
    "SearchTransformer",
    "ToolOutputRecord",
    "ToolOutputRepository",
    "ToolOutputTransformer",
    "ToolOutputTransformPipeline",
    "TransformContext",
    "TransformEvent",
    "TransformResult",
    "clear_metrics_notifier",
    "content_to_text",
    "detect_content_type",
    "load_native_transformers",
    "load_transformer_plugins",
    "set_metrics_notifier",
]
