"""Render reports as terminal text or JSON."""

from __future__ import annotations

import os
import sys
from collections import defaultdict
from typing import Any

from .analyze import CAUSE_HELP, CAUSES, Break, Report, fmt_duration
from .pricing import PRICES_AS_OF


class Style:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def red(self, text: str) -> str:
        return self._wrap("31", text)

    def green(self, text: str) -> str:
        return self._wrap("32", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)


def style_for(stream: Any, force_off: bool = False) -> Style:
    on = (not force_off and not os.environ.get("NO_COLOR")
          and hasattr(stream, "isatty") and stream.isatty())
    return Style(on)


def num(n: float) -> str:
    n = float(n)
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if abs(n) >= 10_000:
        return f"{n / 1000:.0f}k"
    if abs(n) >= 1000:
        return f"{n / 1000:.1f}k"
    return f"{n:.0f}"


def money(x: float) -> str:
    if abs(x) >= 100:
        return f"${x:,.0f}"
    if abs(x) >= 1:
        return f"${x:.2f}"
    if abs(x) >= 0.01:
        return f"${x:.3f}"
    return f"${x:.4f}"


def pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _when(r: Report) -> str:
    ts = r.session.last_ts
    return ts.astimezone().strftime("%Y-%m-%d %H:%M") if ts else "?"


def _project(r: Report) -> str:
    return r.session.cwd or r.session.project


def _break_line(b: Break, st: Style) -> str:
    ts = b.request.ts.astimezone().strftime("%H:%M:%S") if b.request.ts else "??:??:??"
    tag = st.green("proven") if b.proven else st.yellow("inferred")
    head = (f"{ts}  {st.bold(b.cause):<24} re-wrote {num(b.lost_tokens)} tokens "
            f"(kept {num(b.kept_tokens)})  +{money(b.cost_usd)}  [{tag}]")
    lines = [head]
    for ev in b.evidence:
        lines.append(st.dim(f"          {ev.cause}: {ev.detail}"))
    if b.nearby:
        lines.append(st.dim("          nearby: " + ", ".join(b.nearby)))
    return "\n".join(lines)


def render_show(r: Report, st: Style) -> str:
    s = r.session
    t = r.totals
    out = [st.bold(f"cachebreak  {s.session_id}"),
           f"  {_project(r)}  ·  {_when(r)}  ·  evidence: {r.evidence_level}"]
    chains = ", ".join(f"{c} {n}" for c, n in sorted(r.chains.items()))
    out.append(f"  {r.requests} requests ({chains})" + (f"  ·  {s.bad_lines} malformed lines"
                                                         if s.bad_lines else ""))
    out.append("")
    out.append(st.bold("Where the prompt tokens went"))
    out.append(f"  cache read   {num(t.read):>8}   hit rate {pct(r.hit_rate)}")
    out.append(f"  cache write  {num(t.write):>8}   (1h {num(t.write_1h)}, 5m {num(t.write_5m)})")
    out.append(f"  uncached     {num(t.input):>8}   output {num(t.output)}")
    out.append(f"  API-equivalent cost {money(r.total_usd)}; re-reading cached context is "
               f"{pct(r.read_share)} of it.")
    if s.cost_usd is not None:
        out.append(st.dim(f"  (Claude Code's own running total: {money(s.cost_usd)})"))
    out.append("")
    if r.breaks:
        share = r.break_usd / r.total_usd if r.total_usd else 0.0
        out.append(st.bold(f"Cache breaks: {len(r.breaks)}") +
                   f"  (extra cost {money(r.break_usd)}, {pct(share)} of the session)")
        out.extend(_break_line(b, st) for b in r.breaks)
    else:
        out.append(st.green("No cache breaks.") + " Every request re-used the previous prefix.")
    shown = [x for x in r.taxes if x.cost_usd >= 0.005 * r.total_usd]
    if shown:
        out.append("")
        out.append(st.bold("Most expensive context (estimated)"))
        for tax in shown:
            out.append(f"  {money(tax.cost_usd):>8}  {num(tax.tokens):>6} tok × "
                       f"{tax.carried:>3} later reads  {tax.label}")
    out.append("")
    out.append(st.dim(_price_note(r.unpriced)))
    return "\n".join(out)


def _price_note(unpriced: list[str]) -> str:
    note = f"Dollars are API list prices as of {PRICES_AS_OF} (a proxy for usage-limit drain)."
    if unpriced:
        note += f" No price row for {', '.join(unpriced)}: priced at $3/MTok input."
    return note


def render_list(reports: list[Report], st: Style) -> str:
    if not reports:
        return "no sessions found"
    out = [st.bold(f"{'session':<10} {'last activity':<17} {'reqs':>5} {'hit':>5} "
                   f"{'breaks':>6} {'break $':>9} {'total $':>9}  project")]
    for r in reports:
        brk = str(len(r.breaks))
        out.append(f"{r.session.session_id[:8]:<10} {_when(r):<17} {r.requests:>5} "
                   f"{pct(r.hit_rate):>5} {(st.red(f'{brk:>6}') if r.breaks else f'{brk:>6}')} "
                   f"{money(r.break_usd):>9} {money(r.total_usd):>9}  {_project(r)}")
    return "\n".join(out)


def render_summary(reports: list[Report], st: Style) -> str:
    total = sum(r.total_usd for r in reports)
    by_cause: dict[str, list[Break]] = defaultdict(list)
    for r in reports:
        for b in r.breaks:
            by_cause[b.cause].append(b)
    lost = sum(r.break_usd for r in reports)
    out = [st.bold(f"cachebreak summary: {len(reports)} sessions, "
                   f"{sum(r.requests for r in reports)} requests, {money(total)} API-equivalent"),
           f"  cache breaks cost an extra {money(lost)} "
           f"({pct(lost / total if total else 0)} of all spend)",
           ""]
    if not by_cause:
        out.append(st.green("  no cache breaks found"))
    for cause in CAUSES:
        items = by_cause.get(cause)
        if not items:
            continue
        cost = sum(b.cost_usd for b in items)
        proven = sum(1 for b in items if b.proven)
        out.append(f"  {st.bold(cause):<26} {len(items):>4} breaks  {money(cost):>9}  "
                   f"({proven} proven)")
        out.append(st.dim(f"      {CAUSE_HELP[cause]}"))
    tools: dict[str, float] = defaultdict(float)
    for r in reports:
        for tax in r.taxes:
            tools[tax.tool or "(prompts & reminders)"] += tax.cost_usd
    if tools:
        out.append("")
        out.append(st.bold("  Costliest context by tool (top items per session)"))
        ranked = sorted(tools.items(), key=lambda kv: kv[1], reverse=True)
        for name, cost in [kv for kv in ranked if kv[1] >= 0.0005][:8]:
            out.append(f"    {money(cost):>9}  {name}")
    return "\n".join(out)


def break_json(b: Break) -> dict[str, Any]:
    gap = b.gap
    return {
        "request_id": b.request.request_id,
        "timestamp": b.request.ts.isoformat() if b.request.ts else None,
        "chain": b.request.chain,
        "cause": b.cause,
        "proven": b.proven,
        "lost_tokens": b.lost_tokens,
        "kept_tokens": b.kept_tokens,
        "cost_usd": round(b.cost_usd, 6),
        "gap_seconds": gap.total_seconds() if gap else None,
        "gap": fmt_duration(gap) if gap else None,
        "evidence": [{"cause": e.cause, "proven": e.proven, "detail": e.detail}
                     for e in b.evidence],
        "nearby": b.nearby,
    }


def report_json(r: Report) -> dict[str, Any]:
    t = r.totals
    return {
        "session_id": r.session.session_id,
        "path": str(r.session.path),
        "project": _project(r),
        "first_activity": r.session.first_ts.isoformat() if r.session.first_ts else None,
        "last_activity": r.session.last_ts.isoformat() if r.session.last_ts else None,
        "evidence": r.evidence_level,
        "requests": r.requests,
        "chains": r.chains,
        "tokens": {"input": t.input, "write_5m": t.write_5m, "write_1h": t.write_1h,
                   "read": t.read, "output": t.output},
        "hit_rate": round(r.hit_rate, 4),
        "total_usd": round(r.total_usd, 6),
        "read_share": round(r.read_share, 4),
        "claude_code_cost_usd": r.session.cost_usd,
        "break_usd": round(r.break_usd, 6),
        "unpriced_models": r.unpriced,
        "prices_as_of": PRICES_AS_OF,
        "breaks": [break_json(b) for b in r.breaks],
        "costliest_context": [{"label": x.label, "tool": x.tool, "tokens": x.tokens,
                               "carried": x.carried, "cost_usd": round(x.cost_usd, 6)}
                              for x in r.taxes],
        "malformed_lines": r.session.bad_lines,
    }


def out_stream() -> Any:
    return sys.stdout
