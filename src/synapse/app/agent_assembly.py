"""Typed resource and middleware assembly helpers for the coding agent.

The public ``build_coding_agent`` entry point remains in ``synapse.app.agent``
for compatibility. This module owns the reusable assembly contracts so the
entry point is a composition root rather than a domain implementation.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from synapse.content.environment import build_context_sections
from synapse.content.prompt_sections import render_system_prompt, stable_prefix
from synapse.content.prompts import build_system_prompt_sections
from synapse.integrations.describe_image import VisionModelConfig
from synapse.models.registry import model_supports_image_input
from synapse.runtime.fs_permissions import build_filesystem_permissions
from synapse.runtime.harness import apply_harness_exclusions
from synapse.runtime.middleware import (
    build_compact_tool_descriptions,
    build_intent_schema_middleware,
    build_model_retry_middleware,
    build_path_normalize_middleware,
    build_strip_redundant_prompt_blocks,
    build_task_namespace_middleware,
    build_tool_error_recovery_middleware,
    build_tool_exclusion_middleware,
)
from synapse.runtime.model_request_compression_middleware import (
    build_model_request_compression_middleware,
)
from synapse.runtime.prompt_cache_boundary_middleware import (
    build_prompt_cache_boundary_middleware,
)
from synapse.runtime.steer import SteerQueue, build_steer_middleware
from synapse.runtime.tool_contract import readonly_excluded_tools
from synapse.tool_output.pipeline import ToolOutputTransformPipeline
from synapse.tool_output.repository import ToolOutputRepository
from synapse.tool_output.transformers import load_transformer_plugins


@dataclass(slots=True)
class AgentResources:
    """Expensive and project-scoped resources used by one agent graph."""

    root: Path
    backend: Any
    model: Any
    model_registry: Any
    model_spec: Any
    selected_profile: Any
    checkpointer: Any
    permissions: Any
    tools: list[Any] = field(default_factory=list)
    tool_output_repository: ToolOutputRepository | None = None
    vision_config: VisionModelConfig | None = None
    primary_image_input: bool = False
    goal_service: Any | None = None
    knowledge_base: Any | None = None
    long_term_memory: Any | None = None
    subagents: Any | None = None
    steer_queue: SteerQueue | None = None
    mcp_deferred: bool = False
    mcp_servers: list[str] = field(default_factory=list)
    mcp_warnings: list[str] = field(default_factory=list)
    mcp_tool_names: list[str] = field(default_factory=list)

    def apply_model_exclusions(self, settings: Any) -> set[str]:
        """Return the final model-request tool exclusion set."""
        effective_excluded = list(getattr(settings, "excluded_tools", []) or [])
        if getattr(settings, "minimal_filesystem_tools", False):
            minimal_excluded = getattr(settings, "minimal_filesystem_excluded_tools", []) or []
            effective_excluded.extend(minimal_excluded)

        apply_harness_exclusions(
            self.model_spec,
            readonly=settings.readonly,
            excluded_tools=effective_excluded,
        )
        excluded = set(effective_excluded) | {"ls", "glob", "grep"}
        if settings.readonly:
            excluded.update(readonly_excluded_tools())
        return excluded

    def ensure_steer_queue(self) -> SteerQueue:
        if self.steer_queue is None:
            self.steer_queue = SteerQueue()
        return self.steer_queue


@dataclass(frozen=True, slots=True)
class MiddlewareContext:
    """Inputs required to build the ordered middleware stack."""

    settings: Any
    project_root: Path
    model: Any
    output_repository: ToolOutputRepository
    primary_image_input: bool
    vision_config: VisionModelConfig
    model_request_excluded_tools: set[str]
    goal_enabled: bool
    goal_service: Any | None
    steer_queue: SteerQueue
    prompt_cache_key: Any | None = None
    # Rendered stable prefix of the system prompt, when a real stable/dynamic
    # boundary exists. Empty when the caller supplied its own prompt, or when the
    # dynamic context sections are disabled -- see ``resolve_system_prompt``.
    prompt_stable_prefix: str = ""
    # Turbo mode: model traffic is routed through a headroom-turbo proxy, so
    # the built-in reversible tool-output compression is skipped (they are
    # mutually exclusive).
    turbo: bool = False


def build_tool_output_pipeline(settings: Any) -> ToolOutputTransformPipeline:
    """Deprecated: build the reversible tool-output pipeline with plugin fallback.

    The transform middleware is no longer registered; this helper is kept for
    compatibility and for anyone reviving the feature.
    """
    try:
        return ToolOutputTransformPipeline(
            transformers=load_transformer_plugins(settings.tool_output_transform_plugins),
            disabled_types=set(settings.tool_output_disabled_types),
            use_native=settings.enable_native_tool_output_compression,
        )
    except Exception:  # noqa: BLE001 - optional plugins must not block startup
        return ToolOutputTransformPipeline(
            disabled_types=set(settings.tool_output_disabled_types),
            use_native=settings.enable_native_tool_output_compression,
        )


def build_agent_middleware(context: MiddlewareContext) -> list[Any]:
    """Build the ordered middleware stack in one testable place."""
    from synapse.app.agent_md import build_agent_md_middleware
    from synapse.content.prompts import build_system_prompt  # noqa: F401
    from synapse.goals.middleware import build_goal_middleware
    from synapse.integrations.vision_middleware import build_describe_image_middleware
    from synapse.runtime.filesystem_tool_prompt_middleware import (
        build_filesystem_tool_prompt_middleware,
    )
    from synapse.runtime.image_window_middleware import build_image_window_middleware
    from synapse.runtime.session_header_middleware import (
        build_session_header_middleware,
    )

    settings = context.settings
    middleware: list[Any] = [
        # Publish the active thread id so the httpx layer can stamp
        # X-Session-ID / Session-Id on every model request (gateway affinity).
        build_session_header_middleware(),
        # Prune stale images before anything else reads the request: screenshot
        # histories otherwise dominate the wire payload on every turn.
        build_image_window_middleware(model=context.model),
        build_agent_md_middleware(context.project_root),
        build_filesystem_tool_prompt_middleware(context.model_request_excluded_tools),
        build_describe_image_middleware(
            image_input=context.primary_image_input,
            config=context.vision_config,
        ),
        build_model_retry_middleware(),
        build_task_namespace_middleware(),
        build_tool_exclusion_middleware(context.model_request_excluded_tools),
        build_goal_middleware(
            enabled=context.goal_enabled,
            service=context.goal_service,
        ),
    ]
    # Deprecated: the reversible tool-output transform middleware
    # (build_tool_output_transform_middleware / build_tool_output_usage_middleware)
    # is intentionally NOT registered for now. The pipeline and read_tool_result
    # stay in place for compatibility; re-enable by appending those builders
    # here when the feature is revived.
    middleware.extend(
        [
            build_tool_error_recovery_middleware(),
            build_path_normalize_middleware(context.project_root),
            *build_intent_schema_middleware(),
            build_steer_middleware(context.steer_queue),
        ]
    )
    middleware.extend(
        [
            build_strip_redundant_prompt_blocks(),
            build_compact_tool_descriptions(),
            build_model_request_compression_middleware(context.output_repository),
        ]
    )
    if context.prompt_stable_prefix and getattr(
        settings, "enable_prompt_cache_boundary", False
    ):
        middleware.append(
            build_prompt_cache_boundary_middleware(context.prompt_stable_prefix)
        )
    if getattr(context.model, "_synapse_openai_oauth", False) is True:
        from synapse.integrations.openai_oauth_middleware import (
            build_openai_oauth_compat_middleware,
        )

        middleware.append(
            build_openai_oauth_compat_middleware(
                fast_mode=lambda: bool(getattr(settings, "openai_fast_mode", False)),
                prompt_cache_key=context.prompt_cache_key,
            )
        )
    from synapse.observability.llm_debug import get_debug_store
    from synapse.runtime.debug_capture_middleware import build_debug_capture_middleware

    middleware.append(build_debug_capture_middleware(get_debug_store()))
    return middleware


def resolve_image_context(
    *,
    model_spec: Any,
    selected_profile: Any,
    registry: Any,
    settings: Any,
) -> tuple[bool, VisionModelConfig]:
    """Resolve primary and fallback vision model configuration."""
    primary = model_supports_image_input(
        model_spec,
        getattr(selected_profile, "image_input", None),
        getattr(selected_profile, "base_url", None) or getattr(settings, "openai_base_url", None),
    )
    return primary, VisionModelConfig.from_registry(registry, settings)


def resolve_system_prompt(
    *,
    system_prompt: str | None,
    root: Path,
    shell_executable: str | None,
    excluded_tools: Iterable[str] | None,
    model_spec: Any,
    include_dynamic_context: bool = False,
) -> tuple[str, str]:
    """Return the rendered system prompt and its cacheable stable prefix.

    ``include_dynamic_context`` appends the per-build environment, git, and date
    sections. They change on every build, so they are only worth their prompt
    tokens when the caller also splits the rendered prompt at the returned stable
    prefix (``enable_prompt_cache_boundary``): without that split a volatile tail
    sits inside the cached system prefix and invalidates it on every build. Left
    off, the rendered prompt is byte-identical to the pre-registry prompt.

    The stable prefix is empty when no real boundary exists: either the caller
    supplied its own prompt, or every section is stable (nothing follows it).
    """
    if system_prompt is not None:
        return system_prompt, ""
    sections = build_system_prompt_sections(
        root,
        shell_executable=shell_executable,
        excluded_tools=excluded_tools,
    )
    if include_dynamic_context:
        sections = [
            *sections,
            *build_context_sections(root, shell=shell_executable, model_spec=model_spec),
        ]
    prompt = render_system_prompt(sections)
    prefix = stable_prefix(sections)
    # A prefix equal to the whole prompt is not a split point: splitting there
    # would only re-tag the message without freeing anything from the cache.
    return prompt, prefix if len(prefix) < len(prompt) else ""


def build_permissions(settings: Any) -> Any:
    """Build filesystem permissions independently from graph compilation."""
    return build_filesystem_permissions(
        enabled=settings.enable_fs_permissions,
        readonly=settings.readonly,
        deny_paths=settings.deny_fs_paths,
    )