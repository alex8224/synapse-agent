"""P2 ACP prompt ContentBlock codec tests."""

from __future__ import annotations

import base64

import pytest
from acp.schema import (
    AudioContentBlock,
    EmbeddedResourceContentBlock,
    ImageContentBlock,
    ResourceLink,
    TextContentBlock,
    TextResourceContents,
)

from synapse.acp.content import (
    ACPContentError,
    decode_prompt_content,
    render_resource_links,
    to_runtime_attachments,
)


def test_text_and_resource_link_are_decoded_without_dropping_metadata() -> None:
    result = decode_prompt_content(
        [
            TextContentBlock(type="text", text="hello"),
            ResourceLink(
                type="resource_link",
                uri="file:///workspace/readme.md",
                name="readme.md",
                description="project readme",
            ),
        ]
    )
    assert result.text == "hello"
    assert len(result.attachments) == 1
    assert result.attachments[0].kind == "resource_link"
    assert result.attachments[0].uri == "file:///workspace/readme.md"
    assert result.attachments[0].description == "project readme"
    rendered = render_resource_links(result)
    assert "URI: file:///workspace/readme.md" in rendered
    assert "Description: project readme" in rendered


def test_image_audio_and_embedded_resource_require_capabilities() -> None:
    image = ImageContentBlock(
        type="image",
        data=base64.b64encode(b"image").decode("ascii"),
        mimeType="image/png",
    )
    audio = AudioContentBlock(
        type="audio",
        data=base64.b64encode(b"audio").decode("ascii"),
        mimeType="audio/wav",
    )
    embedded = EmbeddedResourceContentBlock(
        type="resource",
        resource=TextResourceContents(uri="file:///context.txt", text="context"),
    )

    for block in (image, audio, embedded):
        with pytest.raises(ACPContentError, match="not enabled"):
            decode_prompt_content([block])

    result = decode_prompt_content(
        [image, audio, embedded],
        allow_image=True,
        allow_audio=True,
        allow_embedded_context=True,
    )
    assert [attachment.kind for attachment in result.attachments] == [
        "image",
        "audio",
        "embedded_text",
    ]
    runtime_attachments = to_runtime_attachments(result)
    assert len(runtime_attachments) == 1
    assert runtime_attachments[0].source == "acp"
    assert runtime_attachments[0].data == b"image"


def test_binary_content_is_strictly_base64_and_bounded() -> None:
    block = ImageContentBlock(type="image", data="not-base64", mimeType="image/png")
    with pytest.raises(ACPContentError, match="invalid base64"):
        decode_prompt_content([block], allow_image=True)

    oversized = ImageContentBlock(
        type="image",
        data=base64.b64encode(b"12345").decode("ascii"),
        mimeType="image/png",
    )
    with pytest.raises(ACPContentError, match="exceeds 4 bytes"):
        decode_prompt_content([oversized], allow_image=True, max_attachment_bytes=4)


def test_empty_or_unknown_prompt_content_is_rejected() -> None:
    assert decode_prompt_content([]).text == ""
    with pytest.raises(ACPContentError, match="unsupported"):
        decode_prompt_content([object()])
