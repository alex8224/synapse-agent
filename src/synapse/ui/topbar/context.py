"""Host context passed into topbar component installers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from rich.text import Text

# Providers may return plain text or pre-styled Rich Text.
LabelFn = Callable[[], str]
RichLabelFn = Callable[[], str | Text]


@dataclass(slots=True)
class TopBarContext:
    """Data sources for built-in (and custom) topbar components.

    App/host fills these callables; each component module only reads what it needs.
    """

    workspace: LabelFn
    title: LabelFn
    branch: RichLabelFn
    usage: LabelFn
    workspace_mark: str = "≡"
    branch_mark: str = "⎇"
