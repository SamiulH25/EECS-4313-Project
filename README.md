# Testing Agents, Phase 2: Finding Bugs in Real Projects

In phase 1 your agent tested single functions. In phase 2 it tests whole Python projects. Each project
comes with its code, its documentation and an existing test suite that passes. The code contains bugs
that the existing tests do not catch. Your agent has to find them by writing new tests. For the
held-out projects, which decide your mark, we do not tell you where the bugs are or how many there are.

The model is the same as in phase 1 (Qwen3.5 9B through OpenRouter), with a fixed token budget per
project. This time the agent itself is yours: you can change the loop, not only the prompts and tools.

## The task

For each project, your agent writes new tests in `tests_new/`. We measure them like this:

- A test that **fails on the correct code** is a **false alarm**. It reports a bug that is not there.
- A bug is **found** if at least one of your tests passes on the correct code and fails when that bug is
  added. Each bug is measured on its own.
- **score = bugs found - 0.5 x false alarms.** Only the first 300 tests (in collection order) count.

The penalty matters. Your agent sees the buggy code, so running the code tells it what the code does now,
not what it should do. Expected values have to come from the documentation, docstrings, and your own
reasoning about what the code promises. Writing many guesses and hoping some are right costs you.

Your mark for the results comes only from **held-out projects** that are not in this repository. They
are different projects, with different bugs. Tuning your agent to the bugs of the development projects
will not help, so build a general tool.

## What you must do

1. **Build your agent** in `student/`. You can change the loop in `student/agent.py`, the prompts and the
   tools.
2. **Implement at least two testing techniques from the lectures** in the agent or the tools, not only
   in the prompt. Examples: equivalence partitioning and boundary value analysis computed by a tool,
   property-based testing with Hypothesis, metamorphic testing, differential testing, model-based or
   state-based testing for classes, combinatorial testing, specification mining from docstrings, or test
   selection by risk. Each one must be real code that changes what your agent does.
3. **Show that at least one of these techniques finds bugs** that the starter agent and the existing tests
   did not find. Use `python -m testagent.compare` (see below) on the development projects.
4. **Submit** one zip with your agent, your report and your results (see "What you must deliver").

## What you can change

| Path | What it is |
|---|---|
| `student/agent.py` | Your agent. The harness calls `solve(task, model, env)` once per project. |
| `student/prompts/` | Your prompts, in any format your agent reads |
| `student/tools/` | Command line tools your agent can run in the shell |
| `student/*.py` | Any other modules your agent needs |

Do not change `testagent/`, `settings.yaml`, `data/` or the model. We copy your `student/` folder into a
clean copy of this repository and run it, so anything you change elsewhere is ignored.

### What `solve(task, model, env)` gets

- `task`: the project name, package and summary, the workspace folder, the help text of your tools, the
  token budget, the step limit and the test limit. See class `Task` in `testagent/run.py`.
- `model`: the course model, wrapped in a token budget. Call `model.query(messages)` as often as you like.
  It raises `LimitsExceeded` when the project's budget is used up. **This is the only model you may use.**
  You may run several conversations with it (for example a planner and a test writer), in sequence or in
  threads, as long as every call goes through `model`.
- `env`: a shell in the workspace. `env.execute({"command": "..."})` runs a command there.

The starter agent is plain mini-swe-agent: one conversation with one prompt, in which the model uses the
shell. Read it first: it is about 40 lines.

### Tools

Tools are executable command line programs in `student/tools/`. The harness puts them on the PATH and
puts each tool's `--help` text into `task.tools`, which the starter prompt shows to the model. A tool
must print help for `--help`, must not call an LLM or the network, and may only use the standard library
and the packages in `pyproject.toml`. `check_tests` is included as an example.

## What the agent sees

Each project gets a fresh workspace:

```
<package>/      the project's code, bugs included
README.rst      the project's documentation
docs/           more documentation, where the project has it
tests/          the existing tests (all pass)
tests_new/      empty; your new tests go here
```

The agent may read and run anything in the workspace. It must not read anything outside it.

## Development projects

| Project | What it is | Bugs | Existing tests |
|---|---|---|---|
| `cachetools` | Memoizing collections and decorators: LRU, LFU, TTL, FIFO, MRU and RR caches | 20 | 196 |
| `sortedcontainers` | Sorted list, sorted dict and sorted set types | 20 | 274 |

How we made them: we took a release of each project, put in small bugs (wrong comparisons, off-by-one
constants, wrong arithmetic or boolean operators), and kept a bug only if the project's own tests
caught it. Then we removed the tests that caught the bugs, so the existing suite passes with every bug
in place. The held-out projects are built the same way, but the bugs there may be of other kinds too.

The development projects are for building and measuring your agent, so nothing about them is secret.
The evaluator needs their correct code and their list of bugs, which are in `data/dev/`, and it reports
how many bugs you found out of how many there are. Your agent and your tools must not read `data/`.
You can easily find the development bugs yourself, but that does not help you: they are not marked. The
held-out projects can have a different number of bugs, and your agent will not be told how many.

## Setup

You need Python 3.10 to 3.13 (3.12 is recommended). With [uv](https://docs.astral.sh/uv/):

```bash
uv venv -p 3.12 .venv
source .venv/bin/activate
uv pip install -e .
cp .env.example .env
python -m pytest
```

Put your OpenRouter key in `.env`. The token budget is 400,000 tokens per project, which costs a few
cents with this model.

**Use Docker.** Your agent runs commands and your tests run the project's code. Run both in a container:

```bash
docker build -t phase2 .
mkdir -p runs && chmod a+w runs
docker run --rm --env-file .env -v "$PWD/student:/home/agent/harness/student:ro" \
    -v "$PWD/runs:/home/agent/harness/runs" phase2 testagent.run --run try1 --workers 2
docker run --rm --network none -v "$PWD/runs:/home/agent/harness/runs" phase2 testagent.evaluate --run try1 --workers 8
```

Without Docker the same commands are `python -m testagent.run --run try1` and
`python -m testagent.evaluate --run try1`.

## Commands

| Command | What it does |
|---|---|
| `python -m testagent.run --run <name>` | Runs your agent on every development project. Add `--only cachetools` for one project, `--student <folder>` for another version of your agent, `--keep-workspace` to look at the workspace afterwards. |
| `python -m testagent.evaluate --run <name>` | Measures the tests of a run. Writes `runs/<name>/evaluation.json`. |
| `python -m testagent.compare --run <a> --against <b>` | Lists the bugs run `a` found and run `b` did not, and the other way round. |
| `python -m testagent.package --run <name> --team <team>` | Builds your submission zip. |

Results of a run are in `runs/<name>/<project>/`: `tests_new/` with the tests, `trajectory.json` with
every message (if your agent saves one), and `usage.json` with tokens and cost.

### How to show a technique works

Run your agent once with the technique and once without it (for example with a copy of `student/` in
which the technique is turned off), evaluate both, and compare:

```bash
python -m testagent.run --run with-x
python -m testagent.run --run without-x --student student_without_x
python -m testagent.evaluate --run with-x
python -m testagent.evaluate --run without-x
python -m testagent.compare --run with-x --against without-x
python -m testagent.compare --run with-x --against starter
```

The model is not deterministic, so run each version at least twice before you conclude anything.

## Limits

| Limit | Value |
|---|---|
| Tokens per project (prompt plus completion) | 400,000 |
| Steps for the starter loop | 120 |
| A single shell command | 120 seconds |
| Tests that count | the first 300 |
| A single test when measured | 10 seconds |
| The whole new suite when measured | 300 seconds |

## What the evaluator rejects

A test file is ignored completely if it could read the code under test: if it imports `os`, `sys`,
`inspect`, `pathlib`, `importlib`, `io`, `subprocess` or similar modules, calls `open`, `exec` or `eval`,
or uses attributes such as `__file__` or `__code__`. During measurement, test code that still tries to
open the project's source files or read its code objects fails, and counts as a false alarm.
`check_tests` tells you about these problems before you measure.

## What you must deliver

Upload one file to eClass: `submission-<team>.zip`, built with
`python -m testagent.package --run <final run> --team <team>`. It contains `student/`, `REPORT.md`
(written from `REPORT_TEMPLATE.md`) and the evaluation of your final run on the development projects.
We run your `student/` folder unchanged on the held-out projects. Be ready to run your agent live and
to explain your design.

## Rules

- Change only `student/`. Everything else is replaced by a clean copy when we run your work.
- Every model call goes through the `model` you are given. No other LLM, no network access from your
  agent or tools apart from that model, and no hard-coded knowledge of particular bugs.
- Your agent and tools must not read `data/`, the harness internals, or anything outside the workspace.
- Tests must test behaviour. Tests that try to read the code under test are rejected.
- We read every agent and tool you submit.
