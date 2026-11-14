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

(pending: filled in from the record runs `final-1/2`, `no-contracts-1/2`, `no-filter-1/2`, `starter`.)

## 5. Results on the development projects

Course settings (`settings.course.yaml`).

(pending: filled in from the record runs.)

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
| Fuzzer: install the timeout handler (one slow program had killed fuzz passes 3+) | 32/40 | 0 | yes (final) |

## 7. Questions

**1. Expected values when the code is buggy.** The prompt forbids deriving values from the code. They
must come from docs, contracts (`list`, `dict`, `bisect` semantics) or an inline reference model. Where
the docs are silent (is an item expired exactly at its expiry time?), the model must compare two ways
of observing the same state instead of guessing. We then trust nothing the model claims. A failing test
survives only if a single small code change explains it. That change must keep the 196/274 existing
tests and every passing generated test green, and must not contradict the code's own conventions or a
documented number.

**2. Where to look.** The agent never puts the whole project in context. Static analysis splits it
into units and ranks them. Ranking uses the number of risk points, lines that deviate from similar code,
and parameter defaults that differ between sibling functions. Coverage from earlier rounds then points
the model at branches nobody has reached.

**3. Keeping false alarms down.**
- Passing tests are dropped, since they find nothing and may encode bugs.
- Tests that crash in the test code, read private state, or are flaky over two orders are dropped.
- Every remaining test must pass the repair check (section 2.5).
- The sibling rules reject repairs that would introduce a deviation, e.g. moving `timer() < expires`
  to `<=` when the other expiry checks are strict.
- At most 2 tests per repaired location.

False alarms went from 140 (LLM judge) to 0–1 per full run.

**4. Generalisation.** Nothing refers to a project, file or bug. Every rule is a general property of
code: a single-site injected bug, internal consistency, documented numbers, documented examples. Rules
were checked against both projects before keeping them. For example, the boundary rule was tested
against every true comparison repair in both projects and rejects none of them. We tuned on the full
dev sets but judged changes by false alarms and by whether real detectors were kept or dropped. Our
dev-only oracle script (`scripts/cand_oracle.py`, not part of the agent) reports this; we did not use
the bug list to steer generation.

**5. Bugs we still miss.**
- *`func.py` default `maxsize=128` changed to 127/129 (6 bugs).* They are only reached when a decorator
  is used bare (`@lru_cache`). The docs explain this, but in every run the model wrote
  `lru_cache(maxsize=lambda x: ...)` or invented a `callable=` argument. The trajectories show
  `TypeError ... takes 0 positional arguments`. The tests crash in the test code and are dropped,
  correctly, and the crash-repair call rarely finds the right form.
- *TTL/TLRU `__setitem__`/`__delitem__` boundary `<` → `<=`.* The model keeps asserting that an item is
  still valid exactly at its expiry time. The repair check rejects those repairs (the sibling boundary
  rule), so we avoid the false alarm. But the model seldom writes the two-observer test (`k in c` vs
  `del c[k]` at the boundary) that would catch these bugs.
