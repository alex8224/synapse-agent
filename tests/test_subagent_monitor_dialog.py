import asyncio

from textual.app import App, ComposeResult
from textual.widgets import Static

from synapse.subagent_monitor import SubagentMonitor, SubagentRun
from synapse.ui.dialogs.subagent_monitor import SubagentMonitorDialog, SubagentRunRow


def test_subagent_row_shows_dash_without_dependencies():
    run = SubagentRun(
        call_id="call-a",
        task_id="agent-loop",
        subagent_type="researcher",
        description="demo",
        depends_on=[],
    )

    assert "dep:-" in SubagentRunRow(run)._build_text().plain


def test_subagent_row_shows_dependency_task_ids():
    run = SubagentRun(
        call_id="call-b",
        task_id="ui-layer",
        subagent_type="researcher",
        description="demo",
        depends_on=["core-arch", "aux-systems"],
    )

    assert "dep:core-arch, aux-systems" in SubagentRunRow(run)._build_text().plain


def test_subagent_run_row_disables_text_selection():
    run = SubagentRun(
        call_id="call-c",
        task_id="tool-output",
        subagent_type="researcher",
        description="demo",
    )

    row = SubagentRunRow(run)

    assert SubagentRunRow.ALLOW_SELECT is False
    assert row.allow_select is False


def test_subagent_monitor_dialog_click_rows_during_refresh():
    monitor = SubagentMonitor()
    for idx in range(4):
        monitor.start_task(
            call_id=f"call-{idx}",
            task_id=f"agent-{idx}",
            subagent_type="researcher",
            description="demo",
        )

    class MonitorHost(App[None]):
        def get_css_variables(self) -> dict[str, str]:
            return {
                **super().get_css_variables(),
                "theme-bg": "#2e3440",
                "theme-user": "#88c0d0",
                "theme-fg": "#eceff4",
                "theme-top": "#3b4252",
                "theme-muted": "#6b7280",
                "theme-border": "#4c566a",
                "theme-bar": "#3b4252",
            }

        def compose(self) -> ComposeResult:
            yield Static("")

        def on_mount(self) -> None:
            self.push_screen(SubagentMonitorDialog(monitor))

    app = MonitorHost()

    async def exercise() -> None:
        async with app.run_test(size=(120, 32)) as pilot:
            await pilot.pause()
            dialog = app.screen
            assert isinstance(dialog, SubagentMonitorDialog)

            for idx in range(4):
                rows = list(dialog.query(SubagentRunRow))
                assert len(rows) == 4
                await pilot.click(rows[idx], offset=(2, 0))
                monitor.add_event(
                    f"call-{idx}",
                    kind="tool",
                    title="execute · inspect state",
                    status="running",
                )
                dialog._refresh(force=True)
                await pilot.pause()

            rows = list(dialog.query(SubagentRunRow))
            assert len(rows) == 4
            assert all(row.allow_select is False for row in rows)

    asyncio.run(asyncio.wait_for(exercise(), timeout=8))
