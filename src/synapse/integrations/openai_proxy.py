"""Windows system-proxy fallback for OpenAI OAuth and Codex HTTPS requests.

httpx honors environment proxies but does not discover WinINet registry settings.
GUI processes may have no HTTPS_PROXY even when the user's system proxy is enabled.
"""

from __future__ import annotations

import os
import urllib.request
from urllib.parse import urlsplit


def openai_proxy_kwargs(url: str) -> dict[str, str]:
    """Use the Windows system proxy only when httpx has no HTTPS env proxy.

    Explicit environment settings retain precedence. The system's per-host
    exclusions and NO_PROXY are respected; the proxy address is never logged.
    """
    if os.name != "nt" or any(
        os.environ.get(name) for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")
    ):
        return {}
    host = urlsplit(url).hostname
    if host is None:
        return {}
    if (os.environ.get("NO_PROXY") or os.environ.get("no_proxy")) and (
        urllib.request.proxy_bypass_environment(host)
    ):
        return {}
    try:
        if urllib.request.proxy_bypass_registry(host):
            return {}
        proxies = urllib.request.getproxies_registry()
    except (AttributeError, OSError, ValueError):
        return {}
    proxy = proxies.get("https") or proxies.get("http")
    return {"proxy": proxy} if proxy else {}
