"""Tests for the lazy session message search index."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from synapse.sessions.search_index import SessionSearchIndex


def _msg(role: str, content: str) -> SimpleNamespace:
    return SimpleNamespace(type=role, content=content)


def test_replace_thread_filters_tool_and_system(tmp_path):
    idx = SessionSearchIndex(tmp_path / "search-index.sqlite")
    msgs = [
        _msg("human", "hello world"),
        _msg("ai", "hi there"),
        _msg("tool", "secret tool output"),
        _msg("system", "system prompt"),
    ]
    idx._replace_thread("t1", "ckpt-1", msgs, "2026-01-01T00:00:00+00:00")
    rows = idx._conn.execute("SELECT role, content FROM messages ORDER BY seq").fetchall()
    assert [(r["role"], r["content"]) for r in rows] == [
        ("human", "hello world"),
        ("ai", "hi there"),
    ]
    assert idx._get_last_indexed("t1") == "ckpt-1"


def test_search_matches_text_and_skips_tools(tmp_path):
    idx = SessionSearchIndex(tmp_path / "search-index.sqlite")
    idx._replace_thread(
        "t1",
        "c1",
        [
            _msg("human", "discuss headroom kompress"),
            _msg("ai", "analysis done"),
            _msg("tool", "headroom raw output"),
        ],
        "2026-01-01T00:00:00+00:00",
    )
    idx._replace_thread(
        "t2",
        "c1",
        [_msg("human", "another topic")],
        "2026-01-02T00:00:00+00:00",
    )
    hits = idx.search("headroom")
    assert len(hits) == 1
    assert hits[0]["thread_id"] == "t1"
    assert hits[0]["role"] == "human"
    # 工具消息未索引，搜不到
    assert idx.search("raw output") == []


def test_search_roles_filter(tmp_path):
    idx = SessionSearchIndex(tmp_path / "search-index.sqlite")
    idx._replace_thread(
        "t1",
        "c1",
        [_msg("human", "question about x"), _msg("ai", "answer about x")],
        "2026-01-01T00:00:00+00:00",
    )
    hits = idx.search("about x", roles=("ai",))
    assert len(hits) == 1
    assert hits[0]["role"] == "ai"


def test_schema_version_change_clears_index(tmp_path):
    path = tmp_path / "search-index.sqlite"
    idx = SessionSearchIndex(path)
    idx._replace_thread("t1", "c1", [_msg("human", "some text")], "")
    assert idx.indexed_count() == 1
    idx.close()

    # 模拟旧版本索引（数据仍在，版本号不同）
    conn = sqlite3.connect(path)
    conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    idx2 = SessionSearchIndex(path)
    assert idx2.indexed_count() == 0


def test_multimodal_content_only_text_indexed(tmp_path):
    idx = SessionSearchIndex(tmp_path / "search-index.sqlite")
    msg = SimpleNamespace(
        type="human",
        content=[
            {"type": "text", "text": "look at this image"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}},
        ],
    )
    idx._replace_thread("t1", "c1", [msg], "")
    # 图片 URL/base64 不进索引
    hits = idx.search("base64")
    assert len(hits) == 0
    hits = idx.search("look at this")
    assert len(hits) == 1
