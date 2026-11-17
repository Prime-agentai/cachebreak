"""Regenerate examples/session.jsonl: a SYNTHETIC transcript in Claude Code's 2.1.3xx record
format that exercises every kind of break. Real measurements are in the README, not here."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from factory import BASE_SHAPE, Transcript  # noqa: E402

t = Transcript(session_id="0e5a3c1d-demo-4c1b-9b57-5e55e5e55e55", cwd="/home/dev/shop-api")
ctx = 24_000
t.user("Add rate limiting to the checkout endpoint").request(
    write=ctx, read=0, ttl="5m", tool_use=("t1", "Read", {"file_path": "package-lock.json"}))
t.tool_result("t1", "x" * 280_000)  # a 70k-token lockfile, carried by every later turn
t.request(write=70_400, read=ctx, ttl="5m",
          tool_use=("t2", "Bash", {"command": "npm test -- --reporter=verbose"}))
ctx += 70_400
t.tool_result("t2", "y" * 48_000)
t.request(write=12_300, read=ctx, ttl="5m", tool_use=("t3", "Edit", {"file_path": "src/checkout.ts"}))
ctx += 12_300
t.tool_result("t3", "ok")
t.request(write=400, read=ctx, ttl="5m")
ctx += 400
# Lunch: 47 minutes idle on a 5-minute cache.
t.wait(minutes=47).user("Now add tests for it").request(write=ctx + 300, read=0, ttl="5m")
ctx += 300
# Connect an MCP server mid-session: tool definitions change before the first breakpoint.
mcp_shape = {**BASE_SHAPE, "tools": BASE_SHAPE["tools"] + [
    {"name": "mcp__linear__create_issue", "description": "Create a Linear issue", "input_schema": {}}]}
t.wait(seconds=40).user("File a ticket for the flaky test").request(
    write=ctx + 900 - 3_100, read=3_100, ttl="5m", shape=mcp_shape)
ctx += 900
# Switch to a bigger model for the hard part.
t.wait(seconds=30).user("/model opus, then fix the race").request(
    write=ctx + 600, read=0, ttl="5m", shape=mcp_shape, model="claude-opus-5-5")
ctx += 600
t.wait(seconds=20).user("thanks").request(write=200, read=ctx, ttl="5m", shape=mcp_shape,
                                           model="claude-opus-5-5")
out = ROOT / "examples" / "session.jsonl"
out.write_text("\n".join(t.lines) + "\n", encoding="utf-8")
print(f"wrote {out}")
