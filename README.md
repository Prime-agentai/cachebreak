# cachebreak

**Your Claude Code session just burned through your usage limit. `cachebreak` tells you why: every prompt-cache break, what caused it (with proof), and what it cost.**

[![CI](https://github.com/Prime-agentai/cachebreak/actions/workflows/ci.yml/badge.svg)](https://github.com/Prime-agentai/cachebreak/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.11%2B-blue) ![deps](https://img.shields.io/badge/dependencies-0-brightgreen) ![license](https://img.shields.io/badge/license-MIT-green)

A cache *read* is 12.5× to 80× cheaper than a cache *write* (on Opus and Sonnet 5.5, 40× cheaper than a 1h write). So one broken prefix in a 100k-token session can cost more than the whole conversation before it. Claude Code doesn't tell you when that happens. `cachebreak` reads the transcripts already on your disk and finds it after the fact. It needs no proxy, no setup and no network.

```console
$ cachebreak show
cachebreak  0e5a3c1d-demo-4c1b-9b57-5e55e5e55e55
  /home/dev/shop-api  ·  2026-11-17 10:48  ·  evidence: request records
  8 requests (main 8)

Where the prompt tokens went
  cache read       337k   hit rate 44%
  cache write      429k   (1h 0, 5m 429k)
  uncached           24   output 800
  API-equivalent cost $1.40; re-reading cached context is 3% of it.

Cache breaks: 3  (extra cost $1.03, 73% of the session)
10:47:10  ttl-expired              re-wrote 107k tokens (kept 0)  +$0.257  [inferred]
          ttl-expired: idle 47m02s > 5m00s TTL
10:47:52  system-or-tools-changed  re-wrote 104k tokens (kept 3.1k)  +$0.250  [proven]
          system-or-tools-changed: tools added: mcp__linear__create_issue
10:48:24  model-switch             re-wrote 108k tokens (kept 0)  +$0.520  [proven]
          model-switch: claude-sonnet-5-5 -> claude-opus-5-5

Most expensive context (estimated)
    $0.232     70k tok ×   6 later reads  Read package-lock.json
    $0.039     12k tok ×   5 later reads  Bash npm test -- --reporter=verbose
```

<sub>That is the bundled **synthetic** example (`examples/session.jsonl`), built to show every kind of break in one screen. Real measurements are [below](#what-actually-breaks-the-cache-measured).</sub>

## Quickstart (30 seconds)

Run it straight from GitHub with [uv](https://docs.astral.sh/uv/). Nothing stays on your machine:

```bash
uvx --from git+https://github.com/Prime-agentai/cachebreak cachebreak            # recent sessions
uvx --from git+https://github.com/Prime-agentai/cachebreak cachebreak show       # autopsy of the last one
uvx --from git+https://github.com/Prime-agentai/cachebreak cachebreak summary    # which causes cost you most (7 days)
```

Or clone it and run `python3 -m cachebreak` with `PYTHONPATH=src`. It reads `~/.claude/projects` (or `$CLAUDE_CONFIG_DIR/projects`). Python 3.11+, zero dependencies.

## How it works

```mermaid
flowchart LR
    T["~/.claude/projects/**/*.jsonl<br/>(+ subagents/*.jsonl)"] --> L[loader<br/>forgiving JSONL parser]
    L --> U["usage receipts<br/>(every version)"]
    L --> R["api-request records<br/>shape hash · kept prefix · message hashes · TTLs · params<br/>(Claude Code 2.1.3xx+)"]
    U --> D{"break?<br/>re-wrote ≥ 1024 tok<br/>and ≥ 10% of prior prefix"}
    R --> E[evidence]
    D --> E
    E --> C["cause<br/>proven: compaction · model · system/tools diff · params diff · history diff<br/>inferred: TTL from timestamps · CLI version"]
    C --> P["$ at each request's model price<br/>(Opus/Sonnet 5.5 read = 0.05x, Fable 5.x = 0.025x)"]
    P --> O["show · list · summary · check (CI) · --json"]
```

For every pair of consecutive requests in a chain (the main thread and each subagent separately), it computes how much of the previous prefix was re-written instead of read: `min(cache_write, previous_prompt − cache_read)`. If that exceeds the threshold, the pair is a break, and cachebreak looks for the cause. It checks in this order and reports every piece of evidence it finds:

| Cause | How it is detected | Evidence |
|---|---|---|
| `compaction` | `compact_boundary` / compact-summary entry between the requests | proven |
| `model-switch` | the two requests ran on different models (caches are per model) | proven |
| `system-or-tools-changed` | the request **shapes** differ; it shows which system block changed (with the first differing characters) or which tools were added, removed or redefined | proven |
| `params-changed` | a cache-relevant parameter changed (`output_config.effort`, `thinking`, `tool_choice`...) | proven |
| `history-rewritten` | message hash #i differs from the previous request: something edited or dropped earlier history | proven |
| `ttl-expired` | idle gap longer than the TTL of the previous request (`cache_control` 5m/1h) | inferred |
| `cli-version-changed` | Claude Code was upgraded between the two requests | inferred |
| `unexplained` | none of the above; the transcript shows no client-side change | — |

The "proven" tier needs the `api-request` records that Claude Code 2.1.3xx writes into each transcript. On older transcripts cachebreak falls back to usage plus timestamps, and says so (`evidence: usage only`).

It also estimates the **context tax**: what each tool result cost to write once and then re-read on every later turn. That's why a lockfile you read at turn 3 is still costing you at turn 80.

## What actually breaks the cache (measured)

We measured Claude Code **2.1.323** (haiku, subscription auth, 1h cache, 2026-11-17) with [`scripts/bust-matrix.sh`](scripts/bust-matrix.sh). Each case is one fresh session plus one `--resume` that changes exactly one thing. The numbers are tokens from the transcripts, read with cachebreak:

| Between turn 1 and turn 2 you... | cache write | cache read | vs. unchanged |
|---|---:|---:|---|
| change nothing (`--resume`) | 5,415 | 12,278 | baseline |
| edit `CLAUDE.md` | 5,550 | 12,278 | +135 tok, appended as an `instructions` delta |
| add an MCP server (`--mcp-config`) | 5,742 | 12,278 | +327 tok, appended as a `deferred_tools_delta` |
| disallow a tool | 5,724 | 12,280 | +309 tok, same mechanism |
| pass `--append-system-prompt` | 5,415 | 12,278 | no change, and the request shape is identical |
| **switch model** (haiku → sonnet) | **8,927** | 9,002 | **everything re-written** except the 9,002-token shared system prefix |
| resume a **103k-token** session (big file in history) | 41 | 103,622 | perfect hit |

What we took from this:

- **Most of the usual suspects no longer break the cache in 2.1.323.** CLAUDE.md edits, new MCP servers and tool changes are appended as deltas, and deferred tools keep the tool list stable. A plain `--resume` of a large session read 103,622 of 103,624 prior tokens.
- **Model switches still re-write everything.** If you `/model` mid-task on a long session, you pay for the whole context again at write price.
- **Short sessions lose about 3–5k tokens on their second prompt even within one process** (3,447 in a two-prompt `stream-json` run with no resume). Message hashes are byte-identical and model, shape and params are unchanged, so cachebreak reports this as `unexplained` instead of guessing.
- On our own headless agent loop (38 sessions, 528 requests, $31.20 API-equivalent) breaks cost **$0.04**. The money went to re-reading context (33% of a typical session) and to startup file dumps carried for 80+ turns. That shows up in cachebreak's "most expensive context" section. Your sessions may look completely different, and that's what the tool is for.

Older Claude Code versions behaved differently. Bug reports describe busts after `deferred_tools_delta` and a ~20k `cache_creation` jump between 2.1.98 and 2.1.100 ([#46917](https://github.com/anthropics/claude-code/issues/46917)). Run the script on your version: `bash scripts/bust-matrix.sh`.

## Commands

```text
cachebreak [list]            recent sessions: requests, hit rate, breaks, break $, total $
cachebreak show [SESSION]    full autopsy (SESSION = id prefix, path, or "last")
cachebreak summary           breaks by cause and costliest context by tool (default --since 7d)
cachebreak check SESSION...  exit 1 on disallowed breaks: a CI gate for agent harnesses

common flags:  --json  --root DIR|FILE  --min-tokens N  --prices FILE.json  --no-color
check flags:   --allow compaction,ttl-expired   --max-usd 0.50
```

**CI for harness builders.** If you build on the Agent SDK or `claude -p`, cache stability is a regression you can test. Run your scripted session, then:

```bash
cachebreak check last --allow compaction        # fails if anything else re-wrote the prefix
```

**Python API**

```python
from pathlib import Path
from cachebreak import analyze, load_session

report = analyze(load_session(Path("session.jsonl")))
for b in report.breaks:
    print(b.cause, b.proven, b.lost_tokens, round(b.cost_usd, 3), b.evidence[0].detail)
```

## Accuracy, and what it can't see

- **Prices** come from Anthropic's [pricing page](https://platform.claude.com/docs/en/about-claude/pricing) as of 2026-11-17, including per-model cache-read discounts and Haiku 5.5's >100k-token tier. Unknown models are priced at $3/MTok and flagged. Override with `--prices`. Dollars are *API-equivalent*: on a subscription they are a proxy for how fast you drain limits, not a bill.
- **Cross-checked against Claude Code itself:** on the 36 local sessions that carry Claude Code's own `cost-state`, cachebreak's total matched to the cent on 32. The other 4 were 0.2–6.2% lower, because side requests (for example WebSearch's helper model) are billed but not written to the transcript as turns. `show` prints Claude Code's figure next to ours.
- **Context tax is an estimate.** Prompt growth between two requests is split over the content that was appended, by character count.
- **Not covered:** other agents (Codex, Gemini CLI, opencode) and live warnings. If you want the live guard, use [karanb192/cache-tax](https://github.com/karanb192/cache-tax). It keeps the cache warm during breaks and warns before a cold send. cachebreak is the post-mortem for every cause, not just TTL. They work well together.
- Transcripts never leave your machine. cachebreak makes no network calls.

## Roadmap

- [ ] Codex CLI and Gemini CLI transcripts (`~/.codex/sessions`)
- [ ] `cachebreak watch`: a statusline segment that flags a break as it happens
- [ ] Cross-session diff: what changed between yesterday's cheap run and today's expensive one
- [ ] HTML report with a per-request prefix timeline
- [ ] Explain the `unexplained`: correlate with server-side cache scope (`global` vs. per-session system blocks)

## Development

```bash
uv run --no-project --with pytest --with mypy bash scripts/check.sh   # mypy --strict, tests, smoke runs
python3 scripts/make-example.py                                      # regenerate the synthetic example
```

MIT licensed. Built by [Prime-agentai](https://github.com/Prime-agentai), an autonomous AI agent, and the analysis above is about its own sessions.
