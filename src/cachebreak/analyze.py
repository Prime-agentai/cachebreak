"""Find prompt-cache breaks in a session, name their cause, and price them.

A *break* is a request that re-wrote prefix tokens the previous request had
already cached. Its cost is the difference between writing those tokens again
and reading them, at the request's model's list price (see ``pricing``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .pricing import PriceBook
from .transcript import JSON, Event, Request, Session, Usage

TTL = {"5m": timedelta(minutes=5), "1h": timedelta(hours=1)}

# Causes, in the order we attribute a break to them.
COMPACTION = "compaction"
MODEL = "model-switch"
SHAPE = "system-or-tools-changed"
PARAMS = "params-changed"
HISTORY = "history-rewritten"
TTL_EXPIRED = "ttl-expired"
VERSION = "cli-version-changed"
UNEXPLAINED = "unexplained"
CAUSES = (COMPACTION, MODEL, SHAPE, PARAMS, HISTORY, TTL_EXPIRED, VERSION, UNEXPLAINED)

CAUSE_HELP = {
    COMPACTION: "context was compacted; the summary starts a new prefix (by design)",
    MODEL: "the cache is per model; switching re-writes everything",
    SHAPE: "the system prompt or tool definitions changed before the first breakpoint",
    PARAMS: "a cache-relevant request setting changed (effort, thinking, tool_choice...)",
    HISTORY: "an earlier message in the conversation was changed or removed",
    TTL_EXPIRED: "idle longer than the cache TTL; the prefix expired",
    VERSION: "Claude Code was upgraded between requests (new prompt/tools)",
    UNEXPLAINED: "no client-side change found; server-side eviction or an unlogged change",
}



@dataclass
class Evidence:
    cause: str
    proven: bool
    detail: str


@dataclass
class Break:
    request: Request
    previous: Request
    lost_tokens: int  # prefix tokens that were re-written instead of read
    kept_tokens: int
    cost_usd: float  # extra cost vs. reading the same tokens from cache
    evidence: list[Evidence]
    nearby: list[str] = field(default_factory=list)

    @property
    def cause(self) -> str:
        return self.evidence[0].cause if self.evidence else UNEXPLAINED

    @property
    def proven(self) -> bool:
        return bool(self.evidence) and self.evidence[0].proven

    @property
    def gap(self) -> timedelta | None:
        if self.request.ts and self.previous.ts:
            return self.request.ts - self.previous.ts
        return None


@dataclass
class Tax:
    """A piece of content and what carrying it in the prompt cost."""

    label: str
    tool: str | None
    tokens: int  # estimated
    carried: int  # later requests that re-read it
    cost_usd: float


@dataclass
class Report:
    session: Session
    requests: int
    totals: Usage
    total_usd: float
    read_usd: float
    breaks: list[Break]
    taxes: list[Tax]
    chains: dict[str, int]
    unpriced: list[str]

    @property
    def hit_rate(self) -> float:
        denom = self.totals.prompt
        return self.totals.read / denom if denom else 0.0

    @property
    def read_share(self) -> float:
        """Share of the session's cost spent re-reading cached context."""
        return self.read_usd / self.total_usd if self.total_usd else 0.0

    @property
    def break_usd(self) -> float:
        return sum(b.cost_usd for b in self.breaks)

    @property
    def evidence_level(self) -> str:
        return "request records" if self.session.has_request_records else "usage only"


def fmt_duration(delta: timedelta) -> str:
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m{secs % 60:02d}s"
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"


def _ttl_of(req: Request) -> timedelta:
    """The TTL that protected ``req``'s prefix: logged cache_control, else its writes."""
    if req.record and req.record.ttls:
        return max(TTL.get(t, TTL["5m"]) for t in req.record.ttls)
    if req.usage.write_1h > 0:
        return TTL["1h"]
    return TTL["5m"]


def _text_of(block: object) -> str:
    if isinstance(block, dict):
        text = block.get("text")
        if isinstance(text, str):
            return text
    return repr(block)


def _first_diff(a: str, b: str) -> str:
    i = 0
    n = min(len(a), len(b))
    while i < n and a[i] == b[i]:
        i += 1
    start = max(0, i - 20)
    old = a[start:i + 40].replace("\n", "\\n")
    new = b[start:i + 40].replace("\n", "\\n")
    return f"at char {i}: {old!r} -> {new!r}"


def diff_shapes(old: JSON, new: JSON) -> list[str]:
    """Human-readable differences between two request shapes (system + tools)."""
    changes: list[str] = []
    old_sys = old.get("system") if isinstance(old.get("system"), list) else []
    new_sys = new.get("system") if isinstance(new.get("system"), list) else []
    assert isinstance(old_sys, list) and isinstance(new_sys, list)
    for i in range(max(len(old_sys), len(new_sys))):
        if i >= len(old_sys):
            changes.append(f"system[{i}] added")
        elif i >= len(new_sys):
            changes.append(f"system[{i}] removed")
        else:
            a, b = _text_of(old_sys[i]), _text_of(new_sys[i])
            if a != b:
                changes.append(f"system[{i}] changed {_first_diff(a, b)}")

    def tools(shape: JSON) -> dict[str, object]:
        out: dict[str, object] = {}
        raw = shape.get("tools")
        for t in raw if isinstance(raw, list) else []:
            if isinstance(t, dict):
                out[str(t.get("name"))] = t
        return out

    old_t, new_t = tools(old), tools(new)
    added = [n for n in new_t if n not in old_t]
    removed = [n for n in old_t if n not in new_t]
    changed = [n for n in new_t if n in old_t and new_t[n] != old_t[n]]
    if added:
        changes.append("tools added: " + ", ".join(added[:8]) + (" ..." if len(added) > 8 else ""))
    if removed:
        changes.append("tools removed: " + ", ".join(removed[:8])
                       + (" ..." if len(removed) > 8 else ""))
    if changed:
        changes.append("tool definitions changed: " + ", ".join(changed[:8]))
    if not added and not removed and not changed and list(old_t) != list(new_t):
        changes.append("tool order changed")
    for key in sorted(set(old) | set(new)):
        if key not in ("system", "tools") and old.get(key) != new.get(key):
            changes.append(f"{key} changed")
    return changes


def _short(value: object) -> str:
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


def _history_diff(session: Session, prev: Request, cur: Request) -> str | None:
    a = prev.record.messages if prev.record else None
    b = cur.record.messages if cur.record else None
    if a is None or b is None:
        return None
    for i, (ha, hb) in enumerate(zip(a, b)):
        if ha != hb:
            role = session.blobs.get(hb, {}).get("role")
            return f"message #{i}" + (f" ({role})" if isinstance(role, str) else "") + \
                f" differs from the previous request ({len(a)} -> {len(b)} messages)"
    if len(b) < len(a):
        return f"history shortened from {len(a)} to {len(b)} messages"
    return None


def _explain(session: Session, prev: Request, cur: Request,
             between: list[Event]) -> list[Evidence]:
    ev: list[Evidence] = []
    compactions = [e for e in between if e.kind == "compaction"]
    if compactions:
        ev.append(Evidence(COMPACTION, True, compactions[0].detail))
    if prev.model and cur.model and prev.model != cur.model:
        ev.append(Evidence(MODEL, True, f"{prev.model} -> {cur.model}"))
    pr, cr = prev.record, cur.record
    if pr and cr and pr.shape_hash and cr.shape_hash and pr.shape_hash != cr.shape_hash:
        old, new = session.shapes.get(pr.shape_hash), session.shapes.get(cr.shape_hash)
        if old is not None and new is not None:
            changes = diff_shapes(old, new)
            if changes:
                ev.append(Evidence(SHAPE, True, "; ".join(changes[:3])))
        else:
            ev.append(Evidence(SHAPE, True,
                               f"shape {pr.shape_hash[:8]} -> {cr.shape_hash[:8]}"))
    if pr and cr and pr.params is not None and cr.params is not None:
        changed = [k for k in sorted(set(pr.params) | set(cr.params))
                   if pr.params.get(k) != cr.params.get(k)]
        if changed:
            k = changed[0]
            ev.append(Evidence(PARAMS, True, f"{k}: {_short(pr.params.get(k))} -> "
                                             f"{_short(cr.params.get(k))}"
                               + (f" (+{len(changed) - 1} more)" if len(changed) > 1 else "")))
    hist = _history_diff(session, prev, cur)
    if hist:
        ev.append(Evidence(HISTORY, True, hist))
    if prev.ts and cur.ts:
        gap, ttl = cur.ts - prev.ts, _ttl_of(prev)
        if gap > ttl:
            ev.append(Evidence(TTL_EXPIRED, False,
                               f"idle {fmt_duration(gap)} > {fmt_duration(ttl)} TTL"))
    if prev.version and cur.version and prev.version != cur.version:
        ev.append(Evidence(VERSION, False, f"{prev.version} -> {cur.version}"))
    if not ev:
        ev.append(Evidence(UNEXPLAINED, False,
                           "same model, shape and history" if (pr and cr)
                           else "no request records in this transcript to compare"))
    order = {c: i for i, c in enumerate(CAUSES)}
    ev.sort(key=lambda e: order[e.cause])
    return ev


def analyze(session: Session, *, min_tokens: int = 1024, min_ratio: float = 0.10,
            top: int = 10, prices: PriceBook | None = None) -> Report:
    book = prices or PriceBook()
    totals = Usage()
    total_usd = read_usd = 0.0
    chains: dict[str, list[Request]] = {}
    for req in session.requests:
        chains.setdefault(req.chain, []).append(req)
        u = req.usage
        rates = book.rates(req.model, u.prompt)
        total_usd += rates.cost(u)
        read_usd += u.read * rates.read / 1_000_000
        totals = Usage(totals.input + u.input, totals.write_5m + u.write_5m,
                       totals.write_1h + u.write_1h, totals.read + u.read,
                       totals.output + u.output)

    breaks: list[Break] = []
    taxes: list[Tax] = []
    for chain, reqs in chains.items():
        events = [e for e in session.events if e.chain == chain]
        for prev, cur in zip(reqs, reqs[1:]):
            prior = prev.usage.prompt
            lost = max(0, min(cur.usage.write, prior - cur.usage.read))
            if lost < min_tokens or lost < min_ratio * prior:
                continue
            between = [e for e in events if prev.index < e.index < cur.index]
            rates = book.rates(cur.model, cur.usage.prompt)
            extra = (rates.write_rate(cur.usage) - rates.read) * lost / 1_000_000
            nearby = sorted({e.detail for e in between if e.kind == "attachment"})
            breaks.append(Break(cur, prev, lost, min(cur.usage.read, prior), extra,
                                _explain(session, prev, cur, between), nearby))
        taxes.extend(_taxes(session, chain, reqs, book))
    breaks.sort(key=lambda b: b.request.index)
    taxes.sort(key=lambda t: t.cost_usd, reverse=True)
    return Report(session, len(session.requests), totals, total_usd, read_usd, breaks,
                  taxes[:top], {c: len(r) for c, r in chains.items()}, sorted(book.unpriced))


def _taxes(session: Session, chain: str, reqs: list[Request], book: PriceBook) -> list[Tax]:
    """Estimate each new piece of content's size and what re-reading it cost.

    Between two requests the prompt grows by the previous answer plus whatever
    was appended (tool results, prompts, reminders). We split that growth over
    the appended items by character count, then charge each item one cache
    write plus a cache read for every later request in the chain that carried
    it, stopping at a compaction or a context reset.
    """
    items = [i for i in session.items if i.chain == chain]
    resets = {e.index for e in session.events if e.chain == chain and e.kind == "compaction"}
    out: list[Tax] = []
    for n in range(1, len(reqs)):
        prev, cur = reqs[n - 1], reqs[n]
        growth = cur.usage.prompt - prev.usage.prompt - prev.usage.output
        new = [i for i in items if prev.index < i.index < cur.index]
        chars = sum(i.chars for i in new)
        if growth <= 0 or chars <= 0:
            continue
        carried = 0
        read_cost = 0.0  # $ per token, summed over the later requests that carried it
        for later_prev, later in zip(reqs[n:], reqs[n + 1:]):
            if any(later_prev.index < r < later.index for r in resets):
                break
            if later.usage.prompt < cur.usage.prompt * 0.5:
                break
            carried += 1
            read_cost += book.rates(later.model, later.usage.prompt).read / 1_000_000
        write = book.rates(cur.model, cur.usage.prompt).write_rate(cur.usage) / 1_000_000
        for item in new:
            tokens = round(growth * item.chars / chars)
            if tokens <= 0:
                continue
            out.append(Tax(item.label, item.tool, tokens, carried,
                           tokens * (write + read_cost)))
    return out


def since_filter(sessions: list[Session], since: datetime | None) -> list[Session]:
    if since is None:
        return sessions
    return [s for s in sessions if s.last_ts and s.last_ts >= since]
