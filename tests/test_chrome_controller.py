from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from synapse.ui.chrome.controller import ChromeController


class _App:
    def __init__(self) -> None:
        self._codex = SimpleNamespace(
            refresh_usage=lambda: None,
            fetch_reset_credits=lambda: None,
            consume_reset=lambda _credit_id: SimpleNamespace(outcome="reset"),
            loading=False,
            consuming=False,
            reset_credits=None,
        )
        self.calls: list[str] = []
        self._ui_thread = True

    def call_from_thread(self, callback: Any, *args: Any, **kwargs: Any) -> None:
        if self._ui_thread:
            raise RuntimeError("must run in a different thread from the app")
        callback(*args, **kwargs)


def test_fetch_codex_usage_bg_falls_back_inline_on_ui_thread() -> None:
    app = _App()
    controller = ChromeController(app)
    app._on_codex_usage_ready = lambda: app.calls.append("ready")

    # Regression: on_mount runs on the UI thread; the usage fetch must not
    # raise RuntimeError from call_from_thread.
    controller.fetch_codex_usage_bg()

    assert app.calls == ["ready"]


def test_fetch_codex_reset_credits_bg_falls_back_inline_on_ui_thread() -> None:
    app = _App()
    controller = ChromeController(app)
    app._open_codex_reset_dialog = lambda: app.calls.append("open-dialog")

    controller.fetch_codex_reset_credits_for_dialog_bg()

    assert app.calls == ["open-dialog"]


def test_consume_codex_reset_bg_falls_back_inline_on_ui_thread() -> None:
    app = _App()
    controller = ChromeController(app)
    app._on_codex_reset_consumed = lambda result: app.calls.append(result.outcome)
    app._on_codex_reset_consume_done = lambda: app.calls.append("done")

    controller.consume_codex_reset_bg("credit-1")

    assert app.calls == ["reset"]


def test_fetch_codex_usage_bg_uses_call_from_thread_off_ui_thread() -> None:
    app = _App()
    app._ui_thread = False
    controller = ChromeController(app)
    app._on_codex_usage_ready = lambda: app.calls.append("ready")

    controller.fetch_codex_usage_bg()

    assert app.calls == ["ready"]
