"""ACP prompt content decoding with explicit capability and size boundaries."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Any

from acp.schema import (
    AudioContentBlock,
    EmbeddedResourceContentBlock,
    ImageContentBlock,
    ResourceLink,
    TextContentBlock,
)


class ACPContentError(ValueError):
    """A prompt content block cannot be represented by the runtime."""


@dataclass(frozen=True, slots=True)
class ACPAttachment:
    """A bounded binary or linked prompt attachment."""

    kind: str
    data: bytes | None = None
    mime_type: str | None = None
    uri: str | None = None
    name: str | None = None
    description: str | None = None


@dataclass(frozen=True, slots=True)
class ACPPromptContent:
    """Runtime-neutral prompt projection kept separate from provider chunks."""

    text: str
    attachments: tuple[ACPAttachment, ...] = ()


def render_resource_links(content: ACPPromptContent) -> str:
    """Render baseline linked resources into explicit, provider-neutral text."""
    links = [attachment for attachment in content.attachments if attachment.kind == "resource_link"]
    embedded_text = [
        attachment
        for attachment in content.attachments
        if attachment.kind == "embedded_text" and attachment.data is not None
    ]
    if not links and not embedded_text:
        return content.text
    rendered = [content.text] if content.text else []
    for link in links:
        label = link.name or link.uri or "resource"
        details = [f"[resource: {label}]", f"URI: {link.uri or ''}"]
        if link.description:
            details.append(f"Description: {link.description}")
        rendered.append("\n".join(details))
    for resource in embedded_text:
        assert resource.data is not None
        rendered.append(resource.data.decode("utf-8"))
    return "\n\n".join(rendered).strip()


def to_runtime_attachments(content: ACPPromptContent) -> tuple[Any, ...]:
    """Convert supported binary ACP blocks to Synapse's attachment type."""
    from synapse.content.multimodal import Attachment

    converted: list[Attachment] = []
    for index, attachment in enumerate(content.attachments, start=1):
        if attachment.kind != "image":
            continue
        if attachment.data is None or not attachment.mime_type:
            raise ACPContentError("image content is missing data or mime type")
        converted.append(
            Attachment(
                id=index,
                name=f"acp-image-{index}",
                mime=attachment.mime_type,
                data=attachment.data,
                source="acp",
            )
        )
    return tuple(converted)


def decode_prompt_content(
    blocks: list[Any],
    *,
    allow_image: bool = False,
    allow_audio: bool = False,
    allow_embedded_context: bool = False,
    max_attachment_bytes: int = 4_000_000,
) -> ACPPromptContent:
    """Decode ACP prompt blocks without silently dropping unsupported content.

    Text and resource links are ACP baseline content. Images, audio, and embedded
    resources are accepted only when their matching capability is explicitly
    enabled by the caller.
    """
    if max_attachment_bytes <= 0:
        raise ValueError("max_attachment_bytes must be positive")

    text_parts: list[str] = []
    attachments: list[ACPAttachment] = []
    for block in blocks:
        if isinstance(block, TextContentBlock):
            text_parts.append(block.text)
        elif isinstance(block, ResourceLink):
            attachments.append(
                ACPAttachment(
                    kind="resource_link",
                    uri=block.uri,
                    name=block.name,
                    mime_type=block.mime_type,
                    description=block.description,
                )
            )
        elif isinstance(block, ImageContentBlock):
            if not allow_image:
                raise ACPContentError("image prompt content is not enabled")
            attachments.append(
                _decode_media(block.data, block.mime_type, "image", max_attachment_bytes, block.uri)
            )
        elif isinstance(block, AudioContentBlock):
            if not allow_audio:
                raise ACPContentError("audio prompt content is not enabled")
            attachments.append(
                _decode_media(block.data, block.mime_type, "audio", max_attachment_bytes, None)
            )
        elif isinstance(block, EmbeddedResourceContentBlock):
            if not allow_embedded_context:
                raise ACPContentError("embedded resource prompt content is not enabled")
            attachments.append(_decode_resource(block, max_attachment_bytes))
        else:
            raise ACPContentError(f"unsupported prompt content block: {type(block).__name__}")

    return ACPPromptContent(text="\n".join(text_parts).strip(), attachments=tuple(attachments))


def _decode_media(
    encoded: str,
    mime_type: str,
    kind: str,
    max_bytes: int,
    uri: str | None,
) -> ACPAttachment:
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ACPContentError(f"invalid base64 {kind} content") from exc
    if not data:
        raise ACPContentError(f"empty {kind} content")
    if len(data) > max_bytes:
        raise ACPContentError(f"{kind} content exceeds {max_bytes} bytes")
    return ACPAttachment(kind=kind, data=data, mime_type=mime_type, uri=uri)


def _decode_resource(block: EmbeddedResourceContentBlock, max_bytes: int) -> ACPAttachment:
    resource = block.resource
    if hasattr(resource, "text"):
        data = resource.text.encode("utf-8")
        if len(data) > max_bytes:
            raise ACPContentError(f"embedded text exceeds {max_bytes} bytes")
        return ACPAttachment(
            kind="embedded_text",
            data=data,
            mime_type=resource.mime_type,
            uri=resource.uri,
        )
    return _decode_media(
        resource.blob,
        resource.mime_type or "application/octet-stream",
        "embedded_blob",
        max_bytes,
        resource.uri,
    )
