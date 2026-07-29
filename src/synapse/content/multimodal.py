"""Multimodal user content: image placeholders, clipboard, compose blocks.

Placeholder syntax in the prompt: ``[image#1]``, ``[image#2]``, ...
Each id maps to an in-memory (or file-backed) attachment held by the TUI composer.
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

PLACEHOLDER_RE = re.compile(r"\[image#(\d+)\]", re.IGNORECASE)

# Common vision-friendly types.
ALLOWED_MIME = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/jpg",
        "image/webp",
        "image/gif",
        "image/bmp",
    }
)

EXT_TO_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}

MIME_TO_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
}


class AttachmentError(ValueError):
    """Invalid image attachment."""


@dataclass(frozen=True, slots=True)
class Attachment:
    """One image ready to embed in a user message."""

    id: int
    name: str
    mime: str
    data: bytes
    source: str = "clipboard"  # clipboard | file | path-text

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def placeholder(self) -> str:
        return f"[image#{self.id}]"

    def data_url(self) -> str:
        b64 = base64.standard_b64encode(self.data).decode("ascii")
        mime = "image/jpeg" if self.mime == "image/jpg" else self.mime
        return f"data:{mime};base64,{b64}"


@dataclass
class ImageBank:
    """Composer-side store: placeholder id -> attachment."""

    items: dict[int, Attachment] = field(default_factory=dict)
    _next_id: int = 1
    max_images: int = 8
    max_bytes: int = 4_000_000

    def clear(self) -> None:
        self.items.clear()
        self._next_id = 1

    def remove(self, image_id: int) -> Attachment | None:
        """Drop one attachment by id. Returns removed item or None."""
        return self.items.pop(int(image_id), None)

    def __len__(self) -> int:
        return len(self.items)

    def next_id(self) -> int:
        return self._next_id

    def add_bytes(
        self,
        data: bytes,
        *,
        mime: str,
        name: str | None = None,
        source: str = "clipboard",
    ) -> Attachment:
        if not data:
            raise AttachmentError("empty image data")
        mime_n = (mime or "").strip().lower()
        if mime_n == "image/jpg":
            mime_n = "image/jpeg"
        if mime_n not in ALLOWED_MIME:
            raise AttachmentError(f"unsupported image type: {mime_n or '?'}")
        if len(data) > self.max_bytes:
            raise AttachmentError(
                f"image too large: {len(data)} bytes (max {self.max_bytes})"
            )
        if len(self.items) >= self.max_images:
            raise AttachmentError(f"too many images (max {self.max_images})")
        idx = self._next_id
        self._next_id += 1
        ext = MIME_TO_EXT.get(mime_n, ".png")
        att = Attachment(
            id=idx,
            name=name or f"clipboard-{idx}{ext}",
            mime=mime_n,
            data=data,
            source=source,
        )
        self.items[idx] = att
        return att

    def add_path(self, path: Path | str) -> Attachment:
        p = Path(path).expanduser()
        if not p.is_file():
            raise AttachmentError(f"not a file: {p}")
        mime = EXT_TO_MIME.get(p.suffix.lower())
        if not mime:
            raise AttachmentError(f"unsupported extension: {p.suffix or '(none)'}")
        data = p.read_bytes()
        return self.add_bytes(data, mime=mime, name=p.name, source="file")

    def summary_line(self) -> str:
        if not self.items:
            return ""
        parts = [f"#{a.id} {a.name} ({_fmt_size(a.size)})" for a in self.items.values()]
        return "images: " + ", ".join(parts)


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f}KB"
    return f"{n / (1024 * 1024):.1f}MB"


def find_placeholders(text: str) -> list[int]:
    """Return placeholder ids in order of appearance (may repeat)."""
    return [int(m.group(1)) for m in PLACEHOLDER_RE.finditer(text or "")]


def strip_placeholder(text: str, image_id: int) -> str:
    """Remove ``[image#N]`` tokens for one id (and surrounding extra spaces)."""
    raw = text or ""
    out = re.sub(
        rf"\s*\[image#{int(image_id)}\]",
        "",
        raw,
        flags=re.IGNORECASE,
    )
    # Collapse leftover double-spaces but keep intentional newlines out of scope
    # (composer is single-line input).
    out = re.sub(r"[ \t]{2,}", " ", out).strip(" ")
    return out


def extract_image_payloads(
    content: Any,
    *,
    max_images: int = 8,
    max_bytes: int = 4_000_000,
) -> list[tuple[bytes, str]]:
    """Pull (raw_bytes, mime) pairs from multimodal user content blocks.

    Supports OpenAI ``image_url`` data-URLs and Anthropic ``image`` base64
    sources. Oversized / invalid blocks are skipped.
    """
    if not isinstance(content, list):
        return []
    out: list[tuple[bytes, str]] = []
    for block in content:
        if len(out) >= max_images:
            break
        if not isinstance(block, dict):
            continue
        btype = str(block.get("type") or "").casefold()
        try:
            if btype == "image_url":
                url_obj = block.get("image_url")
                url = ""
                if isinstance(url_obj, dict):
                    url = str(url_obj.get("url") or "")
                elif isinstance(url_obj, str):
                    url = url_obj
                parsed = _decode_data_url(url)
                if parsed is None:
                    continue
                data, mime = parsed
                if 0 < len(data) <= max_bytes:
                    out.append((data, mime))
            elif btype == "image":
                # Anthropic native / LC-style
                src = block.get("source")
                if isinstance(src, dict) and str(src.get("type") or "") == "base64":
                    mime = str(src.get("media_type") or src.get("mime_type") or "image/png")
                    raw_b64 = str(src.get("data") or "")
                    data = base64.standard_b64decode(raw_b64)
                    if 0 < len(data) <= max_bytes:
                        out.append((data, "image/jpeg" if mime == "image/jpg" else mime))
                elif block.get("base64"):
                    mime = str(block.get("mime_type") or block.get("media_type") or "image/png")
                    data = base64.standard_b64decode(str(block.get("base64")))
                    if 0 < len(data) <= max_bytes:
                        out.append((data, "image/jpeg" if mime == "image/jpg" else mime))
        except Exception:  # noqa: BLE001
            continue
    return out


def _decode_data_url(url: str) -> tuple[bytes, str] | None:
    raw = (url or "").strip()
    if not raw.startswith("data:") or ";base64," not in raw:
        return None
    header, b64 = raw.split(";base64,", 1)
    mime = header[5:] if header.startswith("data:") else "image/png"
    mime = (mime or "image/png").strip().lower() or "image/png"
    if mime == "image/jpg":
        mime = "image/jpeg"
    try:
        data = base64.standard_b64decode(b64)
    except Exception:  # noqa: BLE001
        return None
    if not data:
        return None
    return data, mime


def insert_at(text: str, index: int, token: str) -> tuple[str, int]:
    """Insert token at index; return (new_text, new_cursor)."""
    raw = text or ""
    pos = max(0, min(int(index), len(raw)))
    out = raw[:pos] + token + raw[pos:]
    return out, pos + len(token)


def compose_user_content(
    text: str,
    bank: ImageBank | None = None,
    *,
    attachments: list[Attachment] | None = None,
    provider: str = "openai",
) -> str | list[dict[str, Any]]:
    """Build message content from prompt text + image bank or explicit list.

    - ``attachments=[]`` / no images -> plain string (legacy path).
    - ``attachments`` with ``[image#N]`` in text -> interleave at placeholders.
    - ``attachments`` without placeholders -> text then images.
    - ``bank``: resolve placeholders from bank.
    """
    raw = text or ""
    if attachments is not None:
        if not attachments:
            return raw
        return _compose_with_attachments(raw, attachments, provider=provider)
    if bank is not None:
        return _compose_from_placeholders(raw, bank, provider=provider)
    return raw


def _bank_from_attachments(attachments: list[Attachment]) -> ImageBank:
    """Build a temporary bank keyed by attachment id (for placeholder resolve)."""
    bank = ImageBank()
    for att in attachments:
        bank.items[int(att.id)] = att
    if bank.items:
        bank._next_id = max(bank.items) + 1
    return bank


def _compose_with_attachments(
    text: str,
    attachments: list[Attachment],
    *,
    provider: str,
) -> str | list[dict[str, Any]]:
    """Prefer placeholder interleave; otherwise append images after text."""
    raw = text or ""
    if find_placeholders(raw):
        return _compose_from_placeholders(
            raw, _bank_from_attachments(attachments), provider=provider
        )
    return _compose_ordered_blocks(raw, attachments, provider=provider)


def _compose_ordered_blocks(
    text: str,
    attachments: list[Attachment],
    *,
    provider: str,
) -> str | list[dict[str, Any]]:
    """Text (or default caption) followed by images — no placeholder tokens."""
    raw = text or ""
    # Drop stale placeholders if present without a matching interleave path.
    cleaned = PLACEHOLDER_RE.sub("", raw)
    cleaned = " ".join(cleaned.split()).strip()
    blocks: list[dict[str, Any]] = []
    if cleaned:
        blocks.append({"type": "text", "text": cleaned})
    elif attachments:
        blocks.append({"type": "text", "text": "(see attached image)"})
    for att in attachments:
        blocks.append(_image_block(att, provider=provider))
    return blocks


def _compose_from_placeholders(
    text: str,
    bank: ImageBank,
    *,
    provider: str,
) -> str | list[dict[str, Any]]:
    raw = text or ""
    ids = find_placeholders(raw)
    if not ids:
        return raw

    missing = sorted({i for i in ids if i not in bank.items})
    if missing:
        raise AttachmentError(
            "missing images for placeholders: "
            + ", ".join(f"[image#{i}]" for i in missing)
        )

    blocks: list[dict[str, Any]] = []
    pos = 0
    for m in PLACEHOLDER_RE.finditer(raw):
        if m.start() > pos:
            seg = raw[pos : m.start()]
            if seg:
                blocks.append({"type": "text", "text": seg})
        att = bank.items[int(m.group(1))]
        blocks.append(_image_block(att, provider=provider))
        pos = m.end()
    if pos < len(raw):
        tail = raw[pos:]
        if tail:
            blocks.append({"type": "text", "text": tail})

    if not any(b.get("type") == "text" and str(b.get("text") or "").strip() for b in blocks):
        blocks.insert(0, {"type": "text", "text": "(see attached image)"})
    return blocks


def normalize_provider_family(provider: str | None) -> str:
    """Map provider / model prefix to a content-block family.

    Families:
    - ``anthropic``: Anthropic Messages image source blocks
    - ``google``: Google GenAI / Vertex (accepts OpenAI-style image_url)
    - ``openai``: OpenAI Chat Completions image_url (default for most gateways)
    """
    raw = (provider or "openai").strip().lower()
    if not raw:
        return "openai"
    # Allow full model ids like ``anthropic:claude-...``.
    prefix = raw.split(":", 1)[0].strip() if ":" in raw else raw
    if prefix in {"anthropic", "claude"} or prefix.startswith("anthropic"):
        return "anthropic"
    if prefix in {
        "google",
        "google_genai",
        "google_vertexai",
        "gemini",
        "vertexai",
    } or prefix.startswith("google"):
        return "google"
    # openai / azure_openai / deepseek / groq / together / openrouter / ...
    return "openai"


def _image_block(att: Attachment, *, provider: str) -> dict[str, Any]:
    """Shape an image content block for the resolved provider family.

    Most OpenAI-compatible gateways (DeepSeek, Qwen, Groq, OpenRouter, ...) and
    Google GenAI accept ``image_url`` data-URLs. Anthropic uses native
    ``image`` + ``source.base64`` blocks.
    """
    family = normalize_provider_family(provider)
    mime = "image/jpeg" if att.mime == "image/jpg" else att.mime
    if family == "anthropic":
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime,
                "data": base64.standard_b64encode(att.data).decode("ascii"),
            },
        }
    # openai-compatible + google_genai
    return {
        "type": "image_url",
        "image_url": {"url": att.data_url()},
    }


# ---------------------------------------------------------------------------
# Clipboard
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClipboardResult:
    kind: Literal["text", "image", "empty", "error"]
    text: str | None = None
    data: bytes | None = None
    mime: str | None = None
    name: str | None = None
    detail: str | None = None


def read_clipboard() -> ClipboardResult:
    """Best-effort clipboard read: prefer image, else text.

    Order:
    1. Platform image (Windows Forms / PIL / xclip / pngpaste)
    2. Platform text
    3. If text is an existing image path -> treat as image file
    """
    img = _read_clipboard_image()
    if img is not None:
        data, mime, name = img
        if data:
            return ClipboardResult(kind="image", data=data, mime=mime, name=name)

    text = _read_clipboard_text()
    if text is None or text == "":
        return ClipboardResult(kind="empty", detail="clipboard empty")

    # Path-to-image convenience: copied file path in explorer / terminal.
    stripped = text.strip().strip('"').strip("'")
    try:
        p = Path(stripped).expanduser()
        if p.is_file() and p.suffix.lower() in EXT_TO_MIME:
            data = p.read_bytes()
            return ClipboardResult(
                kind="image",
                data=data,
                mime=EXT_TO_MIME[p.suffix.lower()],
                name=p.name,
            )
    except OSError:
        pass

    return ClipboardResult(kind="text", text=text)


def _read_clipboard_text() -> str | None:
    # 1) Windows: CF_UNICODETEXT via ctypes
    if os.name == "nt":
        t = _win_clipboard_text()
        if t is not None:
            return t
        t = _ps_clipboard_text()
        if t is not None:
            return t
    # 2) macOS
    if sys_platform() == "darwin":
        out = _run_capture(["pbpaste"])
        if out is not None:
            return out.decode("utf-8", errors="replace")
    # 3) Linux
    for cmd in (
        ["wl-paste", "-n"],
        ["xclip", "-selection", "clipboard", "-o"],
        ["xsel", "--clipboard", "--output"],
    ):
        out = _run_capture(cmd)
        if out is not None:
            return out.decode("utf-8", errors="replace")
    return None


def _read_clipboard_image() -> tuple[bytes, str, str] | None:
    # Prefer PIL if available (cross-platform).
    grabbed = _pil_clipboard_image()
    if grabbed is not None:
        return grabbed

    if os.name == "nt":
        grabbed = _ps_clipboard_image()
        if grabbed is not None:
            return grabbed

    if sys_platform() == "darwin":
        grabbed = _mac_clipboard_image()
        if grabbed is not None:
            return grabbed

    if sys_platform().startswith("linux"):
        grabbed = _linux_clipboard_image()
        if grabbed is not None:
            return grabbed
    return None


def sys_platform() -> str:
    return sys.platform


def _pil_clipboard_image() -> tuple[bytes, str, str] | None:
    try:
        from io import BytesIO

        from PIL import ImageGrab  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001
        return None
    try:
        im = ImageGrab.grabclipboard()
    except Exception:  # noqa: BLE001
        return None
    if im is None:
        return None
    # ImageGrab may return a list of file paths
    if isinstance(im, list):
        for item in im:
            try:
                p = Path(str(item))
                if p.is_file() and p.suffix.lower() in EXT_TO_MIME:
                    return p.read_bytes(), EXT_TO_MIME[p.suffix.lower()], p.name
            except OSError:
                continue
        return None
    try:
        buf = BytesIO()
        # Normalize to PNG for broad model support.
        if getattr(im, "mode", "") not in {"RGB", "RGBA"}:
            im = im.convert("RGBA")
        im.save(buf, format="PNG")
        return buf.getvalue(), "image/png", "clipboard.png"
    except Exception:  # noqa: BLE001
        return None


def _win_clipboard_text() -> str | None:
    try:
        import ctypes
    except Exception:  # noqa: BLE001
        return None

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    CF_UNICODETEXT = 13
    if not user32.OpenClipboard(None):
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            data = ctypes.wstring_at(ptr)
            return data
        finally:
            kernel32.GlobalUnlock(handle)
    except Exception:  # noqa: BLE001
        return None
    finally:
        user32.CloseClipboard()


def _ps_clipboard_text() -> str | None:
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "[System.Windows.Forms.Clipboard]::GetText()"
    )
    out = _run_capture(
        ["powershell", "-NoProfile", "-STA", "-Command", script],
        timeout=5,
    )
    if out is None:
        return None
    return out.decode("utf-8", errors="replace")


def _ps_clipboard_image() -> tuple[bytes, str, str] | None:
    """Windows: System.Windows.Forms.Clipboard.GetImage -> temp PNG."""
    tmp = Path(tempfile.gettempdir()) / f"coding-agent-clip-{os.getpid()}.png"
    # Remove stale
    try:
        if tmp.is_file():
            tmp.unlink()
    except OSError:
        pass
    script = f"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$img = [System.Windows.Forms.Clipboard]::GetImage()
if ($null -eq $img) {{ exit 2 }}
$img.Save('{str(tmp).replace("'", "''")}', [System.Drawing.Imaging.ImageFormat]::Png)
$img.Dispose()
"""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", script],
            capture_output=True,
            timeout=8,
            check=False,
        )
    except Exception:  # noqa: BLE001
        return None
    if proc.returncode != 0 or not tmp.is_file():
        return None
    try:
        data = tmp.read_bytes()
    except OSError:
        return None
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    if not data:
        return None
    return data, "image/png", "clipboard.png"


def _mac_clipboard_image() -> tuple[bytes, str, str] | None:
    tmp = Path(tempfile.gettempdir()) / f"coding-agent-clip-{os.getpid()}.png"
    try:
        if tmp.is_file():
            tmp.unlink()
    except OSError:
        pass
    # pngpaste is optional; osascript fallback is heavy — try pngpaste only.
    out = _run_capture(["pngpaste", str(tmp)], timeout=5)
    if out is None and not tmp.is_file():
        return None
    if not tmp.is_file():
        return None
    try:
        data = tmp.read_bytes()
    except OSError:
        return None
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    if not data:
        return None
    return data, "image/png", "clipboard.png"


def _linux_clipboard_image() -> tuple[bytes, str, str] | None:
    for cmd in (
        ["wl-paste", "-t", "image/png"],
        ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
    ):
        out = _run_capture(cmd, timeout=5)
        if out:
            return out, "image/png", "clipboard.png"
    return None


def _run_capture(
    cmd: list[str],
    *,
    timeout: float = 3.0,
) -> bytes | None:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except Exception:  # noqa: BLE001
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout if proc.stdout else None


def provider_from_settings(settings: Any | None) -> str:
    """Resolve content-block provider from settings.model prefix.

    Prefer explicit ``init_chat_model``-style prefixes:
    ``openai:...``, ``anthropic:...``, ``google_genai:...``, ``azure_openai:...``.

    Falls back to openai-compatible blocks. Does **not** treat bare ``claude``
    substrings inside an ``openai:`` model id as Anthropic (common gateway case).
    """
    if settings is None:
        return "openai"

    candidates: list[str] = []
    for attr in ("model", "active_model"):
        val = str(getattr(settings, attr, "") or "").strip()
        if val:
            candidates.append(val)

    for cand in candidates:
        if ":" in cand:
            prefix = cand.split(":", 1)[0].strip().lower()
            if prefix:
                return normalize_provider_family(prefix)

    # Unprefixed model ids (rare): only treat clear Anthropic/Google family names.
    for cand in candidates:
        low = cand.lower()
        if low.startswith("claude") or low.startswith("anthropic"):
            return "anthropic"
        if low.startswith("gemini") or low.startswith("google"):
            return "google"

    return "openai"
