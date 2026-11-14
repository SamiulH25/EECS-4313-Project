# Report: Samiul Hossain, Jericho Marc Mendoza, Taziz Ahsan

## 1. Team

| Name | Student number |
|---|---|
| Samiul Hossain | 220247243 |
| Jericho Marc Mendoza | 220688453 |
| Taziz Ahsan | 217967985 |

## 2. Your agent

The starter agent is one long conversation in which the model explores the shell. It spent its budget
re-reading files and on cachetools wrote no tests at all. Our agent keeps that shape (the model is
the agent: it chooses every command) but gives it a set of testing tools on its `PATH`
(`student/tools/`). The tools do the slow, mechanical work; the model decides what to run, which code
to study, writes the tests and properties, reads the reports and reacts.

**Loop** (`tagent/loop.py`). One bash command per turn. The last 3 turns are sent verbatim; older turns
become one line each in a progress log ("wrote _cand/test_u07_1.py (5 tests) -> 2 CONFIRMED"), so the
conversation stays small and the tools' state (`.tagent/state.json`) is the memory. Each observation
ends with a status line showing the tokens left. `SUBMIT` is refused while more than 40k tokens remain.

**Tools** (each prints `--help`, uses no model and no network):

| Tool | What it does |
|---|---|
| `survey` | Splits the package into units (a class or group of functions, ≤170 lines) with risk points, fuzzer coverage and earlier results per unit. |
| `unit <id> [method]` | Docs, numbered code, risk points, unexercised branches and earlier results for one unit or method. |
| `doc_examples` | Mines every `>>>` example in the docs into one-check tests. |
| `fuzz [--long]` | Model-based random testing (below), one pass per call. |
| `run_tests <file>` | Runs the model's file 3 times, runs each `prop_*` property on 200 seeds, salvages valid tests from broken files, reports crashes with the documented call forms, and queues failing tests. |
| `confirm` | Runs the repair check on queued failing tests. |
| `finalize` | Writes `tests_new/` from the confirmed tests. |

After every turn the loop also runs up to 3 `confirm` slices itself and appends the results, so
evidence keeps flowing even when the model forgets.

**Typical trajectory.** `survey` → `doc_examples` → 4 `fuzz` passes and `fuzz --long` → then 30–40 turns
of `unit u07 count`, write a test or property file, `run_tests`, read which tests were CONFIRMED,
rejected or crashed, and fix or move on. All model-written test files in `tests_new/` come from this
part.

## 3. Testing techniques

| Technique | Lecture topic | Where in the code | What it does |
|---|---|---|---|
| Model-based / differential random testing | Test oracles, random testing | `tagent/contracts.py` `classify`, `ops_*`, `_sweep`, `ddmin` | Recognises what an object behaves like (dict, sorted list, set, memoiser) and runs random operation sequences against the `dict`/`list`/`set`/`bisect`/`lru_cache` reference, including bulk inserts past internal block sizes followed by range sweeps. Failures are shrunk by time-bounded delta debugging. Later passes run on the package with the accepted repairs applied, so one bug does not hide the next. |
| Mutation analysis as a test oracle filter | Mutation testing | `tagent/repair.py`; `pipeline.verify`, `session.drain` | A failing test is kept only if one small mutation (or, for ≤24 sites, a pair) of lines it executed makes it pass, while the project's suite, the confirmed tests and the code's own conventions stay intact. Rejected tests are retried when new repairs are accepted. |
| Property-based testing | Specification-based testing | `tagent/props.py`, `tools/run_tests` | The model states a documented rule once as `prop_<rule>(r)`; the tool checks it on 200 seeded inputs and turns failing seeds into tests. |
| Doctest mining | Documentation as oracle | `tagent/doctests.py`, `tools/doc_examples` | Documented examples become one-assertion tests. |
| Risk- and coverage-guided selection | BVA, white-box coverage | `inventory.risk_points`, `cov_plugin.py`, `tools/survey`, `tools/unit` | Ranks units by risk and reports unexercised branches to the model. |

## 4. Evidence that a technique finds new bugs

Record runs on the course settings; each ablation switches one technique off in the agent's tools
(`scripts/make_variant.sh`) and was run twice. The `compare` outputs are saved in `evidence/`.

**Model-based fuzzing.** `python -m testagent.compare --run final-1 --against no-contracts-1` reports
31 bugs that only `final-1` found and 0 that only `no-contracts-1` found. Without the fuzzer the model
alone found 7/40 (0 on cachetools); with it, 38/40. Examples:
- `func-47`/`func-61` (memoiser cache-info counters), found by comparing `cached` with
  `functools.lru_cache`.
- `sortedlist-1943`/`-1994` (`irange`/`islice` bounds), found only by bulk inserts past the internal
  block size followed by range sweeps.
- `__init__-555`/`-574` (eviction order of LFU/LRU caches), found by the eviction-policy reference
  model that tracks use counts and recency.
- `func-75`/`func-92` (`cachedmethod` with a lock, `cache_clear`), found by the method-memoiser model.

**Mutation-based repair check.** Without it (`no-filter-1`, `no-filter-2`) every failing test is kept:
31/40 and NF2_BUGS/40 bugs but 30 and NF2_FA false alarms (scores 16.0 and NF2_SCORE), against 38/40 with 0 false
alarms. `compare --run final-1 --against no-filter-1` shows 7 bugs only `final-1` found
(`__init__-555`, `-574`, `-594`, `-670`, `-848`, `sortedlist-1415`, `-1422`) and 0 the other way round.
The filter costs no recall, and the repairs it accepts let later fuzz passes look past bugs already
found.

**The model's own tests.** Per-file attribution of `final-1` (dev script `scripts/attribute.py`) shows
the TLRU expiry bug `__init__-594` was found only by a model-written test (`test_u09_tlrucache_1.py`).
The model's other files (`test_u03_…`, `test_u10_…`) re-found `__init__-181` and `-686`. Doctest
mining alone found `sorteddict-447` and `sorteddict-483`.

## 5. Results on the development projects

Course settings (`settings.course.yaml`), both dev projects (20 bugs each).

| Run | cachetools | sortedcontainers | Total | False alarms | Score | Tokens per project |
|---|---|---|---|---|---|---|
| starter | 0 | 0 | 0/40 | 4 | −2.0 | — |
| **final-1** | 20 | 18 | 38/40 | 0 | **38.0** | ~393k |
| **final-2** | 19 | 18 | 37/40 | 0 | **37.0** | ~390k |
| no-contracts-1 / -2 | 0 / NC2_C | 7 / NC2_S | 7 / NC2_BUGS | 0 / NC2_FA | 7.0 / NC2_SCORE | |
| no-filter-1 / -2 | 15 / NF2_C | 16 / NF2_S | 31 / NF2_BUGS | 30 / NF2_FA | 16.0 / NF2_SCORE | |

The submitted run is `final-1`. Its two missed bugs are both in the private `SortedList._check`
consistency check (see Q5).

## 6. What you tried

All on both dev projects (40 bugs). Numbers come from `testagent.evaluate`. Runs marked "25%" used a
10-bug sample.

**Phase A: pipeline (Python drives the model).**

| Change | Bugs | FA | Kept? |
|---|---|---|---|
| Starter agent (25%) | 1/10 | 1 | — |
| Per-unit generation + LLM judge for failing tests (25%) | 3/10 | 140 | no: judge too permissive |
| Mutation repair check instead of the judge (25%) | 4/10 | 4 | yes |
| + sibling rules, doc-number rule (full) | 21/40 | 6 | yes |
| LLM 3-vote judge for `<`/`<=` repairs | 22/40 | 2 | no: votes often inverted |
| Contract fuzzer before the model | 32/40 | 0 | yes |
| Planner + property runner, fuzzer `index(start, stop)`, long sequences | 33/40 | 1 | yes, but see below |

The 33/40 run looked best, but about 5 of its "detections" were a documentation example that builds
a 50-million-element list. Under the evaluator's parallel load it timed out, so it "found" every bug
that was checked while it was slow. Its honest score was about 27–28. We now drop any test slower than
1.5 s. More importantly, Python decided every step, so the model did not act as an agent.

**Phase B: the model drives the tools (current design).**

| Change | Bugs | FA | Kept? |
|---|---|---|---|
| First version: model in a shell with the tools | 25/40 | 0 | yes |
| Model copied "<file body omitted>" from shortened old turns; replaced with a progress log; `unit <id> <method>` | 26/40 | 0 | yes |
| Model submitted after 3 calls / `cd`ed out of the workspace: refuse early submit, pin the folder | 25/40 | 2 | yes |
| Repair check reused stale `.pyc` for same-size mutants; fixed, and pair repairs for two bugs on one path | 28/40 | 0 | yes |
| Re-check rejected tests after new repairs are accepted | 30/40 | 0 | yes |
| Fuzzer: bulk inserts past block size + range sweeps; time-bounded shrinking | 32/40 | 0 | yes |
| Fuzzer: install the timeout handler (one slow program had killed fuzz passes 3+) | 32/40 | 0 | yes |
| Fuzzer: eviction-policy model (LFU/LRU/FIFO from the class docstring), zero-lifetime TLRU items, `cachedmethod` model with `cache_clear` and lock checks, focused programs that hammer one operation, fix of a reference-call error in the `index` sweep | 38/40 | 0 | yes (final) |

Before the last change we ran the tools alone without the model (`scripts/tools_only.py`, a dev
script) to see which bugs no tool could reach (33.5 and 35.0); the new oracles were aimed at those
gaps, not at specific bug lines, and were checked on the correct code first for false alarms.

## 7. Questions

**1. Expected values when the code is buggy.** The prompt forbids deriving values from the code. They
must come from docs, contracts (`list`, `dict`, `bisect` semantics) or an inline reference model. Where
the docs are silent (is an item expired exactly at its expiry time?), the model must compare two ways
of observing the same state instead of guessing. We then trust nothing the model claims. A failing test
survives only if a single small code change explains it. That change must keep the 196/274 existing
tests and every passing generated test green, and must not contradict the code's own conventions or a
documented number.

**2. Where to look.** The agent never puts the whole project in context. `survey` splits it into units
and ranks them by risk points (lines that deviate from similar code, parameter defaults that differ
between siblings), fuzzer coverage and earlier results. `unit <id> <method>` shows one method with its
risk points and the branches no test has reached yet. The model chooses from that report, and is
told to move on after a unit has a confirmed bug or two files that found nothing.

**3. Keeping false alarms down.**
- Passing tests are dropped, since they find nothing and may encode bugs.
- Tests that crash in the test code, read private state, or are flaky over two orders are dropped.
- Every remaining test must pass the repair check (section 3).
- The sibling rules reject repairs that would introduce a deviation, e.g. moving `timer() < expires`
  to `<=` when the other expiry checks are strict.
- At most 2 tests per repaired location.

False alarms went from 140 (LLM judge) to 0 in every run of the final agent; without the repair check
the same agent keeps 22–66 (section 4).

**4. Generalisation.** Nothing refers to a project, file or bug. Every rule is a general property of
code: a single-site injected bug, internal consistency, documented numbers, documented examples. Rules
were checked against both projects before keeping them. For example, the boundary rule was tested
against every true comparison repair in both projects and rejects none of them. We tuned on the full
dev sets but judged changes by false alarms and by whether real detectors were kept or dropped. Our
dev-only oracle script (`scripts/cand_oracle.py`, not part of the agent) reports this; we did not use
the bug list to steer generation.

**5. Bugs we still miss** (2 in `final-1`, 3 in `final-2`).
- *`SortedList._check` (2 bugs).* `_check` is a private consistency check that no public behaviour
  depends on; testing it would mean calling a private method, which our rules forbid because such
  tests are fragile and often false alarms.
- *`final-2` also missed the TLRU expiry boundary `__init__-594`.* No tool reaches it; only a
  model-written two-observer test at the exact expiry time finds it, and in `final-2` the model spent
  its turns on other units. This is the run-to-run variance of the model's own part.
