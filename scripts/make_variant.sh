#!/usr/bin/env bash
# Dev only: make an ablation copy of student/ with one technique switched off.
# Usage: scripts/make_variant.sh no-doctests|no-repair-filter|no-contracts|no-planner   -> student_variants/<name>
set -euo pipefail
name="$1"
dest="student_variants/$name"
rm -rf "$dest"; mkdir -p student_variants
cp -r student "$dest"
case "$name" in
  no-doctests)      sed -i 's/^USE_DOCTESTS = True/USE_DOCTESTS = False/' "$dest/tagent/pipeline.py" ;;
  no-repair-filter) sed -i 's/^USE_REPAIR_FILTER = True/USE_REPAIR_FILTER = False/' "$dest/tagent/pipeline.py" ;;
  no-contracts)     sed -i 's/^USE_CONTRACTS = True/USE_CONTRACTS = False/' "$dest/tagent/pipeline.py" ;;
  no-planner)       sed -i -e 's/^USE_PLANNER = True/USE_PLANNER = False/' -e 's/^USE_PROPS = True/USE_PROPS = False/' "$dest/tagent/pipeline.py" ;;
  *) echo "unknown variant $name"; exit 1 ;;
esac
grep -n "^USE_" "$dest/tagent/pipeline.py"
