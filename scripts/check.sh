#!/usr/bin/env bash
# The one gate: strict types, tests, and smoke runs of the real entrypoint.
# Locally:  uv run --no-project --with pytest --with mypy bash scripts/check.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
"$PY" -m mypy --strict src
"$PY" -m pytest -q
export PYTHONPATH=src
"$PY" -m cachebreak --version
"$PY" -m cachebreak show --root examples/session.jsonl --no-color > /dev/null
"$PY" -m cachebreak show --root examples/session.jsonl --json | "$PY" -c \
  'import json,sys; d=json.load(sys.stdin); assert [b["cause"] for b in d["breaks"]] == ["ttl-expired","system-or-tools-changed","model-switch"], d["breaks"]'
if "$PY" -m cachebreak check examples/session.jsonl > /dev/null; then
  echo "check should fail on the example session" >&2; exit 1
fi
echo "check.sh: all green"
