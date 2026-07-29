"""Deterministic offline evaluation for tool-output transformers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from synapse.tool_output.pipeline import ToolOutputTransformPipeline, TransformContext


@dataclass(frozen=True)
class ToolOutputEvalCase:
    case_id: str
    content: str
    tool_name: str = "execute"
    query: str = ""
    required: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ToolOutputEvalCase:
        return cls(
            case_id=str(value["id"]),
            content=str(value["content"]),
            tool_name=str(value.get("tool_name") or "execute"),
            query=str(value.get("query") or ""),
            required=tuple(str(item) for item in value.get("required", [])),
        )


@dataclass(frozen=True)
class ToolOutputEvalResult:
    case_id: str
    content_type: str
    transformer: str
    original_bytes: int
    visible_bytes: int
    savings_ratio: float
    required_total: int
    required_retained: int

    @property
    def passed(self) -> bool:
        return (
            self.required_retained == self.required_total
            and self.visible_bytes < self.original_bytes
        )


def load_cases(path: Path | str) -> list[ToolOutputEvalCase]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError("tool-output eval fixture must be a JSON array")
    return [ToolOutputEvalCase.from_dict(item) for item in value if isinstance(item, dict)]


def evaluate_cases(
    cases: list[ToolOutputEvalCase], *, pipeline: ToolOutputTransformPipeline | None = None
) -> list[ToolOutputEvalResult]:
    active = pipeline or ToolOutputTransformPipeline()
    results: list[ToolOutputEvalResult] = []
    for case in cases:
        transformed = active.transform(
            case.content,
            TransformContext(tool_name=case.tool_name, status="success", query=case.query),
        )
        original_bytes = len(case.content.encode("utf-8"))
        visible_bytes = len(transformed.content.encode("utf-8"))
        retained = sum(item in transformed.content for item in case.required)
        results.append(
            ToolOutputEvalResult(
                case_id=case.case_id,
                content_type=transformed.content_type.value,
                transformer=transformed.transformer,
                original_bytes=original_bytes,
                visible_bytes=visible_bytes,
                savings_ratio=round(1 - visible_bytes / original_bytes, 4)
                if original_bytes
                else 0.0,
                required_total=len(case.required),
                required_retained=retained,
            )
        )
    return results


def summarize_results(results: list[ToolOutputEvalResult]) -> dict[str, Any]:
    original = sum(item.original_bytes for item in results)
    visible = sum(item.visible_bytes for item in results)
    required_total = sum(item.required_total for item in results)
    required_retained = sum(item.required_retained for item in results)
    return {
        "cases": len(results),
        "passed": sum(item.passed for item in results),
        "original_bytes": original,
        "visible_bytes": visible,
        "savings_ratio": round(1 - visible / original, 4) if original else 0.0,
        "required_retention": (
            round(required_retained / required_total, 4) if required_total else 1.0
        ),
        "results": [
            {
                "id": item.case_id,
                "type": item.content_type,
                "transformer": item.transformer,
                "savings_ratio": item.savings_ratio,
                "required_retention": (
                    round(item.required_retained / item.required_total, 4)
                    if item.required_total
                    else 1.0
                ),
                "passed": item.passed,
            }
            for item in results
        ],
    }
