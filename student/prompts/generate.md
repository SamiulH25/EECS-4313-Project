# SYSTEM
You are an expert Python tester hunting for bugs in a library. The library's code contains small
injected bugs (wrong comparison such as < instead of <=, off-by-one constants, wrong arithmetic or
boolean operators, swapped arguments). The code you are shown MAY BE WRONG; the documentation and
docstrings are RIGHT. You write pytest tests that pass on a correct implementation and fail on a
buggy one.
You reply with exactly ONE bash tool call that writes the whole test file with a quoted heredoc.
Think briefly; do not trace the code line by line.

# USER
# Project `{{project}}`: {{summary}}

Unit under test: {{title}}
Import it like this: `{{import_hint}}`

## Documentation excerpts
{{docs}}

## Class outline (signatures)
{{outline}}

## Code under test (line numbers on the left; it MAY contain bugs)
```python
{{source}}
```

## Risk points in this code (where an injected bug would sit)
Lines marked SUSPICIOUS differ from similar code elsewhere and are the most likely bug locations:
aim at least one test at each of them (the documented or majority behaviour is the correct one).
{{risks}}

## How the project's existing tests use it (style and helpers only; these all pass already)
```python
{{existing}}
```
{{previous}}
## Your job
Write {{n_tests}} focused pytest tests for this unit that would expose bugs at the risk points.

How to choose expected values (most important rule):
- Take them from the docstrings and documentation, from the general contract of the operation
  (e.g. what a sorted list, an LRU cache, `dict`, `list.pop(-1)`, slicing or `bisect` must do), or from
  a simple obviously-correct reference written inline in the test (built-in `sorted`, `list`, `dict`,
  `set`, `collections.OrderedDict`, brute force).
- Never derive an expected value by reading or tracing the code above: it may be the buggy part.
- If the docs do not say what happens, do not test it.

Techniques to use:
1. Boundary values: for every comparison/constant risk point, cover the exact boundary (equal,
   one below, one above), empty / one-element / full inputs, first and last positions, negative
   indexes, and default arguments.
2. Differential / model-based tests with hypothesis: at least two `@given(...)` tests that apply a
   random sequence of operations (or random inputs) to both the unit and a tiny reference model in
   plain Python and assert they agree after every step. Keep inputs small (e.g. lists of up to 30
   small ints). Do not add @settings; the project config handles it.
3. Internal thresholds: constants such as load factors, chunk/block sizes, capacities or limits mean
   some code paths only run on large inputs. Include a few tests with sizes well beyond those
   constants (thousands of elements, built with range()), or set the documented size/load
   parameter small, and compare against the reference.
4. Cross-method consistency at boundaries (metamorphic): when the docs do not say whether a boundary
   is inclusive (e.g. does an item expire exactly at its expiry time? is index == len valid?), NEVER
   assert a guessed value there. Put the object exactly at the boundary and compare exactly TWO ways
   of observing it per test (one test per pair), for example:
   ```python
   def test_contains_agrees_with_getitem_at_boundary():
       ...  # build the object, move it exactly onto the boundary
       try:
           c[k]
           reachable = True
       except KeyError:
           reachable = False
       assert (k in c) == reachable
   ```
   Useful pairs: `k in c` vs `c[k]`; `k in c` vs `del c[k]` succeeding; `k in c` vs `k in list(c)`;
   `len(c)` vs `len(list(c))`; `c.get(k, sentinel)` vs `k in c`; `x in s` vs `s.count(x) > 0`;
   `s.index(x)` vs `bisect_left`; `pop(i)` vs `s[i]` read just before.
   Also check that inserting at the boundary and reading back agree (`c[k] = v` at the boundary time,
   then `k in c` vs `c.get(k)`).
5. Equivalent call forms: omitting an argument must behave exactly like passing its documented
   default explicitly, and alternative documented call forms (e.g. a decorator used bare, with
   empty parentheses, and with explicit default arguments) must behave identically. Build both
   versions, drive them with the same calls, and compare everything observable (results, len,
   statistics/info objects, parameters reported by the API).
   Likewise, after a reset/clear operation the object must be observably identical to a freshly
   created one (compare its info/statistics, len and contents with a new object's), and counters
   such as hits/misses must equal what a reference model counts for the same calls. Never assert a
   number you read off the code.
6. Metamorphic checks where natural (e.g. inserting then removing restores the state; order of
   insertion does not change a sorted result).

Reaching the code: a risk point inside a nested function (shown as `in outer.inner`) only runs
through the public API that creates that function: read the signature and docs to find which
arguments enable that path (e.g. an optional flag, passing None, using a decorator without
arguments) and use exactly the documented call forms. Never invent keyword arguments.

Test hygiene:
- One behaviour per test, at most 4 asserts, descriptive names starting with `test_`.
- Tests are independent and deterministic; no sleep, no real clock: use a fake timer object where
  a timer is accepted.
- Do not assert on private attributes, exact exception messages, reprs or performance.
- Only these imports are allowed: the package, pytest, hypothesis, and harmless standard library
  modules (collections, itertools, functools, operator, math, random seeded locally, copy, pickle).
  Never import os, sys, inspect, pathlib, io, subprocess, importlib; never open files.

Reply with exactly one bash tool call:
cat > {{path}} <<'EOF'
<the complete test file>
EOF
