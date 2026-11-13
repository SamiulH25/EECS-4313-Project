#!/usr/bin/env bash
# Iteration run: agent + evaluation, 8 workers. Usage: scripts/iterate.sh <run> [projects_dir] [extra run args...]
# projects_dir defaults to data/dev. Record runs: cp settings.course.yaml settings.yaml first.
set -euo pipefail
cd "$(dirname "$0")/.."
run=$1; projects=${2:-data/dev}; shift $(( $# >= 2 ? 2 : 1 ))
py=.venv/bin/python
mkdir -p runs
$py -m testagent.run --run "$run" --projects "$projects" --workers 8 "$@" 2>&1 | tee "runs/$run.run.log"
$py -m testagent.evaluate --run "$run" --projects "$projects" --workers 8 2>&1 | tee "runs/$run.eval.log"
