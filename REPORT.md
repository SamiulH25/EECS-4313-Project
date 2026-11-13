# Report: If Life Gives You Qwen You Make A Testing Harness

## 1. Team

- Samiul Hossain, 220247243
- Jericho Marc Mendoza, 220688453
- Taziz Ahsan, 217967985

## 2. Final configuration

Strategy in `student/final.yaml`: combined

The model chooses the inputs; every expected value and every check comes from a tool that runs the
reference.

1. The model sends 5-8 simple legal cases to `probe --no-submit` as one Python list expression, then
   runs `cat solution.py`. `probe` runs the reference on each case and saves one-assertion tests. It
   also prints a summary of the answers.
2. The model sends one batch of 20-25 cases aimed at the code's branches, ties and resets, and at
   every kind of answer.
3. `probe` rejects cases that break SPEC constraints, take longer than 1 s or are oversized, and then
   searches for at most 50 s and adds tests until the call budget is full.
   - Mutants: it builds AST mutants of `solution.py`.
   - Candidate inputs: reorderings, rescalings, random and edited inputs, inputs at the stated limits,
     and the cases with the lowest and highest answers.
   - Selection: it keeps candidates by greedy set cover over mutant kills, new branches or loop
     counts, and new input/output pairs.
4. `run_tests --submit` runs the static checks and pytest, then submits. If the model skips step 2 and
   calls `run_tests --submit` itself, the search still runs first.

## 3. Prompts and tools

| Part | Mode | What it does | Why it is there |
|---|---|---|---|
| `prompts/combined.yaml` | combined | Three fixed steps (seed, one batch, repair once). It bans solving, hand-tracing and handwritten assertions, and gives short input hints. | Earlier agents reasoned until they ran out of tokens. |
| `whitebox/probe` | combined | Turns cases into tests whose oracles come from the reference. Deep copies, 1 s per case, deduplication, constraint checks, bracket repair, auto-submit. | Mis-copied values and tests past the budget were losing the starter its bug-finding tests. |
| `whitebox/_augment.py` | combined | The search and greedy cover from section 2. | The model's inputs were small, sorted and similar. |
| `whitebox/_mutation.py`, `mutants` | combined | AST mutants, including realistic-bug operators (deleted call, one-argument `max`/`min`, dropped sort key or modulo). | With only the classic operators, lcb-3657 had one surviving mutant, so the search had nothing to aim at. |
| `_input_constraints.py`, `list_constraints` | shared | Parses SPEC ranges, lengths and `==` chains into checks. | An audit found 50 out-of-spec tests in the earlier suites for 13 problems. |
| `run_tests`, `check_tests` | shared | Static rules, the call budget, pytest within the 120 s command limit, submission. | One safe way to submit. |

## 4. Results on the development problems

The final run is `runs/fp8-search-full` (fp8 course settings). Rates are the evaluator's means per
problem, with total counts in brackets. The starter is the unchanged course kit (`separate` strategy).

| Metric | Starter baseline | Our first full run (fp8) | Our final configuration (fp8) |
| --- | --- | --- | --- |
| Bug detection rate | 70.19% (240/350) | 74.19% (257/350) | **88.98% (314/350)** |
| Hard-mutant score | 82.03% (177/240) | 84.37% (194/240) | **94.30% (223/240)** |
| Path coverage | 97.10% | 97.14% | **99.39%** |
| Oracle precision | 88.89% (242 wrong) | 100% | 100% |
| Branch coverage | 97.43% | 97.68% | 99.32% |
| Tokens per problem | 201,943 | 81,733 | 16,668 |
| Cost for all problems (USD) | $2.192 | $0.511 | $0.107 |

All 61 final agents submitted, with no wrong, flaky, over-budget or rejected tests. In the starter,
54 of 122 stages hit the token limit, 71 tests fell past the budget and one problem had no valid test.

## 5. What you tried

Each row compares single runs on the same problems; noise is several bugs.

| Change | Bug detection | Hard-mutant score | Path coverage | Kept? |
| --- | --- | --- | --- | --- |
| 3 problems : starter, then `mutants` tool + "at most 20 tests" | 16/21, then 12/21 | 26/27, then 23/27 | 100%, then 97.9% | no |
| 3 problems : `probe` writes the tests from the reference | 18/21 | 26/27 | 100% | yes |
| 8 problems : separate, then combined strategy | 27/45, then 32/45 | 34/48, then 35/48 | 99.27%, then 97.83% | combined |
| 13 hardest: round 3 suites, then a wrong-solutions-first prompt | 34/74, then 41/74 | 31/47, then 26/47 | 90.28%, then 84.16% | no |
| 13 hardest: v4, a short fixed command flow | 51/74 | 32/47 | 98.69% | yes |
| 13 hardest: v4 + search inside `probe` | 55/74 | 40/47 | 99.55% | yes |
| lcb-3657: "think briefly", then adding `/no_think` | 2/7, then 4/7 | 2/2, then 2/2 | 100%, then 100% | no |
| 2 problems: no hints, then generic input hints | 4/9, then 4/9 | 5/12, then 5/12 | same | revised |
| 61 problems: first full run, then final configuration | 257/350, then 314/350 | 194/240, then 223/240 | 97.14%, then 99.39% | yes |

What we learned:

- At first, bad oracles and the call budget lost the most bugs. The starter often picked a killing
  input, but that test was then dropped for a wrong value or for falling past the budget.
- Long prompts made the 9B model drift. The wrong-solutions-first checklist lost 5 hard mutants.
  `/no_think` did not stop reasoning and broke commands. Only the limits enforced by tools held;
  prompt-only instructions were ignored.
- Then the model's inputs were the limit. On lcb-3657, 20 of 22 inputs were already sorted, and
  reversing them kills 6 of 7 bugs. Hints barely helped, so input variety had to move into the tool.

## 6. Design questions

Q1. Biggest improvement: the search inside `probe`. On the 13 hardest problems, with the prompt
unchanged and both runs on fp8, it killed 8 more hard mutants (32/47 to 40/47); bugs found moved from
51/74 to 55/74, which is inside the run-to-run noise of section 5. On all 61 problems the final
configuration went from 257/350 to 314/350. Most model calls in the final run are the same two `probe`
commands as before, so the gain is in the tool rather than in the prompt.

Q2. Expected values in black-box mode. `prompts/blackbox.yaml` tells the model to work out each
value twice by two different methods and to drop the test if the two disagree. `list_constraints`
suggests only legal boundary values. This works poorly at 9B: our black-box agent alone
(`runs/ours-blackbox-together`) wrote 166 wrong tests out of 700. In 25 of 61 runs it also reasoned
until it replied without a tool call and was stopped. That is why the final configuration is combined.
`probe` takes every value from running `solution.py` on a deep copy with a fresh `Solution`, dropping
any case the constraint checks reject. The search never rebuilds structured parameters (tree, parent,
edge, permutation, prime), so those values stay sorted and distinct. That leaves 0 wrong tests out of
2,356, against 242 for the starter.

Q3. What white-box adds. We compared our combined agent with our black-box prompt and tools alone,
both on Together, on all 61 problems.

| Metric | Black-box alone | Combined |
|---|---|---|
| Bugs caught | 105/350 | 308/350 |
| Hard mutants | 67/240 | 218/240 |
| Path coverage | 54.96% | 99.52% |
| Wrong-oracle tests | 166 | 0 |
| Problems with no valid test | 26 | 0 |

With the code, oracles come from execution, mutants and branch tracing guide the search, and the
agent finishes in 2-4 commands. Our black-box prompt was never tuned: the starter's black-box tests
alone caught 195/335 bugs, so part of that gap is an untuned black-box prompt.

Q4. Choosing tests within the budget. The model's cases are kept first; greedy set cover fills the
remaining slots at 3 points per new mutant killed, 1 per new branch or loop count and 0.5 per new
input/output pair. Then come the lowest and highest answers and up to 5 cases at the stated limits.
`probe` caps the file and `check_tests` verifies it, so no test fell past the budget.

Q5. Where the agent still does badly.

- lcb-3603 (tree from `parent` and `s`; 0/3 bugs, 782 s). The model typed both by hand and
  miscounted their lengths, so 18 batches were rejected for `n == parent.length == s.length`. The
  search does not rebuild trees, so the suite kept only small chains and stars. The bugs need deep,
  unbalanced trees with repeated letters. A tree-aware generator would help.
- lcb-3605 (primes; 1/5 bugs). The model sent 26 one-element arrays of small primes. Because the
  spec says the values are prime, the search may only reorder them. The bugs need primes with long runs
  of trailing ones in binary. An edit that keeps values prime would find them.
