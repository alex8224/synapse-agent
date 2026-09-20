"""The speech engines this daemon can run, as data rather than as branches.

The console picks an engine by id, so adding one must not mean editing the card,
the service and the settings screen in three places.  This registry is the single
list: the service resolves an id through it, and the wire hands the same entries to
the console, which paints whatever it is given.

Three kinds exist today, and they differ in *where* the work happens rather than in
what the reader sees:

- ``browser`` -- the console's own recognizer.  No daemon involvement at all; it is
  listed here so the settings screen can offer it beside the others.
- ``local`` -- the offline ONNX engine (:mod:`synapse.stt.engine`).  Needs the
  optional extra and a model set, and pays a one-off build.
- ``cloud`` -- a hosted streaming service.  Needs a key, no local compute, and the
  audio leaves the machine -- which is exactly what the label must say.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["PROVIDERS", "SttProviderInfo", "provider_info", "provider_ids"]


@dataclass(frozen=True)
class SttProviderInfo:
    """One selectable engine, in the shape both the service and the console use."""

    #: Stable id: the value stored in settings and sent over the wire.
    id: str
    #: What the settings screen shows.
    label: str
    #: ``browser`` / ``local`` / ``cloud`` -- decides what the console may assume
    #: (a cloud engine has no warm-up, a local one has no key, and so on).
    kind: str
    #: Whether the engine needs a credential the reader must supply.
    needs_key: bool = False
    #: Whether the engine needs model files on this machine.
    needs_models: bool = False
    #: One line the settings screen shows under the choice.
    detail: str = ""


PROVIDERS: tuple[SttProviderInfo, ...] = (
    SttProviderInfo(
        id="browser",
        label="浏览器内置",
        kind="browser",
        detail="浏览器自带的识别（Chrome / Edge），免费、零配置；音频由浏览器厂商识别。",
    ),
    SttProviderInfo(
        id="local",
        label="本地离线引擎",
        kind="local",
        needs_models=True,
        detail="本机 ONNX 模型，完全离线、零费用；首次使用需加载模型（约 1 分钟）。",
    ),
    SttProviderInfo(
        id="doubao",
        label="豆包流式识别（在线）",
        kind="cloud",
        needs_key=True,
        detail="火山引擎豆包大模型流式识别：中文最强、支持热词与上下文；按小时计费，音频会上传到服务商。",
    ),
)


def provider_info(provider_id: str) -> SttProviderInfo | None:
    """The entry for one id, or None when the id is unknown.

    Unknown ids are answered rather than raised on purpose: a settings file written
    by a newer build (or a typo) must degrade to "no such engine" in the console,
    not break every status read.
    """
    for info in PROVIDERS:
        if info.id == provider_id:
            return info
    return None


def provider_ids() -> tuple[str, ...]:
    """Every selectable id, in the order the console should offer them."""
    return tuple(info.id for info in PROVIDERS)
