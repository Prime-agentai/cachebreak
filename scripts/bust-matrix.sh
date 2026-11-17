#!/usr/bin/env bash
# Measure which everyday actions break Claude Code's prompt cache.
# Each case: one fresh session (turn 1), then one --resume (turn 2) that changes ONE thing.
# Needs a logged-in `claude`. Costs a few cheap model turns per case (haiku by default).
set -euo pipefail
MODEL="${CB_MODEL:-haiku}"
ROOT="${1:-$(mktemp -d /tmp/cachebreak-matrix.XXXXXX)}"
PROMPT1="Reply with exactly the word: one"
PROMPT2="Reply with exactly the word: two"
mkdir -p "$ROOT"
echo "matrix root: $ROOT" >&2

sid_of() { python3 -c 'import json,sys; print(json.load(sys.stdin)["session_id"])'; }

run_case() {
  local name="$1"; shift
  local dir="$ROOT/$name"; mkdir -p "$dir"; cd "$dir"
  printf '# Project notes\nKeep answers short.\n' > CLAUDE.md
  local sid
  sid=$(claude -p "$PROMPT1" --model "$MODEL" --output-format json < /dev/null | sid_of)
  "$@"  # mutate between turns; may set EXTRA (array) for turn 2
  claude -p "$PROMPT2" --resume "$sid" --model "${MODEL2:-$MODEL}" --output-format json "${EXTRA[@]}" < /dev/null >/dev/null
  echo "$name $sid $dir"
  unset MODEL2; EXTRA=()
}

EXTRA=()
noop() { :; }
model_switch() { MODEL2=sonnet; }
append_sys() { EXTRA=(--append-system-prompt "Always be terse."); }
edit_claude_md() { printf '# Project notes\nKeep answers short.\nUse British spelling.\n' > CLAUDE.md; }
add_mcp() {
  printf '{"mcpServers":{"echo":{"command":"python3","args":["-c","import sys,json\\nfor l in sys.stdin:\\n m=json.loads(l)\\n if \\"id\\" not in m: continue\\n r={\\"protocolVersion\\":\\"2025-06-18\\",\\"capabilities\\":{\\"tools\\":{}},\\"serverInfo\\":{\\"name\\":\\"echo\\",\\"version\\":\\"1\\"}} if m[\\"method\\"]==\\"initialize\\" else {\\"tools\\":[{\\"name\\":\\"echo\\",\\"description\\":\\"Echo text back\\",\\"inputSchema\\":{\\"type\\":\\"object\\",\\"properties\\":{\\"text\\":{\\"type\\":\\"string\\"}}}}]} if m[\\"method\\"]==\\"tools/list\\" else {}\\n print(json.dumps({\\"jsonrpc\\":\\"2.0\\",\\"id\\":m[\\"id\\"],\\"result\\":r}),flush=True)"]}}}' > mcp.json
  EXTRA=(--mcp-config mcp.json)
}
disallow_tool() { EXTRA=(--disallowedTools WebFetch); }

run_case resume-unchanged noop
run_case model-switch model_switch
run_case append-system-prompt append_sys
run_case edit-claude-md edit_claude_md
run_case add-mcp-server add_mcp
run_case disallow-a-tool disallow_tool
