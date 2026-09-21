"""Pure DTOs, limits, and validators for the Windows window-screenshot surface.

The console can ask this host to capture one window with the bundled
``windows-capture`` tool, and the runtime is the *only* thing that talks to that
tool: the browser never opens the named pipe and never sees a tool path, a raw
tool error, or a capture byte.  This module owns the transport-neutral half of
that surface — the frozen request/result DTOs, the tool/console limits, the
closed state set, and the cheap syntactic validation of a capture request.

It performs no filesystem access, spawns no process, and imports no adapter: the
CLI/pipe adapter lives in :mod:`synapse.runtime.service.screenshot_tool` and the
resident task scheduler in :mod:`synapse.runtime.service.screenshot_service`.
Keeping this module pure lets ``contract_registry`` import it without pulling in
``subprocess``.

Every bound here mirrors the tool's own documented limits (README §5) or the
composer's image budget; error text never echoes a caller-supplied value.
"""

from __future__ import annotations

from dataclasses import dataclass

from synapse.runtime.service.errors import InvalidRequestError
from synapse.runtime.sessions.ref import SessionRef

__all__ = [
    "DEFAULT_SCREENSHOT_FRAME_TIMEOUT_MS",
    "DEFAULT_SCREENSHOT_INTERVAL_MS",
    "DEFAULT_SCREENSHOT_TTL_SECONDS",
    "MAX_SCREENSHOT_COUNT",
    "MAX_SCREENSHOT_FRAMES_PER_TASK",
    "MAX_SCREENSHOT_FRAME_TIMEOUT_MS",
    "MAX_SCREENSHOT_INTERVAL_MS",
    "MAX_SCREENSHOT_MAX_EDGE",
    "MAX_SCREENSHOT_START_DELAY_MS",
    "MAX_SCREENSHOT_TASK_ID_BYTES",
    "MAX_SCREENSHOT_TTL_SECONDS",
    "MIN_SCREENSHOT_TTL_SECONDS",
    "OpenScreenshotSettingsCommand",
    "SCREENSHOT_STATES",
    "SCREENSHOT_TERMINAL_STATES",
    "ScreenshotCancelCommand",
    "ScreenshotCancelResult",
    "ScreenshotCaptureCommand",
    "ScreenshotCaptureResult",
    "ScreenshotFrameAttachment",
    "ScreenshotSettings",
    "ScreenshotStatus",
    "ScreenshotStatusQuery",
    "ScreenshotToolStatus",
    "validate_cancel_command",
    "validate_capture_command",
    "validate_settings",
    "validate_status_query",
]

# --- tool / console limits --------------------------------------------------

#: The tool refuses more frames per job than this (README §5, ``max_frames_per_job``).
MAX_SCREENSHOT_COUNT = 600
#: One console capture never asks for more than the composer's per-submit image
#: budget: a screenshot result becomes composer attachments, and more than this
#: could never be sent in one turn anyway.
MAX_SCREENSHOT_FRAMES_PER_TASK = 8
MAX_SCREENSHOT_INTERVAL_MS = 600_000
MAX_SCREENSHOT_START_DELAY_MS = 600_000
MAX_SCREENSHOT_MAX_EDGE = 8192
MIN_SCREENSHOT_TTL_SECONDS = 1
MAX_SCREENSHOT_TTL_SECONDS = 3600
MAX_SCREENSHOT_FRAME_TIMEOUT_MS = 60_000
DEFAULT_SCREENSHOT_INTERVAL_MS = 250
DEFAULT_SCREENSHOT_TTL_SECONDS = 300
DEFAULT_SCREENSHOT_FRAME_TIMEOUT_MS = 5000
#: An opaque server-generated task id is a bounded token, never a path.
MAX_SCREENSHOT_TASK_ID_BYTES = 128

#: The closed set of capture task states the console renders.
SCREENSHOT_STATES = (
    "idle",
    "queued",
    "running",
    "completed",
    "cancelled",
    "failed",
    "target_required",
)
#: States after which a task never changes again.
SCREENSHOT_TERMINAL_STATES = ("completed", "cancelled", "failed", "target_required")


# --- DTOs -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScreenshotSettings:
    """The bounded capture parameters the console may override.

    Every field has a default, so the wire treats the whole object and each
    member as optional: an absent value lets the tool apply its own saved config
    or its built-in default (``override > saved > default``).
    """

    count: int = 1
    interval_ms: int = DEFAULT_SCREENSHOT_INTERVAL_MS
    start_delay_ms: int = 0
    max_edge: int = 0
    ttl_seconds: int = DEFAULT_SCREENSHOT_TTL_SECONDS
    frame_timeout_ms: int = DEFAULT_SCREENSHOT_FRAME_TIMEOUT_MS
    allow_reuse: bool = True


@dataclass(frozen=True, slots=True)
class ScreenshotToolStatus:
    """Resident status of the host capture tool (availability + busy flag)."""

    available: bool
    platform: str
    reason: str = ""
    version: str | None = None
    busy: bool = False
    active_task_id: str | None = None


@dataclass(frozen=True, slots=True)
class ScreenshotFrameAttachment:
    """One finalized screenshot frame, ready to reference from a turn."""

    attachment_id: str
    name: str
    mime: str
    size: int
    revision: str | None = None


@dataclass(frozen=True, slots=True)
class ScreenshotCaptureCommand:
    """Start one asynchronous window capture for the calling session."""

    session: SessionRef
    settings: ScreenshotSettings | None = None
    save_config: bool = False
    #: Upper bound on the frames this capture may produce, independent of the
    #: tool's own saved ``count``.  When present the runtime reads the tool's
    #: saved config and starts the job with ``min(saved_count, max_frames)``, so a
    #: console whose composer can hold fewer images never asks the tool for more
    #: (the tool would otherwise honour its saved ``count``, up to 600).
    max_frames: int | None = None


@dataclass(frozen=True, slots=True)
class ScreenshotCaptureResult:
    """The task snapshot returned the moment a capture is queued."""

    session: SessionRef
    task_id: str
    state: str
    requested: int
    settings: ScreenshotSettings


@dataclass(frozen=True, slots=True)
class ScreenshotStatusQuery:
    """Read the calling session's current capture task."""

    session: SessionRef
    task_id: str = ""


@dataclass(frozen=True, slots=True)
class ScreenshotStatus:
    """The resident snapshot of one session's capture task.

    ``available`` / ``unavailable_reason`` are the host tool's resident status,
    folded into the same read so a console learns in one call both whether the
    capture surface can run at all and what its current task is doing.
    """

    session: SessionRef
    task_id: str
    state: str
    requested: int
    captured: int
    attachments: tuple[ScreenshotFrameAttachment, ...]
    available: bool = True
    unavailable_reason: str = ""
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class ScreenshotCancelCommand:
    """Request cancellation of one session's capture task."""

    session: SessionRef
    task_id: str


@dataclass(frozen=True, slots=True)
class ScreenshotCancelResult:
    """Whether the task is now cancelling / already terminal."""

    session: SessionRef
    task_id: str
    state: str
    cancelled: bool


@dataclass(frozen=True, slots=True)
class OpenScreenshotSettingsCommand:
    """Open (or focus) the capture tool's own settings window."""

    session: SessionRef


# --- validation -------------------------------------------------------------


def _session(session: object) -> SessionRef:
    if type(session) is not SessionRef:
        raise InvalidRequestError(
            f"screenshot session must be a SessionRef, got type {type(session).__name__!r}"
        )
    return session


def _bounded_int(value: object, *, field: str, minimum: int, maximum: int) -> int:
    if type(value) is not int:
        raise InvalidRequestError(f"screenshot {field} must be an integer")
    if value < minimum or value > maximum:
        raise InvalidRequestError(f"screenshot {field} is outside the allowed range")
    return value


def validate_settings(settings: object) -> ScreenshotSettings:
    """Validate one settings object against the tool's documented bounds."""
    if type(settings) is not ScreenshotSettings:
        raise InvalidRequestError(
            "screenshot settings must be a ScreenshotSettings, "
            f"got type {type(settings).__name__!r}"
        )
    if type(settings.allow_reuse) is not bool:
        raise InvalidRequestError("screenshot allow_reuse must be a boolean")
    return ScreenshotSettings(
        count=_bounded_int(
            settings.count, field="count", minimum=1, maximum=MAX_SCREENSHOT_COUNT
        ),
        interval_ms=_bounded_int(
            settings.interval_ms, field="interval_ms", minimum=0, maximum=MAX_SCREENSHOT_INTERVAL_MS
        ),
        start_delay_ms=_bounded_int(
            settings.start_delay_ms,
            field="start_delay_ms",
            minimum=0,
            maximum=MAX_SCREENSHOT_START_DELAY_MS,
        ),
        max_edge=_bounded_int(
            settings.max_edge, field="max_edge", minimum=0, maximum=MAX_SCREENSHOT_MAX_EDGE
        ),
        ttl_seconds=_bounded_int(
            settings.ttl_seconds,
            field="ttl_seconds",
            minimum=MIN_SCREENSHOT_TTL_SECONDS,
            maximum=MAX_SCREENSHOT_TTL_SECONDS,
        ),
        frame_timeout_ms=_bounded_int(
            settings.frame_timeout_ms,
            field="frame_timeout_ms",
            minimum=0,
            maximum=MAX_SCREENSHOT_FRAME_TIMEOUT_MS,
        ),
        allow_reuse=settings.allow_reuse,
    )


def _task_id(value: object) -> str:
    if type(value) is not str:
        raise InvalidRequestError("screenshot task id must be a string")
    if value != value.strip() or "\x00" in value:
        raise InvalidRequestError("screenshot task id is malformed")
    if len(value.encode("utf-8", errors="surrogatepass")) > MAX_SCREENSHOT_TASK_ID_BYTES:
        raise InvalidRequestError("screenshot task id exceeds the length limit")
    return value


def validate_capture_command(command: object) -> ScreenshotCaptureCommand:
    """Validate one capture request without touching the tool."""
    if type(command) is not ScreenshotCaptureCommand:
        raise InvalidRequestError(
            "screenshot capture command must be a ScreenshotCaptureCommand, "
            f"got type {type(command).__name__!r}"
        )
    settings = command.settings
    if settings is not None:
        settings = validate_settings(settings)
    if type(command.save_config) is not bool:
        raise InvalidRequestError("screenshot save_config must be a boolean")
    max_frames = command.max_frames
    if max_frames is not None:
        max_frames = _bounded_int(
            max_frames,
            field="max_frames",
            minimum=1,
            maximum=MAX_SCREENSHOT_FRAMES_PER_TASK,
        )
    return ScreenshotCaptureCommand(
        session=_session(command.session),
        settings=settings,
        save_config=command.save_config,
        max_frames=max_frames,
    )


def validate_status_query(query: object) -> ScreenshotStatusQuery:
    """Validate one status query (an empty task id asks for the session's own)."""
    if type(query) is not ScreenshotStatusQuery:
        raise InvalidRequestError(
            "screenshot status query must be a ScreenshotStatusQuery, "
            f"got type {type(query).__name__!r}"
        )
    return ScreenshotStatusQuery(session=_session(query.session), task_id=_task_id(query.task_id))


def validate_cancel_command(command: object) -> ScreenshotCancelCommand:
    """Validate one cancel request (the task id is required)."""
    if type(command) is not ScreenshotCancelCommand:
        raise InvalidRequestError(
            "screenshot cancel command must be a ScreenshotCancelCommand, "
            f"got type {type(command).__name__!r}"
        )
    task_id = _task_id(command.task_id)
    if not task_id:
        raise InvalidRequestError("screenshot cancel requires a task id")
    return ScreenshotCancelCommand(session=_session(command.session), task_id=task_id)
