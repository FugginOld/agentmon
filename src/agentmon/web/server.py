"""FastAPI server for the agentmon web UI.

The server is a thin read-mostly layer over the same JSONL run files the terminal UI
reads: ``RunWatcher`` reduces them into ``Run`` objects and ``Run.snapshot()`` is sent
to the browser as-is (REST for one-off reads, Server-Sent Events for live updates).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import webbrowser
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import events, paths, sink, tail

STATIC_DIR = Path(__file__).parent / "static"
POLL_INTERVAL = 0.25  # seconds between file polls
RUNNING_REFRESH = 1.0  # resend a snapshot at least this often while a run is running
SWITCH_CHECK = 1.0  # how often "follow latest" looks for a newer run file
HEARTBEAT = 15.0  # SSE comment keep-alive interval
MAX_BODY = 1_000_000  # bytes accepted by POST /api/events
MAX_BATCH = 1000  # events accepted per POST
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


# ---------------------------------------------------------------------- helpers
def _find_run_file(run_id: str) -> Path | None:
    """Look a run up by *id only* (never by filesystem path: this is reachable over HTTP)."""
    candidate = paths.run_file(run_id)
    if candidate.is_file():
        return candidate
    for info in tail.list_runs():
        if info.id == run_id:
            return info.path
    return None


def _snapshot_of(path: Path) -> dict:
    watcher = tail.RunWatcher(path)
    watcher.poll()
    return watcher.run.snapshot()


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


async def stream_snapshots(
    run: str | None,
    is_disconnected: Callable[[], Awaitable[bool]],
    *,
    resolve: Callable[[str | None], Path | None] | None = None,
    poll_interval: float = POLL_INTERVAL,
) -> AsyncIterator[str]:
    """Yield SSE frames for a run until the client disconnects.

    ``run`` is a run id, or ``None``/``"latest"`` to follow the newest run file (switching
    when a newer one appears). ``resolve`` maps ``run`` to a file (defaults to an id lookup).
    """
    follow_latest = run in (None, "latest")
    lookup = resolve or (lambda r: tail.resolve(None) if follow_latest else _find_run_file(r or ""))

    watcher: tail.RunWatcher | None = None
    force = True  # send a snapshot on the first pass and after every switch
    announced_empty = False
    last_sent = 0.0
    last_beat = time.monotonic()
    last_switch_check = 0.0

    while not await is_disconnected():
        now = time.monotonic()
        if watcher is None or (follow_latest and now - last_switch_check >= SWITCH_CHECK):
            last_switch_check = now
            path = lookup(run)
            if path is None:
                if not announced_empty:
                    announced_empty = True
                    yield _sse("empty", {"run": run})
                watcher = None
            elif watcher is None or path != watcher.path:
                watcher = tail.RunWatcher(path)
                force = True
                announced_empty = False
        if watcher is not None:
            changed = watcher.poll()
            running = watcher.run.status == "running"
            if force or changed or (running and now - last_sent >= RUNNING_REFRESH):
                force = False
                last_sent = now
                yield _sse("snapshot", watcher.run.snapshot())
                last_beat = now
        if time.monotonic() - last_beat >= HEARTBEAT:
            last_beat = time.monotonic()
            yield ": keep-alive\n\n"
        await asyncio.sleep(poll_interval)


# ------------------------------------------------------------------------- app
def create_app(run: str | None = None) -> FastAPI:
    """Build the app. ``run`` pins the live stream to one run (id or path); default follows latest."""
    app = FastAPI(title="agentmon", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/config")
    def config() -> dict:
        """Tells the UI whether the server was started pinned to one run."""
        return {"pinned": run is not None}

    @app.get("/api/runs")
    def list_runs() -> list[dict]:
        return [{"id": r.id, "title": r.title, "mtime": r.mtime, "size": r.size} for r in tail.list_runs()]

    @app.get("/api/runs/{run_id}/snapshot")
    def snapshot(run_id: str) -> dict:
        path = _find_run_file(run_id)
        if path is None:
            raise HTTPException(404, f"unknown run: {run_id}")
        return _snapshot_of(path)

    @app.get("/api/runs/{run_id}/events.jsonl")
    def export(run_id: str) -> FileResponse:
        path = _find_run_file(run_id)
        if path is None:
            raise HTTPException(404, f"unknown run: {run_id}")
        return FileResponse(path, media_type="application/x-ndjson", filename=f"{paths.safe_id(run_id)}.jsonl")

    @app.get("/api/stream")
    async def stream(request: Request) -> StreamingResponse:
        # `run` (the query param) is read by hand: it would otherwise shadow create_app's `run`.
        wanted = request.query_params.get("run")
        if wanted is None and run is not None:  # server was started pinned to a run
            pinned = tail.resolve(run)
            gen = stream_snapshots(run, request.is_disconnected, resolve=lambda _r: pinned or tail.resolve(run))
        else:
            gen = stream_snapshots(wanted or "latest", request.is_disconnected)
        return StreamingResponse(
            gen, media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/events")
    async def ingest(request: Request) -> JSONResponse:
        """Generic HTTP ingest: one event object or a list of them."""
        # Requiring a JSON content type forces browsers to preflight cross-site POSTs, which
        # we never approve, so web pages can't write events to a local server.
        if not request.headers.get("content-type", "").lower().startswith("application/json"):
            return JSONResponse({"error": "Content-Type must be application/json"}, status_code=415)
        body = await request.body()
        if len(body) > MAX_BODY:
            return JSONResponse({"error": "body too large"}, status_code=413)
        try:
            payload = json.loads(body)
        except ValueError:
            return JSONResponse({"error": "body is not valid JSON"}, status_code=400)
        items = payload if isinstance(payload, list) else [payload]
        if not items or len(items) > MAX_BATCH:
            return JSONResponse({"error": f"send between 1 and {MAX_BATCH} events"}, status_code=400)
        prepared: list[dict] = []
        for i, item in enumerate(items):
            if isinstance(item, dict):
                item = {**item}
                item.setdefault("v", events.SCHEMA_VERSION)
                item.setdefault("ts", events.now_iso())
            try:
                prepared.append(events.validate(item))
            except events.EventError as exc:
                return JSONResponse({"error": f"event {i}: {exc}"}, status_code=400)
        for item in prepared:  # validate everything first so a bad batch writes nothing
            sink.append(item)
        return JSONResponse({"accepted": len(prepared)})

    return app


def serve(run: str | None = None, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = False) -> int:
    """Run the web UI until interrupted. Returns a process exit code."""
    url = f"http://{'localhost' if host in LOOPBACK else host}:{port}/"
    print(f"agentmon web UI: {url}")
    if host not in LOOPBACK:
        print(f"warning: listening on {host} - anyone who can reach this port can read your runs, "
              "and POST /api/events lets them write events.")
    if open_browser:
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    try:
        uvicorn.run(create_app(run), host=host, port=port, log_level="warning")
    except KeyboardInterrupt:
        pass
    return 0
