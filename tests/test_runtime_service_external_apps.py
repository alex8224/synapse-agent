"""Opening a workspace file with a host program: discovery, refusals, and the wire.

The launch itself is never executed here: the module takes an injectable launcher and
an injectable probe table, so every assertion is about the decision (which argv, which
refusal) rather than about a process on the machine running the tests.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from synapse.runtime.service.access import (
    _REQUIRED_DELEGATE_METHODS,
    APPS_LIST,
    WORKSPACE_OPEN_EXTERNAL,
    AclAuthorizer,
    AclGrant,
    Principal,
    bind_access,
)
from synapse.runtime.service.errors import (
    ExternalAppLaunchError,
    ExternalAppPathError,
    ExternalAppsUnavailableError,
    ExternalAppUnknownError,
    InvalidRequestError,
    PermissionDeniedError,
)
from synapse.runtime.service.external_apps import (
    APPS_LIST_LIMIT,
    SYSTEM_APP_ID,
    ExternalApp,
    ExternalAppIcon,
    ListExternalAppsQuery,
    OpenExternalCommand,
    discover_external_apps,
    list_external_apps_host,
    open_external_workspace,
)
from synapse.runtime.sessions.ref import SessionRef
from synapse.runtime.transport.protocol import ProtocolError, dispatch

REF = SessionRef(project_id="p", thread_id="t")


class _Launcher:
    """A launcher that records argv instead of starting anything."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: Any) -> None:
        self.calls.append(list(argv))


def _session(workspace: Path | str | None) -> SimpleNamespace:
    return SimpleNamespace(workspace=None if workspace is None else str(workspace))


def _app(app_id: str, kind: str = "editor") -> ExternalApp:
    return ExternalApp(
        id=app_id,
        name=app_id.upper(),
        short_name=app_id,
        kind=kind,
        extensions=("ts",),
        icon=ExternalAppIcon(kind="glyph", value=app_id),
        is_system_default=app_id == SYSTEM_APP_ID,
        available=True,
    )


def _catalog(*app_ids: str) -> tuple[ExternalApp, ...]:
    """A catalog that always carries the system association, like the host's own."""
    return tuple(
        _app(app_id, "system" if app_id == SYSTEM_APP_ID else "editor") for app_id in app_ids
    )


def _targets(**entries: str) -> dict[str, str]:
    return dict(entries)


def _command(path: str = "a.txt", **kwargs: Any) -> OpenExternalCommand:
    return OpenExternalCommand(session=REF, path=path, **kwargs)


def _workspace(tmp_path: Path, *names: str) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    for name in names:
        (workspace / name).write_text("one\n", encoding="utf-8")
    return workspace


# --- the launch decision -----------------------------------------------------------


def test_a_named_app_is_started_with_the_resolved_workspace_path(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, "a.txt")
    launcher = _Launcher()

    result = open_external_workspace(
        _command(app_id="editor"),
        _session(workspace),
        apps=_catalog("editor", SYSTEM_APP_ID),
        targets=_targets(editor="C:\\tools\\editor.exe"),
        launcher=launcher,
        platform="nt",
    )

    assert result.opened is True
    assert result.app_id == "editor" and result.mode == "open"
    assert launcher.calls == [["C:\\tools\\editor.exe", str((workspace / "a.txt").resolve())]]


def test_an_absent_app_id_uses_the_system_association(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, "a.txt")
    launcher = _Launcher()
    opened: list[str] = []

    result = open_external_workspace(
        _command(),
        _session(workspace),
        apps=_catalog("editor", SYSTEM_APP_ID),
        targets=_targets(editor="C:\\tools\\editor.exe"),
        launcher=launcher,
        startfile=opened.append,
        platform="nt",
    )

    assert result.app_id == SYSTEM_APP_ID
    assert opened == [str((workspace / "a.txt").resolve())]
    assert launcher.calls == []


def test_reveal_locates_the_file_instead_of_opening_it(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, "a.txt")
    resolved = (workspace / "a.txt").resolve()

    on_windows = _Launcher()
    open_external_workspace(
        _command(app_id="explorer", mode="reveal"),
        _session(workspace),
        apps=_catalog("explorer", SYSTEM_APP_ID),
        targets=_targets(explorer="C:\\Windows\\explorer.exe"),
        launcher=on_windows,
        platform="nt",
    )
    assert on_windows.calls == [["explorer.exe", f"/select,{resolved}"]]

    on_linux = _Launcher()
    open_external_workspace(
        _command(app_id="explorer", mode="reveal"),
        _session(workspace),
        apps=_catalog("explorer", SYSTEM_APP_ID),
        targets=_targets(explorer="/usr/bin/xdg-open"),
        launcher=on_linux,
        platform="linux",
    )
    assert on_linux.calls == [["xdg-open", str(resolved.parent)]]


def test_the_default_association_uses_the_platform_opener(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, "a.txt")
    launcher = _Launcher()

    open_external_workspace(
        _command(),
        _session(workspace),
        apps=_catalog(SYSTEM_APP_ID),
        launcher=launcher,
        platform="linux",
    )

    assert launcher.calls == [["xdg-open", str((workspace / "a.txt").resolve())]]


# --- refusals ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["", "../a.txt", "a/../../a.txt", "/etc/passwd", "C:/windows/system32", "a\\b.txt", "./a.txt"],
)
def test_an_unsafe_path_is_refused_before_anything_is_started(tmp_path: Path, path: str) -> None:
    workspace = _workspace(tmp_path, "a.txt")
    launcher = _Launcher()

    with pytest.raises(ExternalAppPathError) as caught:
        open_external_workspace(
            _command(path),
            _session(workspace),
            apps=_catalog(SYSTEM_APP_ID),
            launcher=launcher,
            platform="nt",
        )

    assert caught.value.code == "external_app_path_invalid"
    assert launcher.calls == []


def test_a_missing_file_is_refused_by_name(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    with pytest.raises(ExternalAppPathError) as caught:
        open_external_workspace(
            _command("gone.txt"),
            _session(workspace),
            apps=_catalog(SYSTEM_APP_ID),
            launcher=_Launcher(),
            platform="nt",
        )

    assert caught.value.code == "external_app_file_missing"


def test_a_symlink_out_of_the_workspace_is_refused(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, "a.txt")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    link = workspace / "link.txt"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows without the privilege
        pytest.skip("this host cannot create a symlink")

    with pytest.raises(ExternalAppPathError) as caught:
        open_external_workspace(
            _command("link.txt"),
            _session(workspace),
            apps=_catalog(SYSTEM_APP_ID),
            launcher=_Launcher(),
            platform="nt",
        )

    assert caught.value.code == "external_app_outside_workspace"


def test_a_session_without_a_workspace_is_refused() -> None:
    with pytest.raises(ExternalAppsUnavailableError) as caught:
        open_external_workspace(
            _command(),
            _session(None),
            apps=_catalog(SYSTEM_APP_ID),
            launcher=_Launcher(),
            platform="nt",
        )

    assert caught.value.code == "external_app_workspace_unavailable"


def test_an_unknown_application_is_refused(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, "a.txt")

    with pytest.raises(ExternalAppUnknownError) as caught:
        open_external_workspace(
            _command(app_id="never-installed"),
            _session(workspace),
            apps=_catalog(SYSTEM_APP_ID),
            launcher=_Launcher(),
            platform="nt",
        )

    assert caught.value.code == "external_app_unknown"


def test_a_malformed_app_id_is_rejected(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, "a.txt")

    for app_id in ("../editor", "EDITOR", "editor --flag", "a" * 65):
        with pytest.raises(InvalidRequestError):
            open_external_workspace(
                _command(app_id=app_id),
                _session(workspace),
                apps=_catalog(SYSTEM_APP_ID),
                launcher=_Launcher(),
                platform="nt",
            )


def test_an_unknown_mode_is_rejected(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, "a.txt")

    with pytest.raises(InvalidRequestError):
        open_external_workspace(
            _command(mode="execute"),
            _session(workspace),
            apps=_catalog(SYSTEM_APP_ID),
            launcher=_Launcher(),
            platform="nt",
        )


def test_a_failed_launch_is_named_and_never_leaks_the_path(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, "a.txt")

    def failing(argv: Any) -> None:
        raise OSError("no such program")

    with pytest.raises(ExternalAppLaunchError) as caught:
        open_external_workspace(
            _command(app_id="editor"),
            _session(workspace),
            apps=_catalog("editor", SYSTEM_APP_ID),
            targets=_targets(editor="C:\\tools\\editor.exe"),
            launcher=failing,
            platform="nt",
        )

    assert caught.value.code == "external_app_launch_failed"
    assert str(workspace) not in str(caught.value)


# --- discovery ---------------------------------------------------------------------


def _fake_probes(*found: str) -> dict[str, Any]:
    """A probe table where exactly the named locations exist."""
    wanted = {Path(name).name.lower() for name in found}
    return {
        "which": lambda name: next(
            (name for name in found if Path(name).name.lower() == name.lower()), None
        ),
        "exists": lambda path: Path(path).name.lower() in wanted,
        "environ": {"LOCALAPPDATA": "C:\\Users\\x\\AppData\\Local"},
    }


def test_discovery_reports_only_the_applications_this_host_has() -> None:
    apps = discover_external_apps(
        platform="nt",
        **_fake_probes("C:\\Users\\x\\AppData\\Local\\Programs\\Microsoft VS Code\\Code.exe"),
    )

    ids = [app.id for app in apps]
    assert "vscode" in ids
    assert "notepad" not in ids and "cursor" not in ids
    assert ids[-1] == SYSTEM_APP_ID
    vscode = next(app for app in apps if app.id == "vscode")
    assert vscode.icon.kind == "glyph" and vscode.icon.value == "vscode"
    assert vscode.kind == "editor" and "tsx" in vscode.extensions
    assert vscode.is_system_default is False
    assert next(app for app in apps if app.id == SYSTEM_APP_ID).is_system_default is True


def test_discovery_skips_a_probe_whose_variable_is_unset() -> None:
    apps = discover_external_apps(
        platform="nt", which=lambda name: None, exists=lambda path: True, environ={}
    )

    assert [app.id for app in apps] == [SYSTEM_APP_ID]


def test_discovery_orders_the_roles_and_keeps_the_system_entry_last() -> None:
    apps = discover_external_apps(
        platform="nt",
        **_fake_probes(
            "C:\\Users\\x\\AppData\\Local\\Programs\\Microsoft VS Code\\Code.exe",
            "C:\\Windows\\System32\\notepad.exe",
            "C:\\Windows\\explorer.exe",
        ),
    )

    kinds = [app.kind for app in apps]
    assert kinds == sorted(kinds, key=("editor", "viewer", "terminal", "shell", "system").index)
    assert kinds[-1] == "system"


def test_an_unknown_platform_offers_only_the_system_entry() -> None:
    apps = discover_external_apps(
        platform="other", which=lambda name: name, exists=lambda path: True
    )

    assert [app.id for app in apps] == [SYSTEM_APP_ID]
    assert apps[0].available is False


def test_the_catalog_page_is_bounded_and_says_when_it_was_truncated() -> None:
    catalog = _catalog("a", "b", "c", SYSTEM_APP_ID)

    page = list_external_apps_host(ListExternalAppsQuery(limit=2), apps=catalog)
    assert [app.id for app in page.apps] == ["a", "b"]
    assert page.truncated is True

    whole = list_external_apps_host(ListExternalAppsQuery(limit=APPS_LIST_LIMIT), apps=catalog)
    assert len(whole.apps) == len(catalog)
    assert whole.truncated is False

    # A limit below the minimum is clamped, not rejected: the module is bounded either way.
    assert len(list_external_apps_host(ListExternalAppsQuery(limit=0), apps=catalog).apps) == 1


def test_a_query_of_the_wrong_type_is_rejected() -> None:
    with pytest.raises(InvalidRequestError):
        list_external_apps_host(SimpleNamespace(limit=1))  # type: ignore[arg-type]


# --- the wire ----------------------------------------------------------------------


class _RecordingService:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def open_external(self, command: Any) -> str:
        self.calls.append(command)
        return "opened"

    async def list_external_apps(self, query: Any) -> str:
        self.calls.append(query)
        return "catalog"


def _dispatch(method: str, params: dict[str, Any]) -> Any:
    service = _RecordingService()
    result = asyncio.run(dispatch(service, method, params))
    return result, service.calls[0]


def test_the_wire_decodes_an_open_request() -> None:
    result, decoded = _dispatch(
        "runtime.workspace.open_external",
        {
            "session": {"project_id": "p", "thread_id": "t"},
            "path": "src/app.py",
            "app_id": "vscode",
            "mode": "reveal",
            "command_id": "cmd-1",
        },
    )

    assert result == "opened"
    assert type(decoded) is OpenExternalCommand
    assert decoded.path == "src/app.py" and decoded.app_id == "vscode"
    assert decoded.mode == "reveal" and decoded.command_id == "cmd-1"


def test_the_wire_defaults_the_mode_and_the_app(tmp_path: Path) -> None:
    _, decoded = _dispatch(
        "runtime.workspace.open_external",
        {"session": {"project_id": "p", "thread_id": "t"}, "path": "a.txt"},
    )

    assert decoded.mode == "open" and decoded.app_id is None


def test_the_wire_rejects_an_unknown_mode_or_an_extra_field() -> None:
    for params in (
        {"session": {"project_id": "p", "thread_id": "t"}, "path": "a.txt", "mode": "run"},
        {"session": {"project_id": "p", "thread_id": "t"}, "path": "a.txt", "shell": "rm -rf /"},
    ):
        with pytest.raises(ProtocolError):
            _dispatch("runtime.workspace.open_external", params)


def test_the_wire_decodes_the_catalog_query_and_bounds_its_limit() -> None:
    _, decoded = _dispatch("runtime.apps.list", {})
    assert type(decoded) is ListExternalAppsQuery
    assert decoded.limit == APPS_LIST_LIMIT

    _, decoded = _dispatch("runtime.apps.list", {"limit": 5})
    assert decoded.limit == 5

    for limit in (0, APPS_LIST_LIMIT + 1):
        with pytest.raises(ProtocolError):
            _dispatch("runtime.apps.list", {"limit": limit})


# --- authorization -----------------------------------------------------------------


class _Delegate:
    """Minimal delegate: the required set, plus the two optional methods under test."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

        async def _noop(*args: object, **kwargs: object) -> None:
            return None

        for name in _REQUIRED_DELEGATE_METHODS:
            setattr(self, name, _noop)

        async def open_external(command: Any) -> str:
            self.calls.append(command)
            return "opened"

        async def list_external_apps(query: Any) -> str:
            self.calls.append(query)
            return "catalog"

        self.open_external = open_external  # type: ignore[method-assign]
        self.list_external_apps = list_external_apps  # type: ignore[method-assign]


class _OldDelegate:
    """A delegate from before this feature: neither method exists."""

    def __init__(self) -> None:
        async def _noop(*args: object, **kwargs: object) -> None:
            return None

        for name in _REQUIRED_DELEGATE_METHODS:
            setattr(self, name, _noop)


def _authorizer(*capabilities: str) -> AclAuthorizer:
    return AclAuthorizer([AclGrant("subject-a", REF.project_id, frozenset(capabilities), None)])


def test_opening_a_file_needs_its_own_capability() -> None:
    delegate = _Delegate()
    principal = Principal("subject-a")

    denied = bind_access(delegate, principal, _authorizer("git.status", APPS_LIST))
    with pytest.raises(PermissionDeniedError):
        asyncio.run(denied.open_external(_command()))
    assert delegate.calls == []

    allowed = bind_access(delegate, principal, _authorizer(WORKSPACE_OPEN_EXTERNAL))
    assert asyncio.run(allowed.open_external(_command())) == "opened"
    assert len(delegate.calls) == 1


def test_listing_applications_needs_its_own_capability() -> None:
    delegate = _Delegate()
    principal = Principal("subject-a")

    denied = bind_access(delegate, principal, _authorizer(WORKSPACE_OPEN_EXTERNAL))
    with pytest.raises(PermissionDeniedError):
        asyncio.run(denied.list_external_apps(ListExternalAppsQuery()))
    assert delegate.calls == []

    allowed = bind_access(delegate, principal, _authorizer(APPS_LIST))
    assert asyncio.run(allowed.list_external_apps(ListExternalAppsQuery())) == "catalog"


def test_a_delegate_without_the_methods_reports_them_unavailable() -> None:
    service = bind_access(
        _OldDelegate(),
        Principal("subject-a"),
        _authorizer(APPS_LIST, WORKSPACE_OPEN_EXTERNAL),
    )

    with pytest.raises(InvalidRequestError):
        asyncio.run(service.list_external_apps(ListExternalAppsQuery()))
    with pytest.raises(InvalidRequestError):
        asyncio.run(service.open_external(_command()))


# --- the service port --------------------------------------------------------------


def _service(workspace: Path) -> Any:
    from synapse.runtime.service import LocalAgentRuntimeService, OpenSessionCommand
    from synapse.runtime.sessions import RuntimeManager

    settings = SimpleNamespace(
        workspace=workspace,
        deny_fs_paths=[],
        max_concurrency=2,
        model="test",
    )
    manager = RuntimeManager(
        settings=settings,
        agent_factory=lambda thread_id, shared: SimpleNamespace(thread_id=thread_id),
        project_id=REF.project_id,
    )
    return LocalAgentRuntimeService(
        lambda project_id: manager if project_id == REF.project_id else None
    ), OpenSessionCommand


def test_the_service_port_lists_the_host_catalog_and_delegates_one_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path, "a.txt")
    service, open_command = _service(workspace)
    seen: list[Any] = []

    def fake_open(command: Any, session: Any) -> Any:
        seen.append((command, session))
        from synapse.runtime.service.external_apps import OpenExternalResult

        return OpenExternalResult(opened=True, app_id="vscode", mode="open")

    monkeypatch.setattr("synapse.runtime.service.local.open_external_workspace", fake_open)

    async def run() -> tuple[Any, Any]:
        await service.open_session(open_command(session=REF))
        page = await service.list_external_apps(ListExternalAppsQuery())
        result = await service.open_external(_command("a.txt", app_id="vscode"))
        return page, result

    page, result = asyncio.run(run())

    assert any(app.id == SYSTEM_APP_ID for app in page.apps)
    assert result.opened is True and result.app_id == "vscode"
    assert len(seen) == 1
    command, session = seen[0]
    assert command.path == "a.txt" and command.app_id == "vscode"
    assert str(getattr(session, "workspace", "")) == str(workspace)
