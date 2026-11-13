#!/bin/bash
# Run a batch: blackbox -> whitebox -> merge -> evaluate, retrying problems whose run did not
# finish (a provider can return a response with no tool call; that problem then aborts).
#
# Usage: scripts/batch.sh <run-name> "<problem-id> ..." [workers]
# Example: scripts/batch.sh ab4 "lcb-3690 lcb-3502 lcb-3644" 3
set -euo pipefail
cd "$(dirname "$0")/.."
RUN=$1; PROBS=$2; WORKERS=${3:-3}
PY=.venv/bin/python

stage() { echo "[stage $1] $(date +%T)"; }
failed() {  # problem ids whose usage.json is missing or did not reach Submitted
  for p in $PROBS; do
    f="runs/$RUN/$1/$p/usage.json"
    [ -f "$f" ] || { echo "$p"; continue; }
    "$PY" -c "import json,sys; sys.exit(0 if json.load(open('$f'))['exit_status']=='Submitted' else 1)" || echo "$p"
  done
}

for mode in blackbox whitebox; do
  stage "$mode"
  "$PY" -m testagent.run --mode "$mode" --run "$RUN" --only $PROBS --workers "$WORKERS"
  for attempt in 1 2; do
    RETRY=$(failed "$mode" | tr '\n' ' ')
    [ -z "$RETRY" ] && break
    echo "[retry $attempt] $mode: $RETRY"
    "$PY" -m testagent.run --mode "$mode" --run "$RUN" --only $RETRY --workers "$WORKERS"
  done
done
stage merge
"$PY" -m testagent.final --run "$RUN" --only $PROBS --merge-only
stage evaluate
"$PY" -m testagent.evaluate --run "$RUN" --mode final --only $PROBS --workers "$WORKERS"
stage done
