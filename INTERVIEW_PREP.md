# Interview prep: EECS4313 testing agent

Not submitted. Numbers come from `runs/fp8-search-full/final/evaluation.json` unless marked otherwise.
Details and history: `REPORT.md`, `NOTES.md`.

## 1. Pitch (30 seconds)

Our agent writes pytest suites for LeetCode problems. Qwen3.5 9B runs on mini-swe-agent with a bash
tool. The model only chooses inputs. Our `probe` tool runs the correct `solution.py` to get every
expected value. It then searches for extra inputs that kill mutants and reach new branches, and
submits. On all 61 development problems with the course fp8 settings it catches 314/350 real bugs
(88.98% per-problem mean). It kills 223/240 hard mutants and reaches 99.39% path coverage. It has
0 wrong oracles and costs $0.107 for the whole set.

## 2. How it works

Strategy: `combined` (`student/final.yaml`). One agent sees the spec and the real `solution.py`.

1. **Seed.** The first command sends 5-8 simple legal cases to `probe --no-submit` as one Python list
   expression, then runs `cat solution.py`. `probe` saves them as tests and prints an answer summary.
   This summary flags it when most answers are the same.
2. **Main batch.** One `probe` call with 20-25 cases aimed at branches, ties, resets, detours and
   every kind of answer.
3. **Checks in `probe`.** It rejects inputs that break parsed SPEC constraints, take more than 1 s or
   are too large. It deep-copies each input and uses a fresh `Solution`, so a mutating solution cannot
   corrupt the test. It repairs unbalanced brackets and removes duplicates.
4. **Search (`_augment.py`, at most 50 s).** It builds AST mutants (`_mutation.py`) and generates
   candidates:
   - reorderings and rescalings;
   - random and edited inputs shaped like the saved ones;
   - inputs at the stated limits;
   - the cases with the lowest and highest answers.

   It picks tests by greedy set cover: 3 points per new mutant killed, 1 per new branch transition or
   loop-count class, and 0.5 per new input/output feature pair. It stops at the call budget.
5. **Submit.** `run_tests --submit` runs the static checks, then pytest, then prints the completion
   marker. If the model skips step 2 and calls `run_tests --submit` itself, the same search runs first.

Most problems use 2-4 model calls.

## 3. Numbers to remember

| Metric | Starter (Together, all 61) | First full run (fp8) | Final (fp8) |
|---|---|---|---|
| Bugs caught | 240/350 (70.19%) | 257/350 (74.19%) | **314/350 (88.98%)** |
| Hard mutants | 177/240 (82.03%) | 194/240 (84.37%) | **223/240 (94.30%)** |
| Path coverage | 97.10% | 97.14% | **99.39%** |
| Branch coverage | 97.43% | 97.68% | 99.32% |
| Mutation score | 93.02% | 94.93% | 98.94% |
| Oracle precision | 88.89% (242 wrong) | 100% | 100% |
| Valid tests | 1,937 | 2,441 | 2,356 |
| Tokens per problem | 201,943 | 81,733 | 16,668 |
| Cost, all 61 problems | $2.192 | $0.511 | $0.107 |
| Agents that submitted | 66/122 stages | 53/61 | 61/61 |

- Against the starter: 74 more bugs, 46 more hard mutants, 0 vs 242 wrong oracles, 0 vs 71 over
  budget, about 1/12 of the tokens. The starter hit `TokenLimitExceeded` on 54 of its 122 stages.
- The final run took 13 min to generate and about 8 min to evaluate, with 12 workers.
- On Together, the same configuration took about 6 min to generate and caught 308/350.
- Per-problem rates are means over problems, so 88.98% is not 314/350 (89.71%).

## 4. Key decisions and the evidence for each

| Decision | Evidence |
|---|---|
| The model never writes expected values | The starter lost bugs to mis-copied values. In one run it printed `1 2 2 3 5 10`, then asserted `== 4`. With `probe`, wrong tests fell from 11 to 2 on 3 problems, and to 0 in the final run. |
| Combined over separate | On 8 problems: 32/45 vs 27/45 bugs, 0 vs 24 wrong oracles, 0 vs 13 over budget, $0.131 vs $0.232. |
| Fixed, short command flow | In the first full run, 8 of 61 agents never submitted: seven hit the token limit and one hit the time limit. They used 41% of the cost. In the final run, all 61 submitted. |
| Input variety moved from prompt to tool | Hints did not change the model's inputs. On the 13 hardest problems on fp8, the search took 51/74 to 55/74 bugs and 32/47 to 40/47 hard mutants with the same prompt. |
| Realistic-bug mutation operators | With the classic operators alone, lcb-3657 had 27 mutants and one survivor, so the search had nothing to aim at. |
| Legality rules in the search | Out-of-spec inputs are invalid tests even with a correct oracle. An audit found 50 in the earlier suites for the 13 hardest problems. |
| Hard time limits: 1 s per case, 50 s search, 50 s pytest | Everything must fit the 120 s command limit. One fp8 response once took 1,784 s, and `student/` cannot put a deadline on the request itself. |

## 5. What failed, briefly

- **"At most 20 tests"** dropped to 12/21. The budget is 50 calls, so the cap starved the suite, and
  every run hit the token limit.
- **Writing many tests at once** gave 83-121 tests per problem, with 169 over the budget.
- **A wrong-solutions-first prompt checklist** got 41/74 on the 13 hardest, but only 26/47 hard
  mutants (the baseline had 31/47). The model drifted, re-read the source and saved nothing on some
  problems.
- **Validating a whole batch at once:** one malformed tree case meant nothing was saved.
- **`/no_think`** did not turn off Qwen3.5's reasoning. It caused malformed commands and cost 2.4×.
- **Generic input hints** made no measured difference on 2 problems, so they were kept only in
  revised form.

## 6. Likely questions and answers (implementation)

Grouped by README topic.

### Final configuration and modes

**Why `strategy: combined` instead of `separate`?**
The combined agent sees SPEC.md and the real `solution.py` in one run, with a 50-call budget and a
250k-token limit. Separate would run black-box (25 calls, no code) and then white-box. On 8 problems,
combined had 0 wrong oracles against 24, 0 tests over the budget against 13, and cost $0.131 against
$0.232. Our black-box prompt alone caught only 105/350, so a separate pipeline would start weak.

**Which files does the combined agent use?**
`prompts/combined.yaml`, plus the tools in `tools/combined/` (empty), `tools/blackbox/`,
`tools/whitebox/` and `tools/shared/`. It writes `tests/test_combined.py`. `blackbox.yaml` and
`whitebox.yaml` are never used in the final run.

**Which template variables does the prompt use?**
`{{spec}}`, `{{signature}}`, `{{func_name}}` (passed to `probe --func`), `{{test_file}}` (passed to
`probe --output`), `{{max_calls}}` (passed to `--max-calls`) and `{{tools}}` (the `--help` text).

### Tools

**How does the model learn the tools?**
From `{{tools}}`: each tool prints its usage for `--help`, and the harness pastes that text into the
prompt. The prompt also gives the exact first command, so the model does not have to guess flags.

**What does one `probe` call do, end to end?**
1. It parses the input as one restricted Python expression. Only literals, comprehensions and
   `comb`/`gcd`/`isqrt` are allowed. It is evaluated with limits of 512 MiB, 5 s CPU and 10 s wall.
2. It checks that every case is a dict keyed by real parameter names. A wrongly bracketed expression
   is repaired once.
3. It checks each case against the SPEC constraints that `_input_constraints.py` parsed.
4. It runs `Solution().{func}(**case)` on a deep copy. The reference source is `exec`'d into a
   namespace, and each case has a 1 s `SIGALRM` limit.
5. It writes `def test_x(): assert Solution().f(...) == <value>`: one call and one assertion per test.
6. It skips duplicates and tests over 100,000 characters.
7. Unless `--no-submit` is given, it runs the search and then hands off to `run_tests --submit`.

**How does the agent finish?**
The harness only detects `COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` when it is the first line of
command output. `run_tests --submit` prints it first, and only after `check_tests` and pytest pass.
`probe` hands off with `os.execv`, so nothing else is printed before the marker.

**Do the tools break any rule?**
No. They read only the task folder (`SPEC.md`, `solution.py`, `tests/`) and make no network or LLM
calls. They do not import `testagent`: we rewrote `check_tests` and `mutants`, because the starter's
`check_tests` imported the evaluator. `_log.py` writes only if `$PROBE_LOG` is set, and graded runs
never set it.

### Call budget

**How do you stay inside the 50 calls?**
Every test makes exactly one call, and nothing is called at module level. `probe` stops adding
tests at `--max-calls`, and `check_tests` counts the tests before submitting. The model's cases come
first in the file and the search's cases come after them. If anything were cut, it would be the
search's cases. Result: 0 tests over the budget.

**How does the search choose which tests fill the budget?**
It runs greedy set cover over three kinds of goals:
- new mutants killed (weight 3), using AST mutants from `_mutation.py`;
- new branch transitions and loop-count classes (0, 1, 2 or 3+), traced with `sys.settrace` (weight 1);
- new input/output feature pairs (weight 0.5).

After the cover it adds the cases with the lowest and highest answers, then up to 5 inputs at the
stated limits. It stops at the deadline or after 1,500 candidates that add nothing.

### What the evaluator checks

**Why are there no rejected files?**
Generated tests only do `from solution import Solution`. They never import `os`, `inspect` or
`pathlib`, never call `open` and never contain the string "solution.py". `check_tests` applies the
same AST rules before submitting.

**Why are there no wrong or flaky tests?**
Every expected value comes from running the reference, on a deep copy with a fresh `Solution`, so
inputs mutated by the solution cannot corrupt a test. The search uses `random.Random(0)` and every
test is pure, so the evaluator's three runs in random order agree.

**How do you avoid illegal inputs, which pass the oracle but are invalid tests?**
`probe` rejects cases that break parsed bounds, lengths or `==` chains. The search also keeps three
rules:
- it never rebuilds parameters named on tree, parent, edge, graph, permutation or prime lines;
- it never reorders parameters that SPEC says are sorted;
- it keeps distinct values distinct.

These are heuristics, not a complete check of the spec.

### Limits

**How do you fit the 120 s command limit and the 15-minute wall clock?**
- Each case: 1 s.
- The model's batch: 30 s in total.
- The search: ends 50 s after `probe` starts.
- `run_tests`' own search: 35 s, with a hard stop at 45 s.
- pytest: 50 s, plus 20 s for `check_tests`.

Most problems finish in 2-4 model calls and 20-250 s. The model request itself has no deadline we
can set from `student/`.

**What about the 30 s limit per test during measurement?**
Each case already ran in under 1 s on the reference, so even a mutant 10 times slower stays under
30 s. Tests at the stated limits are capped at about 1,000 elements, or a 31×31 grid.

### Running it live

**How do you run the final configuration?**
`python -m testagent.final --run live --workers 8`, then
`python -m testagent.evaluate --run live --mode final`. On fp8 that takes about 13 min to generate
and 8 min to measure. For a quick demo, add `--only lcb-3588` (about 20 s).

## 7. Weak spots to own

- **lcb-3603 (tree, 0/3 bugs, 782 s, 268k tokens).** The model typed `parent` and `s` by hand and
  miscounted their lengths, so 18 batches were rejected. The search cannot help, because tree
  parameters are frozen. A fix would be a tree-aware generator.
- **lcb-3605 (primes, 1/5).** The "prime" rule freezes `nums`, so the search cannot change values.
  The bugs need primes with long runs of trailing ones in binary. A fix would be an edit that keeps
  values prime.
- **lcb-3627 (grid paths, 2/6 bugs on the 61-problem Together run).** The bugs need a blocked direct
  path, so that the best route goes up or left.
- The comparisons with the starter use different providers (starter on Together, final on fp8).
- The speed comes from the provider. The slowest problems are single model responses (about 128 s
  at 28 tokens/s), not our tools.

## 8. Rules compliance

- Only `student/` changed. The harness, `settings.yaml` for grading and the model are unchanged.
- Tools never read `data/`, harness internals or anything outside the task folder. They make no LLM
  or network calls.
- `check_tests` and `mutants` were rewritten so they do not import harness code.
- Tests pass the evaluator's AST checks, with no forbidden imports and no mentions of `solution.py`.
- `_log.py` writes only when `$PROBE_LOG` is set, so graded runs leave nothing behind.
