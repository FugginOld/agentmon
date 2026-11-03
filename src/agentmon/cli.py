"""agentmon command line.

    agentmon web            open the web dashboard (needs agentmon[web])
    agentmon tui            open the terminal UI   (needs agentmon[tui])
    agentmon install-hooks  stream Claude Code sessions into agentmon
    agentmon emit ...       append events from any agent / shell script
    agentmon demo           replay a sample multi-agent run
    agentmon runs           list recorded runs
"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__, fmt, paths

WEB_HINT = "The web UI isn't installed. Install it with:  pipx install 'agentmon[web]'  (or pip install 'agentmon[web]')"
TUI_HINT = "The terminal UI isn't installed. Install it with:  pipx install 'agentmon[tui]'  (or pip install 'agentmon[tui]')"


def _parse_value(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return text


def cmd_web(args) -> int:
    try:
        from .web.server import serve
    except ImportError as exc:
        if exc.name and exc.name.split(".")[0] in ("fastapi", "uvicorn", "starlette"):
            print(WEB_HINT, file=sys.stderr)
            return 2
        raise
    return serve(run=args.run, host=args.host, port=args.port, open_browser=args.open)


def cmd_tui(args) -> int:
    try:
        from .tui.app import launch
    except ImportError as exc:
        if exc.name and exc.name.split(".")[0] in ("textual", "rich"):
            print(TUI_HINT, file=sys.stderr)
            return 2
        raise
    return launch(run=args.run, view=args.view)


def cmd_hook(args) -> int:
    from .hooks.claude_code import handle_stdin

    return handle_stdin()


def cmd_install_hooks(args) -> int:
    from .hooks import claude_code

    path, settings = claude_code.install(scope=args.scope, dry_run=args.dry_run)
    if args.dry_run:
        print(json.dumps({"hooks": settings.get("hooks", {})}, indent=2))
        print(f"\n(dry run) would update {path}")
    else:
        print(f"Installed agentmon hooks for {len(claude_code.HOOK_EVENTS)} Claude Code events in {path}")
        print("New Claude Code sessions will appear in `agentmon web` / `agentmon tui`.")
    return 0


def cmd_uninstall_hooks(args) -> int:
    from .hooks import claude_code

    path, removed = claude_code.uninstall(scope=args.scope, dry_run=args.dry_run)
    print(f"Removed {removed} agentmon hook entr{'y' if removed == 1 else 'ies'} from {path}")
    return 0


def cmd_emit(args) -> int:
    from . import events, sink

    if args.type == "-":
        bad = 0
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
                if args.run and "run" not in ev:
                    ev["run"] = args.run
                ev.setdefault("v", events.SCHEMA_VERSION)
                ev.setdefault("ts", events.now_iso())
                sink.append(events.validate(ev))
            except (ValueError, events.EventError) as exc:
                bad += 1
                print(f"skipped: {exc}", file=sys.stderr)
        return 1 if bad else 0
    if not args.run:
        print("--run is required", file=sys.stderr)
        return 2
    fields = json.loads(args.json) if args.json else {}
    for item in args.set or []:
        key, _, value = item.partition("=")
        fields[key] = _parse_value(value)
    try:
        path = sink.append(events.make(args.type, args.run, **fields))
    except events.EventError as exc:
        print(f"invalid event: {exc}", file=sys.stderr)
        return 2
    if args.verbose:
        print(path)
    return 0


def cmd_demo(args) -> int:
    from . import demo

    try:
        demo.run(run_id=args.run, speed=args.speed, instant=args.instant, until=args.until)
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


def cmd_runs(args) -> int:
    from .tail import list_runs

    runs = list_runs()
    if not runs:
        print(f"No runs yet in {paths.runs_dir()}")
        return 0
    for r in runs[: args.limit]:
        print(f"{fmt.hms(r.mtime)}  {r.id[:36]:<36}  {fmt.truncate(r.title or '', 40)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agentmon", description="Live workflow monitor for AI agents.")
    p.add_argument("--version", action="version", version=f"agentmon {__version__}")
    sub = p.add_subparsers(dest="command", metavar="command")

    w = sub.add_parser("web", help="open the web dashboard")
    w.add_argument("--run", help="run id or file (default: newest)")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8765)
    w.add_argument("--open", action="store_true", help="open a browser tab")
    w.set_defaults(func=cmd_web)

    t = sub.add_parser("tui", help="open the terminal UI")
    t.add_argument("--run", help="run id or file (default: newest)")
    t.add_argument("--view", choices=["swim", "dag", "seq"], default="swim")
    t.set_defaults(func=cmd_tui)

    h = sub.add_parser("hook", help="Claude Code hook entry point (reads the payload on stdin)")
    h.set_defaults(func=cmd_hook)

    for name, func, helptext in (("install-hooks", cmd_install_hooks, "register agentmon hooks with Claude Code"),
                                 ("uninstall-hooks", cmd_uninstall_hooks, "remove agentmon hooks from Claude Code")):
        ih = sub.add_parser(name, help=helptext)
        ih.add_argument("--scope", choices=["user", "project", "local"], default="user",
                        help="user: ~/.claude/settings.json · project: .claude/settings.json · local: .claude/settings.local.json")
        ih.add_argument("--dry-run", action="store_true")
        ih.set_defaults(func=func)

    e = sub.add_parser("emit", help="append an event (or '-' to read JSONL events from stdin)")
    e.add_argument("type", help="event type, or '-' for JSONL on stdin")
    e.add_argument("--run", help="run id")
    e.add_argument("--set", action="append", metavar="KEY=VALUE", help="field (value parsed as JSON when possible)")
    e.add_argument("--json", help="fields as a JSON object")
    e.add_argument("-v", "--verbose", action="store_true")
    e.set_defaults(func=cmd_emit)

    d = sub.add_parser("demo", help="replay a sample multi-agent run")
    d.add_argument("--run", default="demo")
    d.add_argument("--speed", type=float, default=1.0)
    d.add_argument("--instant", action="store_true", help="write everything at once")
    d.add_argument("--until", type=float, help="stop the script at this many seconds")
    d.set_defaults(func=cmd_demo)

    r = sub.add_parser("runs", help="list recorded runs")
    r.add_argument("--limit", type=int, default=20)
    r.set_defaults(func=cmd_runs)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return int(args.func(args) or 0)
