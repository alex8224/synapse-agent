"""Tests for PromptInput terminal-native paste routing.

Shift+Insert / middle-click paste arrives as a bracketed-paste ``Paste`` event.
The stock ``Input`` keeps only the first line; ``PromptInput`` must forward the
full text to the prompt controller, stop propagation, and prevent the parent
``Input._on_paste`` from running (named handlers are not deduplicated across the
MRO, so ``stop()`` alone is insufficient).
"""

from __future__ import annotations

from textual.events import Paste

from synapse.ui.tui import PromptInput


def test_prompt_input_routes_full_multiline_paste() -> None:
    received: list[str] = []
    inp = PromptInput(on_paste_text=received.append)

    event = Paste("alpha\nbeta\ngamma")
    inp._on_paste(event)

    assert event._stop_propagation is True
    assert event._no_default_action is True
    assert received == ["alpha\nbeta\ngamma"]


def test_prompt_input_ignores_empty_paste() -> None:
    received: list[str] = []
    inp = PromptInput(on_paste_text=received.append)

    event = Paste("")
    inp._on_paste(event)

    assert event._stop_propagation is True
    assert event._no_default_action is True
    assert received == []


def test_prompt_input_prevents_parent_default_handler() -> None:
    """The parent ``Input._on_paste`` must not run after our handler."""
    received: list[str] = []
    inp = PromptInput(on_paste_text=received.append)

    event = Paste("line1\nline2")
    methods = inp._get_dispatch_methods(Paste.handler_name, event)

    first_cls, first_method = next(methods)
    assert first_cls is PromptInput
    first_method(event)

    # prevent_default() inside our handler must stop the dispatch generator
    # from yielding the inherited Input._on_paste.
    assert list(methods) == []
    assert received == ["line1\nline2"]
