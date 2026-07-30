"""Model-facing image description tool for text-only primary models."""

from __future__ import annotations

import base64
from typing import Any

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from synapse.content.multimodal import EXT_TO_MIME
from synapse.integrations.describe_image import (
    VisionModelClient,
    VisionModelConfig,
    VisionModelError,
)


class DescribeImageInput(BaseModel):
    """Arguments for the image description tool."""

    image_path: str = Field(
        description=(
            "Workspace image path, or an HTTP(S) URL when remote image URLs are enabled."
        )
    )
    prompt: str | None = Field(
        default=None,
        description="Optional detail to focus on, such as visible errors or text.",
    )


def build_describe_image_tool(
    backend: Any,
    config: VisionModelConfig | None,
) -> Any:
    """Create a workspace-aware image description tool."""
    client = VisionModelClient(config) if config is not None else None

    @tool("describe_image", args_schema=DescribeImageInput)
    async def describe_image(image_path: str, prompt: str | None = None) -> str:
        """Describe an image with the configured vision model.

        Use this for workspace screenshots, diagrams, or other image files when the
        primary model cannot inspect images directly. The path must refer to an image
        inside the workspace. HTTP(S) URLs require ``vision_model.allow_remote_urls``.
        """
        if client is None:
            return "Image description is unavailable: vision_model is not configured."

        source = image_path.strip()
        if not source:
            return "Image description failed: image_path must not be empty."
        try:
            if source.startswith(("http://", "https://")):
                return await client.describe_url(source, extra_prompt=prompt)

            response = backend.download_files([source])[0]
            error = getattr(response, "error", None)
            content = getattr(response, "content", None)
            if error or content is None:
                return f"Image description failed: {error or 'file could not be read'}."
            if len(content) > client.config.max_input_bytes:
                return (
                    "Image description failed: image is too large "
                    f"({len(content)} bytes; max {client.config.max_input_bytes})."
                )
            suffix = source.rsplit(".", 1)[-1].casefold() if "." in source else ""
            mime = EXT_TO_MIME.get(f".{suffix}")
            if mime is None:
                return "Image description failed: unsupported image extension."
            encoded = base64.standard_b64encode(content).decode("ascii")
            return await client.describe_data_url(
                f"data:{mime};base64,{encoded}",
                extra_prompt=prompt,
            )
        except VisionModelError as exc:
            return f"Image description failed: {exc}."

    return describe_image


def build_describe_image_tools(
    *,
    image_input: bool | None,
    backend: Any,
    config: VisionModelConfig | None,
) -> list[Any]:
    """Build the tool only when the profile explicitly disables image input."""
    if image_input is not False:
        return []
    return [build_describe_image_tool(backend, config)]
