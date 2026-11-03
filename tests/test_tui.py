"""Tests for the terminal UI (agentmon[tui]): pure renderers and a Textual pilot run."""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

from rich.cells import cell_len  # noqa: E402

from agentmon import demo, fmt, store  # noqa: E402
from agentmon.tui import render as R  # noqa: E402
from agentmon.tui.app import AgentmonApp  # noqa: E402


def _snap(until: float | None) -> dict:
    return store.load(demo.build("t", until=until)).snapshot()


@pytest.fixture(params=[55, None], ids=["mid-run", "complete"])
def snap(request) -> dict:
    return _snap(request.param)


@pytest.fixture
def mid() -> dict:
    return _snap(55)


def _lines(text) -> list[str]:
    return text.plain.split("\n")


def _styled(text, chars: str, color: str) -> bool:
    """Is any character from ``chars`` rendered in ``color``?"""
    plain = text.plain
    for span in text.spans:
        style = span.style
        if getattr(style, "color", None) is not None and style.color.name.lower() == color.lower():
            if any(c in chars for c in plain[span.start:span.end]):
                return True
    return False


# --------------------------------------------------------------------------- canvas
def test_canvas_junctions_merge():
    cv = R.Canvas(5, 5)
    s = R.Style(color="#ffffff")
    cv.line([(0, 2), (4, 2)], s)
    cv.line([(2, 0), (2, 4)], s)
    assert cv.cell_char(2, 2) == "┼"
    cv.line([(0, 0), (0, 1), (1, 1)], s)  # a corner whose bits come from two segments
    assert cv.cell_char(0, 1) == "└"
    cv.line([(4, 0), (4, 2)], s)  # meets the horizontal at its end -> top-right corner joins
    assert cv.cell_char(4, 2) == "┘"
    assert cv.cell_char(2, 0) == "│"


def test_canvas_dashed_only_when_every_line_is_dashed():
    cv = R.Canvas(3, 3)
    s = R.Style(color="#ffffff")
    cv.line([(1, 0), (1, 2)], s, dashed=True)
    assert cv.cell_char(1, 1) == "┊"
    cv.line([(0, 1), (2, 1)], s, dashed=False)
    assert cv.cell_char(1, 1) == "┼"


def test_route_edge_cases():
    boxes = [R.Rect(0, 0, 10, 3), R.Rect(0, 5, 10, 3), R.Rect(0, 10, 10, 3), R.Rect(20, 10, 10, 3)]
    straight = R.route_edge(boxes[0], boxes[1], boxes, 40)
    assert straight[-1] == (5, 4) and len({p[0] for p in straight}) == 1
    # blocked column (a box sits between rows 0 and 2) forces a gutter detour
    detour = R.route_edge(boxes[0], boxes[2], boxes, 40)
    assert len({p[0] for p in detour}) > 1 and detour[-1] == (5, 9)
    # every segment is axis aligned
    for path in (straight, detour, R.route_edge(boxes[0], boxes[3], boxes, 40)):
        assert all(a[0] == b[0] or a[1] == b[1] for a, b in zip(path, path[1:]))


# --------------------------------------------------------------------------- views
@pytest.mark.parametrize("view", ["swim", "dag", "seq"])
def test_graph_views_are_rectangular(snap, view):
    rendered = R.BUILDERS[view](snap)
    lines = _lines(rendered.text)
    widths = {cell_len(ln) for ln in lines}
    assert widths == {rendered.width}, f"{view}: ragged lines {sorted(widths)}"
    assert len(lines) == rendered.height
    # every character occupies exactly one terminal column (no wide glyphs sneaking in)
    assert all(cell_len(ch) == 1 for ln in lines for ch in ln)


def test_timeline_is_rectangular(snap):
    lines = _lines(R.render_timeline(snap, 120))
    assert len(lines) == len(snap["layouts"]["timeline"]["lanes"]) + 1
    assert len({cell_len(ln) for ln in lines}) == 1


def test_step_titles_present(snap):
    for view in ("swim", "dag"):
        plain = R.BUILDERS[view](snap).text.plain
        for step in snap["steps"]:
            assert fmt.truncate(step["title"], R.BOX_W - 7) in plain, (view, step["title"])


def test_swim_headers_and_boxes(mid):
    plain = R.render_swim(mid).plain
    for name in ("ORCHESTRATOR", "EXPLORER", "CODER", "TESTER"):
        assert name in plain
    assert plain.count("╭") == len(mid["steps"])
    assert plain.count("╰") == len(mid["steps"])
    assert "● active" in plain and "✓ done" in plain
    for icon in "✓●✗○":
        assert icon in plain


def test_every_edge_ends_in_an_arrowhead(mid):
    plain = R.render_swim(mid).plain
    targets = {e["dst"] for e in mid["layouts"]["swim"]["edges"]}
    assert plain.count("▼") == len(targets)  # arrowheads merge where edges share a target


def test_edges_are_coloured_by_kind(mid):
    assert {e["kind"] for e in mid["layouts"]["swim"]["edges"]} >= {"done", "pending", "error"}
    text = R.render_swim(mid)
    assert _styled(text, "▼│┌┐└┘├┤─", R.RED), "error edge must be red"
    assert _styled(text, "┊┄", R.MUTED), "pending edge is dashed and muted"
    dag = R.render_dag(mid)
    assert _styled(dag, "▼│─", R.RED)
    assert _styled(dag, "▼│─┌┐└┘├┤", R.WHITE), "critical-path edges are bright"


def test_error_edge_red_when_run_complete():
    text = R.render_swim(_snap(None))
    assert _styled(text, "▼│─┌┐└┘├┤", R.RED)  # the failed pytest still routes red into the retry


def test_selection_is_highlighted(mid):
    plain_a = R.build_swim(mid, None)
    plain_b = R.build_swim(mid, "c3")
    assert plain_a.text.plain != plain_b.text.plain  # heavy border glyphs
    assert "━" in plain_b.text.plain and "━" not in plain_a.text.plain
    # the selected title is drawn reversed (dark ink on a coloured background)
    assert any(getattr(s.style, "bgcolor", None) is not None for s in plain_b.text.spans)
    r = plain_b.rects["c3"]
    assert R.step_at(plain_b.rects, r.x + 2, r.y + 1) == "c3"
    assert R.step_at(plain_b.rects, 0, 0) is None


def test_dag_stats_block(mid):
    plain = R.render_dag(mid).plain
    assert f"{len(mid['steps'])} nodes" in plain
    assert "critical path" in plain and "levels" in plain


def test_sequence_now_line_only_while_running():
    running = R.render_seq(_snap(55)).plain
    done = R.render_seq(_snap(None)).plain
    assert "now ▸" in running and "┈" in running
    assert "now ▸" not in done
    assert "+0.0" in running and "Orchestrator" in running
    assert "┃" in running and "┆" in running
    assert "◀" in running and "▶" in running


def test_sequence_error_message_is_red(mid):
    text = R.render_seq(mid)
    assert _styled(text, "◀─", R.RED)
    assert _styled(text, "┄", R.AMBER), "running call is amber and dashed"


def test_timeline_marks_and_title(mid):
    text = R.render_timeline(mid, 120)
    plain = text.plain
    assert "▓" in plain and "█" in plain and "░" in plain
    assert _styled(text, "│", R.AMBER) and _styled(text, "█", R.RED)
    assert "now +55s" in R.timeline_title(mid)
    done = R.render_timeline(_snap(None), 120)
    assert not _styled(done, "│", R.AMBER)
    assert "now" not in R.timeline_title(_snap(None))


def test_side_panes(mid):
    ins = R.inspector_text(mid, "c3", 48).plain
    assert "Fix refresh expiry" in ins and "RUNNING" in ins
    assert '"old_string"' in ins and "clock-skew" in ins and "tokens" in ins
    assert "No step selected" in R.inspector_text(mid, None).plain
    ev = R.events_text(mid, 60).plain.split("\n")
    assert "thinking" in ev[0]  # newest first
    ctx = R.context_text(mid, 50).plain
    assert "released" in ctx and "31%" in ctx and "$0.62" in ctx
    bar = R.status_text(mid, 180).plain
    assert "RUNNING" in bar and "auth-pkce-refactor" in bar and "errors 1" in bar and "cost $0.62" in bar
    assert R.status_text(None, 80).plain.strip().startswith("agentmon")


def test_empty_snapshot_renders():
    snap = store.load([]).snapshot()
    for view in ("swim", "dag", "seq"):
        R.BUILDERS[view](snap)  # must not raise on a run with no steps
    R.render_timeline(snap, 80)
    R.inspector_text(snap, None)
    R.events_text(snap)
    R.context_text(snap)
    R.status_text(snap, 100)


# --------------------------------------------------------------------------- app
def svg_text(path: str) -> str:
    """Plain screen text of a Textual SVG screenshot, one line per terminal row.

    Textual positions every styled run by its x coordinate (12.2px per column), so the
    columns are rebuilt from those rather than by concatenating runs.
    """
    import html
    import re

    pattern = r'<text[^>]*x="([\d.]+)"[^>]*clip-path="url\(#[\w-]*-line-(\d+)\)">(.*?)</text>'
    rows: dict[int, dict[int, str]] = {}
    for m in re.finditer(pattern, open(path, encoding="utf-8").read()):
        col = round(float(m.group(1)) / 12.2)
        for i, ch in enumerate(html.unescape(m.group(3)).replace("\xa0", " ")):
            rows.setdefault(int(m.group(2)), {})[col + i] = ch
    return "\n".join(
        "".join(cells.get(c, " ") for c in range(max(cells) + 1)).rstrip() for _, cells in sorted(rows.items()))


async def test_app_empty_state_then_picks_up_new_run(isolated_home):
    app = AgentmonApp()
    async with app.run_test(size=(180, 60)) as pilot:
        await pilot.pause()
        assert app.snap is None
        canvas = app.query_one("#canvas")
        assert "agentmon install-hooks" in str(canvas.render()) and "agentmon demo" in str(canvas.render())
        demo.run(instant=True, until=55, echo=lambda *_: None)
        await pilot.pause(1.6)  # scan interval is 1s
        assert app.snap is not None and app.snap["run"]["id"] == "demo"


async def test_app_keys_and_views(isolated_home, tmp_path):
    demo.run(instant=True, until=55, echo=lambda *_: None)
    app = AgentmonApp()
    async with app.run_test(size=(180, 60)) as pilot:
        await pilot.pause()
        assert app.view == "swim" and app.follow and app.selected == "c3"
        await pilot.press("2")
        assert app.view == "dag"
        await pilot.press("3")
        assert app.view == "seq"
        await pilot.press("1")
        assert app.view == "swim"
        await pilot.press("k")
        assert app.selected == "t2" and not app.follow  # previous step by seq
        await pilot.press("j")
        assert app.selected == "c3"
        await pilot.press("f")
        assert app.follow
        await pilot.press("down", "up")  # arrows are step selection too
        assert not app.follow
        # clicking a tab switches view
        await pilot.click(app.query("TabLabel").last())
        assert app.view == "seq"
        for key in ("2", "1"):
            await pilot.press(key)
        shot = app.save_screenshot("swim.svg", path=str(tmp_path))
        screen = svg_text(shot)
        assert "Fix refresh expiry" in screen and "[1] Swimlanes" in screen and "Inspector" in screen


async def test_app_run_picker(isolated_home):
    demo.run("alpha", instant=True, until=20, echo=lambda *_: None)
    demo.run("beta", instant=True, until=55, echo=lambda *_: None)
    app = AgentmonApp(run="alpha")
    async with app.run_test(size=(180, 60)) as pilot:
        await pilot.pause()
        assert app.snap["run"]["id"] == "alpha" and not app.follow_latest
        await pilot.press("r")
        await pilot.pause()
        assert type(app.screen).__name__ == "RunPicker"
        await pilot.press("escape")
        await pilot.pause()
        assert type(app.screen).__name__ != "RunPicker"
        await pilot.press("L")
        await pilot.pause()
        assert app.follow_latest and app.snap["run"]["id"] == "beta"


async def test_app_follows_live_updates(isolated_home):
    from agentmon import sink

    evs = demo.build("live", until=30)
    for ev in evs[:12]:
        sink.append(ev)
    app = AgentmonApp()
    async with app.run_test(size=(180, 60)) as pilot:
        await pilot.pause(0.5)
        first = app.snap["run"]["events"]
        for ev in evs[12:]:
            sink.append(ev)
        await pilot.pause(0.8)
        assert app.snap["run"]["events"] > first
        assert app.selected == app.snap["focus"]


@pytest.mark.parametrize("size", [(100, 30), (60, 20)])
async def test_app_survives_small_terminals(isolated_home, size):
    demo.run(instant=True, echo=lambda *_: None)
    app = AgentmonApp(view="dag")
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        for key in ("3", "1", "j", "k", "f"):
            await pilot.press(key)
        assert app.snap is not None and app.snap["run"]["status"] == "done"


async def test_app_picker_enter_switches_run(isolated_home):
    demo.run("alpha", instant=True, until=20, echo=lambda *_: None)
    demo.run("beta", instant=True, until=55, echo=lambda *_: None)
    app = AgentmonApp()
    async with app.run_test(size=(180, 60)) as pilot:
        await pilot.pause()
        assert app.snap["run"]["id"] == "beta"  # newest by default
        await pilot.press("r", "down", "enter")  # the list is newest first, so this picks alpha
        await pilot.pause()
        assert app.snap["run"]["id"] == "alpha" and not app.follow_latest
