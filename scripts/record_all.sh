#!/usr/bin/env bash
# Record runs with the course settings: final agent x2, starter baseline, ablations x2 each.
cd "$(dirname "$0")/.."
cmp -s settings.yaml settings.course.yaml || { echo "settings.yaml is not the course settings"; exit 1; }
scripts/make_variant.sh no-doctests >/dev/null
scripts/make_variant.sh no-repair-filter >/dev/null
for spec in "final-1:student" "final-2:student" "starter:student_variants/starter" \
            "no-doctests-1:student_variants/no-doctests" "no-doctests-2:student_variants/no-doctests" \
            "no-filter-1:student_variants/no-repair-filter" "no-filter-2:student_variants/no-repair-filter"; do
  run=${spec%%:*}; dir=${spec#*:}
  if [ -f "runs/$run/evaluation.json" ]; then echo "skip $run"; continue; fi
  echo "=== $run ($dir) $(date -u +%H:%M)"
  scripts/iterate.sh "$run" data/dev --student "$dir" > "runs/$run.iter.out" 2>&1
  grep "^Total" "runs/$run.eval.log"
done
echo "ALL DONE $(date -u +%H:%M)"
