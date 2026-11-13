# Report: Samiul Hossain, Jericho Marc Mendoza, Taziz Ahsan

## 1. Team

| Name | Student number |
|---|---|
| Samiul Hossain | 220247243 |
| Jericho Marc Mendoza | 220688453 |
| Taziz Ahsan | 217967985 |

## 2. Your agent

The starter agent is one long conversation in which the model explores the shell. It spent its budget
re-reading files and on cachetools wrote no tests at all. Our agent (`student/tagent/pipeline.py`) splits
the work: deterministic tools do the exploring and the checking, and the model decides where effort
goes and states the rules that tools cannot infer.

1. **Doctest mining** (tool). Every `>>>` example in the docs becomes a one-check test.
2. **Contract fuzzer** (tool, `tagent/contracts.py`). It finds the public classes and functions, works out
   how to construct them from parameter names, and recognises what each object behaves like (a dict,
   a sorted list, a set, an `lru_cache`-style memoiser, a hash function). It then runs thousands of random
   operation sequences against the matching standard-library reference, plus observer-consistency
   contracts (`k in m` agrees with `m[k]`, `len` agrees with iteration). Failures are shrunk with delta
   debugging and kept only if they fail 3 times out of 3. Later passes run on a copy of the package with
   the repairs accepted so far applied, so one bug does not hide the next. A final pass writes long
   regression sequences that pass on that repaired copy.
3. **Model in the loop.** The package is split into units (a class or a group of functions, ≤170 lines)
   with an outline, risk points and documentation excerpts. Each of up to 4 rounds:
   - **Plan** (one model call): the model reads a one-line tool report per unit (risk points, whether
     the fuzzer drove it, earlier results) and picks the units and a mode for each: `props`, `tests`
     or `skip`, with a one-sentence focus.
   - **Generate** (one fresh, short call per unit, 4 threads). In `props` mode the model writes documented
     rules as `prop_<rule>(r)` functions of a seeded random generator; `tagent/props.py` runs each on
     200 seeds and failing seeds become ordinary tests. In `tests` mode it writes example tests.
   - **Feedback**: which properties held, failed on some inputs, or crashed through API misuse, plus
     uncovered branches, goes into the next round's plan and prompts.
4. **Clean-up without the model.** Broken files are salvaged test by test, imports are added,
   `@settings` without `@given` is removed, and crashing tests get one repair call with the documented
   call forms.
5. **Repair check (the main filter).** A failing test is kept only if a single small mutation of a line it
   executed makes it pass, while the project's suite, all passing tests and the code's own conventions
   (sibling comparisons, documented numbers) stay intact. Accepted repairs carry into later checks.
6. **Assembly.** Kept tests are rerun twice; at most 2 per repaired location (per class for fuzzer
   tests) and 300 overall. Jobs reserve their expected tokens so parallel calls cannot overrun the budget.

## 3. Testing techniques

| Technique | Lecture topic | Where in the code | What it does |
|---|---|---|---|
| Model-based / differential random testing | Test oracles, random testing | `tagent/contracts.py` `classify`, `ops_*`, `fuzz`, `ddmin` | Random operation sequences checked against `dict`/`list`/`set`/`bisect`/`functools.lru_cache`, shrunk by delta debugging. |
| Property-based testing | Specification-based testing | `tagent/props.py`, `pipeline.run_props`, `prompts/generate.md` | The model states documented rules once; the tool checks them on 200 random inputs. |
| Mutation analysis as a test oracle filter | Mutation testing | `tagent/repair.py`; `pipeline.verify` | A failing test is believed only if one mutation of a covered line explains it without breaking the suite or any passing test. |
| Deviant-behaviour (sibling) analysis | Static analysis / bug patterns | `repair.py` `deviates_after`, `boundary_unsupported`; `inventory.suspicious_lines` | Repairs that make code disagree with its siblings are rejected; deviating lines are flagged as risk points. |
| Doctest mining | Documentation as oracle | `tagent/doctests.py` | Documented examples become one-assertion tests. |
| Risk- and coverage-guided selection | BVA, white-box coverage | `inventory.risk_points`, `cov_plugin.py`, `pipeline.plan` | Ranks units by risk; the planner and later rounds target unexercised branches. |

## 4. Evidence that a technique finds new bugs

(pending: filled in from the record runs `final-1/2`, `no-doctests-1/2`, `no-filter-1/2`, `starter`.)

## 5. Results on the development projects

Course settings (`settings.course.yaml`).

(pending: filled in from the record runs.)

## 6. What you tried

All on both dev projects (40 bugs), iteration runs pinned to one provider for speed. Numbers come from
`testagent.evaluate`. Runs marked "25%" used a 10-bug sample (5 per project).

| Change | Bugs found | False alarms | Kept? |
|---|---|---|---|
| Starter agent (25% sample) | 1/10 | 1 | — |
| Per-unit generation + LLM judge for failing tests (25%) | 3/10 | 140 | no: judge too permissive |
| Mutation repair check instead of the judge (25%) | 4/10 | 4 | yes |
| + screening profile, doc-number and constant-sibling rules (full) | 21/40 | 6 | yes |
| LLM 3-vote judge for `<`/`<=` repairs | 22/40 | 2 | no: votes were often inverted; replaced by the sibling boundary rule |
| Sibling boundary rule, in-process parametrize/fixtures, crash feedback | 24/40 | 0 | yes |
| Doc excerpts by API directive, crash repair with docs | 23/40 | 0 | yes |
| Salvage valid tests from broken files | 22/40 | 0 | yes (recovered 10 lost files per project) |
| Fix repair-site offsets drifting after accepted fixes | 24/40 | 1 | yes (bug in our tool: one bad fix disabled all later checks) |
| Prompt: reset equals fresh object; counters via reference model | 25/40 | 4 | yes, with the private-access fix below |
| Treat `getattr(obj, "_private")` as private-state access | 17/40 | 0 | yes (FA fix; the recall drop is run-to-run variance: sortedcontainers kept as many tests) |
| Contract fuzzer before the model (tools alone, no model calls) | 32/40 | 0 | yes |
| Fuzzer + old model loop | 32/40 | 4 | yes |
| Planner + property runner (model in the loop) | 30/40 | 5 | changed: `@settings` without `@given` slipped through |
| Strip pointless `@settings`, per-class cap for fuzzer tests, token reservation | 31/40 | 2 | yes |
| Fuzzer: set `count`, `index(start, stop)`, colliding keys, long regression sequences | 33/40 | 1 | yes (final) |

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
