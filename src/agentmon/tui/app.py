"""Textual front end for agentmon.

Layout (matches the approved mockup)::

    status bar    brand · run title · RUNNING badge · live metrics · clock
    tab row       [1] Swimlanes  [2] DAG  [3] Sequence                legend
    graph pane    scrollable both ways           | Inspector  (selected step)
                                                 | Events     (newest first)
                                                 | Context    (per-agent bars)
    timeline      one row per agent, amber │ marks "now"
    footer        key bindings

The app polls a :class:`~agentmon.tail.RunWatcher` every 250 ms, rebuilds the snapshot
when new events arrive (and once a second while the run is live so clocks tick), and
only re-renders the graph view that is on screen.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical, VerticalScroll
from textual.geometry import Region, Spacing
from textual.screen import ModalScreen
from textual.widgets import Footer, Label, OptionList, Static
from textual.widgets.option_list import Option

from .. import fmt, paths, tail
from . import render as R

VIEWS = ("swim", "dag", "seq")
TAB_LABELS = {"swim": "[1] Swimlanes", "dag": "[2] DAG", "seq": "[3] Sequence"}
POLL_SECONDS = 0.25
SNAPSHOT_SECONDS = 1.0  # while running, rebuild at least this often so clocks advance


def empty_message() -> Text:
    """Friendly first-run screen shown when there is no run (or the run file is still empty)."""
    t = Text()
    t.append("No agent runs yet.\n\n", Style(color=R.WHITE, bold=True))
    t.append(f"agentmon follows run files in {paths.runs_dir()}\n", Style(color=R.MUTED))
    t.append("and will pick up the newest one as soon as it appears.\n\n", Style(color=R.MUTED))
    t.append("Stream your Claude Code sessions:\n", Style(color=R.TEXT))
    t.append("  agentmon install-hooks", Style(color=R.AMBER, bold=True))
    t.append("        then start a new Claude Code session\n\n", Style(color=R.MUTED))
    t.append("Or watch a sample multi-agent run:\n", Style(color=R.TEXT))
    t.append("  agentmon demo --speed 4", Style(color=R.AMBER, bold=True))
    t.append("        live replay, run it in another terminal\n", Style(color=R.MUTED))
    t.append("  agentmon demo --instant --until 55", Style(color=R.AMBER, bold=True))
    t.append("  a frozen mid-run snapshot\n", Style(color=R.MUTED))
    return t


class TabLabel(Static):
    """A clickable tab; clicking switches the graph view."""

    def __init__(self, view: str) -> None:
        super().__init__(TAB_LABELS[view], classes="tab")
        self.view = view

    def on_click(self) -> None:
        self.app.action_view(self.view)  # type: ignore[attr-defined]


class GraphCanvas(Static):
    """The graph itself; a click selects the step under the pointer."""

    def on_click(self, event: events.Click) -> None:
        self.app.click_graph(event.x, event.y)  # type: ignore[attr-defined]

    def on_resize(self, event: events.Resize) -> None:
        # New content changes our size only after layout; that is when a pending scroll can land.
        self.app.try_scroll()  # type: ignore[attr-defined]


class RunPicker(ModalScreen["Path | None"]):
    """Modal list of recorded runs, newest first."""

    BINDINGS = [Binding("escape", "close", "Close")]

    DEFAULT_CSS = """
    RunPicker { align: center middle; }
    RunPicker > Vertical { width: 90; max-width: 95%; height: auto; max-height: 80%;
        background: #161B22; border: round #6EA8FE; padding: 0 1; }
    RunPicker Label { color: #8B95A6; margin-bottom: 1; }
    RunPicker OptionList { height: auto; max-height: 24; background: #161B22; border: none; padding: 0; }
    """

    def __init__(self, runs: list[tail.RunInfo], current: Path | None) -> None:
        super().__init__()
        self._runs = runs
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Switch run  (enter to open, esc to cancel)")
            yield OptionList(*[Option(self._row(r)) for r in self._runs])

    def _row(self, info: tail.RunInfo) -> Text:
        here = self._current is not None and info.path == self._current
        age = max(0, time.time() - info.mtime)
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append("● " if here else "  ", Style(color=R.AMBER))
        t.append(fmt.truncate(info.title or info.id, 34).ljust(34), Style(color=R.WHITE))
        t.append(" " + fmt.truncate(info.id, 22).ljust(22), Style(color=R.MUTED))
        t.append(f" {fmt.duration(age)} ago".rjust(12), Style(color=R.DIM))
        t.append(f" {info.size / 1024:.0f}k".rjust(7), Style(color=R.DIM))
        return t

    def on_mount(self) -> None:
        ol = self.query_one(OptionList)
        for i, info in enumerate(self._runs):
            if self._current is not None and info.path == self._current:
                ol.highlighted = i
                break
        else:
            ol.highlighted = 0
        ol.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self._runs[event.option_index].path)

    def action_close(self) -> None:
        self.dismiss(None)


class AgentmonApp(App[None]):
    """Live terminal monitor for one agentmon run."""

    TITLE = "agentmon"

    CSS = """
    Screen { background: #0D1117; color: #C9D1D9; }
    #statusbar { height: 1; background: #1C2128; }
    #tabs { height: 1; margin-top: 1; border-bottom: none; }
    .tab { width: auto; padding: 0 2; color: #8B95A6; text-style: bold; }
    .tab:hover { color: #E7EAF0; }
    .tab.active { background: #E7EAF0; color: #0D1117; }
    #legend { width: 1fr; text-align: right; color: #5A6475; }
    #main { height: 1fr; }
    #graph { width: 1fr; border: round #3A4250; border-title-color: #6EA8FE; border-title-style: bold;
        border-subtitle-color: #8B95A6; padding: 0 1; }
    #graph:focus { border: round #6EA8FE; }
    #canvas { width: auto; height: auto; }
    #side { width: 52; }
    .pane { border: round #3A4250; padding: 0 1; border-title-style: bold; border-title-color: #C9D1D9; }
    .pane:focus { border: round #6EA8FE; }
    #inspector { height: 3fr; border: round #5C4520; border-title-color: #F2A93B; }
    #inspector:focus { border: round #F2A93B; }
    #events { height: 2fr; }
    #context { height: auto; }
    #timeline { height: 6; margin-top: 0; }
    Footer { background: #161B22; }
    """

    BINDINGS = [
        Binding("1", "view('swim')", "Swimlanes"),
        Binding("2", "view('dag')", "DAG"),
        Binding("3", "view('seq')", "Sequence"),
        Binding("j", "select(1)", "Next step"),
        Binding("k", "select(-1)", "Prev step"),
        Binding("down", "select(1)", "Next step", show=False, priority=True),
        Binding("up", "select(-1)", "Prev step", show=False, priority=True),
        Binding("f", "toggle_follow", "Follow"),
        Binding("r", "pick_run", "Runs"),
        Binding("L", "latest_run", "Latest"),
        Binding("tab", "focus_next", "Pane"),
        Binding("shift+tab", "focus_previous", "Pane", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, run: str | None = None, view: str = "swim") -> None:
        super().__init__()
        self._run_arg = run
        self.view = view if view in VIEWS else "swim"
        self.follow_latest = run in (None, "latest")
        self.follow = True
        self.selected: str | None = None
        self.watcher: tail.RunWatcher | None = None
        self.snap: dict | None = None
        self._rects: dict[str, R.Rect] = {}
        self._known: set[Path] = set()
        self._snap_at = 0.0
        self._scan_at = 0.0
        self._clock_at = 0.0
        self._graph_dims = (0, 0)  # size of the rendered graph, to know when layout caught up
        self._want_scroll = False
        self._last: dict[str, Text] = {}  # last Text pushed to each widget, to skip no-op updates

    # ------------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        yield Static(id="statusbar")
        with Horizontal(id="tabs"):
            for v in VIEWS:
                yield TabLabel(v)
            yield Static(self._legend(), id="legend")
        with Horizontal(id="main"):
            with ScrollableContainer(id="graph"):
                yield GraphCanvas(id="canvas")
            with Vertical(id="side"):
                with VerticalScroll(id="inspector", classes="pane"):
                    yield Static(id="inspector-body")
                with VerticalScroll(id="events", classes="pane"):
                    yield Static(id="events-body")
                yield Static(id="context", classes="pane")
        yield Static(id="timeline", classes="pane")
        yield Footer()

    @staticmethod
    def _legend() -> Text:
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append("legend ", Style(color=R.MUTED))
        for glyph, color, label in (("✓", R.GREEN, "done"), ("●", R.AMBER, "running"), ("✗", R.RED, "failed"),
                                    ("○", R.MUTED, "queued")):
            t.append(glyph, Style(color=color))
            t.append(f" {label} ", Style(color=R.MUTED))
        t.append(" ───", Style(color=R.WHITE))
        t.append(" done ", Style(color=R.MUTED))
        t.append("┄┄┄", Style(color=R.AMBER))
        t.append(" active ", Style(color=R.MUTED))
        t.append("┄┄┄", Style(color=R.MUTED))
        t.append(" pending ", Style(color=R.MUTED))
        t.append("───", Style(color=R.RED))
        t.append(" failed ", Style(color=R.MUTED))
        return t

    def on_mount(self) -> None:
        self.query_one("#timeline").border_title = "┤ Timeline ├"
        self.query_one("#context").border_title = "┤ Context ├"
        self.query_one("#events").border_title = "┤ Events ├"
        self.query_one("#inspector").border_title = "┤ Inspector ├"
        path = tail.resolve(self._run_arg)
        self._known = self._scan_files()
        self._open(path)
        self._sync_tabs()
        self.set_interval(POLL_SECONDS, self._tick)

    def on_resize(self, event: events.Resize) -> None:
        # Width-dependent panes re-render at the new size once layout has settled.
        self.call_after_refresh(self._refresh_side)

    # ------------------------------------------------------------------ run management
    @staticmethod
    def _scan_files() -> set[Path]:
        d = paths.runs_dir()
        return set(d.glob("*.jsonl")) if d.is_dir() else set()

    def _open(self, path: Path | None) -> None:
        """Start watching ``path`` (or show the empty state when there is none)."""
        self.watcher = tail.RunWatcher(path) if path else None
        if self.watcher:
            self.watcher.poll()  # load what is already on disk before the first render
        self.snap = None
        self.selected = None
        self.follow = True
        self._last.clear()
        self._rects = {}
        self._rebuild(force_scroll=True)

    def _current_path(self) -> Path | None:
        return self.watcher.path if self.watcher else None

    def _scan_for_new_run(self) -> None:
        """When following latest, hop to a run file that has appeared since the last scan."""
        files = self._scan_files()
        fresh = files - self._known
        self._known = files
        if not fresh:
            return
        newest = max(fresh, key=lambda p: p.stat().st_mtime if p.exists() else 0.0)
        if newest != self._current_path():
            self._open(newest)

    def _tick(self) -> None:
        now = time.monotonic()
        changed = self.watcher.poll() if self.watcher else False
        if self.follow_latest and now - self._scan_at >= SNAPSHOT_SECONDS:
            self._scan_at = now
            self._scan_for_new_run()
            return
        running = self.snap is not None and self.snap["run"]["status"] == "running"
        if self.watcher and (changed or self.snap is None or (running and now - self._snap_at >= SNAPSHOT_SECONDS)):
            self._rebuild()
        elif now - self._clock_at >= SNAPSHOT_SECONDS:
            self._update_status()

    # ------------------------------------------------------------------ state -> widgets
    def _rebuild(self, force_scroll: bool = False) -> None:
        self._snap_at = self._clock_at = time.monotonic()
        if self.watcher is None or self.watcher.run.event_count == 0:
            self.snap = None
        else:
            self.snap = self.watcher.run.snapshot()
        before = self.selected
        if self.snap is not None:
            ids = {s["id"] for s in self.snap["steps"]}
            if self.follow or self.selected not in ids:
                self.selected = self.snap["focus"]
        else:
            self.selected = None
        self._update_status()
        self._render_graph(scroll=force_scroll or self.selected != before)
        self._refresh_side()

    def _set(self, key: str, widget: Static, text: Text) -> None:
        """Update a Static only when its content actually changed."""
        if self._last.get(key) != text:
            self._last[key] = text
            widget.update(text)

    def _update_status(self) -> None:
        self._clock_at = time.monotonic()
        bar = self.query_one("#statusbar", Static)
        width = bar.size.width or self.size.width
        self._set("status", bar, R.status_text(self.snap, width, time.time()))

    def _render_graph(self, scroll: bool = False) -> None:
        graph = self.query_one("#graph")
        canvas = self.query_one("#canvas", Static)
        graph.border_title = f"┤ {R.VIEW_TITLES[self.view]} ├"
        graph.border_subtitle = "follow ●" if self.follow else "manual · f to follow"
        if self.snap is None:
            self._rects = {}
            self._set("graph", canvas, empty_message())
            return
        rendered = R.BUILDERS[self.view](self.snap, self.selected)
        self._rects = rendered.rects
        self._graph_dims = (rendered.width, rendered.height)
        self._set("graph", canvas, rendered.text)
        if scroll:
            self.request_scroll()

    def request_scroll(self) -> None:
        """Scroll the selected step into view as soon as the graph has been laid out."""
        self._want_scroll = True
        self.call_after_refresh(self.try_scroll)

    def try_scroll(self) -> None:
        if not self._want_scroll:
            return
        canvas = self.query_one("#canvas", Static)
        if self.snap is not None and (canvas.size.width < self._graph_dims[0] or canvas.size.height != self._graph_dims[1]):
            return  # layout has not caught up with the new content yet; on_resize will retry
        self._want_scroll = False
        # The container learns its new virtual size one refresh after the child resizes.
        self.call_after_refresh(self._scroll_now)

    def _scroll_now(self) -> None:
        rect = self._rects.get(self.selected or "")
        if rect is None:
            return
        self.query_one("#graph", ScrollableContainer).scroll_to_region(
            Region(rect.x, rect.y, rect.w, rect.h), spacing=Spacing(2, 4, 2, 4), animate=False)

    def _refresh_inspector(self) -> None:
        pane = self.query_one("#inspector")
        body = self.query_one("#inspector-body", Static)
        width = max(20, pane.size.width - 4) if pane.size.width else 44
        self._set("inspector", body, R.inspector_text(self.snap, self.selected, width))
        if self.snap and self.selected:
            ids = [s["id"] for s in self.snap["steps"]]
            n = ids.index(self.selected) + 1 if self.selected in ids else 0
            pane.border_title = f"┤ Inspector · step {n}/{len(ids)} ├"
        else:
            pane.border_title = "┤ Inspector ├"

    def _refresh_side(self) -> None:
        self._refresh_inspector()
        events_pane = self.query_one("#events")
        ewidth = max(20, events_pane.size.width - 4) if events_pane.size.width else 48
        self._set("events", self.query_one("#events-body", Static), R.events_text(self.snap, ewidth))
        live = self.snap is not None and self.snap["run"]["status"] == "running"
        events_pane.border_title = "┤ Events ● streaming ├" if live else "┤ Events ├"
        ctx = self.query_one("#context", Static)
        cwidth = max(20, ctx.size.width - 4) if ctx.size.width else 44
        self._set("context", ctx, R.context_text(self.snap, cwidth))
        timeline = self.query_one("#timeline", Static)
        if self.snap is None:
            self._set("timeline", timeline, Text("waiting for a run…", Style(color=R.MUTED)))
            timeline.styles.height = 3
            timeline.border_title = "┤ Timeline ├"
            return
        lanes = len(self.snap["layouts"]["timeline"]["lanes"])
        timeline.styles.height = lanes + 3  # rows + axis + border
        twidth = max(30, timeline.size.width - 4) if timeline.size.width else 120
        self._set("timeline", timeline, R.render_timeline(self.snap, twidth, self.selected))
        timeline.border_title = f"┤ {R.timeline_title(self.snap)} ├"

    def _sync_tabs(self) -> None:
        for tab in self.query(TabLabel):
            tab.set_class(tab.view == self.view, "active")

    # ------------------------------------------------------------------ actions
    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if isinstance(self.screen, RunPicker) and action != "quit":
            return False  # the picker owns the keyboard while it is open
        return True

    def action_view(self, name: str) -> None:
        if name not in VIEWS:
            return
        changed = name != self.view
        self.view = name
        self._sync_tabs()
        self._render_graph(scroll=True)
        if changed:
            self.query_one("#graph", ScrollableContainer).scroll_home(animate=False)

    def action_select(self, delta: int) -> None:
        if self.snap is None or not self.snap["steps"]:
            return
        ids = [s["id"] for s in self.snap["steps"]]
        cur = ids.index(self.selected) if self.selected in ids else (len(ids) - 1 if delta > 0 else len(ids))
        self.select_step(ids[max(0, min(len(ids) - 1, cur + delta))])

    def select_step(self, step_id: str) -> None:
        """Manual selection: turns follow off and refreshes the highlight and inspector."""
        self.follow = False
        self.selected = step_id
        self._render_graph(scroll=True)
        self._refresh_inspector()
        self._refresh_timeline_only()

    def _refresh_timeline_only(self) -> None:
        if self.snap is None:
            return
        timeline = self.query_one("#timeline", Static)
        twidth = max(30, timeline.size.width - 4) if timeline.size.width else 120
        self._set("timeline", timeline, R.render_timeline(self.snap, twidth, self.selected))

    def click_graph(self, x: int, y: int) -> None:
        sid = R.step_at(self._rects, x, y)
        if sid:
            self.select_step(sid)

    def action_toggle_follow(self) -> None:
        self.follow = not self.follow
        if self.follow:
            self._rebuild(force_scroll=True)
        else:
            self._render_graph()
        self.notify("follow on" if self.follow else "follow off", timeout=1.5)

    def action_pick_run(self) -> None:
        runs = tail.list_runs()
        if not runs:
            self.notify("no runs recorded yet", severity="warning")
            return
        self.push_screen(RunPicker(runs, self._current_path()), self._picked)

    def _picked(self, path: Path | None) -> None:
        if path is None:
            return
        self.follow_latest = False
        if path != self._current_path():
            self._open(path)
        self.notify(f"watching {path.stem}", timeout=2)

    def action_latest_run(self) -> None:
        self.follow_latest = True
        self._known = self._scan_files()
        path = tail.resolve(None)
        if path is not None and path != self._current_path():
            self._open(path)
        self.notify("following the latest run", timeout=2)


def launch(run: str | None = None, view: str = "swim") -> int:
    """Entry point used by ``agentmon tui``. Returns a process exit code."""
    if run not in (None, "latest") and tail.resolve(run) is None:
        print(f"agentmon: no run matching {run!r} (see `agentmon runs`)", file=sys.stderr)
        return 1
    AgentmonApp(run=run, view=view).run()
    return 0
