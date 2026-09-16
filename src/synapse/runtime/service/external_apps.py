"""Opening one workspace file with a program that runs on the host machine.

The console can show a file, but reviewing a change often means opening it in the
reader's own editor.  That is a *host-side* action: this machine enumerates the
applications it can start, and this machine starts exactly one of them for exactly
one workspace-relative path.

Three rules shape the whole module:

- **The workspace comes from the session, never from the request.**  The payload
  carries a workspace-relative POSIX path; the session the runtime already resolved
  supplies the root, and the resolved target must still be inside it (so a symlinked
  directory cannot aim the launch somewhere else).
- **The request names an application, never a command.**  The caller sends an
  ``app_id`` that must be one of the ids this host enumerated; the argv is built here
  from that table, with no shell, no string interpolation into a command line, and no
  caller-supplied argument.  A program's own path is host-internal: it is never part of
  the wire payload, so the browser cannot learn the filesystem layout of the host.
- **Every refusal is named.**  "Why did nothing happen" is the whole question a reader
  has, so the conditions (path invalid, outside the workspace, file missing, unknown
  application, launch failed, no desktop session) each carry their own ``service_code``.

Applications are discovered from a curated candidate table whose probes are checked
against the host (an executable on ``PATH`` or a known install location), because the
standard library exposes no "installed applications" API.  Icons are glyph ids the
console maps to its own icon set; no binary icon is produced here, and no registry,
third-party package or network call is involved.

That is a deliberate limit as much as a design: **an editor outside the table never
appears**, however installed it is, and an application installed in a non-standard
location is only found when one of its probes matches.  The system association
(``SYSTEM_APP_ID``) is always offered, so the reader is never left with no way to open
a file.  The extension point for a wider set is the same table -- add a ``_Candidate``
with its per-platform probes and a glyph id the console knows -- and a future slice can
merge a real enumeration (Windows ``App Paths``, Linux ``.desktop`` files, macOS
``/Applications``) into it without changing the wire contract.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

#: The workspace-relative path bound is the same one the artifact surface applies to
#: every logical path on the wire, so it is imported rather than restated here.
from synapse.runtime.service.artifacts import MAX_PATH_BYTES
from synapse.runtime.service.errors import (
    ExternalAppLaunchError,
    ExternalAppPathError,
    ExternalAppsUnavailableError,
    ExternalAppUnknownError,
    InvalidRequestError,
)
from synapse.runtime.sessions.ref import SessionRef

__all__ = [
    "APPS_LIST_LIMIT",
    "EXTERNAL_APP_ICON_KINDS",
    "EXTERNAL_APP_KINDS",
    "EXTERNAL_APP_MODES",
    "MAX_APP_ID_BYTES",
    "MAX_PATH_BYTES",
    "SYSTEM_APP_ID",
    "ExternalApp",
    "ExternalAppIcon",
    "ExternalAppPage",
    "ListExternalAppsQuery",
    "OpenExternalCommand",
    "OpenExternalResult",
    "discover_external_apps",
    "list_external_apps_host",
    "open_external_workspace",
]

#: The application roles the console can group and label by.
EXTERNAL_APP_KINDS: Final = ("editor", "viewer", "terminal", "shell", "system")
#: How an application's icon travels: a glyph id the console resolves, or a data URL.
EXTERNAL_APP_ICON_KINDS: Final = ("glyph", "data_url")
#: ``open`` starts the program on the file; ``reveal`` only locates it in the file manager.
EXTERNAL_APP_MODES: Final = ("open", "reveal")
#: The application the operating system's own file association selects.
SYSTEM_APP_ID: Final = "system"
#: One page of applications; the host never returns an unbounded catalog.
APPS_LIST_LIMIT: Final = 64
#: An application id is an opaque token, bounded before it is ever compared.
MAX_APP_ID_BYTES: Final = 64

_APP_ID_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_ENV_PATTERN: Final = re.compile(
    r"%([A-Za-z_][A-Za-z0-9_]*)%|\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)"
)
_KIND_ORDER: Final = {kind: index for index, kind in enumerate(EXTERNAL_APP_KINDS)}


@dataclass(frozen=True, slots=True)
class ExternalAppIcon:
    """The icon the console paints for one application.

    ``kind`` is ``glyph`` (``value`` names a mark the console ships) or ``data_url``
    (``value`` is an inline image).  A host path is never one of the two.
    """

    kind: str
    value: str


@dataclass(frozen=True, slots=True)
class ExternalApp:
    """One application this host can start, as the console needs to show it."""

    id: str
    name: str
    short_name: str
    kind: str
    #: The extensions the application claims, for the console's "recommended" group.
    extensions: tuple[str, ...]
    icon: ExternalAppIcon
    is_system_default: bool
    available: bool


@dataclass(frozen=True, slots=True)
class ListExternalAppsQuery:
    """Read the host's application catalog."""

    limit: int = APPS_LIST_LIMIT


@dataclass(frozen=True, slots=True)
class ExternalAppPage:
    """One bounded page of the host's application catalog."""

    apps: tuple[ExternalApp, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class OpenExternalCommand:
    """Start one application on one workspace-relative path."""

    session: SessionRef
    path: str
    #: An id from :func:`discover_external_apps`; absent means the system association.
    app_id: str | None = None
    mode: str = "open"
    command_id: str | None = None


@dataclass(frozen=True, slots=True)
class OpenExternalResult:
    """Which application was started, and in which mode."""

    opened: bool
    app_id: str
    mode: str


@dataclass(frozen=True, slots=True)
class _Candidate:
    """One application the host may have, and how to find it on each platform.

    A probe that contains a separator, a ``~`` or an environment reference is a path
    template (expanded, then checked for existence); anything else is looked up on
    ``PATH``.  Windows commands are always named ``*.exe``: ``CreateProcess`` cannot
    start a ``.cmd`` shim, so the install-dir executable is preferred.
    """

    id: str
    name: str
    short_name: str
    kind: str
    glyph: str
    extensions: tuple[str, ...]
    probes: Mapping[str, tuple[str, ...]]


def _candidate(
    id: str,
    name: str,
    short_name: str,
    kind: str,
    glyph: str,
    extensions: tuple[str, ...],
    *,
    nt: tuple[str, ...] = (),
    linux: tuple[str, ...] = (),
    darwin: tuple[str, ...] = (),
) -> _Candidate:
    """Build one candidate from its per-platform probes."""
    return _Candidate(
        id=id,
        name=name,
        short_name=short_name,
        kind=kind,
        glyph=glyph,
        extensions=extensions,
        probes={"nt": nt, "linux": linux, "darwin": darwin},
    )


#: The applications the host looks for, most specific install location first.
_CANDIDATES: Final[tuple[_Candidate, ...]] = (
    _candidate(
        "vscode",
        "Visual Studio Code",
        "VS Code",
        "editor",
        "vscode",
        ("tsx", "ts", "js", "jsx", "json", "md", "py", "rs", "css", "html"),
        nt=(
            r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe",
            r"%ProgramFiles%\Microsoft VS Code\Code.exe",
            r"%ProgramFiles(x86)%\Microsoft VS Code\Code.exe",
            "code.exe",
        ),
        linux=("code", "code-insiders"),
        darwin=("/Applications/Visual Studio Code.app", "code"),
    ),
    _candidate(
        "cursor",
        "Cursor",
        "Cursor",
        "editor",
        "cursor",
        ("ts", "tsx", "py", "rs", "md"),
        nt=(
            r"%LOCALAPPDATA%\Programs\cursor\Cursor.exe",
            r"%ProgramFiles%\Cursor\Cursor.exe",
            "cursor.exe",
        ),
        linux=("cursor",),
        darwin=("/Applications/Cursor.app", "cursor"),
    ),
    _candidate(
        "zed",
        "Zed",
        "Zed",
        "editor",
        "zed",
        ("rs", "ts", "md"),
        nt=(r"%LOCALAPPDATA%\Programs\Zed\zed.exe", "zed.exe"),
        linux=("zed", "zeditor"),
        darwin=("/Applications/Zed.app", "zed"),
    ),
    _candidate(
        "sublime",
        "Sublime Text",
        "Sublime Text",
        "editor",
        "sublime",
        ("txt", "md", "json"),
        nt=(
            r"%ProgramFiles%\Sublime Text\sublime_text.exe",
            r"%ProgramFiles(x86)%\Sublime Text\sublime_text.exe",
        ),
        linux=("subl", "sublime_text"),
        darwin=("/Applications/Sublime Text.app", "subl"),
    ),
    _candidate(
        "notepadpp",
        "Notepad++",
        "Notepad++",
        "editor",
        "notepadpp",
        ("txt", "md", "json", "log"),
        nt=(
            r"%ProgramFiles%\Notepad++\notepad++.exe",
            r"%ProgramFiles(x86)%\Notepad++\notepad++.exe",
        ),
        linux=("notepadqq",),
    ),
    _candidate(
        "notepad",
        "记事本（Windows）",
        "记事本",
        "viewer",
        "notepad",
        ("txt", "log"),
        nt=(r"%WINDIR%\System32\notepad.exe",),
    ),
    _candidate(
        "terminal",
        "Windows Terminal",
        "终端",
        "terminal",
        "terminal",
        (),
        nt=(r"%LOCALAPPDATA%\Microsoft\WindowsApps\wt.exe",),
        linux=("x-terminal-emulator", "gnome-terminal", "konsole"),
        darwin=("/Applications/Utilities/Terminal.app", "open"),
    ),
    _candidate(
        "explorer",
        "资源管理器（定位到文件）",
        "资源管理器",
        "shell",
        "explorer",
        (),
        nt=(r"%WINDIR%\explorer.exe",),
        linux=("xdg-open",),
        darwin=("open",),
    ),
)


def _platform_key(platform: str | None = None) -> str:
    """The probe table to read: ``nt`` / ``darwin`` / ``linux``, or ``other``."""
    if platform is not None:
        return platform
    if os.name == "nt":
        return "nt"
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    return "other"


def _expand(entry: str, environ: Mapping[str, str]) -> str | None:
    """Expand one probe template, or ``None`` when a variable it needs is unset.

    A template with an unset variable names no location: skipping it keeps a
    half-expanded path (``\\Programs\\...``) from being probed as if it were real.
    """
    missing = False

    def replace(match: re.Match[str]) -> str:
        nonlocal missing
        name = match.group(1) or match.group(2) or match.group(3)
        value = environ.get(name)
        if value is None:
            missing = True
            return ""
        return value

    expanded = _ENV_PATTERN.sub(replace, entry)
    if missing or "%" in expanded or "$" in expanded:
        return None
    return os.path.expanduser(expanded)


def _looks_like_path(entry: str) -> bool:
    """Whether one probe names a location rather than an executable on ``PATH``."""
    return "/" in entry or "\\" in entry or entry.startswith("~") or "%" in entry


def _resolve_probe(
    entry: str,
    *,
    environ: Mapping[str, str],
    which: Callable[[str], str | None],
    exists: Callable[[str], bool],
) -> str | None:
    """The host location one probe resolves to, or ``None`` when it is absent."""
    if not _looks_like_path(entry):
        return which(entry)
    expanded = _expand(entry, environ)
    if expanded is None or expanded == "":
        return None
    if exists(expanded):
        return expanded
    return None


def _resolve_host_apps(
    *,
    platform: str | None = None,
    which: Callable[[str], str | None] | None = None,
    exists: Callable[[str], bool] | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[tuple[ExternalApp, ...], dict[str, str]]:
    """Enumerate the host's applications and how to start each one.

    Returns the wire-visible catalog plus the host-internal ``app id -> launch
    target`` map, which never leaves this module.
    """
    key = _platform_key(platform)
    lookup = which if which is not None else shutil.which
    probe_exists = exists if exists is not None else os.path.exists
    env = environ if environ is not None else os.environ

    apps: list[ExternalApp] = []
    targets: dict[str, str] = {}
    if key in {"nt", "darwin", "linux"}:
        for candidate in _CANDIDATES:
            resolved = None
            for probe in candidate.probes.get(key, ()):
                resolved = _resolve_probe(probe, environ=env, which=lookup, exists=probe_exists)
                if resolved is not None:
                    break
            if resolved is None:
                continue
            apps.append(
                ExternalApp(
                    id=candidate.id,
                    name=candidate.name,
                    short_name=candidate.short_name,
                    kind=candidate.kind,
                    extensions=candidate.extensions,
                    icon=ExternalAppIcon(kind="glyph", value=candidate.glyph),
                    is_system_default=False,
                    available=True,
                )
            )
            targets[candidate.id] = resolved

    # The operating system's own association is always offered: it needs no
    # installed editor and no probe, and it is the honest answer when nothing
    # in the table was found.
    apps.append(
        ExternalApp(
            id=SYSTEM_APP_ID,
            name="系统默认应用",
            short_name="系统默认",
            kind="system",
            extensions=(),
            icon=ExternalAppIcon(kind="glyph", value="system"),
            is_system_default=True,
            available=key in {"nt", "darwin", "linux"},
        )
    )
    apps.sort(key=lambda app: (_KIND_ORDER[app.kind], app.name))
    return tuple(apps), targets


def discover_external_apps(
    *,
    platform: str | None = None,
    which: Callable[[str], str | None] | None = None,
    exists: Callable[[str], bool] | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[ExternalApp, ...]:
    """The applications this host can start, as the console may see them."""
    apps, _ = _resolve_host_apps(platform=platform, which=which, exists=exists, environ=environ)
    return apps


def list_external_apps_host(
    query: ListExternalAppsQuery,
    *,
    apps: tuple[ExternalApp, ...] | None = None,
) -> ExternalAppPage:
    """One bounded page of the host's application catalog."""
    if type(query) is not ListExternalAppsQuery:
        raise InvalidRequestError(
            "list external apps query must be a ListExternalAppsQuery, "
            f"got type {type(query).__name__!r}"
        )
    catalog = discover_external_apps() if apps is None else apps
    limit = max(1, min(int(query.limit), APPS_LIST_LIMIT))
    page = catalog[:limit]
    return ExternalAppPage(apps=page, truncated=len(catalog) > len(page))


def _workspace_of(session: object) -> Path:
    """The session's own workspace, or a typed refusal.

    The workspace comes from the session the runtime already resolved, never from
    the request, so no caller can aim a launch at a directory of its own choosing.
    """
    workspace = getattr(session, "workspace", None)
    if not workspace:
        raise ExternalAppsUnavailableError(
            "the session workspace is unavailable",
            code="external_app_workspace_unavailable",
        )
    try:
        resolved = Path(str(workspace)).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ExternalAppsUnavailableError(
            "the session workspace is unavailable",
            code="external_app_workspace_unavailable",
        ) from exc
    if not resolved.is_dir():
        raise ExternalAppsUnavailableError(
            "the session workspace is unavailable",
            code="external_app_workspace_unavailable",
        )
    return resolved


def _resolve_target(workspace: Path, path: object) -> Path:
    """The absolute target of one workspace-relative POSIX path, or a refusal.

    The path is validated as a relative POSIX path with no parent segments, and the
    resolved target must still be inside the workspace, so a symlinked directory
    cannot aim the launch somewhere else.  The file has to exist: opening a program
    on a path that is not there would fail silently on the host instead.
    """
    if not isinstance(path, str) or path == "" or len(path.encode("utf-8")) > MAX_PATH_BYTES:
        raise ExternalAppPathError("the file path is required", code="external_app_path_invalid")
    if "\\" in path or path.startswith("/") or ":" in path:
        raise ExternalAppPathError(
            "the file path must be workspace-relative",
            code="external_app_path_invalid",
        )
    pure = PurePosixPath(path)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        raise ExternalAppPathError(
            "the file path must not contain parent segments",
            code="external_app_path_invalid",
        )
    if pure.as_posix() != path:
        # ``PurePosixPath`` normalizes ``./a`` and ``a/`` away; the wire path has to be
        # canonical so what is validated is exactly what is resolved.
        raise ExternalAppPathError(
            "the file path is not canonical POSIX",
            code="external_app_path_invalid",
        )
    target = workspace.joinpath(*pure.parts)
    try:
        resolved = target.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ExternalAppPathError(
            "the file does not exist in the workspace",
            code="external_app_file_missing",
        ) from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise ExternalAppPathError(
            "the file path cannot be resolved", code="external_app_path_invalid"
        ) from exc
    if resolved != workspace and workspace not in resolved.parents:
        raise ExternalAppPathError(
            "the file path escapes the workspace",
            code="external_app_outside_workspace",
        )
    return resolved


def _app_id_of(command: OpenExternalCommand) -> str:
    """The requested application id, defaulted and bounded before it is compared."""
    app_id = command.app_id
    if app_id is None or app_id == "":
        return SYSTEM_APP_ID
    if type(app_id) is not str or not _APP_ID_PATTERN.match(app_id):
        raise InvalidRequestError("app id is not a valid application identifier")
    if len(app_id.encode("utf-8")) > MAX_APP_ID_BYTES:
        raise InvalidRequestError("app id is too long")
    return app_id


def _run(argv: Sequence[str], launcher: Callable[[Sequence[str]], None] | None) -> None:
    """Start one program with a fixed argv: no shell, no interpolation, no extra args."""
    try:
        if launcher is not None:
            launcher(argv)
            return
        subprocess.Popen(  # noqa: S603 - fixed argv, no shell, no caller-supplied text
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # The host refused to start the program; the message stays path-free.
        raise ExternalAppLaunchError(
            "the host could not start the requested application",
            code="external_app_launch_failed",
        ) from exc


def _open_default(
    target: Path,
    platform: str,
    *,
    startfile: Callable[[str], None] | None,
    launcher: Callable[[Sequence[str]], None] | None,
) -> None:
    """Hand one path to the operating system's own association."""
    if platform == "nt":
        opener = startfile if startfile is not None else getattr(os, "startfile", None)
        if opener is None:
            raise ExternalAppsUnavailableError(
                "this host cannot open a file with its default application",
                code="external_app_unavailable",
            )
        try:
            opener(str(target))
        except OSError as exc:
            raise ExternalAppLaunchError(
                "the host could not start the requested application",
                code="external_app_launch_failed",
            ) from exc
        return
    _run(["open" if platform == "darwin" else "xdg-open", str(target)], launcher)


def _reveal_argv(target: Path, platform: str) -> list[str]:
    """The argv that locates one path in the platform's file manager."""
    if platform == "nt":
        return ["explorer.exe", f"/select,{target}"]
    if platform == "darwin":
        return ["open", "-R", str(target)]
    return ["xdg-open", str(target.parent)]


def open_external_workspace(
    command: OpenExternalCommand,
    session: object,
    *,
    apps: tuple[ExternalApp, ...] | None = None,
    targets: Mapping[str, str] | None = None,
    launcher: Callable[[Sequence[str]], None] | None = None,
    startfile: Callable[[str], None] | None = None,
    platform: str | None = None,
) -> OpenExternalResult:
    """Start one application on one workspace-relative path.

    Every refusal is named: an unusable workspace, an unsafe or absent path, an
    application this host did not enumerate, and a launch the host rejected.
    ``apps`` / ``targets`` are the test seam: a caller that injects the catalog
    also injects the launch targets, and a production call reads both from this
    module's own probe table.
    """
    if type(command) is not OpenExternalCommand:
        raise InvalidRequestError(
            "open external command must be an OpenExternalCommand, "
            f"got type {type(command).__name__!r}"
        )
    mode = command.mode
    if mode not in EXTERNAL_APP_MODES:
        raise InvalidRequestError("open mode must be one of " + ", ".join(EXTERNAL_APP_MODES))
    app_id = _app_id_of(command)
    key = _platform_key(platform)
    workspace = _workspace_of(session)
    target = _resolve_target(workspace, command.path)

    if apps is None:
        catalog, resolved_targets = _resolve_host_apps(platform=platform)
    else:
        catalog, resolved_targets = apps, dict(targets or {})
    known = {app.id for app in catalog}
    if app_id not in known:
        raise ExternalAppUnknownError(
            f"this host has no application registered as {app_id!r}",
            code="external_app_unknown",
        )

    if mode == "reveal":
        _run(_reveal_argv(target, key), launcher)
    elif app_id == SYSTEM_APP_ID:
        _open_default(target, key, startfile=startfile, launcher=launcher)
    else:
        executable = resolved_targets.get(app_id)
        if executable is None:
            raise ExternalAppUnknownError(
                f"this host has no application registered as {app_id!r}",
                code="external_app_unknown",
            )
        _run([executable, str(target)], launcher)
    return OpenExternalResult(opened=True, app_id=app_id, mode=mode)
