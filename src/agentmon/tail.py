"""Follow run files on disk and keep a reduced ``Run`` up to date."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .events import parse_line
from .store import Run


class RunWatcher:
    """Incrementally applies new lines from one run file. Call ``poll()`` periodically."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.run = Run(self.path.stem)
        self._offset = 0
        self._partial = b""
        self._inode: int | None = None
        self._mtime: int | None = None
        self.skipped = 0

    def poll(self) -> bool:
        """Read any new complete lines. Returns True if the run changed."""
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return False
        rewritten = st.st_size == self._offset and self._mtime is not None and st.st_mtime_ns != self._mtime
        if self._inode is not None and (st.st_ino != self._inode or st.st_size < self._offset or rewritten):
            self.__init__(self.path)  # file replaced, truncated or rewritten in place: start over
        self._inode = st.st_ino
        self._mtime = st.st_mtime_ns
        if st.st_size == self._offset:
            return False
        with open(self.path, "rb") as fh:
            fh.seek(self._offset)
            chunk = fh.read()
        self._offset += len(chunk)
        data = self._partial + chunk
        lines = data.split(b"\n")
        self._partial = lines.pop()  # last piece has no newline yet
        changed = False
        for raw in lines:
            ev = parse_line(raw.decode("utf-8", errors="replace"))
            if ev is None:
                if raw.strip():
                    self.skipped += 1
                continue
            if self.run.event_count == 0:
                self.run.id = ev["run"]
            self.run.apply(ev)
            changed = True
        return changed


@dataclass
class RunInfo:
    id: str
    path: Path
    title: str | None
    mtime: float
    size: int


def _peek_title(path: Path) -> tuple[str | None, str | None]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for _ in range(20):
                line = fh.readline()
                if not line:
                    break
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("type") == "run_start":
                    return ev.get("run"), ev.get("title")
                if ev.get("run"):
                    return ev.get("run"), None
    except OSError:
        pass
    return None, None


def list_runs(directory: Path | None = None) -> list[RunInfo]:
    d = directory or paths.runs_dir()
    if not d.is_dir():
        return []
    out = []
    for p in d.glob("*.jsonl"):
        st = p.stat()
        run_id, title = _peek_title(p)
        out.append(RunInfo(run_id or p.stem, p, title, st.st_mtime, st.st_size))
    return sorted(out, key=lambda r: r.mtime, reverse=True)


def resolve(run: str | None) -> Path | None:
    """Map a run id, file path, or 'latest'/None to a run file."""
    if run and run not in ("latest",):
        candidate = Path(run).expanduser()
        if candidate.suffix == ".jsonl" and candidate.exists():
            return candidate
        by_id = paths.run_file(run)
        if by_id.exists():
            return by_id
        for info in list_runs():
            if info.id == run or info.id.startswith(run):
                return info.path
        return None
    runs = list_runs()
    return runs[0].path if runs else None
