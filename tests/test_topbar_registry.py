"""Topbar registry: freeform regions, components, packing."""

from __future__ import annotations

from rich.text import Text

from synapse.ui.topbar import (
    TopBarAlign,
    TopBarRegion,
    TopBarRegionSpec,
    TopBarRegistry,
    display_width,
    install_default_components,
    layout_from_registry,
    pack_topbar_regions,
    render_region_text,
)


def test_region_parse_and_reject() -> None:
    assert TopBarRegion.parse("left") is TopBarRegion.LEFT
    assert TopBarRegion.parse(TopBarRegion.RIGHT) is TopBarRegion.RIGHT
    try:
        TopBarRegion.parse("middle")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_register_order_and_region_move() -> None:
    reg = TopBarRegistry()
    reg.register_fn("a", lambda: "A", region="right", order=20)
    reg.register_fn("b", lambda: "B", region="right", order=10)
    assert render_region_text(reg.components("right")) == "B  ·  A"

    assert reg.set_region("a", "left")
    assert reg.set_order("b", 5)
    assert [c.id for c in reg.components("left")] == ["a"]
    assert [c.id for c in reg.components("right")] == ["b"]


def test_visible_and_unregister() -> None:
    reg = TopBarRegistry()
    reg.register_fn("x", lambda: "X", region="left", order=1)
    reg.register_fn("y", lambda: "Y", region="left", order=2)
    assert reg.set_visible("x", False)
    assert render_region_text(reg.components("left")) == "Y"
    assert reg.unregister("y")
    assert render_region_text(reg.components("left")) == ""
    assert "x" in reg
    assert len(reg.components("left", include_hidden=True)) == 1


def test_install_defaults_layout_contains_pieces() -> None:
    reg = TopBarRegistry()
    install_default_components(
        reg,
        workspace=lambda: "proj/ws",
        title=lambda: "My Session",
        branch=lambda: "main",
        usage=lambda: "1K/0/0 500/50%",
    )
    left = render_region_text(reg.components(TopBarRegion.LEFT))
    right = render_region_text(reg.components(TopBarRegion.RIGHT))
    assert "proj/ws" in left
    assert "main" in left
    assert left.index("proj/ws") < left.index("main")
    assert "1K/0/0" in right
    assert "main" not in right
    line = layout_from_registry(reg, usable_width=100)
    plain = line.plain
    assert "My Session" in plain
    assert display_width(plain) <= 100


def test_custom_component_in_right_region() -> None:
    reg = TopBarRegistry()
    install_default_components(
        reg,
        workspace=lambda: "ws",
        title=lambda: "t",
        branch=lambda: "",
        usage=lambda: "u",
    )
    reg.register_fn(
        "mode",
        lambda: "safe",
        region=TopBarRegion.RIGHT,
        order=5,
        priority=30,
    )
    right = render_region_text(reg.components(TopBarRegion.RIGHT))
    assert right == "safe  ·  u"
    left = render_region_text(reg.components(TopBarRegion.LEFT))
    assert left.startswith("≡") or "ws" in left


def test_pack_prefers_right_over_center_when_tight() -> None:
    packed = pack_topbar_regions(
        usable_width=40,
        left="≡  long-workspace-name",
        center="a very long session title that should shrink",
        right="2M/1.9M/12K 300K/60%",
        col_gap=3,
    )
    assert display_width(packed.as_plain()) <= 40
    assert packed.right


def test_priority_drops_low_priority_sibling() -> None:
    reg = TopBarRegistry()
    reg.register_fn("keep", lambda: "KEEP", region="right", order=1, priority=100)
    reg.register_fn("drop", lambda: "DROPME", region="right", order=2, priority=1)
    line = layout_from_registry(reg, usable_width=12)
    plain = line.plain
    assert "KEEP" in plain
    assert display_width(plain) <= 12


def test_layout_returns_rich_text() -> None:
    reg = TopBarRegistry()
    reg.register_fn("l", lambda: "L", region="left", order=1)
    reg.register_fn("c", lambda: "C", region="center", order=1)
    reg.register_fn("r", lambda: "R", region="right", order=1)
    line = layout_from_registry(reg, usable_width=40)
    assert isinstance(line, Text)
    assert "L" in line.plain and "C" in line.plain and "R" in line.plain


def test_custom_region_with_width_align_fg_bg() -> None:
    reg = TopBarRegistry()
    reg.register_region(
        "badge",
        order=25,
        width=10,
        align="center",
        fg="#ffffff",
        bg="#c44",
        gap_after=2,
        priority=80,
    )
    reg.register_fn("badge_text", lambda: "HOT", region="badge", order=1)
    reg.register_fn("left_x", lambda: "L", region="left", order=1)
    reg.register_fn("right_x", lambda: "R", region="right", order=1)

    spec = reg.get_region("badge")
    assert isinstance(spec, TopBarRegionSpec)
    assert spec.width == 10
    assert spec.align is TopBarAlign.CENTER
    assert spec.bg == "#c44"

    line = layout_from_registry(reg, usable_width=40)
    plain = line.plain
    assert "HOT" in plain
    assert "L" in plain and "R" in plain
    assert display_width(plain) <= 40


def test_region_order_places_custom_before_left() -> None:
    reg = TopBarRegistry()
    reg.register_region("lead", order=1, width=4, align="left", fg="#0f0")
    reg.register_fn("lead_mark", lambda: ">>", region="lead")
    reg.register_fn("ws", lambda: "WS", region="left")
    line = layout_from_registry(reg, usable_width=30)
    plain = line.plain.rstrip()
    assert plain.index(">>") < plain.index("WS")


def test_configure_region_style() -> None:
    reg = TopBarRegistry()
    assert reg.set_region_style("left", fg="#abc", bg="#111", align="right", width=12)
    left = reg.get_region("left")
    assert left is not None
    assert left.fg == "#abc"
    assert left.bg == "#111"
    assert left.align is TopBarAlign.RIGHT
    assert left.width == 12
    assert not reg.set_region_style("nope", fg="#fff")
