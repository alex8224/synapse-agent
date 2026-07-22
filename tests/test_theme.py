"""UI theme registry, config wiring, and /theme slash command."""

from __future__ import annotations

import json
from pathlib import Path

from synapse.config import Settings, load_settings
from synapse.slash_cmds import handle_slash
from synapse.slash_complete import SlashCompleteContext, complete_slash
from synapse.ui import theme as theme_mod
from synapse.ui.theme import (
    BUILTIN_THEMES,
    DEFAULT_THEME_NAME,
    bootstrap_theme,
    format_theme_list_lines,
    get_theme,
    list_theme_names,
    load_custom_themes,
    persist_theme_preference,
    reload_theme_catalog,
    set_theme,
)


def test_builtin_themes_present():
    names = set(BUILTIN_THEMES)
    assert DEFAULT_THEME_NAME in names
    for expected in (
        "ansi",
        "cursor-dark",
        "github-dark",
        "dracula",
        "nord",
        "solarized-dark",
        "solarized-light",
        "catppuccin-mocha",
        "one-dark",
        "github-light",
        "one-light",
        "gruvbox-light",
        "catppuccin-latte",
        "tokyo-night-light",
        "ayu-light",
        "nord-light",
    ):
        assert expected in names
    assert len(names) >= 16


def test_set_theme_runtime_switch():
    bootstrap_theme(DEFAULT_THEME_NAME)
    assert get_theme().name == DEFAULT_THEME_NAME
    t = set_theme("dracula", persist=False, reload=False)
    assert t.name == "dracula"
    assert get_theme().bg == BUILTIN_THEMES["dracula"].bg
    set_theme(DEFAULT_THEME_NAME, persist=False, reload=False)


def test_custom_theme_extends(tmp_path: Path, monkeypatch):
    cfg = tmp_path / ".synapse"
    cfg.mkdir()
    (cfg / "themes.json").write_text(
        json.dumps(
            {
                "themes": {
                    "my-dark": {
                        "extends": "cursor-dark",
                        "label": "My Dark",
                        "bg": "#010203",
                        "user": "#112233",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(theme_mod, "user_config_dir", lambda: tmp_path / "nouser")
    # project layer = tmp_path/.synapse via workspace=tmp_path
    customs = load_custom_themes(tmp_path)
    assert "my-dark" in customs
    t = customs["my-dark"]
    assert t.bg == "#010203"
    assert t.user == "#112233"
    # inherited
    assert t.fg == BUILTIN_THEMES["cursor-dark"].fg


def test_persist_and_load_settings_theme(tmp_path: Path, monkeypatch):
    user = tmp_path / "home" / ".synapse"
    user.mkdir(parents=True)
    monkeypatch.setattr(theme_mod, "user_config_dir", lambda: user)
    # Also patch config user dir used by load_settings layers
    import synapse.config_paths as cfgp

    monkeypatch.setattr(cfgp, "user_config_dir", lambda: user)
    monkeypatch.setattr(cfgp, "executable_config_dirs", lambda: [])

    path = persist_theme_preference("nord", workspace=tmp_path, scope="user")
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["theme"] == "nord"

    workspace = tmp_path / "ws"
    workspace.mkdir()
    settings = load_settings(workspace=workspace)
    assert settings.theme == "nord"
    assert get_theme().name == "nord"


def test_settings_default_theme(monkeypatch):
    monkeypatch.delenv("AGENT_THEME", raising=False)
    s = Settings(_env_file=None)
    assert s.theme == "cursor-dark"


def test_format_theme_list_lines():
    bootstrap_theme("github-dark")
    lines = format_theme_list_lines()
    assert any("github-dark" in line and "*" in line for line in lines)
    assert any("usage:" in line for line in lines)


def test_slash_theme_list_and_switch(tmp_path: Path, monkeypatch):
    user = tmp_path / "home" / ".synapse"
    user.mkdir(parents=True)
    monkeypatch.setattr(theme_mod, "user_config_dir", lambda: user)
    import synapse.config_paths as cfgp

    monkeypatch.setattr(cfgp, "user_config_dir", lambda: user)

    settings = Settings(_env_file=None, theme="cursor-dark")
    result = handle_slash(
        "/theme",
        settings=settings,
        agent=object(),
        thread_id="t1",
        project_root=tmp_path,
    )
    assert result.handled
    assert not result.error
    assert any("available:" in line or "theme:" in line for line in result.lines)

    result2 = handle_slash(
        "/theme nord",
        settings=settings,
        agent=object(),
        thread_id="t1",
        project_root=tmp_path,
    )
    assert result2.handled
    assert not result2.error
    assert result2.theme_name == "nord"
    assert settings.theme == "nord"
    assert (user / "settings.json").is_file()

    bad = handle_slash(
        "/theme no-such-theme",
        settings=settings,
        agent=object(),
        thread_id="t1",
        project_root=tmp_path,
    )
    assert bad.error


def test_slash_complete_theme():
    reload_theme_catalog()
    names = list_theme_names()
    assert names
    cands = complete_slash("/theme ", SlashCompleteContext())
    joined = " ".join(cands)
    assert "list" in joined
    assert names[0] in joined
    cands2 = complete_slash("/theme dra", SlashCompleteContext())
    assert any("dracula" in c for c in cands2)


def test_css_variables_keys():
    vars_ = BUILTIN_THEMES["cursor-dark"].css_variables()
    assert vars_["theme-bg"].startswith("#")
    assert "theme-fg" in vars_
    assert "theme-border-focus" in vars_


def _contrast_ratio(foreground: str, background: str) -> float:
    def luminance(color: str) -> float:
        channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [
            channel / 12.92
            if channel <= 0.04045
            else ((channel + 0.055) / 1.055) ** 2.4
            for channel in channels
        ]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    light, dark = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def test_light_theme_text_contrast():
    light_theme_names = (
        "solarized-light",
        "github-light",
        "one-light",
        "gruvbox-light",
        "catppuccin-latte",
        "tokyo-night-light",
        "ayu-light",
        "nord-light",
    )
    for name in light_theme_names:
        theme = BUILTIN_THEMES[name]
        assert _contrast_ratio(theme.fg, theme.bg) >= 4.5, name
        assert _contrast_ratio(theme.dim, theme.bg) >= 4.5, name
        assert _contrast_ratio(theme.muted, theme.bg) >= 3.4, name


def test_tui_css_variables_preserve_textual_defaults():
    from synapse.ui.tui import CodingAgentApp

    app = CodingAgentApp(
        agent=object(),
        settings=Settings(_env_file=None),
        thread_id="theme-test",
    )
    variables = app.get_css_variables()
    assert "background" in variables
    assert "foreground" in variables
    assert "theme-muted" in variables


def test_tui_css_does_not_override_runtime_theme_variables():
    from synapse.ui.tui import CodingAgentApp

    assert "$theme-bg:" not in CodingAgentApp.CSS
    assert "$theme-fg:" not in CodingAgentApp.CSS
    assert "#log {" in CodingAgentApp.CSS
    assert "color: $theme-fg;" in CodingAgentApp.CSS


def test_ansi_theme_is_transparent():
    from synapse.ui.theme import resolve_theme_name, theme_kind

    assert resolve_theme_name("inherit") == "ansi"
    assert resolve_theme_name("terminal") == "ansi"
    assert resolve_theme_name("auto") == "ansi"
    t = set_theme("ansi", persist=False, reload=False)
    assert t.name == "ansi"
    assert t.bg == "transparent"
    assert t.top == "transparent"
    assert t.ansi is True
    assert t.is_terminal_inherit is True
    assert theme_kind(t) == "ansi"
    # Rich Text styles (no ansi_ prefix).
    assert t.fg == "default"
    assert t.dim == "bright_black"
    assert "ansi_" not in t.dim
    # CSS dual tokens for Textual.
    css = t.css_variables()
    assert css["theme-bg"] == "transparent"
    assert css["theme-fg"] == "ansi_default"
    assert css["theme-user"] == "ansi_cyan"
    assert t.code_theme == "ansi_dark"
    # ANSI keeps original transparent topbar (no region color blocks).
    bands = t.topbar_region_bands()
    assert bands["left"][1] == ""
    assert bands["center"][1] == ""
    assert bands["right"][1] == ""
    set_theme(DEFAULT_THEME_NAME, persist=False, reload=False)


def test_topbar_region_bands_from_theme_and_override(tmp_path: Path, monkeypatch):
    """Built-ins have no region bands; themes.json can opt in + set pad/gap."""
    solid = set_theme("cursor-dark", persist=False, reload=False)
    bands = solid.topbar_region_bands()
    assert bands["left"][1] == ""
    assert bands["center"][1] == ""
    assert bands["right"][1] == ""
    # CSS metrics still present with classic defaults.
    assert solid.top_pad_x == 1
    assert solid.top_gap == 3
    assert solid.css_variables()["theme-top-pad-x"] == "1"
    assert solid.css_variables()["theme-top"] == solid.top

    cfg = tmp_path / ".synapse"
    cfg.mkdir()
    (cfg / "themes.json").write_text(
        json.dumps(
            {
                "themes": {
                    "banded": {
                        "extends": "cursor-dark",
                        "top_left": "#112233",
                        "top_center": "#445566",
                        "top_right": "none",
                        "top_pad_x": 2,
                        "top_gap": 1,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(theme_mod, "user_config_dir", lambda: tmp_path / "nouser")
    customs = load_custom_themes(tmp_path)
    t = customs["banded"]
    b = t.topbar_region_bands()
    assert b["left"][1] == "#112233"
    assert b["center"][1] == "#445566"
    assert b["right"][1] == ""  # suppressed
    assert t.top_pad_x == 2
    assert t.top_gap == 1
    assert t.css_variables()["theme-top-pad-x"] == "2"
    set_theme(DEFAULT_THEME_NAME, persist=False, reload=False)


def test_textual_themes_registerable():
    from synapse.ui.theme import (
        TEXTUAL_THEME_ANSI,
        TEXTUAL_THEME_DARK,
        TEXTUAL_THEME_LIGHT,
        apply_textual_theme,
        textual_themes,
    )

    themes = {t.name: t for t in textual_themes()}
    assert TEXTUAL_THEME_ANSI in themes
    assert themes[TEXTUAL_THEME_ANSI].background == "transparent"
    assert themes[TEXTUAL_THEME_ANSI].surface == "transparent"
    assert themes[TEXTUAL_THEME_ANSI].ansi is True
    assert themes[TEXTUAL_THEME_ANSI].variables.get("ansi-background") == "transparent"
    assert themes[TEXTUAL_THEME_DARK].background.startswith("#")
    assert themes[TEXTUAL_THEME_LIGHT].dark is False

    class FakeApp:
        def __init__(self) -> None:
            self.theme = "textual-dark"
            self.registered: list[str] = []

        def register_theme(self, th: object) -> None:
            self.registered.append(getattr(th, "name", ""))

    app = FakeApp()
    set_theme("ansi", persist=False, reload=False)
    applied = apply_textual_theme(app)
    assert applied == TEXTUAL_THEME_ANSI
    assert app.theme == TEXTUAL_THEME_ANSI
    assert TEXTUAL_THEME_ANSI in app.registered
    set_theme("github-light", persist=False, reload=False)
    assert apply_textual_theme(app) == TEXTUAL_THEME_LIGHT
    set_theme("dracula", persist=False, reload=False)
    assert apply_textual_theme(app) == TEXTUAL_THEME_DARK
    set_theme(DEFAULT_THEME_NAME, persist=False, reload=False)


def test_slash_theme_ansi_alias(tmp_path: Path, monkeypatch):
    user = tmp_path / "home" / ".synapse"
    user.mkdir(parents=True)
    monkeypatch.setattr(theme_mod, "user_config_dir", lambda: user)
    import synapse.config_paths as cfgp

    monkeypatch.setattr(cfgp, "user_config_dir", lambda: user)

    settings = Settings(_env_file=None, theme="cursor-dark")
    result = handle_slash(
        "/theme inherit",
        settings=settings,
        agent=object(),
        thread_id="t1",
        project_root=tmp_path,
    )
    assert result.handled
    assert not result.error
    assert result.theme_name == "ansi"
    assert settings.theme == "ansi"
    assert get_theme().bg == "transparent"
