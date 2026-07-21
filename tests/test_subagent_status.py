"""Subagent status label helpers."""

from synapse.ui.stream import human_nested_tools_detail, human_tool_label


def test_human_tool_label_prefers_intent():
    label = human_tool_label(
        {
            "name": "read_file",
            "args": {"intent": "读取 pytest 配置", "file_path": "/pyproject.toml"},
        }
    )
    assert "读取 pytest 配置" in label
    assert "pyproject" not in label


def test_human_nested_tools_detail_joins_intents():
    detail = human_nested_tools_detail(
        [
            {"name": "read_file", "args": {"intent": "读 A"}},
            {"name": "grep", "args": {"intent": "搜 B"}},
            {"name": "glob", "args": {"intent": "匹配 C"}},
            {"name": "execute", "args": {"intent": "跑 D"}},
        ],
        limit=3,
    )
    assert "读 A" in detail
    assert "搜 B" in detail
    assert "+1" in detail
