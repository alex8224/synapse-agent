"""Bottombar registry: freeform regions, components, packing."""

from __future__ import annotations

from rich.text import Text

from synapse.ui.bottombar import (
    BottomBarAlign,
    BottomBarRegion,
    BottomBarRegionSpec,
    BottomBarRegistry,
    display_width,
    install_default_components,
    layout_from_registry,
    pack_bottombar_regions,
    render_region_text,
)


def test_region_parse_and_reject() -> None:
    assert BottomBarRegion.parse("left") is BottomBarRegion.LEFT
    assert BottomBarRegion.parse(BottomBarRegion.RIGHT) is BottomBarRegion.RIGHT
    try:
        BottomBarRegion.parse("middle")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_register_order_and_region_move() -> None:
    reg = BottomBarRegistry()
    reg.register_fn("a", lambda: "A", region="right", order=20)
    reg.register_fn("b", lambda: "B", region="right", order=10)
    assert render_region_text(reg.components("right")) == "B  ·  A"

    assert reg.set_region("a", "left")
    assert reg.set_order("b", 5)
    assert [c.id for c in reg.components("left")] == ["a"]
    assert [c.id for c in reg.components("right")] == ["b"]


def test_visible_and_unregister() -> None:
    reg = BottomBarRegistry()
    reg.register_fn("x", lambda: "X", region="left", order=1)
    reg.register_fn("y", lambda: "Y", region="left", order=2)
    assert reg.set_visible("x", False)
    assert render_region_text(reg.components("left")) == "Y"
    assert reg.unregister("y")
    assert render_region_text(reg.components("left")) == ""
    assert "x" in reg
    assert len(reg.components("left", include_hidden=True)) == 1


def test_install_defaults_layout_contains_pieces() -> None:
    busy = {"v": False}
    reg = BottomBarRegistry()
    install_default_components(
        reg,
        busy=lambda: busy["v"],
        thread=lambda: "abcd…wxyz",
        mode=lambda: "steer×2" if busy["v"] else "",
        idle_hints=lambda: "Tab complete · Esc cancel",
        busy_hints=lambda: "Esc cancel · Enter queue",
        model=lambda: "haha-grok-4.5 · max",
        mcp=lambda: "mcp off",
    )
    left = render_region_text(reg.components(BottomBarRegion.LEFT))
    right = render_region_text(reg.components(BottomBarRegion.RIGHT))
    assert "Tab complete" in right
    assert "haha-grok-4.5 · max" in left
    assert "mcp off" in left
    # thread is not shown by default
    assert "abcd…wxyz" not in left
    # order: model → mcp (left)
    assert left.index("haha-grok") < left.index("mcp off")
    assert "thread" not in {c.id for c in reg.components(include_hidden=True)}

    busy["v"] = True
    right_busy = render_region_text(reg.components(BottomBarRegion.RIGHT))
    center = render_region_text(reg.components(BottomBarRegion.CENTER))
    assert "Enter queue" in right_busy
    assert "steer×2" in center

    line = layout_from_registry(reg, usable_width=120)
    plain = line.plain
    assert "Enter queue" in plain
    assert "haha-grok-4.5" in plain
    assert "mcp off" in plain
    assert display_width(plain) <= 120


def test_mcp_label_colors_by_status() -> None:
    from synapse.ui.bottombar.components.mcp import style_for_mcp_label

    on = style_for_mcp_label("mcp on")
    err = style_for_mcp_label("mcp err")
    off = style_for_mcp_label("mcp off")
    assert on != err
    assert err != off
    assert "81c995" in on  # green
    assert "f28b82" in err  # red
    assert "5f6368" in off  # muted

    status = {"v": "mcp on"}
    reg = BottomBarRegistry()
    install_default_components(
        reg,
        busy=lambda: False,
        thread=lambda: "",
        mode=lambda: "",
        idle_hints=lambda: "",
        busy_hints=lambda: "",
        model=lambda: "m",
        mcp=lambda: status["v"],
    )
    left = render_region_text(reg.components(BottomBarRegion.LEFT))
    assert "mcp on" in left

    mcp_comp = next(c for c in reg.components(BottomBarRegion.LEFT) if c.id == "mcp")
    painted = mcp_comp.content()
    assert isinstance(painted, Text)
    assert painted.plain == "mcp on"
    # Rich may put the color on the whole Text.style or on spans.
    painted_style = str(painted.style or "") + " ".join(str(s.style) for s in painted.spans)
    assert "81c995" in painted_style

    status["v"] = "mcp err"
    painted_err = mcp_comp.content()
    assert isinstance(painted_err, Text)
    assert painted_err.plain == "mcp err"
    err_style = str(painted_err.style or "") + " ".join(
        str(s.style) for s in painted_err.spans
    )
    assert "f28b82" in err_style


def test_custom_component_in_left_region() -> None:
    reg = BottomBarRegistry()
    install_default_components(
        reg,
        busy=lambda: False,
        thread=lambda: "tid",
        mode=lambda: "",
        idle_hints=lambda: "hints",
        busy_hints=lambda: "busy",
        model=lambda: "m · high",
        mcp=lambda: "mcp on",
    )
    reg.register_fn(
        "extra",
        lambda: "safe",
        region=BottomBarRegion.LEFT,
        order=5,
        priority=30,
    )
    left = render_region_text(reg.components(BottomBarRegion.LEFT))
    # order 5 (extra) before model(10) / mcp(20); thread not installed
    assert left.startswith("safe  ·  ")
    assert "m · high" in left
    assert "mcp on" in left
    assert "tid" not in left
    right = render_region_text(reg.components(BottomBarRegion.RIGHT))
    assert "hints" in right


def test_pack_prefers_right_over_center_when_tight() -> None:
    packed = pack_bottombar_regions(
        usable_width=40,
        left="Tab complete · / commands · Esc cancel",
        center="steer×3",
        right="abcd…wxyz",
        col_gap=3,
    )
    assert display_width(packed.as_plain()) <= 40
    assert packed.right


def test_layout_returns_rich_text() -> None:
    reg = BottomBarRegistry()
    reg.register_fn("l", lambda: "L", region="left", order=1)
    reg.register_fn("c", lambda: "C", region="center", order=1)
    reg.register_fn("r", lambda: "R", region="right", order=1)
    line = layout_from_registry(reg, usable_width=40)
    assert isinstance(line, Text)
    assert "L" in line.plain and "C" in line.plain and "R" in line.plain


def test_custom_region_with_width_align_fg_bg() -> None:
    reg = BottomBarRegistry()
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
    assert isinstance(spec, BottomBarRegionSpec)
    assert spec.width == 10
    assert spec.align is BottomBarAlign.CENTER
    line = layout_from_registry(reg, usable_width=80)
    assert "HOT" in line.plain
