"""Command-line interface: ``cachebreak [list|show|summary|check]``."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from . import __version__
from .analyze import CAUSES, COMPACTION, Report, analyze, since_filter
from .pricing import PriceBook
from .discover import projects_root, resolve, session_files
from .report import (money, num, render_list, render_show, render_summary, report_json, break_json,
                     style_for)
from .transcript import load_session


COMMANDS = frozenset({"list", "show", "summary", "check"})


def parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    m = re.fullmatch(r"(\d+)\s*([mhdw])", value.strip())
    if m:
        unit = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}[m.group(2)]
        return datetime.now(timezone.utc) - timedelta(**{unit: int(m.group(1))})
    try:
        ts = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--since wants 30m, 12h, 7d, 2w or an ISO date, got {value!r}") from exc
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", help="projects directory (default: $CLAUDE_CONFIG_DIR/projects "
                                       "or ~/.claude/projects), or one transcript file")
    common.add_argument("--json", action="store_true", help="machine-readable output")
    common.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    common.add_argument("--min-tokens", type=int, default=1024,
                        help="ignore re-writes smaller than this (default 1024)")
    common.add_argument("--prices", metavar="FILE",
                        help="JSON price overrides: {model: {input, write_5m, write_1h, read, "
                             "output}} in $/MTok")

    p = argparse.ArgumentParser(
        prog="cachebreak",
        description="Find every prompt-cache break in your Claude Code sessions, prove what "
                    "caused it, and price it. Reads local transcripts only; no network.",
        parents=[common])
    p.add_argument("--version", action="version", version=f"cachebreak {__version__}")
    sub = p.add_subparsers(dest="cmd")

    ls = sub.add_parser("list", parents=[common], help="recent sessions with their breaks")
    ls.add_argument("--project", help="only projects whose directory name contains this")
    ls.add_argument("--since", help="only sessions active since (30m, 12h, 7d, ISO date)")
    ls.add_argument("-n", "--limit", type=int, default=15)

    sh = sub.add_parser("show", parents=[common], help="full autopsy of one session")
    sh.add_argument("session", nargs="?", default="last",
                    help="session id (prefix), transcript path, or 'last' (default)")
    sh.add_argument("--top", type=int, default=10, help="costliest context items to show")

    sm = sub.add_parser("summary", parents=[common], help="breaks by cause across sessions")
    sm.add_argument("--project")
    sm.add_argument("--since", default="7d", help="window (default 7d; 'all' for everything)")

    ck = sub.add_parser("check", parents=[common],
                        help="exit 1 if sessions have cache breaks (for CI and harness tests)")
    ck.add_argument("sessions", nargs="+", help="session ids, transcript paths, or 'last'")
    ck.add_argument("--allow", default=COMPACTION,
                    help=f"comma-separated causes to tolerate (default {COMPACTION}); "
                         f"one of: {', '.join(CAUSES)}")
    ck.add_argument("--max-usd", type=float, default=0.0,
                    help="tolerate disallowed breaks up to this extra cost in $ (default 0)")
    return p


def _report(path: Path, args: argparse.Namespace, top: int = 10) -> Report:
    return analyze(load_session(path), min_tokens=args.min_tokens, top=top,
                   prices=PriceBook.from_file(args.prices))


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    raw = list(argv if argv is not None else sys.argv[1:])
    # No subcommand means "list", including list-only flags: `cachebreak -n 5 --since 1d`.
    if not COMMANDS & set(raw) and not {"-h", "--help", "--version"} & set(raw):
        raw.insert(0, "list")
    args = parser.parse_args(raw)
    root = projects_root(args.root)
    st = style_for(sys.stdout, args.no_color)
    try:
        if args.cmd == "list":
            return _cmd_list(args, root, st)
        if args.cmd == "show":
            return _cmd_show(args, root, st)
        if args.cmd == "summary":
            return _cmd_summary(args, root, st)
        if args.cmd == "check":
            return _cmd_check(args, root)
    except LookupError as exc:
        print(f"cachebreak: {exc}", file=sys.stderr)
        return 2
    except argparse.ArgumentTypeError as exc:
        print(f"cachebreak: {exc}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0
    except (OSError, ValueError) as exc:
        print(f"cachebreak: {exc}", file=sys.stderr)
        return 2
    parser.print_help()
    return 2


def _cmd_list(args: argparse.Namespace, root: Path, st: object) -> int:
    since = parse_since(args.since)
    reports: list[Report] = []
    for path in session_files(root, args.project):
        r = _report(path, args, top=0)
        if r.requests == 0:
            continue
        if since and not since_filter([r.session], since):
            continue
        reports.append(r)
        if len(reports) >= args.limit:
            break
    if args.json:
        print(json.dumps([report_json(r) for r in reports], indent=2))
    else:
        if not reports:
            print(f"no sessions with model requests under {root}")
            return 0
        print(render_list(reports, st))  # type: ignore[arg-type]
    return 0


def _cmd_show(args: argparse.Namespace, root: Path, st: object) -> int:
    r = _report(resolve(args.session, root), args, top=args.top)
    if args.json:
        print(json.dumps(report_json(r), indent=2))
    else:
        print(render_show(r, st))  # type: ignore[arg-type]
    return 0


def _cmd_summary(args: argparse.Namespace, root: Path, st: object) -> int:
    since = None if args.since == "all" else parse_since(args.since)
    reports = [r for r in (_report(p, args) for p in session_files(root, args.project))
               if r.requests]
    reports = [r for r in reports if since is None or since_filter([r.session], since)]
    if args.json:
        causes: dict[str, dict[str, float]] = {}
        for r in reports:
            for b in r.breaks:
                c = causes.setdefault(b.cause, {"breaks": 0, "proven": 0, "cost_usd": 0.0})
                c["breaks"] += 1
                c["proven"] += int(b.proven)
                c["cost_usd"] = round(c["cost_usd"] + b.cost_usd, 6)
        print(json.dumps({"sessions": len(reports),
                          "requests": sum(r.requests for r in reports),
                          "total_usd": round(sum(r.total_usd for r in reports), 6),
                          "break_usd": round(sum(r.break_usd for r in reports), 6),
                          "causes": causes}, indent=2))
    else:
        print(render_summary(reports, st))  # type: ignore[arg-type]
    return 0


def _cmd_check(args: argparse.Namespace, root: Path) -> int:
    allowed = {c.strip() for c in args.allow.split(",") if c.strip()}
    unknown = allowed - set(CAUSES)
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown cause(s) in --allow: {', '.join(sorted(unknown))}")
    failing = []
    results = []
    for target in args.sessions:
        r = _report(resolve(target, root), args)
        bad = [b for b in r.breaks if b.cause not in allowed]
        cost = sum(b.cost_usd for b in bad)
        ok = cost <= args.max_usd if bad else True
        results.append({"session_id": r.session.session_id, "ok": ok,
                        "breaks": [break_json(b) for b in bad], "cost_usd": round(cost, 6)})
        if not ok:
            failing.append((r, bad, cost))
    if args.json:
        print(json.dumps({"ok": not failing, "sessions": results}, indent=2))
    else:
        for r, bad, cost in failing:
            print(f"FAIL {r.session.session_id}: {len(bad)} cache break(s), "
                  f"extra {money(cost)}")
            for b in bad:
                ev = b.evidence[0]
                print(f"  {b.cause}: re-wrote {num(b.lost_tokens)} tokens ({ev.detail})")
        if not failing:
            print(f"ok: {len(results)} session(s), no disallowed cache breaks")
    return 1 if failing else 0
