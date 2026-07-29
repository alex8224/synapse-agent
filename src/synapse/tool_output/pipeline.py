"""Tool-output transformation orchestration and safety guards."""
from __future__ import annotations

import time

from synapse.tool_output.detection import (
    _CODE_SUFFIXES,
    _code_marker_count,
    _diff_bloat_metadata,
    _strip_numbered_source_lines,
    detect_content_type,
)
from synapse.tool_output.models import (
    CompressionStageEvent,
    ContentType,
    Detection,
    TransformContext,
    TransformResult,
)
from synapse.tool_output.transformers import (
    CodeTransformer,
    DiffTransformer,
    GenericTransformer,
    GitSummaryTransformer,
    JsonTransformer,
    LogTransformer,
    NativeTransformer,
    SearchTransformer,
    ToolOutputTransformer,
    load_native_transformers,
)


class ToolOutputTransformPipeline:
    """Deterministic Headroom-inspired algorithms with optional local plugins."""

    def __init__(
        self,
        *,
        transformers: list[ToolOutputTransformer] | None = None,
        disabled_types: set[str] | frozenset[str] | None = None,
        use_native: bool = True,
    ) -> None:
        builtins: list[ToolOutputTransformer] = [
            SearchTransformer(),
            LogTransformer(),
            DiffTransformer(),
            GitSummaryTransformer(),
            JsonTransformer(),
            CodeTransformer(),
            GenericTransformer(),
        ]
        self.transformers = [
            *(transformers or []),
            *load_native_transformers(enabled=use_native),
            *builtins,
        ]
        self.disabled_types = frozenset(str(item) for item in (disabled_types or set()))

    def transform(self, content: str, context: TransformContext) -> TransformResult:
        original_bytes = len(content.encode("utf-8"))
        detection_started = time.perf_counter()
        raw_detection = detect_content_type(content)
        detection = raw_detection
        detection_view = content
        numbered_lines = 0
        non_empty_lines = 0
        normalized_code_markers = 0
        suffix_is_code = context.file_suffix.casefold() in _CODE_SUFFIXES
        numbered_source_hint = bool(
            suffix_is_code
            and numbered_lines
            and numbered_lines >= max(1, non_empty_lines // 2)
        )
        if context.tool_name == "read_file":
            normalized, numbered_lines, non_empty_lines = _strip_numbered_source_lines(content)
            normalized_code_markers = _code_marker_count(normalized)
            numbered_source_hint = bool(
                suffix_is_code
                and numbered_lines
                and numbered_lines >= max(1, non_empty_lines // 2)
            )
            if numbered_lines and normalized_code_markers >= 3:
                detection_view = normalized
                detection = detect_content_type(normalized)
            if numbered_source_hint:
                detection_view = normalized
                detection = Detection(ContentType.CODE, max(0.8, detection.confidence))
        detection_metadata = {
            "content_type": detection.content_type.value,
            "confidence": detection.confidence,
            "raw_content_type": raw_detection.content_type.value,
            "raw_confidence": raw_detection.confidence,
            "tool_name": context.tool_name,
            "file_suffix": context.file_suffix,
            "numbered_lines": numbered_lines,
            "non_empty_lines": non_empty_lines,
            "normalized_code_markers": normalized_code_markers,
            "numbered_source_hint": numbered_source_hint,
            "classification_conflict": raw_detection.content_type is not detection.content_type,
        }
        if detection.content_type is ContentType.DIFF:
            detection_metadata.update(_diff_bloat_metadata(content))
        stages: list[CompressionStageEvent] = [
            CompressionStageEvent(
                phase="detect",
                algorithm="tool-aware-content-detector-v2",
                applied=True,
                reason_code="classified",
                input_bytes=original_bytes,
                output_bytes=len(detection_view.encode("utf-8")),
                duration_ms=(time.perf_counter() - detection_started) * 1000,
                metadata=detection_metadata,
            )
        ]
        if (
            context.tool_name == "read_file"
            and context.status == "success"
            and suffix_is_code
            and detection.content_type is ContentType.CODE
        ):
            stages.append(
                CompressionStageEvent(
                    phase="eligibility",
                    algorithm="fresh-read-source-policy-v1",
                    applied=False,
                    reason_code="fresh_read_source_protected",
                    input_bytes=original_bytes,
                    output_bytes=original_bytes,
                    metadata={
                        "file_path": context.file_path,
                        "file_suffix": context.file_suffix,
                        "normalized_code_markers": normalized_code_markers,
                    },
                )
            )
            return TransformResult(
                content,
                "fresh-read-source-policy-v1",
                ContentType.CODE,
                0,
                0,
                {
                    "fallback": "fresh_read_source_protected",
                    "detection_confidence": detection.confidence,
                    **detection_metadata,
                },
                tuple(stages),
            )
        if detection.content_type.value in self.disabled_types:
            stages.append(
                CompressionStageEvent(
                    phase="eligibility",
                    algorithm="disabled-types-policy",
                    applied=False,
                    reason_code="disabled_content_type",
                    input_bytes=original_bytes,
                    output_bytes=original_bytes,
                )
            )
            return TransformResult(
                content,
                "disabled",
                detection.content_type,
                0,
                0,
                {"fallback": "disabled", "detection_confidence": detection.confidence},
                tuple(stages),
            )
        transformer = next(
            (
                item
                for item in self.transformers
                if detection.content_type in getattr(item, "content_types", set())
            ),
            GenericTransformer(),
        )
        transform_started = time.perf_counter()
        result = transformer.transform(content, context)
        result_bytes = len(result.content.encode("utf-8"))
        native = isinstance(transformer, NativeTransformer)
        native_reason = str(result.metadata.get("fallback") or "compressed")
        stages.append(
            CompressionStageEvent(
                phase="native-transform" if native else "transform",
                algorithm=str(getattr(transformer, "name", result.transformer)),
                applied=result.content != content,
                reason_code=native_reason,
                input_bytes=original_bytes,
                output_bytes=result_bytes,
                duration_ms=(time.perf_counter() - transform_started) * 1000,
                metadata=dict(result.metadata),
            )
        )
        execution_path = "native" if native else "python_only"
        native_result_is_unsafe_or_unhelpful = native and (
            result.metadata.get("fallback") == "native_error"
            or result.critical_retained < result.critical_total
            or result_bytes >= original_bytes
        )
        if native_result_is_unsafe_or_unhelpful:
            fallback_transformer = next(
                (
                    item
                    for item in self.transformers
                    if detection.content_type in getattr(item, "content_types", set())
                    and not isinstance(item, NativeTransformer)
                ),
                None,
            )
            if fallback_transformer is not None:
                fallback_started = time.perf_counter()
                result = fallback_transformer.transform(content, context)
                result_bytes = len(result.content.encode("utf-8"))
                stages.append(
                    CompressionStageEvent(
                        phase="python-fallback",
                        algorithm=str(getattr(fallback_transformer, "name", result.transformer)),
                        applied=result.content != content,
                        reason_code="python_fallback_used",
                        input_bytes=original_bytes,
                        output_bytes=result_bytes,
                        duration_ms=(time.perf_counter() - fallback_started) * 1000,
                        metadata=dict(result.metadata),
                    )
                )
                execution_path = "python_fallback_after_native"
        result = TransformResult(
            result.content,
            result.transformer,
            result.content_type,
            result.critical_total,
            result.critical_retained,
            {
                **detection_metadata,
                **result.metadata,
                "execution_path": execution_path,
                "detection_confidence": detection.confidence,
            },
            tuple(stages),
        )
        if result.critical_retained < result.critical_total:
            stages.append(
                CompressionStageEvent(
                    phase="critical-guard",
                    algorithm="critical-retention-v1",
                    applied=False,
                    reason_code="critical_content_lost",
                    input_bytes=original_bytes,
                    output_bytes=result_bytes,
                    metadata={
                        "critical_total": result.critical_total,
                        "critical_retained": result.critical_retained,
                    },
                )
            )
            return TransformResult(
                content,
                "passthrough",
                detection.content_type,
                result.critical_total,
                result.critical_total,
                {
                    "fallback": "critical_content_lost",
                    "execution_path": execution_path,
                    "detection_confidence": detection.confidence,
                },
                tuple(stages),
            )
        stages.append(
            CompressionStageEvent(
                phase="critical-guard",
                algorithm="critical-retention-v1",
                applied=True,
                reason_code="accepted",
                input_bytes=original_bytes,
                output_bytes=result_bytes,
                metadata={
                    "critical_total": result.critical_total,
                    "critical_retained": result.critical_retained,
                },
            )
        )
        if result_bytes >= original_bytes:
            stages.append(
                CompressionStageEvent(
                    phase="byte-guard",
                    algorithm="non-increase-v1",
                    applied=False,
                    reason_code="no_byte_savings",
                    input_bytes=original_bytes,
                    output_bytes=result_bytes,
                )
            )
            return TransformResult(
                content,
                "passthrough",
                detection.content_type,
                result.critical_total,
                result.critical_total,
                {
                    "fallback": "no_byte_savings",
                    "execution_path": execution_path,
                    "detection_confidence": detection.confidence,
                },
                tuple(stages),
            )
        stages.append(
            CompressionStageEvent(
                phase="byte-guard",
                algorithm="non-increase-v1",
                applied=True,
                reason_code="accepted",
                input_bytes=original_bytes,
                output_bytes=result_bytes,
            )
        )
        return TransformResult(
            result.content,
            result.transformer,
            result.content_type,
            result.critical_total,
            result.critical_retained,
            result.metadata,
            tuple(stages),
        )
