"""Load a Claude Code session transcript (JSONL) into typed records.

The loader is deliberately forgiving: transcripts are append-only logs written
by a process that can be killed mid-line, and their schema grows with every
Claude Code release. Unknown entry types are ignored, malformed lines are
counted, and every optional field degrades to ``None`` instead of raising.

Two tiers of evidence come out of a transcript:

* **usage** (every version): one ``assistant`` entry per content block, each
  repeating the API ``usage`` of its request. We de-duplicate by request id.
* **request records** (Claude Code 2.1.3xx and later): ``api-request`` entries
  carry the request shape hash, the kept-prefix length, the hashes of the new
  messages and the cache_control TTLs; ``api-request-shape`` carries the system
  prompt and tool definitions behind each shape hash. With these, a cache break
  can be *proven* by diffing two requests instead of inferred from token counts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

JSON = dict[str, Any]

# Request params that do not affect the prompt cache (or are reported separately).
NON_CACHE_PARAMS = frozenset({"model", "max_tokens", "metadata", "stream", "temperature",
                              "top_p", "top_k", "stop_sequences"})

COMPACT_SUBTYPES = frozenset({"compact_boundary", "microcompact_boundary"})


def parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


@dataclass(frozen=True)
class Usage:
    input: int = 0
    write_5m: int = 0
    write_1h: int = 0
    read: int = 0
    output: int = 0

    @property
    def write(self) -> int:
        return self.write_5m + self.write_1h

    @property
    def prompt(self) -> int:
        """Total prompt tokens of the request: uncached + written + read."""
        return self.input + self.write + self.read

    @classmethod
    def from_api(cls, raw: object) -> Usage:
        if not isinstance(raw, dict):
            return cls()
        total_write = _int(raw.get("cache_creation_input_tokens"))
        split = raw.get("cache_creation")
        if isinstance(split, dict) and (
            "ephemeral_1h_input_tokens" in split or "ephemeral_5m_input_tokens" in split
        ):
            w1h = _int(split.get("ephemeral_1h_input_tokens"))
            w5m = _int(split.get("ephemeral_5m_input_tokens"))
            # Trust the total if the split is incomplete.
            if w1h + w5m != total_write:
                w5m = max(0, total_write - w1h)
        else:
            w1h, w5m = 0, total_write
        return cls(
            input=_int(raw.get("input_tokens")),
            write_5m=w5m,
            write_1h=w1h,
            read=_int(raw.get("cache_read_input_tokens")),
            output=_int(raw.get("output_tokens")),
        )


@dataclass
class RequestRecord:
    """An ``api-request`` entry: what Claude Code actually sent."""

    id: str
    ts: datetime | None
    model: str | None
    shape_hash: str | None
    keep: int | None
    tail: list[str]
    ttls: list[str]
    query_source: str | None
    params: dict[str, object] | None = None  # cache-relevant request params
    messages: list[str] | None = None  # reconstructed full message-hash list


@dataclass
class Request:
    """One model request, as seen through its usage receipt."""

    index: int  # order of appearance in the file
    request_id: str
    ts: datetime | None
    model: str | None
    usage: Usage
    chain: str  # "main" or "agent:<id>"
    version: str | None
    entrypoint: str | None
    record: RequestRecord | None = None


@dataclass
class Event:
    """Something between requests that can explain a break."""

    index: int
    ts: datetime | None
    chain: str
    kind: str  # "compaction" | "attachment" | "api_error"
    detail: str


@dataclass
class ToolCall:
    id: str
    name: str
    summary: str


@dataclass
class ContentItem:
    """New prompt content added between two requests (a tool result, a prompt...)."""

    index: int
    chain: str
    label: str
    chars: int
    tool: str | None = None


@dataclass
class Session:
    path: Path
    session_id: str
    project: str
    requests: list[Request] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    items: list[ContentItem] = field(default_factory=list)
    shapes: dict[str, JSON] = field(default_factory=dict)
    blobs: dict[str, JSON] = field(default_factory=dict)
    tool_calls: dict[str, ToolCall] = field(default_factory=dict)
    cost_usd: float | None = None
    bad_lines: int = 0
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    cwd: str | None = None

    @property
    def has_request_records(self) -> bool:
        return any(r.record is not None for r in self.requests)


def _chain_of(entry: JSON) -> str:
    if entry.get("isSidechain"):
        agent = entry.get("agentId")
        return f"agent:{agent}" if isinstance(agent, str) and agent else "agent"
    return "main"


def summarize_tool_input(name: str, raw: object) -> str:
    """A short, human label for a tool call: the file, command or URL it touched."""
    if not isinstance(raw, dict):
        return name
    for key in ("file_path", "notebook_path", "path", "url", "pattern", "query", "command",
                "description", "prompt"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            text = " ".join(value.split())
            if len(text) > 70:
                text = text[:67] + "..."
            return f"{name} {text}"
    return name


def _content_chars(content: object) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "image":
                    total += 6000  # images are billed by pixels; ~1.5k tokens is typical
                    continue
                for key in ("text", "content", "input"):
                    val = block.get(key)
                    if isinstance(val, str):
                        total += len(val)
                    elif val is not None:
                        total += _content_chars(val) if isinstance(val, list) else len(
                            json.dumps(val))
            elif isinstance(block, str):
                total += len(block)
        return total
    if content is None:
        return 0
    return len(json.dumps(content))


def _record(entry: JSON) -> RequestRecord:
    params = entry.get("params") if isinstance(entry.get("params"), dict) else {}
    assert isinstance(params, dict)
    ttls: list[str] = []
    for cc in entry.get("cacheControl") or []:
        if isinstance(cc, dict) and isinstance(cc.get("value"), dict):
            ttls.append(str(cc["value"].get("ttl") or "5m"))
    tail = [h for h in entry.get("tail") or [] if isinstance(h, str)]
    keep = entry.get("keep")
    return RequestRecord(
        id=str(entry.get("id")),
        ts=parse_ts(entry.get("timestamp")),
        model=params.get("model") if isinstance(params.get("model"), str) else None,
        shape_hash=entry.get("shapeHash") if isinstance(entry.get("shapeHash"), str) else None,
        keep=keep if isinstance(keep, int) else None,
        tail=tail,
        ttls=ttls,
        query_source=entry.get("querySource") if isinstance(entry.get("querySource"), str)
        else None,
        params={k: v for k, v in params.items() if k not in NON_CACHE_PARAMS} or None
        if params else None,
    )


def load_session(path: Path) -> Session:
    """Parse one transcript file (plus its ``subagents/`` files, if present)."""
    session = Session(path=path, session_id=path.stem, project=path.parent.name)
    files = [path]
    sub_dir = path.with_suffix("") / "subagents"
    if sub_dir.is_dir():
        files.extend(sorted(sub_dir.glob("*.jsonl")))

    index = 0
    records: dict[str, RequestRecord] = {}
    by_request: dict[str, Request] = {}
    pending_ref: dict[str, str] = {}
    prev_messages: dict[str, list[str]] = {}
    id_seen = False

    for file in files:
        sub_chain = None if file == path else f"agent:{file.stem.removeprefix('agent-')}"
        try:
            handle = file.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    session.bad_lines += 1
                    continue
                if not isinstance(entry, dict):
                    session.bad_lines += 1
                    continue
                index += 1
                kind = entry.get("type")
                ts = parse_ts(entry.get("timestamp"))
                if ts is not None:
                    if session.first_ts is None or ts < session.first_ts:
                        session.first_ts = ts
                    if session.last_ts is None or ts > session.last_ts:
                        session.last_ts = ts
                if session.cwd is None and isinstance(entry.get("cwd"), str):
                    session.cwd = entry["cwd"]
                if (file == path and not id_seen and isinstance(entry.get("sessionId"), str)
                        and entry["sessionId"]):
                    session.session_id, id_seen = entry["sessionId"], True
                chain = sub_chain or _chain_of(entry)

                if kind == "api-request":
                    rec = _record(entry)
                    src = f"{chain}|{rec.query_source}"
                    prev = prev_messages.get(src)
                    if rec.keep is not None and (rec.keep == 0 or (prev is not None
                                                                   and rec.keep <= len(prev))):
                        rec.messages = (prev[: rec.keep] if prev else []) + rec.tail
                        prev_messages[src] = rec.messages
                    else:
                        prev_messages.pop(src, None)
                    records[rec.id] = rec
                    req_id = pending_ref.pop(rec.id, None)
                    if req_id and req_id in by_request:
                        by_request[req_id].record = rec
                elif kind == "api-request-shape":
                    h, shape = entry.get("shapeHash"), entry.get("shape")
                    if isinstance(h, str) and isinstance(shape, dict):
                        session.shapes[h] = shape
                elif kind == "api-request-blob":
                    h, msg = entry.get("hash"), entry.get("message")
                    if isinstance(h, str) and isinstance(msg, dict):
                        session.blobs[h] = msg
                elif kind == "assistant":
                    _assistant(session, entry, index, ts, chain, by_request, records,
                               pending_ref)
                elif kind == "user":
                    _user(session, entry, index, chain)
                elif kind == "system":
                    sub = entry.get("subtype")
                    if sub in COMPACT_SUBTYPES:
                        meta = entry.get("compactMetadata") or entry.get("microcompactMetadata")
                        trig = meta.get("trigger") if isinstance(meta, dict) else None
                        session.events.append(Event(index, ts, chain, "compaction",
                                                    f"{sub}" + (f" ({trig})" if trig else "")))
                    elif sub == "api_error":
                        session.events.append(Event(index, ts, chain, "api_error", "API error"))
                elif kind == "attachment":
                    att = entry.get("attachment")
                    if isinstance(att, dict) and isinstance(att.get("type"), str):
                        session.events.append(Event(index, ts, chain, "attachment",
                                                    _attachment_detail(att)))
                elif kind == "cost-state":
                    cost = entry.get("totalCostUSD")
                    if isinstance(cost, (int, float)):
                        session.cost_usd = max(session.cost_usd or 0.0, float(cost))
    session.requests.sort(key=lambda r: r.index)
    return session


def _attachment_detail(att: JSON) -> str:
    kind = str(att["type"])
    added = att.get("addedNames")
    removed = att.get("removedNames")
    parts = [kind]
    if isinstance(added, list) and added:
        parts.append(f"+{len(added)}")
    if isinstance(removed, list) and removed:
        parts.append(f"-{len(removed)}")
    return " ".join(parts)


def _assistant(session: Session, entry: JSON, index: int, ts: datetime | None, chain: str,
               by_request: dict[str, Request], records: dict[str, RequestRecord],
               pending_ref: dict[str, str]) -> None:
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return
    for block in msg.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            tid, name = block.get("id"), block.get("name")
            if isinstance(tid, str) and isinstance(name, str):
                session.tool_calls[tid] = ToolCall(tid, name,
                                                   summarize_tool_input(name, block.get("input")))
    if entry.get("isApiErrorMessage") or "usage" not in msg:
        return
    req_id = entry.get("requestId") or msg.get("id")
    if not isinstance(req_id, str) or req_id in by_request:
        return
    model = msg.get("model")
    if model == "<synthetic>":
        return
    req = Request(
        index=index,
        request_id=req_id,
        ts=ts,
        model=model if isinstance(model, str) else None,
        usage=Usage.from_api(msg.get("usage")),
        chain=chain,
        version=entry.get("version") if isinstance(entry.get("version"), str) else None,
        entrypoint=entry.get("entrypoint") if isinstance(entry.get("entrypoint"), str) else None,
    )
    ref = entry.get("requestRef")
    if isinstance(ref, str):
        if ref in records:
            req.record = records[ref]
        else:
            pending_ref[ref] = req_id
    by_request[req_id] = req
    session.requests.append(req)


def _user(session: Session, entry: JSON, index: int, chain: str) -> None:
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return
    if entry.get("isCompactSummary"):
        session.events.append(Event(index, parse_ts(entry.get("timestamp")), chain,
                                    "compaction", "compact summary"))
    content = msg.get("content")
    if isinstance(content, list):
        other = 0
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tid = block.get("tool_use_id")
                call = session.tool_calls.get(tid) if isinstance(tid, str) else None
                label = call.summary if call else "tool result"
                session.items.append(ContentItem(index, chain, label,
                                                 _content_chars(block.get("content")),
                                                 call.name if call else None))
            else:
                other += _content_chars([block])
        if other:
            session.items.append(ContentItem(index, chain, "user message", other))
    elif isinstance(content, str) and content:
        session.items.append(ContentItem(index, chain, "user message", len(content)))
