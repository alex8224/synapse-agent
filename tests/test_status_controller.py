from __future__ import annotations

from types import SimpleNamespace

from textual.css.query import NoMatches

from synapse.ui.status_controller import StatusController


class _UnmountedApp:
    settings = SimpleNamespace()
    size = SimpleNamespace(width=80)
    is_running = False
    sub_title = ""

    def query_one(self, *_args, **_kwargs):
        raise NoMatches("#status")

    def _refresh_bottombar(self) -> None:
        raise AssertionError("must not refresh an unmounted status bar")


def test_status_controller_ignores_missing_status_during_unmount() -> None:
    controller = StatusController(_UnmountedApp())

    controller.set_activity("tool", "running")
    controller.tick()
    controller.render_status()

    assert controller._phase == "tool"
    assert controller._detail == "running"
