# Team notes (dev log, not submitted)

Everything outside `student/` is replaced at grading. This file only records local decisions.

## Search-based augmentation, current state (2026-10-26)

**settings.yaml is back on the course fp8 block** (restored from `settings.fp8.yaml` on
2026-10-26 for the report run `runs/fp8-search-full/`). Together iteration runs (search-v2-cohort,
search-v3-full) are not comparable to fp8 runs for cost/latency.

student/tools/whitebox now does most of the input design itself (`_augment.py`, `_mutation.py`):
- Search: random inputs shaped like the saved ones, local edits, reorderings, scaled/negated
  copies; greedy 3-pass cover of mutant kills (3), new branch transitions/loop-count classes (1)
  and input/output feature pairs (0.5); stop after 1500 unproductive candidates or the deadline.
- Limits: bounds parsed from `low <(=) [alias ==] x[i][j](.length) <(=) high`; up to 5 inputs at
  the stated limits (random / all-low / all-high; strings by length bound; grids ~31x31; lists
  up to 1000) plus lowest/highest answer copies. Evaluated untraced (tracing is ~10x slower).
- Legality: candidates pass the constraint checker and run on the reference; parameters on
  parent/tree/edge/graph/prime/generated-such-that lines are never rebuilt, sort/order lines
  lock reordering, distinct/unique lines keep values distinct. Not a full spec audit.
- `run_tests --submit` runs `probe --fill-only` first (35 s search), so a model that skips the
  main batch still gets a filled suite (lcb-3699 search-v2: 8 tests -> 25 after this fix).
  probe's own hand-off passes `--no-fill` so the search never runs twice.
- Logging: probe/_augment log every phase via `_log.py` to the file named by `$PROBE_LOG`;
  unset (graded runs, current driver) means no log at all. Driver-side live printing was
  NOT built (user dropped it).

Offline benchmark (saved full-run model inputs; first 30 replayed through new probe; bugs
scored locally, which reproduces the official 257/350 for saved suites): 257 saved -> 309/350.

Live 13-problem cohort, local bug scoring (official evaluator too slow to wait for):
search-v1 (fp8) 55/74; search-v2 (Together) 61/74 vs retained v4 51/74. Weak: lcb-3699 2/6
(skipped probe batch; fixed afterwards, not re-run), lcb-3603 1/3 ($0.046, 25 calls, tree).
Run artifacts: `runs/search-v1-cohort/`, `runs/search-v2-cohort/`. Single runs per config.
Official evaluator, search-v2 vs v4-audited: bugs 61/74 vs 51/74, hard mutants 41/47 vs 32/47,
mean path 99.55% vs 98.69%, branch 98.40% both, 479 vs 456 valid tests, 0 wrong/flaky/over-budget.
Key limit is now $54 (2026-10-26T01:57Z: $2.0266 used).

Full 61-problem run `runs/search-v3-full/` (Together, workers 12, includes the run_tests fill
fix; generation 02:19:48Z-02:25:59Z, ~6 min; slowest problem 230 s), official evaluator vs the
earlier fp8 full run `runs/fp8-full-20261025/`: bugs 308/350 vs 257/350, hard mutants 218/240
vs 194/240, branch 99.32% vs 97.68%, path 99.52% vs 97.14%, 2302 vs 2441 valid tests, 0 wrong /
0 flaky / 1 over-budget both, cost $0.191 (Together) vs $0.511 (fp8). Weakest bug rates:
lcb-3603 1/3, lcb-3605 1/5, lcb-3627 2/6 (257k tokens, $0.045), lcb-3487 3/5, lcb-3702 3/5.
Provider differs, so this is not yet the fp8 number for the report.

**Report run `runs/fp8-search-full/` (fp8 course settings, workers 12, same student/ as
search-v3-full):** bugs 314/350 (88.98%), hard mutants 223/240, mutation 98.94%, branch 99.32%,
path 99.39%, 2356 valid tests, 0 wrong / 0 flaky / 0 over-budget / 0 rejected, 61/61 Submitted,
1.017M tokens, $0.107. Generation 03:00:55Z-03:13:56Z (13 min; lcb-3603 782 s, lcb-3617 429 s),
evaluation ~8 min more, ~21 min end to end. Weakest: lcb-3603 0/3 (268k tokens), lcb-3605 1/5,
lcb-3657 4/7. Key usage after: $2.3314 of $54.

REPORT.md drafted from this run (2026-10-26). Still open: team names, and the all-61 unchanged
starter baseline. No pristine starter `student/` exists on this machine (every runs/*/student
snapshot is already modified), so a fresh copy of the course kit is needed to run it.
Done: starter `student/` taken from the public course repo
(github.com/alirezadaghigh99/testing-agents-starter-kit, commit ff4e8c1; harness identical to ours),
snapshot at `runs/starter-snapshot/student/`, run as `runs/starter-together` on Together (user's call,
for speed): bugs 240/350 (70.19%), hard 177/240 (82.03%), path 97.10%, branch 97.43%, mutation
93.02%, oracle precision 88.89% (242 wrong), 71 over budget, 5 rejected files, 1 empty problem,
12.32M tokens, $2.192. Stages: 66 Submitted, 54 TokenLimitExceeded, 2 RepeatedFormatError.
REPORT.md section 4 and INTERVIEW_PREP.md filled from it.

## Time and completion fixes (2026-10-25, unmeasured)

The user asked for fixes to per-problem time use, test runtime, and agents that try to solve the
problem instead of saving a suite. Only student/ changed. No paid run has measured this yet.

- Cause found in `fp8-expression-v1` traces: 3612/3657 stalled in one huge reasoning turn after
  `cat solution.py` with no suite saved; 3699 computed answers itself with `python3 -c` and never
  called probe; 3655 hit the timer after slow/crashing probe cases.
- Combined prompt: the FIRST command saves 5-8 spec cases with `probe --no-submit`, then
  `cat solution.py`, so a suite exists before the expensive turn. The second turn sends one main
  batch. At most one repair, then `run_tests --submit`. Explicit: never solve/compute answers,
  think briefly, no python -c.
- probe: 1 s limit per solution call (SIGALRM, BaseException so `except Exception` cannot swallow
  it; long single C-level ops are only rejected afterwards), 30 s total case-execution budget,
  100,000-char cap per test. It auto-submits when there are no errors OR the batch saved >=10 tests
  (rejected entries are dropped). Rationale: the evaluator reruns every test per mutant (timeout
  10x reference suite time) and under tracing (120 s/test), and a 600 s whole-suite cap.
- run_tests pytest timeout 90 -> 50 s so expression (10) + cases (30) + checker (20) + pytest (50)
  fits the 120 s command limit.
- Not fixable from student/: the harness OpenRouter call ignores the 900 s wall deadline
  (one response took 1784 s). The prompt only reduces the chance; the seed suite limits the damage.
- Verification: full repository suite 82 passed (140.73 s), including two new probe regressions;
  `python -m testagent.submission student` OK.

### Think-less vs /no_think A/B on lcb-3657 (2026-10-25)

One problem, one run each, run in parallel via `scripts/final_run.py --only lcb-3657`.
lcb-3657 previously stalled in one long reasoning turn with no suite.

| | A: think less (kept) | B: A + `/no_think` |
|---|---:|---:|
| Exit | Submitted | Submitted |
| Model calls | 6 | 13 |
| Tokens | 30,428 | 74,482 |
| Cost | $0.003188 | $0.007696 |
| Max reasoning tokens in one step | 224 | 410 |
| Valid tests | 22 | 15 |
| Bugs | 2/7 | 4/7 |
| Hard mutants / path / branch | 2/2, 100%, 100% | 2/2, 100%, 100% |

- `/no_think` did NOT disable reasoning on Qwen3.5 (reasoning tokens still reported). It caused
  four malformed bracket expressions, `echo | probe` misuse and repeated `--no-submit` batches.
  Its higher bug count is one noisy sample from a smaller suite, not evidence. Not kept.
- A's flow matched the prompt: seed, read code, one main batch (auto-submitted). Neither run had a
  runaway reasoning turn. A's 2/7 equals v4's 2/7 on this problem: case quality is still weak here.
- student/ = A (identical to `runs/ab-thinkless-3657/student/`). Key at 23:58:23Z: $1.88920476
  used of $4, $2.11079524 remaining (A/B delta about $0.0109 from 23:54:02Z).

### Generic input hints, control vs hints-v1 (2026-10-26)

lcb-3657 diagnosis: 20/22 inputs were listed pre-sorted (row-major), so sort bugs 3/4/7/8 were
invisible; reversing the SAME inputs kills 6/7. bug_2 needs a long interval enclosing a later
short one. Response: an "Input hints" section in combined.yaml (order scrambled, dominance,
irregular shapes, balanced outcomes, divisor alignment, mixed signs). No problem IDs or values.
Tested on two different problems; control = same prompt without hints, run the same hour.

| | lcb-3653 control | lcb-3653 hints | lcb-3627 control | lcb-3627 hints |
|---|---:|---:|---:|---:|
| Bugs | 2/3 | 2/3 | 2/6 | 2/6 |
| Hard mutants | 3/3 | 3/3 | 2/9 | 2/9 |
| Path / branch | 100/100% | 100/100% | 94.1/83.3% | 94.1/83.3% |
| Model calls / tokens | 4 / 14,455 | 18 / 152,873 | 3 / 10,043 | 3 / 10,578 |
| Cost | $0.001549 | $0.015688 | $0.001095 | $0.001132 |

- No measured gain. 3653 swapped which bug survived: control missed bug_1 (sentinel -10^9 too high,
  needs values near -10^9 so sums go below it); hints missed bug_2 (k=1 all-negative).
- 3653 hints run drifted: it passed the expression as a quoted argument instead of a heredoc,
  probe rejected it (`unrecognized arguments`), and the model abandoned probe for handwritten
  unittest files, python -c, mutants and coverage (18 calls, 10x cost). Same failure as the
  /no_think run. A tool-side fix (accept the expression as a positional argument) would remove it.
- 3627 hints had no effect on inputs: mostly zero/small grids with no detour. Surviving bugs need
  a blocked direct path so the best route goes up/left, and a nonzero start cell. The abstract
  "dominance" hint does not translate to grid paths for this model.
- student/ currently = hints-v1 (frozen copy `runs/hints-v1/student/`); the no-hint prompt is
  frozen at `runs/hints-control/student/`. Key at 00:12:02Z: $1.90607531 used, $2.09392469 left.
- lcb-3657 (rectangle cuts) with hints-v1 (`runs/hints-v1-3657/`) vs the identical no-hint prompt
  (`runs/ab-thinkless-3657/`): bugs 4/7 vs 2/7, hard 2/2 both, path 100% both, 31 vs 22 valid tests,
  3 vs 6 model calls, 12,466 vs 30,428 tokens, $0.001395 vs $0.003188. Survivors: bug_2, bug_5,
  bug_7 (control: bug_2, bug_3, bug_4, bug_7, bug_8). Inputs with a nested interval 11/31 vs 1/22;
  True answers 27/31 vs 20/22, so the outcome-balance hint was ignored; bug_5 (far-edge) was lost.
  Single sample on the problem the hints were derived from: encouraging, not proof.

### Tool-side augmentation (augment-v1..v3, 2026-10-26)

Web research (Haiku agent; MuTAP/CoverUp/TrickCatcher/metamorphic testing) plus NOTES history led
to tool-only changes, because extra model turns are what failed before. All in student/tools/whitebox:
- `_mutation.py`: mutator moved out of `mutants` (shared) and given realistic-bug operators:
  delete call statement (`x.sort()`), max/min keep-one-arg or swap, drop sorted()/key=/reverse=,
  drop modulo. Before this, lcb-3657 had 27 mutants and only one (equivalent) survivor, so
  mutant guidance had nothing to aim at. `mutants` output now includes these operators.
- `_augment.py`, called by probe just before auto-submit: reversed/rotated/shuffled copies of suite
  inputs, plus scaled-to-bound and negated copies for parameters whose element bound is >= 10^6.
  A copy is kept when it kills a local mutant no suite input kills (greedy). Then up to 2
  extreme-value copies are added regardless: the lowest and the highest numeric answer (ranking
  by input length or by |answer| missed the negative-sentinel bug). Order is locked for parameters
  named on Constraints lines mentioning sort/parent/tree/leading/generated such that.
  Deadline 50 s from probe start; any exception skips augmentation, never blocks submission.
- probe: expression accepted as a quoted argument; bracket repair (cut at first mismatched closer,
  append the right closers, accept only a list of dicts); answer summary on non-submitting batches;
  auto-submit needs min(20, max_calls // 2) saved tests (augment-v1 auto-submitted a 3-test suite).
- Prompt: read the seed answer summary; leave about 10 calls unused for probe.

Offline replay (same model inputs as the controls, new probe): 3657 2->4/7, 3653 2->3/3, 3627 2->2/6.

Live, one run per configuration, same three problems:

| Config | Bugs | Hard mutants | Model calls | Cost |
|---|---:|---:|---:|---:|
| control (no hints) | 6/16 | 7/14 | 13 | $0.0058 |
| hints-v1 | 8/16 | 7/14 | 24 | $0.0182 |
| augment-v1 | 7/16 | 5/14 | 12 | $0.0059 |
| augment-v2 (+repair, min suite) | 7/16 | 7/14 | 16 | $0.0099 |
| augment-v3 (+low/high extremes) | 9/16 | 7/14 | 8 | $0.0035 |

augment-v3 per problem: 3657 4/7 (26 valid), 3653 2/3 (bug_3 survived: k=1 subset shortcut, a
model case-choice miss; path 85.7%), 3627 3/6 (detour bugs 1/5/6 still survive). No wrong
oracles or over-budget tests in any run. Agent times 90/134/302 s. Single samples: run-to-run
noise is several bugs, so v3 is the working candidate, not a proven win. student/ = augment-v3
(frozen at `runs/augment-v3/student/`). Key at 01:03:41Z: $1.92944231 used, $2.07055769 left.

## Counterexample-driven continuation and credits (2026-10-25)

User requested continued improvement plus credit usage in every progress update. Perfection cannot
be established from the development set. Operational checks are legal inputs, completed runs,
correct oracles, fewer known surviving bugs, and no measured regression on additional problems.
The previous retained configuration remains frozen in `runs/fp8-issues-v4/student/`.
The student/ before the time fixes above was the unretained candidate `fp8-expression-v1`; its benchmark was stopped
at the user's request after reported timer failures. The prompt-only `fp8-counterexamples-v1`
trial was rejected. Do not launch more tests without a new user instruction.

### Live credit baseline

OpenRouter `/api/v1/key` at 2026-10-25T21:14:37.972993Z reported:
- Key usage: $1.72416796 of a $4.00 limit; remaining $2.27583204; limit_reset null.
- Account total credits $15.00, total usage $11.872308976 (account remainder $3.127691024).
- The key cap is tighter than the account balance. Do not confuse account and key credits.

The exact starting report is saved at `runs/fp8-counterexamples-v1/credits.before.json`.
Read live balances before/after paid batches and report the last checked usage/remaining balance
with timestamps; do not infer remaining credits from rounded experiment costs. Usage deltas may
include any concurrent use of the same key, and audited evaluator copies make no model calls.
Initial 13-problem headroom check required at least $0.90 available; it passed. Do not launch
another paid batch blindly or deliberately exhaust the key. The local credit reporter is temporary
development tooling, never part of student/ or the submitted tools.

### Candidate change and evaluation plan

- Prompts now choose plausible wrong strategies FIRST, then design inputs that distinguish them.
  Boundary/special values must be crossed with adversarial data classes, rather than tested separately.
- Make the relevant alternative uniquely necessary: late-only solutions, smaller later tie winners,
  overwritten winning state, non-monotone paths, and one-axis/one-alignment cases.
- Stress the semantic dimension: recursion depth rather than tree width, binomial answer counts
  rather than element magnitude, and exact/equivalent or near-colliding numeric representations.
- The initial prompt-only trial preferred small discriminators plus semantic stress cases but kept
  the old JSON pipeline. It regressed to 41/74 bugs, 26/47 hard mutants, two empty suites and
  $0.091498 recorded generation cost (2,505.00 s including evaluation). It was not retained.
  Key usage rose from $1.72416796 to $1.81566501 ($0.09149705 delta); the exact reports are
  credits.before.json/credits.after.json in that run. Do not rerun it merely to confirm the regression.
- Its traces showed source rereads, missing pipes/shared generator files, repeated invalid input
  repair and two wall-time exits before any suite was saved. A longer instruction list did not
  reliably control the 9B model's shell orchestration.
- The new probe consumes ONE restricted Python list expression of named-argument dictionaries.
  `_case_expressions.py` permits pure constructors/comprehensions plus comb/gcd/isqrt, rejects
  imports/attributes/dunder names/arbitrary calls, and evaluates in a Linux worker with 512 MiB
  address-space, 5 s CPU, 10 s wall and 32 MiB serialized-output caps. Limits fail closed; tighter
  inherited limits are preserved. Final values are JSON-native, with no silent tuple/key coercion.
- Clean --output calls now exec the existing verified-submission helper directly, with no output
  before its marker. Mixed invalid entries preserve valid tests and return failure. No external
  Python generator, pipe or extra model submission turn is required.
- Combined/white-box prompts, CLI regressions and tool docs were migrated together. Model/fp8
  routing, harness, final strategy and call budgets remain unchanged. The parser bounds data
  construction; it is not an OS sandbox for arbitrary bash commands.
- Validation: 41 focused tests passed (8.83 s), full suite 65 passed (127.16 s), submission OK.
  A real LocalEnvironment smoke generated mutable-input tests and submitted from one probe call;
  a forbidden file operation failed without a file side effect or submission.
- No development problem IDs, actual faulty implementations or known concrete witnesses were added
  to student/. The generic principles follow offline analysis; submitted tools do not read data/
  reference fixtures, development bugs or evaluator internals. The provided task-local solution.py
  remains the allowed white-box oracle.

The expression candidate's 13-case benchmark was cancelled at the user's request on
2026-10-25T23:14Z after reported timer failures; no complete comparison is claimed.
Cancellation was followed by a process check: no matching testagent, evaluator, pytest or
run_tests processes remained. Preserve partial artifacts in `runs/fp8-expression-v1/`.
Live key usage at 23:14:55Z: $1.85070096/$4, remaining $2.14929904.
The delta from this trial's credit baseline is $0.03503595; total continuation delta from
$1.72416796 is $0.12653300. Neither delta establishes a completed benchmark result.
The additional regression cohort was NOT launched: lcb-3562, lcb-3587, lcb-3591,
lcb-3639, lcb-3644, lcb-3654, lcb-3685, lcb-3716. Its older raw scores total 35/58.
The independent constraint-audited control is now scored at `runs/fp8-regression-baseline-audited/`:
34/58 bugs, 22/30 hard mutants, 100% mean branch and 97.40% mean path coverage, 327 tests.
Its copied $0.062926 usage metadata belongs to OLD generation; the audit/replay made no model calls.
They are development regression cases outside the recent tuning cohort, NOT a clean blind holdout:
Their historical bug descriptions were already inspected. Further testing is paused by user request.

### Stopped-run diagnosis (offline inspection, 2026-10-25)

No tests, evaluator replays or model calls were restarted for this investigation. Findings come
from saved trajectories, AST-parsed generated tests and installed request/deadline implementation.

- Observed exits: 9 Submitted, 2 TimeExceeded (3655, 3699), 2 with no recorded terminal exit
  (3612, 3657). The interrupted cases must not be relabelled as confirmed timeouts.
- The installed DefaultAgent checks its 900 s wall limit BEFORE querying, not during an
  outstanding request. OpenRouter's adapter makes synchronous requests with a 60 s socket
  timeout and up to 10 attempts with retry backoff, without propagating the task's remaining deadline.
  A socket timeout is not an end-to-end request deadline.
- In 3655, the second response's provider-created timestamp to local receipt spans 1784.1 s.
  Usage reports 60,557 completion tokens, including 60,020 reasoning tokens, although the
  serialized model configuration and request-construction code specify max_tokens=8192.
  The cap did not bound reported completion usage; logs do not establish why the provider
  exceeded it. Do not claim that fp8 quantization itself caused the problem.
- That response also proposed unequal-digit-width inputs. Two oracle calls raised IndexError;
  31 tests were saved, but the next agent query hit the already-expired task timer.
- In 3699, the agent repeatedly copied/calculated answers with Python instead of using probe,
  then encountered malformed tool responses. It timed out without a saved test file. Across
  the run there are four recorded format-error feedback messages. The two recorded timeouts
  account for $0.0220328 of approximately $0.035036 logged generation cost.
- Nine successful submissions show that expression parsing/automatic submission was not
  universally broken. However, final.py waits for generation to return before merging any
  final suites; the cancelled generation batch never reached final bug-detection evaluation.
  There is NO measured current-candidate bug score to compare against the retained 51/74.
- Static constraint audit: 360 saved tests across 10 problems; 9 invalid and no unresolved
  cases. Invalid inputs: four tree label/parent length mismatches, two event-time overflows,
  two unequal digit widths, and one overlapping coin-interval case. Successful execution
  against the reference does not establish specification validity.
- Semantic coverage remains uneven: all 29 grid cases start at zero; tree cases reach depth
  only 14 (15 nodes), not the requested recursion threshold; no k=1 mixed-sign sum case.
  Conversely, middle-mode length now reaches 1000 versus 10 previously, and k=1 all-negative
  sum cases now exist. These are input-shape improvements, not measured bug-score gains.

Priority before another batch: address request/deadline overruns in permitted local orchestration,
then reduce workflow drift and require relational input constraints. Do not increase task limits,
modify the grading harness, or add another long prompt checklist to hide the failure. The candidate
is still unretained, previous best preserved, and all testing remains paused by user request.

Live key check at 2026-10-25T23:18:41Z: $1.85070096 used, $2.14929904 remaining;
unchanged during this investigation.


## Failure-focused revision (2026-10-25)

Selected working setup: `fp8-issues-v4`, combined strategy with unchanged course fp8 settings and limits.
The measured full-run baseline remains frozen at `runs/fp8-full-20261025/student/`; its scores
do not describe the newer prompts/tools now in student/.

### Diagnosis and changes

- All eight non-submitted baseline runs either repeatedly probed malformed argument shapes or
  continued coverage/mutation exploration after a passing suite. An example is lcb-3588: 32 valid
  tests were saved early, then many large speculative probes added no tests before token exhaustion.
- lcb-3659 repeatedly confused a nested grid plus k with a single flattened argument and saved no
  tests. lcb-3603 rejected many inconsistent parent/string lengths and kept only nine cases.
- Low-detection suites overused small, sorted or uniform cases. The generic prompt now asks for
  threshold neighbors, independently varied conditions, asymmetric/order-sensitive structures,
  large legal magnitudes, multi-cycle growth and combinatorial count scale. No problem IDs,
  reference bugs, expected values or per-problem branches are embedded in prompts or tools.
- `probe` input is now one JSON object per line mapping actual method parameter names to values.
  This is a clean cutover from positional Python literals: old tuples/bare lists are rejected.
  inspect.signature binding validates names, canonicalizes parameter order/defaults and deduplicates
  existing positional or keyword calls. Argument values are rendered before executing a fresh instance.
  JSON generators can construct related shapes from common size variables instead of hand-counting.
- `run_tests --submit --max-calls N` checks the entire suite then runs pytest and emits the supported
  completion signal first only after success. It rejects filters, bad budgets, empty/failing suites,
  forbidden source access and collection-count overflow. Invoke it ALONE: prior command output
  would prevent LocalEnvironment from recognizing the first-line completion marker.
- Combined prompts no longer request mutation/coverage diagnostics. They generate cases, optionally
  correct malformed inputs once, then invoke verified submission. White-box callers were migrated too.
- The first focused trial still used only tiny iteration counts on a zero-detection growth problem.
  The second trial puts 8–12 actual constraint-scale cases before small behavior cases, asks for
  independent input-only assertions over ALL cases before emitting JSON, and reads list_constraints
  with the implementation. Numeric scale and legal shape construction are mandatory prompt steps.
- Whole-batch validation in v2 proved too brittle: a single malformed tree case prevented all JSON
  output, and repeated failed generators consumed the wall-time limit without saving any tests.
  v3 validates/reports/rejects cases individually, enables shell pipefail, saves a few simple cases
  before stress cases, bounds collection-valued outputs to moderate sizes, and gives standard
  by-construction chain/star and exact-length label idioms. It does not auto-repair illegal data.
- `probe --output` now exits nonzero when there are no saved or existing tests; a failed producer
  with empty stdout can no longer look like successful suite generation.
- v3 exposed shared `/tmp/generate_tests.py` writes from concurrent problem workspaces, and one
  generator was executed without piping into probe. These paths violate task isolation and can race.
  v4 requires INLINE pipelines and explicitly forbids saved/shared generator files and outside-task
  writes. It also rejects invented uniqueness/sortedness restrictions: complex constraints must be
  satisfied by construction rather than speculative model-written validators.
- No harness, model or settings changes. Tools remain self-contained; specification legality and
  one-call-per-test assumptions are not replaced by universal runtime input/budget validation.

### Verification and focused comparison

The final complete repository suite passed 47 tests (128.90 s). Integrated helper regressions
passed 23 tests (6.90 s), and submission validation passed. A real LocalEnvironment smoke recognized
automatic Submitted for a passing suite and rejected a wrong oracle without submitting. A second
smoke confirmed pipefail propagates a rejected generator input while preserving its legal cases;
the preserved suite then passed full verification and submitted. Empty generation is a regression case.

The cohort is chosen mechanically from the full run: exit_status != Submitted OR bug_kill_rate < 0.4.
It includes ONLY these 13 problematic cases:
lcb-3588, lcb-3603, lcb-3612, lcb-3627, lcb-3629, lcb-3632, lcb-3653, lcb-3655,
lcb-3657, lcb-3659, lcb-3699, lcb-3700 and lcb-3715.

On that cohort the baseline caught 36/74 bugs and 32/47 hard mutants, used 2,223,087 tokens and
cost $0.228322. Five agents submitted, seven hit token limits and one hit its time limit.
One suite was empty. These are subset results, not the full-run rates.

`fp8-issues-v1` completed all 13 agents with Submitted status, 303,447 tokens and $0.031487
recorded cost (634.93 s including evaluation). Official evaluator scores: 39/74 bugs, 30/47 hard
mutants, 99.10% mean path coverage, 98.40% mean branch coverage, 529 oracle-valid tests,
no wrong/flaky tests, empty suites, duplicate smells or budget overflow. It improved completion
and cost but regressed on lcb-3655 (8/8 to 4/8 bugs) and still caught 0/5 on lcb-3629.

`fp8-issues-v2` completed with 51/74 bugs, 31/47 hard mutants, 484,900 tokens and $0.050811
recorded cost (1,755.78 s including evaluation). Twelve agents submitted; lcb-3603 timed out with
no suite because whole-batch assertions kept rejecting its inconsistent shapes. The tree failure
is a regression, not a successful completion hidden by aggregate scores. The offline audit found
20 out-of-spec tests, all in the rectangle problem. Removing those tests did not change its
51/74 bug or 31/47 hard-mutant counts; audited scores are saved in `fp8-issues-v2-audited`.
Audited evaluation alone took 797.29 s: stronger numeric-scale cases can increase evaluator CPU
time substantially even while model calls and dollar cost fall. Do not advertise dollar savings
as an equivalent end-to-end speedup.

`fp8-issues-v3` completed with 50/74 bugs, 31/47 hard mutants, 393,683 tokens and $0.042054
recorded cost (1,388.74 s including evaluation). Eleven agents submitted. lcb-3603 retained tests
but timed out; lcb-3699 timed out without a suite after executing a shared generator without probe.
There was also an SDK-retried provider read timeout. The independent audit found 31 out-of-spec
tests; its audited copy completed with 48/74 bugs and 31/47 hard mutants, 90.70% mean branch
coverage and 91.86% mean path coverage. No failed attempt is overwritten or hidden.

`fp8-issues-v4` completed on the SAME 13 cases with eight workers and no problem reruns.
It is retained in student/; content hashes match the frozen `runs/fp8-issues-v4/student/` snapshot.
These development-set experiments do not establish unseen-problem performance or regression safety
on the other 48 cases. No per-problem prompts or best-of-multiple-run suite merging were introduced.

### Independent specification-legality audit

The evaluator's tests_valid/oracle precision checks passing assertions, not input-domain legality.
A separate offline audit against the listed numeric, length, tree, grid, rectangle non-overlap and
interval constraints found 50 out-of-spec tests in the baseline's 13-suite cohort and 18 in v1.
There were no unresolved/nonliteral cases in those two audits. This count does not cover the other
48 full-run problems. Event ordering is interpreted as nondecreasing unless an explicit strict
inequality is specified. Up to 2,000 rectangles, overlap is checked pairwise. For larger cases the
audit accepts only a sufficient proof: sorted projections are pairwise disjoint along at least one
axis. Both 100,000-rectangle strip cases in v4 satisfy that proof; other large cases remain unresolved
until separately reviewed rather than being silently accepted.

Original artifacts remain unchanged. Constraint-audited copies and the exact excluded test names
are in `runs/fp8-issues-baseline-audited/` and `runs/fp8-issues-v1-audited/`, each with
`constraint-audit.json`. Their evaluations completed without new model calls. Copied usage/cost
metadata describes the original generation, not additional spend.
Reported improvements must distinguish official evaluator scores from independently audited scores;
do not count successful out-of-domain assertions as evidence of valid bug detection.

After removing out-of-spec tests, baseline scores are 34/74 bugs and 31/47 hard mutants, versus
v1's 38/74 bugs and 30/47 hard mutants. Mean branch/path coverage: baseline 90.70%/90.28%,
v1 98.40%/99.07%. Baseline has 407 oracle-valid tests and one empty suite; v1 has 511 and no
empty suites. Both audited copies have zero wrong/flaky tests or budget overflow.
These comparisons do not hide the digit-operation regression or substitute oracle precision
for input legality. The original benchmark result files are still available alongside the audits.

### Selected v4: audited quality/cost result

Both columns below exclude independently identified out-of-spec tests, replayed through the same
evaluator. The original baseline and all four original trial artifacts remain unchanged.

| Metric on the same 13 problematic cases | Full-run baseline subset | Selected v4 |
|---|---:|---:|
| Mean bug detection per problem | 40.84% | 67.72% |
| Bugs caught | 34/74 | 51/74 |
| Mean hard-mutant score | 67.07% | 79.49% |
| Hard mutants killed | 31/47 | 32/47 |
| Mean branch coverage | 90.70% | 98.40% |
| Mean path coverage | 90.28% | 98.69% |
| Oracle-valid tests after constraint audit | 407 | 456 |
| Empty suites | 1 | 0 |
| Generation stages reaching Submitted | 5/13 | 12/13 |
| Recorded model calls | 230 | 64 |
| Recorded tokens | 2,223,087 | 443,154 |
| Recorded generation cost | $0.228322 | $0.043433 |
| Out-of-spec tests found in original suites | 50 | 5 |

Recorded model cost fell 80.98% and tokens 80.07%. All four paid tuning trials together cost
$0.167785; offline constraint audits/evaluator replays made no additional model calls. Every v4
trajectory retained fp8 routing; all 64 recorded response-provider fields were SiliconFlow.
No wrong-oracle/flaky tests, source-access rejections, duplicate smells or budget overflow occurred
in the audited selected suites. Original v4 also had zero wrong/flaky/over-budget tests, but five
out-of-spec tests were excluded offline (four tree cases and one event case). Their exclusion did
not change the 51/74 bug or 32/47 hard-mutant totals. No unresolved audit cases remain in this cohort.

Seven problems improved, three were unchanged, and three lost one bug each versus the audited
baseline (lcb-3627, lcb-3632, lcb-3655). Notable gains: lcb-3629 0/5→5/5, lcb-3659 0/5→5/5,
lcb-3588 2/4→4/4, lcb-3699 1/6→4/6 and lcb-3700 2/7→5/7. Weak cases remain: lcb-3603 1/3,
lcb-3627 2/6, lcb-3653 1/3 and lcb-3657 2/7.

Limits and timing:
- lcb-3699 still hit TimeExceeded after saving a suite; it is included, not retried or discarded.
- One v4 command on lcb-3588 still wrote a /tmp generator despite the explicit prompt rule.
  Prompts are not an OS sandbox; do not claim workspace-isolation enforcement from the instruction.
- The selected generation+evaluation took 2,349.51 s (39m10s); its independent audited evaluation
  took another 1,170.46 s. Numeric/maximum-size stress cases can make evaluator runtime expensive.
  Fewer model calls and lower dollar cost are not proof of an end-to-end wall-time speedup.
- The new configuration has NOT been run on the other 48 problems. The old complete 61-problem
  evaluation belongs to a different student snapshot: do not package new student/ with those old
  results as if they measured this setup. A fresh complete run and unchanged-starter comparison
  are still required for the course report.

Evidence:
- `runs/fp8-issues-v4/`: original generation, trajectories, settings and frozen student snapshot.
- `runs/fp8-issues-v4-audited/final/evaluation.json`: selected constraint-audited scores.
- `runs/fp8-issues-v4-audited/constraint-audit.json`: exactly which five tests were excluded.
- `runs/fp8-issues-baseline-audited/final/evaluation.json`: matched constraint-audited baseline.




## Optimization work (2026-10-25)

- `probe` now renders call arguments before executing the solution and uses a fresh Solution
  instance for every input. Before the fix, an implementation that appended to its input produced
  a generated test calling the already-mutated list, with an incorrect expected value.
- `probe --output tests/test_*.py --max-calls N` now saves tests directly, including the import.
  It deduplicates literal inputs across files, avoids name collisions and limits the suite under the
  one-unparametrized-call-per-test contract. With --output, valid cases are saved while invalid inputs
  are reported with nonzero status. Stdout-only mode still fails atomically. This keeps valid work
  when the model supplies a partly invalid batch. Specification legality remains the agent's job.
- `mutants` requires a passing original suite in the same isolated layout used for mutated runs.
  Before the fix, four wrong assertions still reported every sampled mutant killed. Too few tests
  now returns failure; summaries count only mutants actually tested.
- White-box runs have one mutation pass and an explicit shared budget. When inherited tests fail,
  remove the failing cases in the workspace and add probe-derived replacements to the white-box file.
  Important harness constraint: `run_problem` restores the original black-box file after collection.
  In-place repairs alone do not survive export; the evaluator removes the original wrong assertions.
- The first combined challenger used redirected stdout and one mutation pass. The wider sample
  exposed repeated probing without saving and unbounded mutation exploration (four token-limit
  exits, including one empty suite). Do not treat its successful three-problem pilot as representative.
- The direct-writer revision (`opt-combined-v2`) asks for 30–40 initial cases, one optional append
  batch, one coverage report and no mutation exploration. The model receives compact saved-count
  feedback instead of test source to transcribe. Turn limits remain prompt instructions; the
  generated-suite cap, deduplication and naming are tool-enforced.
- Initial direct-writer verification passed 30 repository tests (130.37 s), including six helper
  regressions. Later failure-focused verification is recorded above; live comparisons follow.
- The optimization experiments below used Together, not the course fp8 route. The separate control
  and combined candidate have matching helper fixes. Reproducible snapshots and local settings are
  saved at `runs/opt-{separate,combined}-pilot/{student,settings.yaml}`; temporary copies are used
  during execution. Runs: `opt-combined-pilot`, `opt-separate-pilot`, `opt-combined-wide`,
  `opt-separate-wide`, and `opt-combined-v2`; each uses a fresh directory with no problem retries.
  The pilot covers lcb-3502, lcb-3644 and lcb-3690. The wider sample adds lcb-3588, lcb-3603,
  lcb-3627, lcb-3651 and lcb-3657 (DP, tree, graph, cyclic array, geometry).
- Selected `strategy: combined` in `student/final.yaml` after the eight-problem comparison below.
- `check_tests` and `mutants` no longer import harness internals. Their local implementations
  produced identical mutation IDs/source on all 61 problems and identical rejection decisions on
  38 saved suites. An isolated `python -I -S` smoke generated, checked, ran and mutation-scored a
  four-test suite successfully. Historic benchmark snapshots retain their original imports; the
  current standalone copies preserve those semantics without depending on forbidden harness code.
- Full fp8 evaluation `fp8-full-20261025` completed on all 61 development problems; results below.
  No completed REPORT.md exists yet. Historic three-problem costs used different providers;
  report macro averages separately from aggregate kill counts.

### Combined pilot result

All three agents reached Submitted. Generation took 137.65 s; evaluation took 102.65 s.
Recorded cost $0.053769, 310,399 tokens, 40 API calls. The suite caught 20/21 bugs and
25/27 hard mutants, with full branch/path coverage, 147 valid tests, no wrong or flaky tests,
no over-budget tests, and no detected duplicates. Per problem: lcb-3502 5/5, lcb-3644 8/8,
lcb-3690 7/8. This is not dominance over v3: v3 caught 18/21 bugs but 26/27 hard mutants.
Do not switch based solely on these three problems; compare the wider sample and matching control.

### Wider first-revision result

Generation took 412.06 s; evaluation took 39.91 s. Recorded cost $0.188670, 1,089,657 tokens.
The five suites caught 13/24 bugs and 7/21 hard mutants. Only lcb-3651 reached Submitted.
There were no wrong-oracle/flaky tests, but lcb-3603 had no tests, overall problem-averaged
branch coverage was 0.7667 and path coverage 0.7882. This revision is not the final choice.
The direct-writer revision has now been measured on all eight problems, with a saved student/settings
snapshot at `runs/opt-combined-v2/`. It caught 32/45 bugs and 35/48 hard mutants at $0.131065
and 753,730 tokens. Seven of eight agents submitted; lcb-3627 still exhausted its token budget after
ignoring the stop instructions. All eight produced valid suites: 296 valid tests, no wrong/flaky tests,
duplicates, rejected files or budget overflow. Problem-averaged branch/path coverage: 0.9792/0.9783.
Generation took 273.34 s and evaluation 161.61 s. The tree case still caught 0/3 bugs with only seven
saved tests; valid output and coverage alone are not evidence of strong bug detection.

Compared with the first combined revision over the same eight problems, this revision caught one
fewer bug (32 vs 33), three more hard mutants (35 vs 32), cost about 46% less, and had no empty suite.
These are single stochastic runs, not a proven quality ordering or an fp8 grading benchmark.

### Selected setup and comparison

The direct-writer combined configuration is the working choice: five more bugs, one more hard
mutant, 43.6% lower recorded cost and 42.2% fewer tokens than the repaired separate control.
It is not metric-wise dominance: mean path coverage is lower, and the first combined revision
caught one more bug at substantially higher cost. No unseen-problem or fp8 quality claim is made.

| Metric on the same eight problems | Repaired separate control | Selected combined direct writer |
|---|---:|---:|
| Bugs caught | 27/45 | 32/45 |
| Hard mutants killed | 34/48 | 35/48 |
| Mean branch coverage | 97.92% | 97.92% |
| Mean path coverage | 99.27% | 97.83% |
| Wrong-oracle tests | 24 | 0 |
| Tests over budget | 13 | 0 |
| Recorded tokens | 1,304,482 | 753,730 |
| Recorded cost | $0.232314 | $0.131065 |
| Problems with every generation stage Submitted | 2/8 | 7/8 |
| Sum of generation batch durations | 1,096.25 s | 273.34 s |

Timing is directional, not a controlled speed benchmark: runs overlapped, worker counts differed,
and the provider rate-limited some requests. Dollar comparisons use the same Together route and
include failed problem attempts; no problem was rerun under the same run name. Total recorded
generation spend for these three eight-problem configurations was $0.605818.

Evidence: `runs/opt-separate-{pilot,wide}/final/evaluation.json` for the control;
`runs/opt-combined-v2/final/evaluation.json` for the selected workflow. The selected benchmark
snapshot predates the standalone helper copies; the copies were checked for identical mutation
and rejection behavior and smoke-tested without harness imports. Current tool docs describe the
final CLI. Temporary student copies have been removed; persistent pilot/candidate snapshots remain.

Known limits: lcb-3603 still caught 0/3 bugs; lcb-3627 still caught 2/6 and exhausted its token budget.
The tool cannot infer specification legality, and prompt-only turn limits can still be ignored.
The selected configuration's all-61 fp8 evaluation is complete below. A matching all-61 unchanged
starter baseline, REPORT.md and submission packaging remain outstanding.

### Full fp8 run (2026-10-25)

Run: `fp8-full-20261025`, combined strategy, all 61 development problems, eight workers.
Generation and final evaluation run sequentially; the enclosing job records their total wall time.
No automatic problem reruns: failures and partial suites remain in the final evaluation.
Student configuration and active fp8 settings are frozen under `runs/fp8-full-20261025/`.
The prior Together configuration is preserved as `settings.before.yaml` there.
The first launch failed before generation because `/usr/bin/time` is not installed; the plain
commands below were then launched successfully. No problem attempts were duplicated by this retry.

Commands:

```bash
.venv/bin/python -m testagent.final --run fp8-full-20261025 \
  --student runs/fp8-full-20261025/student --workers 8
.venv/bin/python -m testagent.evaluate --run fp8-full-20261025 --mode final --workers 8
```

Status: generation and final evaluation completed successfully; all expected problem IDs are present.
End-to-end wall time: 2,700.01 s (45 minutes), including both generation and evaluation.
All 61 trajectories used `sort: throughput`, `require_parameters: true`, `quantizations: [fp8]`.
All 748 saved response-provider fields report SiliconFlow.

| Metric | Full fp8 result |
|---|---:|
| Problems evaluated | 61 |
| Mean bug detection per problem | 74.19% |
| Total bugs caught | 257/350 |
| Mean hard-mutant score | 84.37% |
| Total hard mutants killed | 194/240 |
| Mean path coverage | 97.14% |
| Mean branch coverage | 97.68% |
| Mean overall mutation score | 94.93% |
| Oracle precision | 100% |
| Valid tests | 2,441 |
| Wrong-oracle / flaky / rejected files | 0 / 0 / 0 |
| Tests over budget | 1 |
| Problems with no valid tests | 1 |
| Recorded tokens | 4,985,719 |
| Recorded model cost | $0.510879 |

Rates are evaluator problem-averages, not aggregate kill fractions; do not equate 74.19% with
257/350 or 84.37% with 194/240. Mean tokens/problem: 81,733; mean cost/problem: $0.008375.
No claim of improvement over the starter on all 61 problems is possible without a matching baseline.

Generation exit statuses: 53 Submitted, seven TokenLimitExceeded, one TimeExceeded.
The eight non-submitted attempts consumed $0.208925, approximately 40.9% of recorded model cost;
their saved tests were still evaluated, rather than excluding failures from the benchmark.

| Non-submitted problem | Exit status |
|---|---|
| lcb-3588 | TokenLimitExceeded |
| lcb-3612 | TokenLimitExceeded |
| lcb-3627 | TokenLimitExceeded |
| lcb-3632 | TokenLimitExceeded |
| lcb-3655 | TokenLimitExceeded |
| lcb-3659 | TokenLimitExceeded |
| lcb-3700 | TimeExceeded |
| lcb-3715 | TokenLimitExceeded |

The empty suite was lcb-3659 (0/5 bugs). lcb-3629 had 47 valid tests but caught 0/5 bugs;
valid oracles and high coverage do not establish useful case selection. Other weak cases:
lcb-3603 1/3, lcb-3653 1/3, lcb-3657 2/7, lcb-3699 1/6 and lcb-3700 2/7.
The sole over-budget test was on lcb-3612; the model can bypass probe's generated-test cap by
writing tests itself, so it is not a harness-enforced universal budget guard.

Authoritative artifacts:
- `runs/fp8-full-20261025/final/evaluation.json`: complete scores and per-problem diagnostics.
- `runs/fp8-full-20261025/combined/usage.json`: all generation statuses, calls, tokens and costs.
- `runs/fp8-full-20261025/combined/<problem>/trajectory.json`: individual command/model traces.
- `runs/fp8-full-20261025/student/` and `settings.yaml`: frozen measured inputs.

No student prompts/tools were changed during this run. Root settings remain on the course fp8 route.





## Provider policy

- **Current:** `settings.yaml` has been restored to the course fp8 routing block below for the
  complete run. The model, sampling parameters, token/cost/time limits and harness are unchanged.
- **Previous local iteration:** Together was pinned as a faster tool-capable endpoint. Its measured
  speed and price below are historical observations, not current provider guarantees.
- **Course/report configuration:**

  ```yaml
  provider:
    sort: throughput
    require_parameters: true
    quantizations:
      - fp8
  ```

  A backup of the shipped block is at `/tmp/settings.fp8.yaml` (regenerate by hand from above if lost).

Measured on the same problem (`lcb-3487`, blackbox): SiliconFlow 24.0 s/call, $0.105/M tokens;
Together 7.4 s/call, $0.176/M tokens. ~3.2x faster, ~1.67x the price.

## Measured baseline (3 problems: lcb-3690, lcb-3502, lcb-3644)

| Suite | Bug detection | Hard mutants | Path cov | Oracle precision | Tests | Cost |
|---|---|---|---|---|---|---|
| Starter agent (final) | 16/21 aggregate; 79.2% problem-average | 26/27 = 97.9% problem-average | 1.00 | 0.934 (11 wrong) | 155 valid + 11 wrong | $0.063 |
| Hand-written expert | 21/21 = 100% | 27/27 = 100% | 1.00 | 1.00 | 50 | $0 |

Expert suites live in `runs/manual/final/<problem>/tests/test_manual.py` (cases generated from
`/tmp/gen_baseline.py`, every expected value cross-checked against a brute-force oracle).

## A/B log (3 problems, Together provider)

| Config | Bugs | Hard | Path | Valid/wrong | Over budget | Cost |
|---|---|---|---|---|---|---|
| Starter kit | 16/21 | 26/27 | 1.00 | 155/11 | 26 | $0.063 |
| v1: `whitebox/mutants` + execution-derived oracles + 20-test cap | **12/21** | 23/27 | 0.979 | 76/6 | 0 | $0.130 |
| v2: same tools, ~`max_calls` tests, 2 diagnostic commands, one-shot write | 18/21 | 25/27 | 0.979 | 318/24 | 169 | $0.129 |
| v3: `probe` tool, mutants guard, append-not-rewrite, verify-last | **18/21** | **26/27** | 1.00 | **127/2** | **0** | **$0.092** |

v3 per problem: lcb-3502 5/5, lcb-3644 6/8, lcb-3690 7/8 (starter: 5/5, 4/8, 7/8).
Oracle precision 0.9845 vs 0.9298 for v2 and 0.9337 for the starter -- `probe` coincided with fewer
wrong values. Wall clock 6 min vs 13 min. Note `lcb-3690` blackbox aborted (`RepeatedFormatError`: the
provider returned three responses with no tool call), so its final suite is white-box only and still
scored 7/8 -- which is why `scripts/batch.sh` now retries problems that do not reach `Submitted`.

Provider mitigations added after ab3 (not evidence that those saved stages completed):

- a response with no tool call aborts that problem -> `scripts/batch.sh` retries it;
- OpenRouter sometimes reports `cost: 0`, which mini-swe-agent rejects -> `MSWEA_COST_TRACKING=ignore_errors`
  in `.env`.


Where the v2 gain comes from (per problem, `evaluate --mode blackbox` vs `--mode final`):

| Problem | starter blackbox | v2 blackbox | v2 final (+whitebox) |
|---|---|---|---|
| lcb-3502 | 5/5 | 4/5 | 4/5 |
| lcb-3644 | 4/8 | 7/8 | 8/8 |
| lcb-3690 | 7/8 | 6/8 | 6/8 |
| total | 14/21 | **17/21** | 18/21 |

So the whole gain is the **black-box prompt** (14 → 17 bugs at $0.044 and ~24 tests per problem).
The white-box changes cost $0.085 for +1 bug, while the starter white-box prompt cost $0.038 for +2.
Neither difference is outside run-to-run noise on three problems.

Budget discipline failed as prose: v2 wrote 83-121 tests per problem (169 over budget, 57 duplicates)
and ran `mutants` only once, never re-running it after writing tests. If we want the cap, it has to be
enforced by a tool returning failure that the agent must clear, not by wording.

v1 regressed and the usage lines say why: **every
run hit `TokenLimitExceeded`** (whitebox 150k, blackbox 100k) and `lcb-3644` whitebox produced no test
file at all -- 23 calls spent exploring. Two errors in v1:

1. "At most 20 tests" starves the suite. The budget is 50 **calls**, so ~50 single-assertion tests can
   be measured; the starter's 155 tests still beat 20 tests → 24 measured.
2. A written-out explore/run/repair loop costs more in re-sent transcript than it returns. Command count,
   not test quality, was the binding constraint.

v2 therefore: at most two diagnostic commands, then the whole test file in one write, expected values
captured in one `python -c` call, one `run_tests`, then submit.

## Why tests fail: the evaluator deletes the tests that would have caught the bug

Replaying the evaluator's own filters (`classify` → `within_budget` → per-bug kill mapping) shows the
agent usually *picks the right input*. The test is then deleted for a wrong expected value or for being
past the call budget, so the bug survives:

| Run | Problem | measured/dropped | bugs lost | killer tests that were dropped |
|---|---|---|---|---|
| starter | lcb-3644 | 41/2 wrong | 4 (bug_2,6,7,8) | `test_first_and_last_elements`, `test_array_with_one_positive` (wrong oracle only) |
| starter | lcb-3690 | 41/8 wrong+26 budget | 1 | `test_all_same_half_ops`, `test_two_runs_with_ops` |
| v2 | lcb-3690 | 50/14 wrong+85 budget | 2 | `test_ten_same_two_ops`, `test_twelve_zeros_three_ops` |
| v2 | lcb-3502 | 50/35 budget | 1 | `test_wb_xyxyz_k1`, `test_wb_xxyyzzww_k1` |

Smoking gun in `runs/ab2/whitebox/lcb-3690`: the correct values were printed by the agent's own probe
(`1 2 2 3 5 10` for numOps 5..0 on `"0000000000"`), and it then wrote `== 4` and `== 3` where the real
answers were `3` and `2`. Two of eleven numbers mis-copied from unlabelled whitespace-separated stdout.

Gaps found in the trajectories, and what fills them:

| Gap | Evidence | Fill |
|---|---|---|
| hand-copying values from unlabelled output | above | `whitebox/probe`: stdin of argument literals -> paste-ready test functions |
| final file never verified | lcb-3644 wrote tests, ran them, then rewrote and submitted | v3 prompt: `run_tests` is the last command |
| budget blindness | 318 tests vs 50 measured; evaluator takes a prefix and stops at the first test that does not fit | v3 prompt: at most `max_calls` tests, best first |
| `mutants` run before any tests exist | command #2-3 in every whitebox run, called once, survivors ignored | tool refuses to report when <4 tests exist; v3 prompt moves it after the first suite |
| impossible actions | blackbox lcb-3644 wasted 4 commands on `cat solution.py`, `cd /workspace && pytest`, `pwd && ls`, `pytest` | v3 prompt: never run pytest in black-box mode |
| whole-file rewrites | lcb-3502 rewrote a 100-test file 5 times | v3 prompt: append with `cat >>` |

## Gotchas

- `usage.json` is written only when a problem's agent exits; live cost is in `trajectory.json`
  (`info.model_stats`) or OpenRouter's `/api/v1/key`.
- A dropped model request is not retried: the problem sits idle until `wall_time_limit_seconds: 900`
  and blocks the whole stage (`run.py` waits for every future).
- The token limit is checked before each call, so a run can finish above the limit (seen: 105,719 / 100,000).
- The final suite is a **single 50-call pool**, not blackbox 25 + whitebox 50. Files are consumed
  alphabetically, so `test_blackbox.py` spends first and whitebox only gets the remainder.

## Black-box alone, for REPORT Q3 (2026-10-26)

`runs/ours-blackbox-together` (current student/ blackbox.yaml + list_constraints, Together, 61 problems,
`testagent.run --mode blackbox` + `evaluate --mode blackbox`): bugs 105/350 (32.96%), hard 67/240,
path 54.96%, branch 56.58%, 166 wrong oracles of 700 (precision 76.29%), 26 problems with no valid
test, $0.379. Exits: 30 Submitted, 25 RepeatedFormatError (reply without a tool call after long
reasoning), 6 TokenLimitExceeded. Our black-box prompt was never tuned and is weaker than the
starter's (starter black-box tests alone in its separate run: 195/335). Not graded: final is combined.
settings.yaml restored to fp8 after the run.

## Submission (2026-10-26)

REPORT.md trimmed to ~1,340 words (excluding table borders). Q1 now cites only evaluator numbers
(fp8 51/74 -> 55/74 on the 13 hardest; 257 -> 314/350 on all 61), not the offline replay.
Built `submission-if-life-gives-you-qwen.zip` from `runs/fp8-search-full` (student/, REPORT.md,
results/final_evaluation.json, final_tests/ for 61 problems). student/ matches that run's snapshot.
