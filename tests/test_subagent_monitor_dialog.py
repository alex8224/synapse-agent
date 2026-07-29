from synapse.subagent_monitor import SubagentRun
from synapse.ui.dialogs.subagent_monitor import SubagentRunRow


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
