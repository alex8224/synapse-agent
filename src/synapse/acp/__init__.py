"""Official ACP adapter for Synapse."""

from synapse.acp.agent import SynapseACPAgent
from synapse.acp.content import (
    ACPAttachment,
    ACPContentError,
    ACPPromptContent,
    decode_prompt_content,
    render_resource_links,
    to_runtime_attachments,
)
from synapse.acp.events import ACPEventBridge, ACPEventBridgeClosed, ACPEventBridgeStats
from synapse.acp.permissions import (
    ACPPermissionError,
    PermissionCoordinator,
    PermissionDecision,
)
from synapse.acp.sessions import (
    ACPManagedSession,
    ACPSessionDescriptor,
    ACPSessionRegistry,
)
from synapse.acp.updates import ACPUpdateProjector, project_update, project_updates

__all__ = [
    "ACPManagedSession",
    "ACPAttachment",
    "ACPContentError",
    "ACPEventBridge",
    "ACPEventBridgeClosed",
    "ACPEventBridgeStats",
    "ACPPermissionError",
    "ACPUpdateProjector",
    "ACPPromptContent",
    "PermissionCoordinator",
    "PermissionDecision",
    "ACPSessionDescriptor",
    "ACPSessionRegistry",
    "SynapseACPAgent",
    "decode_prompt_content",
    "render_resource_links",
    "project_update",
    "project_updates",
    "to_runtime_attachments",
]
