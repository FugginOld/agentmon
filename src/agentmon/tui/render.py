"""Pure renderers: ``Run.snapshot()`` dicts in, styled ``rich.text.Text`` out.

Nothing here imports Textual, so every view can be unit-tested (and printed with
``Text.plain``). The graph views draw onto a :class:`Canvas`, a grid of cells where
lines are stored as direction bitmasks (up/down/left/right) and only turned into
box-drawing characters when the canvas is flattened. That makes crossings and
junctions merge into the right glyph (``┼ ├ ┤ ┬ ┴ ┌ ┐ └ ┘``) regardless of the
order edges were added in. Drawing order is: lines, boxes, tees, arrowheads.

Glyphs are deliberately limited to ``✓ ● ✗ ○`` plus box-drawing and block
characters so every cell is exactly one terminal column wide.
"""

from __future__ import annotations

import math
import re
import textwrap
from dataclasses import dataclass, field
from typing import NamedTuple

from rich.style import Style
from rich.text import Text

from .. import fmt

# --------------------------------------------------------------------------- palette
GREEN = "#4CC38A"
GREEN2 = "#2F8F63"
AMBER = "#F2A93B"
RED = "#F2706B"
DIM = "#5A6475"
MUTED = "#8B95A6"
WHITE = "#E7EAF0"
TEXT = "#C9D1D9"
BLUE = "#6EA8FE"
INK = "#0D1117"  # dark foreground for filled badges

ICONS = {"done": "✓", "running": "●", "error": "✗", "queued": "○"}


def _hex(color: str | None, fallback: str = MUTED) -> str:
    return color if color and re.fullmatch(r"#[0-9A-Fa-f]{6}", color) else fallback


class Rect(NamedTuple):
    """A box on the canvas in character cells."""

    x: int
    y: int
    w: int
    h: int


@dataclass
class Rendered:
    """A rendered graph plus where each step landed (for scrolling and click hit-testing)."""

    text: Text
    rects: dict[str, Rect] = field(default_factory=dict)
    width: int = 0
    height: int = 0


def step_at(rects: dict[str, Rect], x: int, y: int) -> str | None:
    """Return the step whose rectangle contains cell (x, y), if any."""
    for sid, r in rects.items():
        if r.x <= x < r.x + r.w and r.y <= y < r.y + r.h:
            return sid
    return None


# --------------------------------------------------------------------------- canvas
UP, DOWN, LEFT, RIGHT = 1, 2, 4, 8
_GLYPH = {
    UP: "│", DOWN: "│", UP | DOWN: "│",
    LEFT: "─", RIGHT: "─", LEFT | RIGHT: "─",
    DOWN | RIGHT: "┌", DOWN | LEFT: "┐", UP | RIGHT: "└", UP | LEFT: "┘",
    UP | DOWN | RIGHT: "├", UP | DOWN | LEFT: "┤",
    DOWN | LEFT | RIGHT: "┬", UP | LEFT | RIGHT: "┴",
    UP | DOWN | LEFT | RIGHT: "┼",
}


class Canvas:
    """A fixed-size grid of styled cells with bitmask line drawing."""

    def __init__(self, width: int, height: int) -> None:
        self.w = max(1, width)
        self.h = max(1, height)
        self._ch = [[" "] * self.w for _ in range(self.h)]
        self._st: list[list[Style | None]] = [[None] * self.w for _ in range(self.h)]
        self._mask = [[0] * self.w for _ in range(self.h)]
        self._solid = [[False] * self.w for _ in range(self.h)]
        self._prio = [[-1] * self.w for _ in range(self.h)]

    # -- primitives ---------------------------------------------------------------
    def put(self, x: int, y: int, text: str, style: Style | str | None = None) -> None:
        """Write literal characters (clearing any line bits underneath)."""
        st = Style.parse(style) if isinstance(style, str) else style
        if not 0 <= y < self.h:
            return
        for i, ch in enumerate(text):
            cx = x + i
            if 0 <= cx < self.w:
                self._ch[y][cx] = ch
                self._st[y][cx] = st
                self._mask[y][cx] = 0

    def _add(self, x: int, y: int, bits: int, style: Style, dashed: bool, prio: int) -> None:
        if not (0 <= x < self.w and 0 <= y < self.h):
            return
        self._mask[y][x] |= bits
        if not dashed:
            self._solid[y][x] = True
        if prio >= self._prio[y][x]:
            self._prio[y][x] = prio
            self._st[y][x] = style

    def line(self, points: list[tuple[int, int]], style: Style, dashed: bool = False, prio: int = 0) -> None:
        """Draw an orthogonal polyline through ``points`` (consecutive points share x or y).

        Each cell on a segment gets the bits for the directions the line leaves it in, so
        corners and T-junctions fall out of the bitmask when several lines meet.
        """
        pts = [points[0]] if points else []
        for p in points[1:]:
            if p != pts[-1]:
                pts.append(p)
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            if y1 == y2:
                lo, hi = sorted((x1, x2))
                for x in range(lo, hi + 1):
                    bits = (RIGHT if x < hi else 0) | (LEFT if x > lo else 0)
                    self._add(x, y1, bits, style, dashed, prio)
            elif x1 == x2:
                lo, hi = sorted((y1, y2))
                for y in range(lo, hi + 1):
                    bits = (DOWN if y < hi else 0) | (UP if y > lo else 0)
                    self._add(x1, y, bits, style, dashed, prio)
            else:  # pragma: no cover - callers only pass axis-aligned segments
                raise ValueError("diagonal segment")

    def cell_char(self, x: int, y: int) -> str:
        """The character that will be shown at (x, y) once line masks are resolved."""
        mask = self._mask[y][x]
        if not mask:
            return self._ch[y][x]
        if not self._solid[y][x]:
            if mask == UP | DOWN or mask in (UP, DOWN):
                return "┊"
            if mask == LEFT | RIGHT or mask in (LEFT, RIGHT):
                return "┄"
        return _GLYPH[mask]

    # -- output -------------------------------------------------------------------
    def to_text(self) -> Text:
        """Flatten to a ``Text``: runs of equal style are merged, every line is full width."""
        out = Text(no_wrap=True, overflow="crop")
        for y in range(self.h):
            run: list[str] = []
            run_style: Style | None = None
            for x in range(self.w):
                ch = self.cell_char(x, y)
                st = self._st[y][x] if ch != " " else None
                if st is not run_style and run:
                    out.append("".join(run), run_style)
                    run = []
                run_style = st
                run.append(ch)
            if run:
                out.append("".join(run), run_style)
            if y < self.h - 1:
                out.append("\n")
        return out


# --------------------------------------------------------------------------- boxes
@dataclass(frozen=True)
class _Theme:
    icon: str
    icon_color: str
    border: str
    title: str
    meta: str
    dashed: bool = False


_THEMES = {
    "done": _Theme("✓", GREEN, DIM, WHITE, MUTED),
    "running": _Theme("●", AMBER, AMBER, AMBER, AMBER),
    "error": _Theme("✗", RED, RED, RED, RED),
    "queued": _Theme("○", MUTED, DIM, MUTED, MUTED, dashed=True),
}

BOX_W = 26
BOX_H = 3


def draw_box(cv: Canvas, x: int, y: int, step: dict, meta: str, selected: bool = False, w: int = BOX_W) -> None:
    """Draw a rounded step box: status icon + title in the top border, meta line inside."""
    th = _THEMES.get(step["status"], _THEMES["queued"])
    hz, vt = ("┄", "┆") if th.dashed else ("─", "│")
    if selected:
        border = Style(color=WHITE, bold=True)
        title_style = Style(color=INK, bgcolor=th.icon_color if th.icon_color != MUTED else WHITE, bold=True)
        icon_style = title_style
        hz, vt = ("━", "┃") if not th.dashed else (hz, vt)
    else:
        border = Style(color=th.border)
        title_style = Style(color=th.title, bold=step["status"] == "running")
        icon_style = Style(color=th.icon_color)
    meta_style = Style(color=th.meta)
    title = fmt.truncate(step["title"], w - 7)
    cv.put(x, y, "╭", border)
    cv.put(x + 1, y, f" {th.icon} ", icon_style)
    cv.put(x + 4, y, title, title_style)
    rest = w - 2 - 3 - len(title)
    cv.put(x + 4 + len(title), y, " " + hz * (rest - 1), border)
    cv.put(x + w - 1, y, "╮", border)
    cv.put(x, y + 1, vt, border)
    cv.put(x + 1, y + 1, (" " + fmt.truncate(meta, w - 4)).ljust(w - 2), meta_style)
    cv.put(x + w - 1, y + 1, vt, border)
    cv.put(x, y + 2, "╰", border)
    cv.put(x + 1, y + 2, hz * (w - 2), border)
    cv.put(x + w - 1, y + 2, "╯", border)


def _tokens_total(step: dict) -> int:
    t = step.get("tokens") or {}
    return int(sum(v for k, v in t.items() if k in ("in", "out") and isinstance(v, (int, float))))


def swim_meta(step: dict) -> str:
    """Meta line for swimlane boxes: ``summary · duration`` (or a status word)."""
    status, dur, summary = step["status"], step.get("duration"), step.get("summary")
    if status == "queued":
        return "queued"
    if status == "running":
        return f"{summary or 'running'} · {fmt.clock(dur)}"
    room = BOX_W - 4
    tail = f" · {fmt.duration(dur)}" if dur is not None else ""
    head = summary or (f"{fmt.tokens(_tokens_total(step))} tok" if _tokens_total(step) else status)
    if len(head) + len(tail) <= room:
        return head + tail
    return fmt.truncate(head, room)  # too long for both: keep the summary, drop the duration


def dag_meta(step: dict, agents: dict[str, dict]) -> str:
    """Meta line for DAG boxes: ``agent · duration`` (running: elapsed clock, failed: summary)."""
    name = (agents.get(step["agent"]) or {}).get("name") or step["agent"]
    status, dur = step["status"], step.get("duration")
    if status == "queued":
        tail = "queued"
    elif status == "running":
        tail = fmt.clock(dur)
    elif status == "error":
        tail = (step.get("summary") or "failed").split(" · ")[0]
    else:
        tail = fmt.duration(dur)
    name = fmt.truncate(name, max(4, BOX_W - 4 - 3 - len(tail)))
    return f"{name} · {tail}"


# --------------------------------------------------------------------------- edges
# edge kind -> (colour, dashed, priority); priority decides who wins where lines overlap
_EDGE_STYLE = {
    "pending": (MUTED, True, 0),
    "done": (DIM, False, 1),
    "active": (AMBER, True, 3),
    "error": (RED, False, 4),
}
_CRITICAL = (WHITE, False, 2)


class _EdgeDraw(NamedTuple):
    style: Style
    dashed: bool
    prio: int


def _edge_draw(edge: dict, critical: bool = False) -> _EdgeDraw:
    color, dashed, prio = _EDGE_STYLE.get(edge["kind"], _EDGE_STYLE["done"])
    if critical and edge["kind"] == "done":
        color, dashed, prio = _CRITICAL
    return _EdgeDraw(Style(color=color), dashed, prio)


def _col_free(x: int, y1: int, y2: int, rects: list[Rect]) -> bool:
    """Is column ``x`` clear of every box (with one cell of margin) between rows y1..y2?"""
    return not any(r.y <= y2 and r.y + r.h - 1 >= y1 and r.x - 1 <= x <= r.x + r.w for r in rects)


def _find_gutter(pref: int, y1: int, y2: int, rects: list[Rect], width: int) -> int:
    for d in range(width + 1):
        for x in (pref - d, pref + d):
            if 0 <= x < width and _col_free(x, y1, y2, rects):
                return x
    return max(0, pref)


def route_edge(src: Rect, dst: Rect, rects: list[Rect], width: int) -> list[tuple[int, int]]:
    """Orthogonal path from the bottom-centre of ``src`` to the top-centre of ``dst``.

    * straight down when the column between them is free of boxes;
    * adjacent rows/levels: along the gap row below ``src``, then down into ``dst``;
    * otherwise: gap row below ``src`` -> gutter column beside ``dst`` -> down the
      gutter -> along the gap row above ``dst`` -> down into ``dst``.

    The final point is the cell directly above ``dst`` (where the arrowhead goes).
    """
    sx = src.x + src.w // 2
    tx = dst.x + dst.w // 2
    y_a = src.y + src.h          # first free row under the source box
    y_b = dst.y - 1              # row holding the arrowhead
    y_s = y_a - 1                # the source's bottom border: the line leaves from here (tee)
    if sx == tx and _col_free(sx, y_a, y_b, rects):
        return [(sx, y_s), (tx, y_b)]
    if y_b - y_a <= 1:
        return [(sx, y_s), (sx, y_a), (tx, y_a), (tx, y_b)]
    y_c = y_b - 1                # gap row above the target, where we travel horizontally
    gx = _find_gutter(dst.x - 2, y_a, y_c, rects, width)
    return [(sx, y_s), (sx, y_a), (gx, y_a), (gx, y_c), (tx, y_c), (tx, y_b)]


_Marks = tuple[dict[tuple[int, int], _EdgeDraw], dict[tuple[int, int], _EdgeDraw]]


def _draw_edges(cv: Canvas, edges: list[dict], rects: dict[str, Rect], critical_pairs: set[tuple[str, str]]) -> _Marks:
    """Draw edge lines; return the tees and arrowheads to stamp once the boxes are down."""
    all_rects = list(rects.values())
    tees: dict[tuple[int, int], _EdgeDraw] = {}
    arrows: dict[tuple[int, int], _EdgeDraw] = {}
    for e in edges:
        src, dst = rects.get(e["src"]), rects.get(e["dst"])
        if src is None or dst is None:
            continue
        d = _edge_draw(e, (e["src"], e["dst"]) in critical_pairs)
        path = route_edge(src, dst, all_rects, cv.w)
        cv.line(path, d.style, d.dashed, d.prio)
        for table, pos in ((tees, (src.x + src.w // 2, src.y + src.h - 1)), (arrows, path[-1])):
            if pos not in table or d.prio >= table[pos].prio:
                table[pos] = d
    return tees, arrows


def _stamp_edge_marks(cv: Canvas, marks: _Marks) -> None:
    """Tees where edges leave a box's bottom border, then arrowheads above the targets."""
    tees, arrows = marks
    for (x, y), d in tees.items():
        cv.put(x, y, "┬", d.style)
    for (x, y), d in arrows.items():
        cv.put(x, y, "▼", d.style)


# --------------------------------------------------------------------------- swimlanes
SWIM_LEFT = 3
SWIM_GAP = 4           # columns between lanes (the gutter edges travel in)
SWIM_PITCH = BOX_W + SWIM_GAP
ROW_PITCH = BOX_H + 2  # box + two gap rows for routing
SWIM_HEAD = 3          # header text, rule, spacer


def _agent_map(snap: dict) -> dict[str, dict]:
    return {a["id"]: a for a in snap["agents"]}


def _lane_status(agent: dict | None, steps: list[dict]) -> tuple[str, str]:
    """(text, colour) for a lane header; only the glyphs ✓ ● ✗ ○ are used."""
    if agent and agent["status"] == "error":
        return "✗ error", RED
    if agent and agent["status"] == "done":
        return "✓ done", GREEN
    if any(s["status"] == "running" for s in steps):
        return "● active", AMBER
    if any(s["status"] == "queued" for s in steps):
        return "○ wait", MUTED
    return "○ idle", MUTED


def build_swim(snap: dict, selected: str | None = None) -> Rendered:
    lay = snap["layouts"]["swim"]
    steps = {s["id"]: s for s in snap["steps"]}
    agents = _agent_map(snap)
    lanes: list[str] = lay["lanes"]
    nrows = lay["rows"]
    width = SWIM_LEFT + max(1, len(lanes)) * SWIM_PITCH - SWIM_GAP + 2
    height = SWIM_HEAD + max(1, nrows) * ROW_PITCH
    cv = Canvas(width, height)

    rects: dict[str, Rect] = {}
    for n in lay["nodes"]:
        rects[n["step"]] = Rect(SWIM_LEFT + n["lane"] * SWIM_PITCH, SWIM_HEAD + n["row"] * ROW_PITCH, BOX_W, BOX_H)

    # lane headers: NAME model ........ status, then a rule
    for i, aid in enumerate(lanes):
        a = agents.get(aid)
        x = SWIM_LEFT + i * SWIM_PITCH
        lane_steps = [s for s in steps.values() if s["agent"] == aid]
        status, scol = _lane_status(a, lane_steps)
        room = BOX_W - len(status) - 1
        name = fmt.truncate(((a or {}).get("name") or aid).upper(), max(3, room - 1))
        cv.put(x, 0, name, Style(color=_hex((a or {}).get("color")), bold=True))
        model = fmt.truncate((a or {}).get("model") or "", max(0, room - len(name) - 1))
        if model:
            cv.put(x + len(name) + 1, 0, model, Style(color=MUTED))
        cv.put(x + BOX_W - len(status), 0, status, Style(color=scol))
    cv.put(0, 1, "─" * width, Style(color=DIM))

    marks = _draw_edges(cv, lay["edges"], rects, set())
    for sid, r in rects.items():
        draw_box(cv, r.x, r.y, steps[sid], swim_meta(steps[sid]), selected == sid)
    _stamp_edge_marks(cv, marks)
    return Rendered(cv.to_text(), rects, width, height)


def render_swim(snapshot: dict, selected: str | None = None) -> Text:
    """Swimlane view: one column per agent, one box per step, edges routed through gutters."""
    return build_swim(snapshot, selected).text


# --------------------------------------------------------------------------- DAG
DAG_LEFT = 2
DAG_COL_GAP = 6
DAG_COL_PITCH = BOX_W + DAG_COL_GAP
DAG_HEAD = 5


def build_dag(snap: dict, selected: str | None = None) -> Rendered:
    lay = snap["layouts"]["dag"]
    steps = {s["id"]: s for s in snap["steps"]}
    agents = _agent_map(snap)
    ncols = max(1, lay["width"])
    total_w = ncols * DAG_COL_PITCH - DAG_COL_GAP
    width = max(DAG_LEFT * 2 + total_w, 44)
    height = DAG_HEAD + max(1, lay["levels"]) * ROW_PITCH
    cv = Canvas(width, height)
    off = (width - total_w) // 2

    rects: dict[str, Rect] = {}
    for n in lay["nodes"]:
        row_w = n["ncols"] * DAG_COL_PITCH - DAG_COL_GAP
        x = off + (total_w - row_w) // 2 + n["col"] * DAG_COL_PITCH
        rects[n["step"]] = Rect(x, DAG_HEAD + n["level"] * ROW_PITCH, BOX_W, BOX_H)

    # stats block
    crit: list[str] = lay["critical"]
    crit_dur = sum((steps[s].get("duration") or 0.0) for s in crit if s in steps)
    slowest = max((s for s in steps.values() if s.get("duration") is not None), key=lambda s: s["duration"], default=None)
    muted = Style(color=MUTED)
    cv.put(0, 0, f"{len(lay['nodes'])} nodes · {len(lay['edges'])} edges · {lay['levels']} levels", muted)
    cv.put(0, 1, f"max parallel {lay['width']}", muted)
    cv.put(0, 2, "critical path ", muted)
    cv.put(14, 2, f"{fmt.duration(crit_dur)} ({len(crit)} steps) ", Style(color=AMBER))
    cv.put(14 + len(f"{fmt.duration(crit_dur)} ({len(crit)} steps) "), 2, "bright", Style(color=WHITE))
    if slowest:
        cv.put(0, 3, f"slowest: {fmt.truncate(slowest['title'], 28)} {fmt.duration(slowest['duration'])}", muted)

    pairs = set(zip(crit, crit[1:]))
    marks = _draw_edges(cv, lay["edges"], rects, pairs)
    for sid, r in rects.items():
        draw_box(cv, r.x, r.y, steps[sid], dag_meta(steps[sid], agents), selected == sid)
    _stamp_edge_marks(cv, marks)
    return Rendered(cv.to_text(), rects, width, height)


def render_dag(snapshot: dict, selected: str | None = None) -> Text:
    """DAG view: dependency levels top-down, critical path drawn in bright white."""
    return build_dag(snapshot, selected).text


# --------------------------------------------------------------------------- sequence
SEQ_LEFT = 9           # time gutter
SEQ_PITCH = 16
SEQ_BOX_W = 14
SEQ_FIRST_ROW = 3


def _participant(pid: str, agents: dict[str, dict]) -> tuple[str, str]:
    if pid == "user":
        return "User", WHITE
    if pid == "tools":
        return "Tools", MUTED
    a = agents.get(pid) or {}
    return a.get("name") or pid, _hex(a.get("color"))


_SEQ_ARROW = {  # kind -> (line char, colour, label colour)
    "call": ("─", WHITE, WHITE),
    "spawn": ("─", WHITE, WHITE),
    "message": ("─", WHITE, WHITE),
    "return": ("┄", MUTED, MUTED),
    "error": ("─", RED, RED),
    "running": ("┄", AMBER, AMBER),
}


def build_seq(snap: dict, selected: str | None = None) -> Rendered:
    lay = snap["layouts"]["seq"]
    agents = _agent_map(snap)
    parts: list[str] = lay["participants"]
    rows: list[dict] = lay["rows"]
    t0 = snap["run"]["started"] or 0.0
    n = len(rows)
    lifeline_x = {p: SEQ_LEFT + 7 + SEQ_PITCH * i for i, p in enumerate(parts)}
    width = SEQ_LEFT + SEQ_PITCH * max(1, len(parts)) + 1
    running = snap["run"]["status"] == "running"
    height = SEQ_FIRST_ROW + 2 * n + (3 if running else 1)
    cv = Canvas(width, height)
    dim = Style(color=DIM)

    # participant header boxes
    for p in parts:
        name, color = _participant(p, agents)
        x = lifeline_x[p] - 7
        cv.put(x, 0, "╭" + "─" * 12 + "╮", dim)
        cv.put(x, 1, "│", dim)
        cv.put(x + 1, 1, fmt.truncate(name, 12).center(12), Style(color=color, bold=True))
        cv.put(x + 13, 1, "│", dim)
        cv.put(x, 2, "╰" + "─" * 6 + "┬" + "─" * 5 + "╯", dim)
        for y in range(SEQ_FIRST_ROW, height):
            cv.put(lifeline_x[p], y, "┆", dim)

    def label_y(i: int) -> int:
        return SEQ_FIRST_ROW + 2 * i

    # activation bars in the agent's colour
    for aid, (a, b) in lay["activations"].items():
        if aid in lifeline_x:
            style = Style(color=_hex((agents.get(aid) or {}).get("color")))
            for y in range(label_y(a), label_y(b) + 2):
                cv.put(lifeline_x[aid], y, "┃", style)

    rects: dict[str, Rect] = {}
    for i, m in enumerate(rows):
        ya = label_y(i) + 1
        sel = bool(selected) and m.get("step") == selected
        glyph, color, lcolor = _SEQ_ARROW.get(m["kind"], _SEQ_ARROW["call"])
        bold = Style(bold=True) if sel else Style()
        # time gutter: +seconds since run start
        cv.put(0, ya, f"+{max(0.0, m['ts'] - t0):.1f}".rjust(SEQ_LEFT - 2), Style(color=RED if m["kind"] == "error" else MUTED))
        if m["src"] not in lifeline_x or m["dst"] not in lifeline_x:
            continue
        xa, xb = lifeline_x[m["src"]], lifeline_x[m["dst"]]
        label_style = Style(color=lcolor) + bold
        if sel:
            label_style = Style(color=INK, bgcolor=lcolor, bold=True)
        if xa == xb:  # self message: a note beside the lifeline
            cv.put(xa + 2, ya, "▏" + fmt.truncate(m["label"], SEQ_PITCH * 3), label_style)
            x0, span = xa, 2
        else:
            line_style = Style(color=color) + bold
            if xb > xa:
                if xb - 2 >= xa + 1:
                    cv.put(xa + 1, ya, glyph * (xb - xa - 2), line_style)
                cv.put(xb - 1, ya, "▶", line_style)
                lx, room = xa + 2, xb - xa - 3
            else:
                if xa - 1 >= xb + 2:
                    cv.put(xb + 2, ya, glyph * (xa - xb - 2), line_style)
                cv.put(xb + 1, ya, "◀", line_style)
                lx, room = xb + 3, xa - xb - 3
            cv.put(lx, label_y(i), fmt.truncate(m["label"], max(4, room)), label_style)
            x0, span = min(xa, xb), abs(xb - xa)
        if m.get("step") and m["step"] not in rects:
            rects[m["step"]] = Rect(x0, label_y(i), span + 1, 2)

    if running:
        y = SEQ_FIRST_ROW + 2 * n + 1
        amber = Style(color=AMBER)
        cv.put(2, y, "now ▸", amber)
        cv.put(SEQ_LEFT, y, "┈" * (width - SEQ_LEFT), amber)
    return Rendered(cv.to_text(), rects, width, height)


def render_seq(snapshot: dict, selected: str | None = None) -> Text:
    """Sequence view: participant lifelines, activation bars, one label+arrow row pair per message."""
    return build_seq(snapshot, selected).text


BUILDERS = {"swim": build_swim, "dag": build_dag, "seq": build_seq}
VIEW_TITLES = {"swim": "Graph · swimlanes", "dag": "Graph · DAG", "seq": "Graph · sequence"}


# --------------------------------------------------------------------------- timeline
_NICE = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 28800, 86400]


def _tick_label(t: float) -> str:
    if t < 120:
        return f"{t:g}s"
    m, s = divmod(int(t), 60)
    if m < 60:
        return f"{m}m" if s == 0 else f"{m}m{s:02d}"
    h, m = divmod(m, 60)
    return f"{h}h" if m == 0 else f"{h}h{m:02d}"


def _axis(span: float, bar_w: int, pad: float) -> tuple[float, float]:
    """Pick a tick step and an axis length (a whole number of steps, >= span*pad)."""
    want = max(span * pad, 1.0)
    for step in _NICE:
        if want / step <= max(2, bar_w / 9):
            break
    else:
        step = _NICE[-1]
    return step, math.ceil(want / step) * step


def timeline_title(snap: dict) -> str:
    """Pane title: ``Timeline · 0–60s · │ now +55s``."""
    tl = snap["layouts"]["timeline"]
    span = tl["t1"] - tl["t0"]
    running = snap["run"]["status"] == "running"
    _, axis = _axis(span, 100, 1.08 if running else 1.0)
    base = f"Timeline · 0–{_tick_label(axis)}"
    return f"{base} · │ now +{span:.0f}s" if running else base


def render_timeline(snapshot: dict, width: int, selected: str | None = None) -> Text:
    """One row per agent: █ done, ▓ running, █ red error, ░ idle between work, ┄ queued, amber │ now."""
    tl = snapshot["layouts"]["timeline"]
    agents = _agent_map(snapshot)
    running = snapshot["run"]["status"] == "running"
    lanes = tl["lanes"]
    names = [fmt.truncate((agents.get(l["agent"]) or {}).get("name") or l["agent"], 12) for l in lanes]
    name_w = min(14, max([len(n) for n in names] + [6]) + 2)
    bar_w = max(10, width - name_w - 1)
    t0, t1 = tl["t0"], tl["t1"]
    span = max(t1 - t0, 1.0)
    step, axis_len = _axis(span, bar_w, 1.08 if running else 1.0)
    scale = bar_w / axis_len

    def xof(t: float) -> int:
        return max(0, min(bar_w - 1, int((t - t0) * scale)))

    now_x = xof(t1) if running else None
    out = Text(no_wrap=True, overflow="crop")
    for lane, name in zip(lanes, names):
        agent = agents.get(lane["agent"]) or {}
        cells: list[tuple[str, Style | None]] = [(" ", None)] * bar_w
        timed = [b for b in lane["bars"] if b["status"] != "queued"]
        if timed:
            first = xof(min(b["start"] for b in timed))
            last = xof(t1) if agent.get("status") == "running" else xof(max(b["end"] for b in timed))
            for x in range(first, last + 1):
                cells[x] = ("░", Style(color=DIM))
        done_idx = 0
        for b in timed:
            a = xof(b["start"])
            e = max(a + 1, min(bar_w, int((b["end"] - t0) * scale + 0.999)))
            if b["status"] == "running":
                ch, col = "▓", AMBER
            elif b["status"] == "error":
                ch, col = "█", RED
            else:
                ch, col = "█", GREEN if done_idx % 2 == 0 else GREEN2
                done_idx += 1
            if selected is not None and b["step"] == selected:
                col = WHITE
            for x in range(a, e):
                cells[x] = (ch, Style(color=col))
        if running and now_x is not None and any(b["status"] == "queued" for b in lane["bars"]):
            for x in range(now_x + 2, min(bar_w, now_x + 5)):
                cells[x] = ("┄", Style(color=MUTED))
        if now_x is not None:
            cells[now_x] = ("│", Style(color=AMBER))
        out.append(f" {name:<{name_w - 1}}", Style(color=_hex(agent.get("color")), bold=True))
        for ch, st in cells:
            out.append(ch, st)
        out.append("\n")
    # axis labels
    axis = [" "] * bar_w
    t, last_end = 0.0, -2
    while t <= axis_len + 1e-9:
        label = _tick_label(t)
        x = int(t * scale)
        start = x if t == 0 else x - len(label) // 2
        start = max(0, min(bar_w - len(label), start))
        if start > last_end + 1:
            axis[start:start + len(label)] = label
            last_end = start + len(label) - 1
        t += step
    out.append(" " * name_w + "".join(axis), Style(color=MUTED))
    return out


# --------------------------------------------------------------------------- side panes
_STATUS_BADGE = {
    "running": ("RUNNING", AMBER),
    "done": ("DONE", GREEN),
    "error": ("FAILED", RED),
    "stale": ("STALE", MUTED),
    "queued": ("QUEUED", MUTED),
}


def badge(label: str, color: str) -> tuple[str, Style]:
    return f" {label} ", Style(color=INK, bgcolor=color, bold=True)


def status_text(snap: dict | None, width: int, now: float | None = None) -> Text:
    """Top status bar: brand, run title, state badge, then live metrics and the wall clock."""
    left = Text(no_wrap=True, overflow="crop")
    left.append(" agentmon ", Style(color=INK, bgcolor="#B49CFF", bold=True))
    if snap is None:
        left.append(" no run selected ", Style(color=MUTED))
        return left
    run, m = snap["run"], snap["metrics"]
    left.append(f" {run['title'] or run['id']} ", Style(color=WHITE, bold=True))
    left.append(f"run #{run['id']} ", Style(color=MUTED))
    label, color = _STATUS_BADGE.get(run["status"], (run["status"].upper(), MUTED))
    left.append(*badge(label, color))
    left.append("  ")
    c = m["steps"]
    toks = m["tokens"]
    parts: list[tuple[str, str, str]] = [
        ("elapsed ", fmt.clock(m["elapsed"]), WHITE),
        ("steps ", f"{c.get('done', 0) + c.get('error', 0)}/{m['steps_total']}", WHITE),
        ("tools ", str(m["tool_calls"]), WHITE),
        ("tokens ", f"{fmt.tokens(toks.get('in', 0) + toks.get('cache_read', 0))}↑ {fmt.tokens(toks.get('out', 0))}↓", WHITE),
        ("cache ", "—" if m["cache_hit"] is None else f"{m['cache_hit'] * 100:.0f}%", WHITE),
        ("cost ", fmt.money(m["cost"]), WHITE),
        ("errors ", str(m["errors"]), RED if m["errors"] else WHITE),
    ]
    for label, value, col in parts:
        left.append(label, Style(color=MUTED))
        left.append(value + "  ", Style(color=col, bold=True))
    clock = fmt.hms(now if now is not None else run["now"])
    pad = max(1, width - left.cell_len - len(clock) - 1)
    left.append(" " * pad)
    left.append(clock + " ", Style(color=MUTED))
    return left


_KEY_RE = re.compile(r'^(\s*)("(?:[^"\\]|\\.)*")(:)(.*)$')


def _clip_lines(text: str, width: int, limit: int) -> list[str]:
    lines = text.splitlines() or [""]
    out = [ln if len(ln) <= width else ln[: width - 1] + "…" for ln in lines[:limit]]
    if len(lines) > limit:
        out.append(f"… {len(lines) - limit} more lines")
    return out


def _bar(frac: float, width: int) -> str:
    n = int(round(max(0.0, min(1.0, frac)) * width))
    if frac > 0 and n == 0:
        n = 1
    return "█" * n + "░" * (width - n)


def inspector_text(snap: dict | None, selected: str | None, width: int = 44) -> Text:
    """Details for the selected step: header, meta, input JSON, summary/output, thought, token bars."""
    out = Text(overflow="fold")
    steps = {s["id"]: s for s in (snap["steps"] if snap else [])}
    step = steps.get(selected or "")
    if snap is None or step is None:
        out.append("No step selected.\n", Style(color=MUTED))
        out.append("j/k or ↑/↓ to select, f to follow the live step.", Style(color=DIM))
        return out
    agents = _agent_map(snap)
    agent = agents.get(step["agent"]) or {}
    th = _THEMES.get(step["status"], _THEMES["queued"])
    width = max(20, width)
    label, color = _STATUS_BADGE.get(step["status"], (step["status"].upper(), MUTED))
    title = fmt.truncate(step["title"], max(8, width - len(label) - 6))
    out.append(f"{th.icon} ", Style(color=th.icon_color))
    out.append(title, Style(color=WHITE, bold=True))
    out.append(" " * max(1, width - len(title) - len(label) - 4))
    out.append(*badge(label, color))
    out.append("\n")
    meta = [(agent.get("name") or step["agent"], _hex(agent.get("color")))]
    if agent.get("model"):
        meta.append((agent["model"], MUTED))
    if step.get("tool"):
        meta.append((step["tool"], BLUE))
    for i, (t, col) in enumerate(meta):
        out.append(("" if i == 0 else " · "), Style(color=DIM))
        out.append(t, Style(color=col))
    out.append("\n")
    t0 = snap["run"]["started"] or 0.0
    bits = []
    if step["started"] is not None:
        bits.append(f"started +{step['started'] - t0:.1f}s")
    if step["duration"] is not None:
        bits.append(f"{fmt.duration(step['duration'])}{'…' if step['status'] == 'running' else ''}")
    bits.append(f"step {step['seq'] + 1}/{len(steps)}")
    out.append(" · ".join(bits) + "\n", Style(color=MUTED))
    if step["deps"]:
        names = ", ".join(fmt.truncate((steps.get(d) or {"title": d})["title"], 18) for d in step["deps"][:4])
        out.append(f"after {names}\n", Style(color=MUTED))

    def section(name: str) -> None:
        out.append(f"─ {name} " + "─" * max(0, width - len(name) - 3) + "\n", Style(color=DIM))

    if step.get("input"):
        section("input")
        for ln in _clip_lines(step["input"], width, 12):
            m = _KEY_RE.match(ln)
            if m:
                out.append(m.group(1) + m.group(2), Style(color=BLUE))
                out.append(m.group(3) + m.group(4) + "\n", Style(color=TEXT))
            else:
                out.append(ln + "\n", Style(color=TEXT))
    body = step.get("output") or step.get("summary")
    if body:
        section("output" if step.get("output") else "summary")
        for ln in _clip_lines(body, width, 10):
            out.append(ln + "\n", Style(color=RED if step["status"] == "error" else TEXT))
        if step.get("output") and step.get("summary"):
            out.append(step["summary"] + "\n", Style(color=MUTED))
    if step["status"] == "running" and agent.get("last_thought"):
        section("thinking")
        for ln in textwrap.wrap(agent["last_thought"], width)[:8]:
            out.append(ln + "\n", Style(color=MUTED, italic=True))
        out.append("▍\n", Style(color=AMBER))
    tokens = step.get("tokens") or {}
    scope = "this step"
    if not tokens and step["status"] == "running":
        tokens, scope = agent.get("tokens") or {}, f"{agent.get('name') or step['agent']} so far"
    rows = [("in", tokens.get("in", 0)), ("cache", tokens.get("cache_read", 0)), ("out", tokens.get("out", 0))]
    if any(v for _, v in rows):
        section(f"tokens {scope}")
        peak = max(v for _, v in rows) or 1
        bw = max(8, min(20, width - 16))
        for name, v in rows:
            out.append(f"{name:<5} ", Style(color=MUTED))
            out.append(_bar(v / peak, bw), Style(color={"in": BLUE, "cache": GREEN, "out": AMBER}[name]))
            out.append(f" {fmt.tokens(v):>6}\n", Style(color=TEXT))
    out.rstrip()
    return out


_EVENT_KIND = {"step_start": "start", "step_end": "result", "agent_start": "spawn", "agent_end": "agent_end",
               "thinking": "thinking", "message": "message", "note": "note", "run_start": "run", "run_end": "run"}


def events_text(snap: dict | None, width: int = 60, limit: int = 100) -> Text:
    """Event log, newest first: time, agent (agent colour), kind, text (red for errors)."""
    out = Text(no_wrap=True, overflow="ellipsis")
    if snap is None or not snap["log"]:
        out.append("no events yet", Style(color=MUTED))
        return out
    agents = _agent_map(snap)
    text_w = max(10, width - 8 - 1 - 9 - 1 - 11)
    for i, ev in enumerate(reversed(snap["log"][-limit:])):
        a = agents.get(ev["agent"] or "")
        kind = _EVENT_KIND.get(ev["type"], ev["type"])
        body = ev["text"]
        for prefix, label in (("queued ", "queued"), ("start ", "start"), ("done ", "result"), ("failed ", "error")):
            if ev["type"] in ("step_start", "step_end") and body.startswith(prefix):
                kind, body = label, body[len(prefix):]
                break
        if ev["level"] == "error":
            kind = "error"
        tcol = RED if ev["level"] == "error" else (MUTED if ev["type"] == "thinking" else TEXT)
        if i:
            out.append("\n")
        out.append(fmt.hms(ev["ts"]) + " ", Style(color=DIM))
        out.append(f"{fmt.truncate((a or {}).get('name') or ev['agent'] or 'run', 9):<9} ",
                   Style(color=_hex((a or {}).get("color"))))
        out.append(f"{kind:<10}", Style(color=RED if kind == "error" else MUTED))
        out.append(fmt.truncate(body, text_w), Style(color=tcol, italic=ev["type"] == "thinking"))
    return out


def context_text(snap: dict | None, width: int = 44) -> Text:
    """Per-agent context-window bars plus token and cost totals (each line fits ``width``)."""
    out = Text(no_wrap=True, overflow="ellipsis")
    if snap is None:
        return out
    nw = min(10, max([len(a["name"]) for a in snap["agents"]] + [5]))
    bw = max(4, min(16, width - nw - 21))
    for i, a in enumerate(snap["agents"]):
        if i:
            out.append("\n")
        color = _hex(a["color"])
        out.append(f"{fmt.truncate(a['name'], nw):<{nw}} ", Style(color=color))
        ctx = a.get("context")
        if a["status"] in ("done", "error"):
            out.append("─" * bw + " released", Style(color=DIM))
        elif ctx and ctx.get("max"):
            frac = ctx["used"] / ctx["max"]
            out.append(_bar(frac, bw), Style(color=RED if frac > 0.9 else AMBER if frac > 0.75 else color))
            out.append(f" {frac * 100:>3.0f}% ", Style(color=TEXT))
            out.append(f"{ctx['used'] / 1000:.0f}k/{ctx['max'] / 1000:.0f}k", Style(color=MUTED))
        else:
            t = a["tokens"]
            out.append("░" * bw + f" {fmt.tokens(t.get('in', 0))} in {fmt.tokens(t.get('out', 0))} out", Style(color=MUTED))
        if a.get("cost") is not None:
            out.append(f" {fmt.money(a['cost'])}", Style(color=MUTED))
    m = snap["metrics"]
    out.append("\n")
    out.append(f"total {fmt.money(m['cost'])} · {fmt.tokens(m['tokens'].get('in', 0))} in · "
               f"{fmt.tokens(m['tokens'].get('out', 0))} out", Style(color=WHITE))
    return out
