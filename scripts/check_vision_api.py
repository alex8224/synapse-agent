"""Call the configured vision model once with a generated PNG smoke image."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from pathlib import Path

from synapse.config import load_settings
from synapse.describe_image import VisionModelClient, VisionModelConfig
from synapse.models_registry import load_merged_models_registry


def _sample_png() -> bytes:
    # 1x1 PNG with a valid, deterministic payload. The vision model can still
    # verify that the endpoint accepts image input and returns a description.
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )


async def _run(workspace: Path) -> int:
    settings = load_settings(workspace=workspace)
    registry = load_merged_models_registry(settings)
    config = VisionModelConfig.from_registry(registry, settings)
    if config is None:
        print("FAIL: vision_model configuration is missing", file=sys.stderr)
        return 2
    if not config.api_key:
        print("FAIL: vision_model API key is missing", file=sys.stderr)
        return 2

    data_url = "data:image/png;base64," + base64.b64encode(_sample_png()).decode("ascii")
    client = VisionModelClient(config)
    try:
        description = await client.describe_data_url(data_url)
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "status": "FAIL",
                    "model": config.model,
                    "base_url": config.base_url,
                    "think": config.think,
                    "error": str(exc),
                },
                ensure_ascii=False,
            )
        )
        return 1

    print(
        json.dumps(
            {
                "status": "OK",
                "model": config.model,
                "base_url": config.base_url,
                "think": config.think,
                "description": description,
            },
            ensure_ascii=False,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-w", "--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args()
    return asyncio.run(_run(args.workspace.resolve()))


if __name__ == "__main__":
    raise SystemExit(main())
