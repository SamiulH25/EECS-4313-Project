# Tools

Your agent has one way to act: the `bash` tool that mini-swe-agent gives the model. Every tool
you build is a command line program the agent runs through bash. That is the only way to give the
agent new abilities. MCP servers and extra function-calling tools are not supported.

## Folders

- `blackbox/`: tools available in black-box mode
- `whitebox/`: tools available in white-box mode
- `combined/`: tools available in combined mode, which also sees the black-box and white-box tools
- `shared/`: tools available in every mode

If two visible folders contain a tool with the same name, the mode's own folder wins.

## Rules for a tool

1. It is an executable file (`chmod +x`) with a shebang line, for example `#!/usr/bin/env python3`.
2. `toolname --help` prints a short description and usage. The harness puts this text into the
   prompt automatically, so this is how the agent learns about your tool.
3. It runs inside the task folder. It may read `SPEC.md`, `solution.py` and `tests/`.
4. It must not call any LLM or any network service. All model calls go through the agent.
5. It may only use the Python standard library and the packages in `pyproject.toml`.
6. It must finish within the command timeout in `settings.yaml` and print compact output.
7. In black-box mode it must not try to get at the real implementation.

## Examples included

- `shared/check_tests`: checks syntax, collection and missing assertions without running tests.
- `blackbox/list_constraints`: lists numeric constraints from the spec with boundary values.
- `whitebox/run_tests`: diagnostic pytest runner; `run_tests --submit --max-calls N` checks the
  whole suite with check_tests, runs pytest, and submits automatically only when both succeed.
  Invoke submit mode alone: the completion marker must be the first command-output line.
  Submit mode rejects pytest selectors, missing/nonpositive budgets, empty or failing suites,
  forbidden source access, duplicate tests and collection counts beyond the budget.
  Checker and pytest subprocesses are bounded to 20 and 50 seconds, with timeout process cleanup.
- `whitebox/coverage_report`: shows missed lines and branches.
- `whitebox/probe`: stdin is ONE restricted Python expression returning a list of named-argument
  dictionaries, e.g. `[{"grid": [[1, 2]], "k": 2}]` or `[{"nums": [1]*n} for n in [2, 4]]`.
  This replaces JSON-line input and external generator scripts/pipelines. Parameter names must
  match the Solution method; no self, descriptions or expected-answer fields.
  Use `probe --output tests/test_combined.py --max-calls 50 <<'CASES'`, then the expression and CASES.
  Repetition, comprehensions, safe constructors and direct comb/gcd/isqrt helpers are supported.
  Assignments, imports, attributes, dunder names and arbitrary calls are rejected. Values must
  remain JSON-native: tuples/sets/ranges must be explicitly converted to lists, never coerced silently.
  Expression construction runs in a Linux worker with fixed 512 MiB address-space, five-second CPU,
  ten-second wall and 32 MiB serialized-output limits. Failure to apply limits rejects evaluation.
  This is a bounded data evaluator, not an OS sandbox for the agent's unrestricted bash commands.
  Signature binding, pre-mutation argument rendering, fresh instances, cross-file input deduplication,
  unique names and the generated-test cap remain. The cap assumes one unparametrized call per test.
  With --output, valid cases survive other entry errors; a nonempty suite hands off to
  run_tests --submit when the batch had no errors OR saved at least 10 new tests.
  `--no-submit` saves a seed suite without submitting. The expression may also be passed as one quoted argument.
  Before submitting, unused budget is filled by a search (`_augment.py`): random inputs shaped
  like the saved ones, local edits, reorderings, scaled/negated copies and inputs at the stated
  limits (bounds parsed from `low <= x <= high` lines; lists up to 1000 elements). Candidates must
  pass the constraint check and run on solution.py; a greedy cover (3 passes) keeps those that kill
  local mutants, reach new branch transitions/loop-count classes, or add input/output feature
  pairs, plus up to 3 limit-scale and 2 lowest/highest-answer cases. Parameters on Constraints
  lines naming parent/tree/edge/graph/prime/generated such that are never rebuilt; sort/order
  lines lock reordering; distinct/unique lines keep values distinct. Search stops at 50 s from
  probe start or after 1500 unproductive candidates.
  Non-submitting batches print an answer summary that flags mostly identical outcomes. Each solution call is limited to 1 s
  (slower cases rejected), case execution stops after 30 s total, and tests over 100,000
  characters are rejected, so probe plus submission fits the 120 s command limit. Do not put another
  command or printed text before the completion marker. No separate model submission turn is needed.
  Without --output, a failed batch emits no partial test code. Empty generation fails explicitly.
  Specification bounds and structural legality are not inferred automatically.
- `whitebox/mutants`: samples local AST mutations only after the original suite passes.
  Operators live in `_mutation.py` (shared with probe) and include realistic-bug ones: deleted
  call statements, max/min argument kept or swapped, dropped sorted()/key=/reverse=, dropped modulo, one element dropped from a list literal.
  A failing/empty original suite cannot establish mutant kills. Surviving mutants may be equivalent.

The checker and mutation generator are self-contained standard-library implementations; submitted
tools do not import harness internals. Pytest and coverage remain installed project dependencies.
