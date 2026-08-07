"""Unit tests for the TUI prompt controller (completion/history/paste)."""

from __future__ import annotations

from types import SimpleNamespace

from synapse.ui.prompt_controller import PromptController


class _FakePrompt:
    def __init__(self) -> None:
        self.value = ""
        self.cursor_position = 0
        self.has_focus = True
        self._suggestion = ""
        self.focused = False

    def focus(self) -> None:
        self.focused = True


class _FakeHint:
    def __init__(self) -> None:
        self.rendered = ""

    def update(self, text: str) -> None:
        self.rendered = text


class _FakeApp:
    """Minimal host surface for PromptController tests."""

    def __init__(self, project_root: str | None = "ws") -> None:
        self.project_root = project_root
        self.settings = SimpleNamespace()
        self.screen = SimpleNamespace()
        self.prompt = _FakePrompt()
        self.hint = _FakeHint()
        self.events: list[str] = []

    def query_one(self, selector: str, _type=None):
        if selector == "#prompt":
            return self.prompt
        if selector == "#complete-hint":
            return self.hint
        raise KeyError(selector)

    def append_event(self, text: str, style: str = "") -> None:
        self.events.append(text)


class _FakeHistory:
    def __init__(self) -> None:
        self.items: list[str] = []

    def up(self, value: str) -> str | None:
        return self.items[-1] if self.items else None

    def down(self, value: str) -> str | None:
        return None

    def add(self, text: str) -> None:
        self.items.append(text)


class _FakeImageBank:
    def __init__(self) -> None:
        self.items: dict[int, object] = {}
        self.next_id = 1

    def add_bytes(self, data: bytes, mime: str | None = None, name: str | None = None):
        att = SimpleNamespace(id=self.next_id, name=name or "paste", mime=mime)
        self.items[self.next_id] = att
        self.next_id += 1
        return att


def _make_controller(project_root: str | None = "ws") -> tuple[PromptController, _FakeApp]:
    app = _FakeApp(project_root)
    controller = PromptController(app, _FakeHistory(), _FakeImageBank())
    return controller, app


def test_complete_context_is_cached_between_keystrokes(monkeypatch) -> None:
    controller, _ = _make_controller()
    built: list[object] = []

    def build(_settings):
        value = object()
        built.append(value)
        return value

    monkeypatch.setattr("synapse.commands.slash_complete.build_complete_context", build)

    first = controller.complete_ctx()
    second = controller.complete_ctx()

    assert first is second
    assert built == [first]


def test_expand_paste_replaces_placeholder_and_clears() -> None:
    controller, _ = _make_controller()
    controller.state.paste_replacements["[prefix... 12345 chars]"] = "FULL TEXT BODY"
    text, display = controller.expand_paste("see [prefix... 12345 chars] more")

    assert text == "see FULL TEXT BODY more"
    assert "FULL TEXT BODY" not in display
    assert "[prefix" in display  # compressed placeholder kept for rendering
    assert controller.state.paste_replacements == {}


def test_expand_paste_untouched_text_passes_through() -> None:
    controller, _ = _make_controller()
    controller.state.paste_replacements["[x... 10 chars]"] = "body"
    text, display = controller.expand_paste("plain message")

    assert text == "plain message"
    assert display == "plain message"


def test_on_prompt_changed_clears_stale_paste_placeholder() -> None:
    controller, app = _make_controller()
    controller.state.paste_replacements["[a... 5 chars]"] = "AAAAA"
    controller.state.paste_replacements["[b... 5 chars]"] = "BBBBB"

    controller.on_prompt_changed("keeps [a... 5 chars] only")

    assert list(controller.state.paste_replacements) == ["[a... 5 chars]"]


def test_on_prompt_changed_resets_slash_completion_state() -> None:
    controller, _ = _make_controller()
    controller.state.complete_base_value = "/model"
    controller.state.complete_active_idx = 2
    controller.state.complete_applied = "/model gpt-x"
    controller.state.complete_candidates = ["/model gpt-x"]

    controller.on_prompt_changed("plain text")

    assert controller.state.complete_active_idx == 0
    assert controller.state.complete_applied is None
    assert controller.state.complete_candidates == []
    # set_complete_hint re-bases the session on the current value
    assert controller.state.complete_base_value == "plain text"


def test_on_prompt_changed_keeps_at_session() -> None:
    controller, _ = _make_controller()
    controller.state.complete_base_value = "@src/foo"
    controller.state.complete_applied = "@src/foobar"

    controller.on_prompt_changed("@src/foobar")

    # @ session survives; hint refresh still runs against base value
    assert controller.state.complete_base_value == "@src/foo"


def test_focus_next_falls_back_when_prompt_unfocused() -> None:
    controller, app = _make_controller()
    app.prompt.has_focus = False
    app.prompt.value = "/model"

    assert controller.focus_next() is False


def test_focus_next_handles_slash_completion() -> None:
    controller, app = _make_controller()
    app.prompt.value = "/model"

    assert controller.focus_next() is True


def test_history_up_recalls_input_history() -> None:
    controller, app = _make_controller()
    controller._input_history.items.append("old message")
    app.prompt.value = ""

    controller.history_up()

    assert app.prompt.value == "old message"
    assert app.prompt.cursor_position == len("old message")


def test_history_up_navigates_completion_menu_when_active() -> None:
    controller, app = _make_controller()
    controller.state.complete_base_value = "/model"
    controller.state.complete_active_idx = 0
    app.prompt.value = "/model"

    controller.history_up()

    # Active menu navigation does not recall history
    assert app.prompt.value == "/model"


def test_paste_clipboard_stores_large_text_as_placeholder() -> None:
    from unittest.mock import patch

    controller, app = _make_controller()
    big = "x" * 500

    class _Result:
        kind = "text"
        text = big

    with patch("synapse.content.multimodal.read_clipboard", return_value=_Result()):
        controller.paste_clipboard()

    assert "500 chars]" in app.prompt.value
    assert controller.state.paste_replacements[app.prompt.value.strip()] == big
    assert "pasted text truncated" in app.events[-1]
