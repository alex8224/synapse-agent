"""Request/result DTOs for the model-endpoint CRUD surface.

Pure data only: the module imports no implementation, transport, settings, or
framework module, and it carries **no secret**.  ``ModelSummary`` reports only
*whether* an endpoint has a configured credential (``has_api_key``); the key
itself, the request headers, the resolved environment, and the raw profile never
leave the service.

Two distinct alias rules live here and must not be conflated:

* **Reference validation** (:func:`_validate_alias_reference`) guards every
  command that *names an existing endpoint* -- test / delete / set-default, and
  the update path of save.  An alias a user has already persisted must stay
  addressable whatever it is called, so this only rejects values that cannot be
  a stable identifier: non-strings, empty or surrounding-whitespace text,
  over-long text, and control/format or whitespace characters.  It deliberately
  does **not** require ASCII.
* **Creation validation** (:data:`MODEL_ALIAS_PATTERN`) is the stricter rule
  that a *brand-new* alias be a single safe ASCII token.  It is enforced
  downstream by ``synapse.models.persist.add_profile`` (which raises
  ``ModelsStoreError``, mapped to ``InvalidRequestError``), so no DTO may
  re-impose it on the update path and thereby block an existing non-ASCII alias.

``profile`` on :class:`SaveModelCommand` is a wire-shaped mapping that is
validated against a strict allow-list here, so a caller can never smuggle an
unmodelled field (a path, a command, ``api_key_env``, ``auth``, ...) into the
persisted store: the wire and the DTO must agree, and a key outside the list is
rejected outright rather than being silently dropped.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from synapse.runtime.sessions.ref import SessionRef

__all__ = [
    "IMAGE_INPUT_STATES",
    "MODEL_ALIAS_PATTERN",
    "MODEL_PROFILE_ALLOWED_KEYS",
    "DeleteModelCommand",
    "ListModelsQuery",
    "ModelListResult",
    "ModelSummary",
    "SaveModelCommand",
    "SetDefaultModelCommand",
    "TestModelCommand",
    "TestModelResult",
]

#: The closed tri-state of an endpoint's native image support.
IMAGE_INPUT_STATES = ("auto", "yes", "no")

#: The *creation-time* alias rule, mirrored from ``synapse.models.persist``: a
#: brand-new endpoint alias must be a single safe ASCII token.  It is **not** a
#: reference rule -- naming an already-persisted endpoint goes through
#: :func:`_validate_alias_reference`, which accepts any non-control,
#: non-whitespace string (e.g. a user's ``官方-deepseek-v4-flash``).  Creation is
#: enforced by ``synapse.models.persist.add_profile``; this constant documents
#: the rule for the contract and other consumers.
MODEL_ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+$")

#: Upper bound (characters) on an alias *reference*; creation is stricter still.
MODEL_ALIAS_MAX_LENGTH = 128

#: The only profile keys a caller may set.  Anything else -- notably
#: ``api_key_env``, ``auth``, and ``turbo_base_url`` -- is refused instead of
#: being silently discarded, so the wire can never disagree with the store.
MODEL_PROFILE_ALLOWED_KEYS = frozenset(
    {
        "model",
        "api_key",
        "base_url",
        "reasoning_effort",
        "image_input",
        "context_window",
        "headers",
        "model_kwargs",
        "extra_body",
        "provider",
        "enable_thinking",
        "thinking_levels",
        "parallel_tool_calls",
        "turbo",
        "extra",
    }
)


def _validate_alias_reference(value: object, field: str = "alias") -> str:
    """Validate an alias used to *reference* an already-persisted endpoint.

    Unlike :data:`MODEL_ALIAS_PATTERN` (the creation rule enforced by
    ``synapse.models.persist.add_profile``), a reference must accept whatever a
    user may already have on disk -- including non-ASCII aliases such as
    ``官方-deepseek-v4-flash``.  It rejects only values that cannot be a stable
    identifier: non-strings, empty or surrounding-whitespace text, over-long
    text, and any control/format (Unicode category ``C*``) or whitespace
    character.
    """
    if type(value) is not str:
        raise ValueError(f"{field} must be a string")
    if not value or value != value.strip():
        raise ValueError(
            f"{field} must be a non-empty string without surrounding whitespace"
        )
    if len(value) > MODEL_ALIAS_MAX_LENGTH:
        raise ValueError(
            f"{field} must be at most {MODEL_ALIAS_MAX_LENGTH} characters"
        )
    for char in value:
        if char.isspace() or unicodedata.category(char).startswith("C"):
            raise ValueError(
                f"{field} must not contain control or whitespace characters"
            )
    return value


def _validate_profile(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("profile must be a mapping")
    unknown = set(value) - MODEL_PROFILE_ALLOWED_KEYS
    if unknown:
        raise ValueError(f"profile has unsupported keys: {sorted(unknown)}")
    for key in value:
        if type(key) is not str:
            raise ValueError("profile keys must be strings")
    return value


@dataclass(frozen=True, slots=True)
class ListModelsQuery:
    """List the model endpoints visible to one session's project."""

    session: SessionRef


@dataclass(frozen=True, slots=True)
class ModelSummary:
    """Redacted projection of one model endpoint (never a secret).

    ``image_input`` is the explicit tri-state (``"auto"`` / ``"yes"`` /
    ``"no"``) of whether the endpoint accepts native image content.
    ``has_api_key`` reports only whether a credential is configured -- the key
    itself is never carried.  ``is_default`` marks the store's effective
    default endpoint.
    """

    alias: str
    model: str
    provider: str | None
    base_url: str | None
    context_window: int | None
    reasoning_effort: str | None
    image_input: str
    has_api_key: bool
    is_default: bool


@dataclass(frozen=True, slots=True)
class ModelListResult:
    """The whole redacted endpoint catalog plus the shared thinking catalog."""

    default: str
    thinking_levels: tuple[str, ...]
    models: tuple[ModelSummary, ...]


@dataclass(frozen=True, slots=True)
class SaveModelCommand:
    """Add or replace one model endpoint (whitelist-validated ``profile``).

    When ``make_default`` is true the saved alias also becomes the store's
    default in the same call.

    ``alias`` is validated as a *reference* (:func:`_validate_alias_reference`),
    not as a creation token: this command also updates an existing endpoint, so
    a non-ASCII alias already in the store must pass.  The "new alias must be a
    safe ASCII token" rule stays with ``synapse.models.persist.add_profile``,
    which raises ``ModelsStoreError`` (mapped to ``InvalidRequestError``) with
    the precise reason when the alias is genuinely new.
    """

    session: SessionRef
    alias: str
    profile: Mapping[str, Any]
    make_default: bool = False

    def __post_init__(self) -> None:
        _validate_alias_reference(self.alias)
        object.__setattr__(self, "profile", _validate_profile(self.profile))
        if type(self.make_default) is not bool:
            raise ValueError("make_default must be a boolean")


@dataclass(frozen=True, slots=True)
class DeleteModelCommand:
    """Remove one model endpoint by alias."""

    session: SessionRef
    alias: str

    def __post_init__(self) -> None:
        _validate_alias_reference(self.alias)


@dataclass(frozen=True, slots=True)
class SetDefaultModelCommand:
    """Make one existing endpoint the store's default."""

    session: SessionRef
    alias: str

    def __post_init__(self) -> None:
        _validate_alias_reference(self.alias)


@dataclass(frozen=True, slots=True)
class TestModelCommand:
    """Probe one endpoint with a minimal request."""

    session: SessionRef
    alias: str

    def __post_init__(self) -> None:
        _validate_alias_reference(self.alias)


@dataclass(frozen=True, slots=True)
class TestModelResult:
    """Outcome of one connectivity probe.

    ``error`` is a bounded, redacted message: it never contains an API key,
    request headers, or the raw upstream body.  The probe never raises, so a
    failure is always reported here instead of as a service error.
    """

    ok: bool
    latency_ms: int
    error: str | None
