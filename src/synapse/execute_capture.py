"""Per-tool-call capture channel for untruncated shell output.

``FilesystemMiddleware`` only converts ``ExecuteResponse.output`` into a
``ToolMessage``. The response contract cannot carry artifacts, so the backend
records the full pre-truncation output in this context-local side channel while
the result-offload middleware invokes the execute tool.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass
class ExecuteOutputCapture:
    """Full and model-visible backend output for one execute invocation."""

    full_output: str | None = None
    displayed_output: str | None = None
    truncated: bool = False


_active_capture: ContextVar[ExecuteOutputCapture | None] = ContextVar(
    "synapse_execute_output_capture",
    default=None,
)


def begin_execute_capture() -> tuple[ExecuteOutputCapture, Token[ExecuteOutputCapture | None]]:
    """Install an empty capture object for the current tool-call context."""
    capture = ExecuteOutputCapture()
    return capture, _active_capture.set(capture)


def end_execute_capture(token: Token[ExecuteOutputCapture | None]) -> None:
    """Restore the enclosing tool-call capture context."""
    _active_capture.reset(token)


def capture_execute_output(*, full_output: str, displayed_output: str, truncated: bool) -> None:
    """Record the backend response before its size limit discarded content."""
    capture = _active_capture.get()
    if capture is None:
        return
    capture.full_output = full_output
    capture.displayed_output = displayed_output
    capture.truncated = truncated
