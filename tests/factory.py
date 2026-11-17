"""Build synthetic Claude Code transcripts in the 2.1.3xx record format."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

T0 = datetime(2026, 11, 17, 9, 0, 0, tzinfo=timezone.utc)

BASE_SHAPE: dict[str, Any] = {
    "system": [{"type": "text", "text": "You are a coding agent."},
               {"type": "text", "text": "Be careful with files."}],
    "tools": [{"name": "Read", "description": "Read a file", "input_schema": {}},
              {"name": "Bash", "description": "Run a command", "input_schema": {}}],
}


def h(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


class Transcript:
    def __init__(self, session_id: str = "11111111-2222-3333-4444-555555555555",
                 cwd: str = "/work/demo", version: str = "2.1.323") -> None:
        self.session_id = session_id
        self.cwd = cwd
        self.version = version
        self.lines: list[str] = []
        self.t = T0
        self.n = 0
        self.messages: list[str] = []
        self.shapes: set[str] = set()
        self._sent = 0

    # -- low level -------------------------------------------------------------
    def _base(self, **extra: Any) -> dict[str, Any]:
        d: dict[str, Any] = {"timestamp": self.t.isoformat().replace("+00:00", "Z"),
                             "sessionId": self.session_id, "cwd": self.cwd,
                             "version": self.version, "entrypoint": "cli",
                             "isSidechain": False}
        d.update(extra)
        return d

    def raw(self, text: str) -> Transcript:
        self.lines.append(text)
        return self

    def add(self, entry: dict[str, Any]) -> Transcript:
        self.lines.append(json.dumps(entry))
        return self

    def wait(self, **kw: float) -> Transcript:
        self.t += timedelta(**kw)
        return self

    # -- semantic helpers --------------------------------------------------------
    def user(self, text: str, **extra: Any) -> Transcript:
        self.messages.append(h(["user", text, len(self.messages)]))
        return self.add(self._base(type="user", message={"role": "user", "content": text},
                                   **extra))

    def tool_result(self, tool_id: str, content: str, **extra: Any) -> Transcript:
        self.messages.append(h(["tool", tool_id]))
        return self.add(self._base(type="user", message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_id, "content": content}]}, **extra))

    def attachment(self, kind: str, **fields: Any) -> Transcript:
        return self.add(self._base(type="attachment", attachment={"type": kind, **fields}))

    def compact(self) -> Transcript:
        return self.add(self._base(type="system", subtype="compact_boundary",
                                   compactMetadata={"trigger": "auto", "preTokens": 150000}))

    def request(self, *, write: int, read: int, output: int = 100, inp: int = 3,
                model: str = "claude-sonnet-5-5", ttl: str = "1h",
                shape: dict[str, Any] | None = None, keep: int | None = None,
                rewrite_from: int | None = None, records: bool = True,
                tool_use: tuple[str, str, dict[str, Any]] | None = None,
                blocks: int = 2, sidechain: bool = False, agent: str | None = None,
                version: str | None = None) -> Transcript:
        """One request: an api-request record (optional) then its assistant entries."""
        self.n += 1
        ref = f"ref-{self.n}"
        if records:
            shp = shape or BASE_SHAPE
            sh = h(shp)
            if sh not in self.shapes:
                self.shapes.add(sh)
                self.add({"type": "api-request-shape", "sessionId": self.session_id,
                          "shapeHash": sh, "shape": shp,
                          "timestamp": self.t.isoformat()})
            if rewrite_from is not None:
                self.messages[rewrite_from] = h(["rewritten", rewrite_from, self.n])
                for i, m in enumerate(self.messages):
                    self.add({"type": "api-request-blob", "hash": m,
                              "message": {"role": "user" if i % 2 == 0 else "assistant",
                                          "content": []}})
            prev_len = self._sent
            k = keep if keep is not None else (prev_len if rewrite_from is None else 0)
            tail = self.messages[k:]
            self.add({"type": "api-request", "sessionId": self.session_id, "id": ref,
                      "timestamp": self.t.isoformat(), "version": version or self.version,
                      "querySource": "sdk", "shapeHash": sh,
                      "params": {"model": model}, "keep": k, "tail": tail,
                      "cacheControl": [{"path": ["messages", len(self.messages) - 1],
                                        "value": {"type": "ephemeral", "ttl": ttl}}]})
            self._sent = len(self.messages)
        self.t += timedelta(seconds=2)
        usage = {"input_tokens": inp, "cache_creation_input_tokens": write,
                 "cache_read_input_tokens": read, "output_tokens": output,
                 "cache_creation": {"ephemeral_1h_input_tokens": write if ttl == "1h" else 0,
                                    "ephemeral_5m_input_tokens": write if ttl == "5m" else 0}}
        content: list[dict[str, Any]] = [{"type": "text", "text": "ok"}]
        if tool_use:
            tid, name, inp_args = tool_use
            content.append({"type": "tool_use", "id": tid, "name": name, "input": inp_args})
        for b in range(blocks):  # Claude Code writes one entry per content block
            extra: dict[str, Any] = {"isSidechain": sidechain}
            if agent:
                extra["agentId"] = agent
            if version:
                extra["version"] = version
            self.add(self._base(type="assistant", requestId=f"req_{self.n}",
                                requestRef=ref if records else None,
                                message={"id": f"msg_{self.n}", "model": model,
                                         "role": "assistant",
                                         "content": content[b:b + 1] or content[:1],
                                         "usage": usage}, **extra))
        self.messages.append(h(["assistant", self.n]))
        return self

    def cost(self, usd: float) -> Transcript:
        return self.add({"type": "cost-state", "sessionId": self.session_id,
                         "totalCostUSD": usd})

    def write(self, root: Path, project: str = "-work-demo") -> Path:
        d = root / project
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{self.session_id}.jsonl"
        path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")
        return path
