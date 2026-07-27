"""MCP server & tool selection panel — invoked by /mcp or F5.

Shows servers and their discovered tools with checkboxes.  Select which tools
to enable per server, then save to the config file so only those tools are
loaded on next startup / reload.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding

from synapse.ui.dialogs.base import DialogBase, OptionItem, SectionHeader

# Wider dialog with visible scrollbar for potentially long tool lists.
MCP_DIALOG_CSS = """
McpPanelDialog {
    align: center middle;
    background: $theme-bg 60%;
}
McpPanelDialog > #dialog-window {
    width: 72;
    height: auto;
    max-height: 38;
    background: $theme-bg;
    border: round $theme-user;
    border-title-color: $theme-fg;
    border-title-background: $theme-top;
    border-title-style: bold;
    border-title-align: left;
    border-subtitle-color: $theme-muted;
    border-subtitle-align: right;
    padding: 0;
    layout: vertical;
}
McpPanelDialog #dialog-body {
    height: auto;
    max-height: 32;
    min-height: 3;
    width: 1fr;
    padding: 0 1;
    background: $theme-bg;
    overflow-y: auto;
    overflow-x: hidden;
    scrollbar-size: 1 2;
    scrollbar-background: $theme-bar;
    scrollbar-color: $theme-dim;
    scrollbar-background-hover: $theme-bar;
    scrollbar-color-hover: $theme-user;
    scrollbar-background-active: $theme-bar;
    scrollbar-color-active: $theme-user;
}
McpPanelDialog DialogBody OptionRow {
    height: 1;
    width: 1fr;
    color: $theme-dim;
    padding: 0 1;
    background: $theme-bg;
    overflow: hidden;
    text-overflow: ellipsis;
}
McpPanelDialog DialogBody OptionRow.-selected {
    color: $theme-user;
    background: $theme-bar;
    text-style: bold;
}
McpPanelDialog DialogBody SectionHeader {
    height: 1;
    width: 1fr;
    color: $theme-orange;
    padding: 0 1;
    text-style: bold;
}
"""


def _resolve_mcp_config_path(settings: Any, project_root: Path | None = None) -> Path | None:
    """Find the mcp.json file to write to (prefer explicit → project → user)."""
    explicit: str | None = getattr(settings, "mcp_config_path", None)
    if explicit:
        p = Path(explicit).expanduser()
        if p.is_file():
            return p
        return p

    from synapse.config_paths import mcp_config_paths

    existing = mcp_config_paths(project_root)
    if existing:
        return existing[-1]

    from synapse.config_paths import project_config_dir

    return project_config_dir(project_root) / "mcp.json"


def _save_include_tools_to_config(
    settings: Any,
    server_name: str,
    include_tools: list[str] | None,
    project_root: Path | None = None,
) -> Path | None:
    """Write ``include_tools`` for one server into the mcp.json config file.

    Returns the path written, or None on failure.
    """
    config_path = _resolve_mcp_config_path(settings, project_root)
    if config_path is None:
        return None

    try:
        if config_path.is_file():
            data = json.loads(config_path.read_text(encoding="utf-8"))
        else:
            data = {}
    except (json.JSONDecodeError, OSError):
        data = {}

    if not isinstance(data, dict):
        data = {}

    servers: list[dict[str, Any]] = []
    if isinstance(data.get("servers"), list):
        servers = data["servers"]
    elif isinstance(data, list):
        servers = data

    for s in servers:
        if isinstance(s, dict) and s.get("name") == server_name:
            if include_tools is None or len(include_tools) == 0:
                s.pop("include_tools", None)
            else:
                s["include_tools"] = include_tools
            break
    else:
        return None

    config_path.parent.mkdir(parents=True, exist_ok=True)

    if isinstance(data.get("servers"), list):
        data["servers"] = servers
        config_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    elif isinstance(data, list):
        config_path.write_text(
            json.dumps(servers, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    return config_path


class McpPanelDialog(DialogBase):
    """List MCP servers and their tools; toggle per-tool; save to config."""

    DEFAULT_CSS = MCP_DIALOG_CSS
    BINDINGS = [
        *DialogBase.BINDINGS,
        Binding("a", "select_all", "All", show=False, priority=True),
        Binding("d", "deselect_all", "None", show=False, priority=True),
        Binding("s", "save", "Save", show=False, priority=True),
        Binding("r", "reload", "Reload", show=False, priority=True),
    ]
    _title_icon = "\u2b21"
    _title_keys = (
        "\u2191\u2193 move \u00b7 enter toggle \u00b7"
        " a all \u00b7 d none \u00b7 s save \u00b7 r reload \u00b7 esc close"
    )

    def __init__(
        self,
        settings: Any,
        *,
        project_root: Any = None,
        on_save: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self._settings = settings
        self._project_root = project_root
        self._on_save = on_save

        try:
            from synapse.mcp_client import get_active_mcp_pool
            from synapse.mcp_client import load_mcp_server_configs

            self._servers = load_mcp_server_configs(
                path=getattr(settings, "mcp_config_path", None),
                json_blob=getattr(settings, "mcp_servers_json", None),
                workspace=getattr(settings, "workspace", None),
            )
        except Exception:  # noqa: BLE001
            self._servers = []

        pool = None
        try:
            pool = get_active_mcp_pool()
        except Exception:  # noqa: BLE001
            pass

        # server_name → (all_discovered_tools, currently_included)
        self._server_tools: dict[str, tuple[list[str], set[str]]] = {}
        for srv in self._servers:
            discovered: list[str] = []
            if pool is not None:
                discovered = list(
                    getattr(pool, "discovered_tools", {}).get(srv.name, [])
                )
            if srv.include_tools is not None:
                included = set(srv.include_tools)
            else:
                included = set(discovered) if discovered else set()
            self._server_tools[srv.name] = (discovered, included)

    @property
    def title_text(self) -> str:
        return "MCP Tools"

    def compose_body(self) -> ComposeResult:
        items: list[OptionItem] = self._build_item_list()
        self._items = items
        yield SectionHeader("Servers & Tools")

    def _build_item_list(self) -> list[OptionItem]:
        items: list[OptionItem] = []
        if not self._servers:
            items.append(OptionItem(key="none", label="(no servers configured)"))
            return items

        for srv in self._servers:
            discovered, included = self._server_tools.get(srv.name, ([], set()))
            status = "enabled" if srv.enabled else "disabled"
            items.append(
                OptionItem(
                    key=f"__srv__{srv.name}",
                    label=f"Server: {srv.name}",
                    meta=f"{srv.transport} \u00b7 {status}",
                )
            )
            if not srv.enabled:
                continue
            if not discovered:
                items.append(
                    OptionItem(
                        key=f"__hint__{srv.name}",
                        label="  (no tools \u2014 press 'r' to connect)",
                    )
                )
                continue
            n_selected = len(included)
            n_total = len(discovered)
            summary = (
                f"{n_selected}/{n_total} selected"
                if n_selected < n_total
                else "all selected"
            )
            items.append(
                OptionItem(
                    key=f"__summary__{srv.name}",
                    label=f"  {summary}",
                )
            )
            for tool_name in discovered:
                checked = tool_name in included
                mark = "\u25cf" if checked else "\u25cb"
                items.append(
                    OptionItem(
                        key=f"__tool__{srv.name}__{tool_name}",
                        label=f"  {mark}  {tool_name}",
                    )
                )
        return items

    def on_mount(self) -> None:
        super().on_mount()
        body = self.query_one("#dialog-body")
        body.set_options(self._items, mark="")

    def action_reload(self) -> None:
        self.dismiss(("mcp-reload",))

    def action_save(self) -> None:
        """Collect tool selections and dismiss — save + reload runs off the UI thread."""
        # Build per-server include_tools to write to config.
        to_save: dict[str, list[str] | None] = {}
        for srv in self._servers:
            if not srv.enabled:
                continue
            discovered, included = self._server_tools.get(srv.name, ([], set()))
            if not discovered:
                continue
            if included == set(discovered):
                # All selected → remove include_tools (loads all)
                to_save[srv.name] = None
            else:
                to_save[srv.name] = sorted(included)

        if not to_save:
            self.dismiss(("mcp-reload",))
            return

        self.dismiss(("mcp-save", to_save))

    def _find_server_name_for_index(self, idx: int) -> str | None:
        if idx < 0 or idx >= len(self._items):
            return None
        key = self._items[idx].key
        for prefix in ("__tool__", "__summary__", "__hint__", "__srv__"):
            if key.startswith(prefix):
                parts = key.split("__", 3)
                if len(parts) >= 3:
                    return parts[2]
        return None

    def _on_selected(self, key: str | None) -> None:
        if key is None:
            self.dismiss(None)
            return
        if key.startswith("__tool__"):
            parts = key.split("__", 3)
            if len(parts) < 4:
                return
            server_name = parts[2]
            tool_name = parts[3]
            discovered, included = self._server_tools.get(server_name, ([], set()))
            if tool_name not in discovered:
                return
            if tool_name in included:
                included.discard(tool_name)
            else:
                included.add(tool_name)
            self._server_tools[server_name] = (discovered, included)
            self._rebuild()

    def action_select_all(self) -> None:
        server_name = self._resolve_server_context()
        if server_name is None:
            return
        discovered, _ = self._server_tools.get(server_name, ([], set()))
        self._server_tools[server_name] = (discovered, set(discovered))
        self._rebuild()

    def action_deselect_all(self) -> None:
        server_name = self._resolve_server_context()
        if server_name is None:
            return
        discovered, _ = self._server_tools.get(server_name, ([], set()))
        self._server_tools[server_name] = (discovered, set())
        self._rebuild()

    def _resolve_server_context(self) -> str | None:
        body = self.query_one("#dialog-body")
        idx = body._selected_idx
        for offset in range(0, -len(self._items), -1):
            s = self._find_server_name_for_index(idx + offset)
            if s:
                return s
        for offset in range(1, len(self._items)):
            s = self._find_server_name_for_index(idx + offset)
            if s:
                return s
        return None

    def _rebuild(self) -> None:
        body = self.query_one("#dialog-body")
        old_idx = body._selected_idx
        items = self._build_item_list()
        self._items = items
        body.set_options(items, mark="")
        body._selected_idx = min(old_idx, len(items) - 1) if items else 0
        body._sync_hover()