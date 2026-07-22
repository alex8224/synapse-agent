"""Extensible topbar: freeform regions + components.

Regions are first-class layout slots (not limited to left/center/right).
Each region can configure:

- horizontal order
- width (fixed cells, hug content, or flex fill)
- min/max width
- align (left / center / right)
- fg / bg styles
- gap after the region
- shrink priority

Components render text into a named region and are ordered by ``order``.

Default chrome still installs three regions (``left`` / ``center`` / ``right``)
with historical behavior, painted into a single-line Textual ``Static``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from dataclasses import replace as dc_replace
from enum import Enum
from typing import Callable, Iterable

from rich.text import Text

# Default join between sibling components in the same region.
DEFAULT_REGION_GAP = "  ·  "
# Spaces after a default region when the next region is present.
# Theme ``top_gap`` overrides this at runtime for the live TUI topbar.
DEFAULT_COL_GAP = 3


class TopBarAlign(str, Enum):
    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"

    @classmethod
    def parse(cls, value: TopBarAlign | str | None) -> TopBarAlign:
        if isinstance(value, cls):
            return value
        key = str(value or "left").strip().lower()
        if key in {"middle", "mid"}:
            key = "center"
        try:
            return cls(key)
        except ValueError as exc:
            allowed = ", ".join(a.value for a in cls)
            raise ValueError(f"unknown topbar align {value!r}; expected one of: {allowed}") from exc


class TopBarRegion(str, Enum):
    """Built-in region ids (custom region ids are plain strings)."""

    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"

    @classmethod
    def parse(cls, value: TopBarRegion | str) -> TopBarRegion:
        """Parse a built-in region enum value (rejects freeform ids)."""
        if isinstance(value, cls):
            return value
        key = str(value or "").strip().lower()
        try:
            return cls(key)
        except ValueError as exc:
            allowed = ", ".join(r.value for r in cls)
            raise ValueError(f"unknown topbar region {value!r}; expected one of: {allowed}") from exc


def normalize_region_id(value: TopBarRegion | str | None) -> str:
    """Accept built-in enum or any non-empty freeform region id."""
    if isinstance(value, TopBarRegion):
        return value.value
    key = str(value or "").strip()
    if not key:
        raise ValueError("topbar region id must be non-empty")
    return key


RenderFn = Callable[[], str | Text]


@dataclass(slots=True)
class TopBarRegionSpec:
    """Layout + style configuration for one horizontal topbar region."""

    id: str
    order: int = 0
    # None = hug content; int = preferred/fixed cell width (still clamped).
    width: int | None = None
    min_width: int = 0
    max_width: int | None = None
    # flex > 0 shares leftover row width (CSS flex-grow style).
    flex: int = 0
    align: TopBarAlign = TopBarAlign.LEFT
    fg: str | None = None
    bg: str | None = None
    # Trailing spaces emitted after this region when a later region is shown.
    gap_after: int = DEFAULT_COL_GAP
    # Higher priority survives longer when the row overflows.
    priority: int = 0
    visible: bool = True

    def style_string(self, *, fallback_fg: str | None = None) -> str | None:
        fg = (self.fg if self.fg is not None else fallback_fg) or ""
        bg = (self.bg or "").strip()
        fg = fg.strip()
        if fg and bg:
            return f"{fg} on {bg}"
        if bg:
            return f"on {bg}"
        return fg or None


@dataclass(slots=True)
class TopBarComponent:
    """One renderable unit inside a named topbar region."""

    id: str
    region: str
    render: RenderFn
    order: int = 0
    priority: int = 0
    min_width: int = 0
    gap_before: str = DEFAULT_REGION_GAP
    style: str | None = None
    visible: bool = True

    def content(self) -> str | Text:
        """Return render output as plain str or styled Rich Text."""
        if not self.visible:
            return ""
        try:
            raw = self.render()
        except Exception:  # noqa: BLE001
            return ""
        if isinstance(raw, Text):
            return raw
        return str(raw or "")

    def text(self) -> str:
        raw = self.content()
        if isinstance(raw, Text):
            return raw.plain
        return raw


def _default_regions() -> dict[str, TopBarRegionSpec]:
    # Classic chrome: left/right hug content, center flex-fills leftover.
    # Theme ``top_gap`` may override gap_after at runtime.
    return {
        TopBarRegion.LEFT.value: TopBarRegionSpec(
            id=TopBarRegion.LEFT.value,
            order=10,
            flex=0,
            align=TopBarAlign.LEFT,
            priority=40,
            gap_after=DEFAULT_COL_GAP,
        ),
        TopBarRegion.CENTER.value: TopBarRegionSpec(
            id=TopBarRegion.CENTER.value,
            order=20,
            flex=1,
            align=TopBarAlign.CENTER,
            priority=10,
            min_width=4,
            gap_after=DEFAULT_COL_GAP,
        ),
        TopBarRegion.RIGHT.value: TopBarRegionSpec(
            id=TopBarRegion.RIGHT.value,
            order=30,
            flex=0,
            align=TopBarAlign.RIGHT,
            priority=50,
            gap_after=0,
        ),
    }


@dataclass(slots=True)
class TopBarRegistry:
    """Mutable registry of regions + components."""

    _regions: dict[str, TopBarRegionSpec] = field(default_factory=_default_regions)
    _items: dict[str, TopBarComponent] = field(default_factory=dict)

    def register_region(
        self,
        spec: TopBarRegionSpec | str,
        *,
        order: int | None = None,
        width: int | None = None,
        min_width: int | None = None,
        max_width: int | None = None,
        flex: int | None = None,
        align: TopBarAlign | str | None = None,
        fg: str | None = None,
        bg: str | None = None,
        gap_after: int | None = None,
        priority: int | None = None,
        visible: bool | None = None,
        replace: bool = True,
    ) -> TopBarRegionSpec:
        """Add or update a region slot."""
        if isinstance(spec, TopBarRegionSpec):
            region = spec
            rid = normalize_region_id(region.id)
            if rid != region.id:
                region = dc_replace(region, id=rid)
        else:
            rid = normalize_region_id(spec)
            region = self._regions.get(rid) or TopBarRegionSpec(id=rid, order=100)

        updates: dict[str, object] = {}
        if order is not None:
            updates["order"] = int(order)
        if width is not None:
            updates["width"] = int(width)
        if min_width is not None:
            updates["min_width"] = max(0, int(min_width))
        if max_width is not None:
            updates["max_width"] = int(max_width) if max_width else None
        if flex is not None:
            updates["flex"] = max(0, int(flex))
        if align is not None:
            updates["align"] = TopBarAlign.parse(align)
        if fg is not None:
            updates["fg"] = fg
        if bg is not None:
            updates["bg"] = bg
        if gap_after is not None:
            updates["gap_after"] = max(0, int(gap_after))
        if priority is not None:
            updates["priority"] = int(priority)
        if visible is not None:
            updates["visible"] = bool(visible)

        if updates:
            region = dc_replace(region, **updates)  # type: ignore[arg-type]
        region = dc_replace(region, id=rid)

        if not replace and rid in self._regions:
            raise KeyError(f"topbar region already registered: {rid}")
        self._regions[rid] = region
        return region

    def unregister_region(self, id: str, *, drop_components: bool = False) -> bool:
        rid = str(id)
        existed = self._regions.pop(rid, None) is not None
        if drop_components:
            for cid, comp in list(self._items.items()):
                if comp.region == rid:
                    self._items.pop(cid, None)
        return existed

    def get_region(self, id: str) -> TopBarRegionSpec | None:
        return self._regions.get(str(id))

    def ensure_region(self, id: TopBarRegion | str) -> TopBarRegionSpec:
        rid = normalize_region_id(id)
        existing = self._regions.get(rid)
        if existing is not None:
            return existing
        spec = TopBarRegionSpec(id=rid, order=100, flex=0, align=TopBarAlign.LEFT)
        self._regions[rid] = spec
        return spec

    def regions(self, *, include_hidden: bool = False) -> list[TopBarRegionSpec]:
        items = list(self._regions.values())
        if not include_hidden:
            items = [r for r in items if r.visible]
        items.sort(key=lambda r: (r.order, r.id))
        return items

    def set_region_style(
        self,
        id: str,
        *,
        fg: str | None = None,
        bg: str | None = None,
        align: TopBarAlign | str | None = None,
        width: int | None = None,
        min_width: int | None = None,
        max_width: int | None = None,
        flex: int | None = None,
        gap_after: int | None = None,
        priority: int | None = None,
        order: int | None = None,
        visible: bool | None = None,
    ) -> bool:
        if self.get_region(id) is None:
            return False
        self.register_region(
            id,
            fg=fg,
            bg=bg,
            align=align,
            width=width,
            min_width=min_width,
            max_width=max_width,
            flex=flex,
            gap_after=gap_after,
            priority=priority,
            order=order,
            visible=visible,
            replace=True,
        )
        return True

    def register(
        self,
        component: TopBarComponent,
        *,
        replace: bool = True,
    ) -> None:
        cid = str(component.id or "").strip()
        if not cid:
            raise ValueError("topbar component id must be non-empty")
        if not replace and cid in self._items:
            raise KeyError(f"topbar component already registered: {cid}")
        rid = normalize_region_id(component.region)
        self.ensure_region(rid)
        if component.id != cid or component.region != rid:
            component = TopBarComponent(
                id=cid,
                region=rid,
                render=component.render,
                order=component.order,
                priority=component.priority,
                min_width=component.min_width,
                gap_before=component.gap_before,
                style=component.style,
                visible=component.visible,
            )
        else:
            component.region = rid
        self._items[cid] = component

    def register_fn(
        self,
        id: str,
        render: RenderFn,
        *,
        region: TopBarRegion | str = TopBarRegion.RIGHT,
        order: int = 100,
        priority: int = 0,
        min_width: int = 0,
        gap_before: str = DEFAULT_REGION_GAP,
        style: str | None = None,
        visible: bool = True,
        replace: bool = True,
    ) -> TopBarComponent:
        comp = TopBarComponent(
            id=str(id),
            region=normalize_region_id(region),
            render=render,
            order=int(order),
            priority=int(priority),
            min_width=max(0, int(min_width or 0)),
            gap_before=str(gap_before),
            style=style,
            visible=bool(visible),
        )
        self.register(comp, replace=replace)
        return comp

    def unregister(self, id: str) -> bool:
        return self._items.pop(str(id), None) is not None

    def get(self, id: str) -> TopBarComponent | None:
        return self._items.get(str(id))

    def set_visible(self, id: str, visible: bool) -> bool:
        comp = self.get(id)
        if comp is None:
            return False
        comp.visible = bool(visible)
        return True

    def set_order(self, id: str, order: int) -> bool:
        comp = self.get(id)
        if comp is None:
            return False
        comp.order = int(order)
        return True

    def set_region(self, id: str, region: TopBarRegion | str) -> bool:
        comp = self.get(id)
        if comp is None:
            return False
        rid = normalize_region_id(region)
        self.ensure_region(rid)
        comp.region = rid
        return True

    def clear(self) -> None:
        self._items.clear()
        self._regions = _default_regions()

    def components(
        self,
        region: TopBarRegion | str | None = None,
        *,
        include_hidden: bool = False,
    ) -> list[TopBarComponent]:
        items = list(self._items.values())
        if region is not None:
            rid = normalize_region_id(region)
            items = [c for c in items if c.region == rid]
        if not include_hidden:
            items = [c for c in items if c.visible]
        items.sort(key=lambda c: (c.order, c.id))
        return items

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, id: object) -> bool:
        return str(id) in self._items


def display_width(text: str) -> int:
    """Terminal cell width (CJK / fullwidth / emoji count as 2)."""
    total = 0
    for ch in text or "":
        o = ord(ch)
        if (
            0x1100 <= o <= 0x115F
            or 0x2E80 <= o <= 0xA4CF
            or 0xAC00 <= o <= 0xD7A3
            or 0xF900 <= o <= 0xFAFF
            or 0xFE10 <= o <= 0xFE19
            or 0xFE30 <= o <= 0xFE6F
            or 0xFF00 <= o <= 0xFF60
            or 0xFFE0 <= o <= 0xFFE6
            or 0x1F300 <= o <= 0x1FAFF
        ):
            total += 2
        else:
            total += 1
    return total


def truncate_to_width(text: str, max_w: int) -> str:
    """Truncate ``text`` so its display width fits ``max_w`` cells."""
    raw = text or ""
    max_w = int(max_w or 0)
    if max_w <= 0:
        return ""
    if display_width(raw) <= max_w:
        return raw
    if max_w == 1:
        return "…"
    out: list[str] = []
    used = 0
    limit = max_w - 1
    for ch in raw:
        cw = display_width(ch)
        if used + cw > limit:
            break
        out.append(ch)
        used += cw
    return "".join(out) + "…"


def center_in_width(text: str, width: int) -> str:
    """Pad ``text`` so it appears centered within ``width`` terminal cells."""
    return align_in_width(text, width, TopBarAlign.CENTER)


def align_in_width(text: str, width: int, align: TopBarAlign | str) -> str:
    """Pad/truncate ``text`` into ``width`` cells with the given alignment."""
    align_e = TopBarAlign.parse(align)
    width = max(0, int(width or 0))
    if width <= 0:
        return ""
    body = truncate_to_width(text or "", width)
    w = display_width(body)
    if w >= width:
        return body
    pad = width - w
    if align_e is TopBarAlign.RIGHT:
        return (" " * pad) + body
    if align_e is TopBarAlign.CENTER:
        left = pad // 2
        right = pad - left
        return (" " * left) + body + (" " * right)
    return body + (" " * pad)


def join_region_parts(parts: Iterable[tuple[str, str]]) -> str:
    """Join ``(gap_before, text)`` pairs, skipping empty texts."""
    chunks: list[str] = []
    for gap, text in parts:
        body = text or ""
        if not body:
            continue
        if not chunks:
            chunks.append(body)
        else:
            chunks.append(f"{gap}{body}")
    return "".join(chunks)


def render_region_text(components: list[TopBarComponent]) -> str:
    """Render ordered components in one region to a plain string."""
    return join_region_parts((comp.gap_before, comp.text()) for comp in components)


def _compress_region_parts(components: list[TopBarComponent], budget: int) -> str:
    """Fit region into ``budget`` cells by dropping/truncating low priority first."""
    if budget <= 0:
        return ""
    snaps: list[tuple[str, str, int, int, int]] = []
    for idx, comp in enumerate(components):
        body = comp.text()
        if not body:
            continue
        snaps.append((comp.gap_before, body, int(comp.priority), max(0, int(comp.min_width or 0)), idx))
    if not snaps:
        return ""

    def joined(items: list[tuple[str, str, int, int, int]]) -> str:
        return join_region_parts((g, t) for g, t, *_ in items)

    text = joined(snaps)
    if display_width(text) <= budget:
        return text
    working = list(snaps)
    while len(working) > 1 and display_width(joined(working)) > budget:
        drop_i = min(range(len(working)), key=lambda i: (working[i][2], -working[i][4]))
        working.pop(drop_i)
    text = joined(working)
    if display_width(text) <= budget:
        return text
    return truncate_to_width(text, budget)


@dataclass(slots=True)
class PackedRegion:
    """One region after width allocation."""

    spec: TopBarRegionSpec
    content: str
    width: int
    components: list[TopBarComponent] = field(default_factory=list)

    def aligned_text(self) -> str:
        return align_in_width(self.content, self.width, self.spec.align)


@dataclass(slots=True)
class TopBarLayout:
    """Result of packing regions into one row."""

    regions: list[PackedRegion]
    usable: int
    left: str = ""
    center: str = ""
    right: str = ""
    pad_left: int = 0
    pad_right: int = 0

    def as_plain(self) -> str:
        parts: list[str] = []
        visible = [r for r in self.regions if r.width > 0 or r.content]
        for i, reg in enumerate(visible):
            parts.append(reg.aligned_text())
            if i < len(visible) - 1 and reg.spec.gap_after > 0:
                parts.append(" " * reg.spec.gap_after)
        body = "".join(parts)
        w = display_width(body)
        if w < self.usable:
            body = body + (" " * (self.usable - w))
        elif w > self.usable:
            body = truncate_to_width(body, self.usable)
        return body


def _clamp_width(spec: TopBarRegionSpec, desired: int) -> int:
    w = max(0, int(desired))
    w = max(w, max(0, int(spec.min_width or 0)))
    if spec.max_width is not None:
        w = min(w, max(0, int(spec.max_width)))
    return w


def pack_region_list(
    items: list[tuple[TopBarRegionSpec, str, list[TopBarComponent]]],
    *,
    usable_width: int,
) -> TopBarLayout:
    """Pack arbitrary regions into a single non-wrapping row."""
    usable = max(1, int(usable_width or 0))
    active: list[tuple[TopBarRegionSpec, str, list[TopBarComponent]]] = []
    for spec, content, comps in items:
        if not spec.visible:
            continue
        content = content or ""
        if not content and int(spec.flex or 0) <= 0 and spec.width is None:
            continue
        active.append((spec, content, comps))
    if not active:
        return TopBarLayout(regions=[], usable=usable)

    base: list[int] = []
    for spec, content, _comps in active:
        natural = display_width(content)
        desired = int(spec.width) if spec.width is not None else natural
        if not content and spec.width is None:
            desired = 0
        base.append(_clamp_width(spec, desired))

    def gaps_width(n: int) -> int:
        if n <= 1:
            return 0
        return sum(max(0, int(active[i][0].gap_after or 0)) for i in range(n - 1))

    widths = list(base)
    gap_w = gaps_width(len(active))
    remaining = usable - (sum(widths) + gap_w)

    if remaining > 0:
        flex_idx = [i for i, (spec, _, _) in enumerate(active) if int(spec.flex or 0) > 0]
        if flex_idx:
            total_flex = sum(int(active[i][0].flex) for i in flex_idx)
            give = remaining
            for j, i in enumerate(flex_idx):
                if j == len(flex_idx) - 1:
                    add = give
                else:
                    add = remaining * int(active[i][0].flex) // total_flex
                    give -= add
                widths[i] = _clamp_width(active[i][0], widths[i] + add)
            used = sum(widths) + gap_w
            rest = usable - used
            if rest > 0:
                last = flex_idx[-1]
                widths[last] = _clamp_width(active[last][0], widths[last] + rest)

    overflow = (sum(widths) + gap_w) - usable
    if overflow > 0:
        order = sorted(
            range(len(active)),
            key=lambda i: (
                int(active[i][0].priority),
                -int(active[i][0].flex or 0),
                -active[i][0].order,
            ),
        )
        for i in order:
            if overflow <= 0:
                break
            spec, content, _comps = active[i]
            floor = max(0, int(spec.min_width or 0))
            reducible = max(0, widths[i] - floor)
            if reducible <= 0:
                continue
            cut = min(reducible, overflow)
            widths[i] -= cut
            overflow -= cut
        if overflow > 0:
            for i in order:
                if overflow <= 0:
                    break
                cut = min(widths[i], overflow)
                widths[i] -= cut
                overflow -= cut

    packed: list[PackedRegion] = []
    for (spec, content, comps), width in zip(active, widths, strict=True):
        body = content
        if display_width(body) > width:
            body = _compress_region_parts(comps, width) if comps else truncate_to_width(body, width)
        packed.append(PackedRegion(spec=spec, content=body, width=width, components=comps))

    layout = TopBarLayout(regions=packed, usable=usable)
    by_id = {p.spec.id: p for p in packed}
    if "left" in by_id:
        layout.left = by_id["left"].content
    if "center" in by_id:
        layout.center = by_id["center"].content
        cw = display_width(by_id["center"].content)
        band = by_id["center"].width
        if band > cw:
            layout.pad_left = (band - cw) // 2
            layout.pad_right = band - cw - layout.pad_left
    if "right" in by_id:
        layout.right = by_id["right"].content
    return layout


def pack_topbar_regions(
    *,
    usable_width: int,
    left: str = "",
    center: str = "",
    right: str = "",
    col_gap: int = DEFAULT_COL_GAP,
) -> TopBarLayout:
    """Legacy three-region packer (kept for tests / simple callers)."""
    gap = max(0, int(col_gap or 0))
    items = [
        (
            TopBarRegionSpec(id="left", order=10, flex=0, align=TopBarAlign.LEFT, priority=40, gap_after=gap),
            left or "",
            [],
        ),
        (
            TopBarRegionSpec(
                id="center",
                order=20,
                flex=1,
                align=TopBarAlign.CENTER,
                priority=10,
                min_width=4,
                gap_after=gap,
            ),
            center or "",
            [],
        ),
        (
            TopBarRegionSpec(id="right", order=30, flex=0, align=TopBarAlign.RIGHT, priority=50, gap_after=0),
            right or "",
            [],
        ),
    ]
    return pack_region_list(items, usable_width=usable_width)


def pack_layout_from_registry(
    registry: TopBarRegistry,
    *,
    usable_width: int,
    col_gap: int | None = None,
) -> TopBarLayout:
    """Pack registry regions without styling (for hit-testing / span queries)."""
    region_specs = registry.regions(include_hidden=False)
    if col_gap is not None:
        gap = max(0, int(col_gap))
        region_specs = [
            dc_replace(spec, gap_after=gap) if spec.id in {"left", "center"} else spec
            for spec in region_specs
        ]

    items: list[tuple[TopBarRegionSpec, str, list[TopBarComponent]]] = []
    for spec in region_specs:
        comps = registry.components(spec.id)
        items.append((spec, render_region_text(comps), comps))
    return pack_region_list(items, usable_width=usable_width)


def layout_from_registry(
    registry: TopBarRegistry,
    *,
    usable_width: int,
    col_gap: int | None = None,
    left_style: str = "",
    center_style: str = "",
    right_style: str = "",
    gap_style: str = "",
    default_fg: str = "",
) -> Text:
    """Render a full topbar line from a registry of regions + components."""
    del gap_style
    fallback = {
        "left": left_style or default_fg,
        "center": center_style or default_fg,
        "right": right_style or default_fg,
    }
    packed = pack_layout_from_registry(
        registry, usable_width=usable_width, col_gap=col_gap
    )
    return render_packed_line(packed, fallback_fg=fallback)


def locate_component_span(
    registry: TopBarRegistry,
    component_id: str,
    *,
    usable_width: int,
    col_gap: int | None = None,
) -> tuple[int, int] | None:
    """Return ``(start_col, width)`` of a component within the packed topbar row.

    Coordinates are content cells (0 = first cell of the usable topbar line),
    matching ``Static`` content after CSS horizontal padding.
    """
    cid = str(component_id or "").strip()
    if not cid:
        return None
    packed = pack_layout_from_registry(
        registry, usable_width=usable_width, col_gap=col_gap
    )
    cursor = 0
    visible = [r for r in packed.regions if r.width > 0 or r.content]
    for i, reg in enumerate(visible):
        natural = reg.content or ""
        lead = 0
        if reg.spec.align is TopBarAlign.RIGHT:
            lead = max(0, reg.width - display_width(natural))
        elif reg.spec.align is TopBarAlign.CENTER:
            lead = max(0, (reg.width - display_width(natural)) // 2)

        x = cursor + lead
        first = True
        for comp in reg.components:
            plain = comp.text()
            if not plain:
                continue
            if not first and comp.gap_before:
                x += display_width(comp.gap_before)
            w = display_width(plain)
            if comp.id == cid:
                return (x, w)
            x += w
            first = False

        cursor += reg.width
        if i < len(visible) - 1 and reg.spec.gap_after > 0:
            cursor += int(reg.spec.gap_after or 0)
    return None


def _region_bg(spec: TopBarRegionSpec) -> str:
    """Normalized region background color, or empty if unset."""
    return (spec.bg or "").strip()


def _with_bg(style: str | None, bg: str) -> str | None:
    """Ensure a Rich style string includes ``on <bg>`` (for region color blocks)."""
    bg = (bg or "").strip()
    if not bg:
        return style
    raw = (style or "").strip()
    if not raw:
        return f"on {bg}"
    # Already has an explicit background — keep caller choice.
    if " on " in f" {raw} ":
        return raw
    return f"{raw} on {bg}"


def _append_chunk_with_region_bg(
    line: Text,
    chunk: str | Text,
    *,
    style: str | None,
    bg: str,
) -> None:
    """Append plain/Text content, painting the region band background when set."""
    if isinstance(chunk, Text):
        if bg:
            painted = chunk.copy()
            painted.stylize(f"on {bg}")
            line.append_text(painted)
        else:
            line.append_text(chunk)
        return
    line.append(str(chunk), style=_with_bg(style, bg) if bg else style)


def render_packed_line(
    layout: TopBarLayout,
    *,
    fallback_fg: dict[str, str] | str | None = None,
) -> Text:
    """Turn a packed layout into styled Rich Text.

    When a region sets ``bg``, the full allocated band (content + horizontal
    pad) is painted as a solid color block. Per-component styles / pre-styled
    ``Text`` keep their foregrounds; the region bg is layered underneath.
    """
    line = Text()
    if isinstance(fallback_fg, str) or fallback_fg is None:
        fb_map: dict[str, str] = {}
        default_fb = fallback_fg or ""
    else:
        fb_map = dict(fallback_fg)
        default_fb = ""

    visible = [r for r in layout.regions if r.width > 0 or r.content]
    for i, reg in enumerate(visible):
        fb = fb_map.get(reg.spec.id, default_fb)
        style = reg.spec.style_string(fallback_fg=fb or None)
        bg = _region_bg(reg.spec)
        body = reg.aligned_text()
        natural = reg.content
        # Prefer component path so branch/workspace keep their own styles, even
        # when the region paints a solid background band.
        if reg.components and render_region_text(reg.components) == natural:
            lead = 0
            if reg.spec.align is TopBarAlign.RIGHT:
                lead = max(0, reg.width - display_width(natural))
            elif reg.spec.align is TopBarAlign.CENTER:
                lead = max(0, (reg.width - display_width(natural)) // 2)
            # Always paint pad cells with the region style so bg fills the band,
            # not only the glyphs (hug regions used to look like text highlight).
            band_style = style or (f"on {bg}" if bg else None)
            if lead > 0:
                line.append(" " * lead, style=band_style)
            first = True
            for comp in reg.components:
                chunk = comp.content()
                plain = chunk.plain if isinstance(chunk, Text) else str(chunk or "")
                if not plain:
                    continue
                if not first and comp.gap_before:
                    line.append(comp.gap_before, style=band_style)
                if isinstance(chunk, Text):
                    _append_chunk_with_region_bg(line, chunk, style=style, bg=bg)
                else:
                    c_style = comp.style if comp.style is not None else style
                    _append_chunk_with_region_bg(line, chunk, style=c_style, bg=bg)
                first = False
            trail = reg.width - lead - display_width(natural)
            if trail > 0:
                line.append(" " * trail, style=band_style)
        elif body:
            line.append(body, style=style)
        elif reg.width > 0 and (style or bg):
            # Empty content but allocated width still paints a solid band.
            band_style = style or (f"on {bg}" if bg else None)
            line.append(" " * reg.width, style=band_style)

        if i < len(visible) - 1 and reg.spec.gap_after > 0:
            # Gaps stay unstyled so the widget CSS topbar bg shows between blocks.
            line.append(" " * reg.spec.gap_after, style=None)

    plain_w = display_width(line.plain)
    if plain_w < layout.usable:
        line.append(" " * (layout.usable - plain_w), style=None)
    elif plain_w > layout.usable:
        return Text(truncate_to_width(line.plain, layout.usable))
    return line


__all__ = [
    "DEFAULT_COL_GAP",
    "DEFAULT_REGION_GAP",
    "PackedRegion",
    "TopBarAlign",
    "TopBarComponent",
    "TopBarLayout",
    "TopBarRegion",
    "TopBarRegionSpec",
    "TopBarRegistry",
    "align_in_width",
    "center_in_width",
    "display_width",
    "join_region_parts",
    "layout_from_registry",
    "locate_component_span",
    "normalize_region_id",
    "pack_layout_from_registry",
    "pack_region_list",
    "pack_topbar_regions",
    "render_packed_line",
    "render_region_text",
    "truncate_to_width",
]
