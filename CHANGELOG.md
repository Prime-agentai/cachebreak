# Changelog

## 0.1.0 (2026-11-17)

First release.

- `list`, `show`, `summary` and `check` commands, all with `--json` output.
- Detects cache breaks per chain (main thread and each subagent), with these causes: compaction, model switch, system/tools shape diff, params diff, history rewrite, TTL expiry, CLI version change and unexplained.
- Proven causes come from Claude Code 2.1.3xx `api-request` records. Older transcripts fall back to usage plus timestamps.
- Dollar pricing per request model (list prices as of 2026-11-17, per-model cache-read discounts, Haiku 5.5 long-prompt tier) with `--prices` overrides.
- Context-tax estimate: what each tool result cost to write once and re-read on every later turn.
- `scripts/bust-matrix.sh` measures what breaks the cache on your Claude Code version.
