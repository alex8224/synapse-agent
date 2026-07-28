"""Git changes popover mount/hide edge cases."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import PropertyMock, patch

from synapse.ui.topbar.core import TopBarRegistry
from synapse.ui.topbar.git_changes_popover import TopBar
from synapse.ui.topbar.git_chrome import GitChangedFile


class _FakeTimer:
    def stop(self) -> None:
        return None


class _FakeSize:
    def __init__(self, width: int = 120) -> None:
        self.width = width


class _FakeScreen:
    def __init__(self) -> None:
        self.nodes: list[object] = []
        self.size = _FakeSize(120)
        self.mount_count = 0
        self.ids: list[str | None] = []

    def mount(self, widget: object) -> None:
        wid = getattr(widget, "id", None)
        if wid is not None and wid in self.ids:
            raise RuntimeError(
                f"Tried to insert a widget with ID {wid!r}, but a widget "
                f"already exists with that ID"
            )
        self.nodes.append(widget)
        self.ids.append(wid)
        self.mount_count += 1

    def query(self, cls: type) -> list[object]:
        return [n for n in self.nodes if isinstance(n, cls)]


def _sample_files() -> list[GitChangedFile]:
    return [
        GitChangedFile(
            path="src/a.py",
            status="M",
            lines_added=1,
            lines_deleted=0,
            is_untracked=False,
            source_path=None,
        )
    ]


def _make_topbar(files: list[GitChangedFile]) -> TopBar:
    reg = TopBarRegistry()
    bar = TopBar(
        registry_provider=lambda: reg,
        workspace_provider=lambda: Path("."),
        dirty_provider=lambda: True,
        usable_width_provider=lambda: 80,
        colors={},
        id="topbar-test",
    )
    bar.set_timer = lambda *_a, **_k: _FakeTimer()  # type: ignore[method-assign]
    bar._load_files = lambda force=False: files  # type: ignore[method-assign]
    bar._branch_span = lambda: (10, 12)  # type: ignore[method-assign]
    return bar


def test_show_popover_is_idempotent_when_already_open() -> None:
    screen = _FakeScreen()
    bar = _make_topbar(_sample_files())
    with (
        patch.object(TopBar, "screen", new_callable=PropertyMock) as screen_prop,
        patch.object(
            TopBar,
            "_popover_is_attached",
            side_effect=lambda pop: pop is not None and pop in screen.nodes,
        ),
    ):
        screen_prop.return_value = screen
        bar.show_popover()
        assert screen.mount_count == 1
        first = bar._popover
        assert first is not None

        bar.show_popover()
        assert screen.mount_count == 1
        assert bar._popover is first


def test_show_popover_uses_unique_ids_across_remount() -> None:
    screen = _FakeScreen()
    bar = _make_topbar(_sample_files())
    with (
        patch.object(TopBar, "screen", new_callable=PropertyMock) as screen_prop,
        patch.object(
            TopBar,
            "_popover_is_attached",
            side_effect=lambda pop: pop is not None and pop in screen.nodes,
        ),
    ):
        screen_prop.return_value = screen
        bar.show_popover()
        first = bar._popover
        assert first is not None
        first_id = first.id

        # Historical fixed-id orphan still registered on screen ids.
        screen.ids.append("git-changes-popover")
        bar._popover = None

        bar.show_popover()
        second = bar._popover
        assert second is not None
        assert second.id != first_id
        assert second.id != "git-changes-popover"
        assert str(second.id).startswith("git-changes-popover-")
        assert len(screen.ids) == len(set(screen.ids))


def test_dismiss_clears_controller_handle() -> None:
    screen = _FakeScreen()
    bar = _make_topbar(_sample_files())
    with (
        patch.object(TopBar, "screen", new_callable=PropertyMock) as screen_prop,
        patch.object(
            TopBar,
            "_popover_is_attached",
            side_effect=lambda pop: pop is not None and pop in screen.nodes,
        ),
    ):
        screen_prop.return_value = screen
        bar.show_popover()
        assert bar._popover is not None
        bar.dismiss()
        assert bar._popover is None
