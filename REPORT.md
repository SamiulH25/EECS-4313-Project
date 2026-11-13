# Report: Samiul Hossain, Jericho Marc Mendoza, Taziz Ahsan

## 1. Team

| Name | Student number |
|---|---|
| Samiul Hossain | 220247243 |
| Jericho Marc Mendoza | 220688453 |
| Taziz Ahsan | 217967985 |

## 2. Your agent

The starter agent is one long conversation in which the model explores the shell. It spent its budget
re-reading files and on cachetools wrote no tests at all. Our agent (`student/tagent/pipeline.py`) is a
fixed pipeline. The code decides what to look at and which tests to trust; the model only writes tests.

1. **Doctest mining** (no model calls). Every `>>>` example in the docs and docstrings becomes a
   one-check pytest test, with the earlier examples replayed as setup.
2. **Inventory.** The package is split into units of at most 170 lines (a class, or a group of
   functions). Each unit gets:
   - an outline, including inherited bases and sibling variants;
   - risk points (comparisons, constants, `+=`, boolean operators, slices), labelled with the
     enclosing function (e.g. `cached.decorator.getinfo`);
   - "suspicious" lines that disagree with similar code elsewhere;
   - documentation excerpts chosen by matching API directives and member names.

   Units are ranked by risk.
3. **Generation.** Each unit gets a fresh, short conversation. It asks for 10–14 tests that take
   expected values from documentation, contracts or inline reference models, using boundary values,
   Hypothesis differential tests, equivalent call forms and two-observer consistency checks. Up to 4
   rounds run in 4 threads. Later rounds receive:
   - the earlier results (which tests passed, failed or crashed, with crash messages);
   - the coverage gaps: branches no test has exercised yet.
4. **Clean-up without the model.**
   - Files with syntax errors or forbidden names are salvaged test by test.
   - Missing imports are added.
   - Crashing tests get one repair call that includes the documented call forms.
5. **Repair check (the main filter).** Each failing test is run in-process while single small
   mutations are tried on the lines it executed. A test is kept only if one mutation makes it pass
   while three things stay true:
   - the project's existing suite still passes;
   - all passing tests ("guards") still pass;
   - the repair does not contradict sibling code or documented numbers.

   Accepted repairs are applied in later checks, so the code converges towards the correct version.
6. **Assembly.** Kept tests are rerun twice and capped at 2 per repaired location and 300 overall.

## 3. Testing techniques

| Technique | Lecture topic | Where in the code | What it does |
|---|---|---|---|
| Doctest mining | Specification-based testing / documentation as oracle | `tagent/doctests.py` `build_file` | Turns every documented example into an executable one-assertion test. |
| Mutation analysis as a test oracle filter | Mutation testing | `tagent/repair.py` `mutation_sites`, `main`; `pipeline.verify` | A failing test is believed only if a single mutation (comparison, arithmetic, boolean, constant ±1, `not`) of a covered line makes it pass without breaking the existing suite or any passing test. |
| Deviant-behaviour (sibling) analysis | Static analysis / bug patterns | `repair.py` `deviates_after`, `boundary_unsupported`, `constant_deviates_after`; `inventory.suspicious_lines` | Code is internally consistent. A repair that makes a comparison or constant disagree with its siblings is rejected, and lines that already deviate are flagged as risk points. |
| Boundary value analysis / risk-based selection | Black-box testing, BVA | `inventory.risk_points`, `inventory.build_units` (ranking) | Lists each comparison and constant with the boundary to test, and spends the budget on the riskiest units first. |
| Coverage-guided generation | White-box testing, coverage | `tagent/cov_plugin.py`, `pipeline.uncovered` | Records branch arcs of every generated test. Later rounds are told which branches were never true, never false or never reached. |
| Differential / metamorphic testing | Test oracles | `prompts/generate.md` (Hypothesis model-based tests, variant hints from `build_units`) | Compares the unit against a tiny reference model, sibling classes, and equivalent call forms. |

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
