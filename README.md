# Testing Agents Starter Kit

In this project you build an LLM agent that writes unit tests. The agent runs on
[mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) and uses one fixed model, Qwen3.5 9B,
through OpenRouter. It works in two modes:

- **Black-box.** The agent sees only the problem statement (without its worked examples) and the
  function signature. It has to find the equivalence classes, boundary values and input combinations
  that matter, and write tests whose expected values come from the statement.
- **White-box.** The agent also sees the code, can run it, and adds tests that reach the parts of the
  code the black-box tests missed.

Your best tests from both modes form one **final test suite** per problem. Everything outside the
`student/` folder is fixed, so the difference between teams comes from your prompts and your tools.

## What you must deliver

Upload **one file** to eClass: `submission-<team>.zip`. Build it with:

```bash
python -m testagent.package --run <your final run> --team <team>
```

The command refuses to build the zip if something is missing. The zip contains:

| In the zip | What it is |
|---|---|
| `student/` | Your prompts, your tools and `final.yaml`, the only things you change |
| `REPORT.md` | Your report, written from [REPORT_TEMPLATE.md](REPORT_TEMPLATE.md) |
| `results/` | The evaluator's output for your final configuration on all development problems |

Steps:

1. Choose your **one final configuration** in `student/final.yaml` (see below). You may try many
   things, but you submit exactly one. We run that configuration, unchanged, on problems you have not seen.
2. Run it on all development (training) problems: `python -m testagent.final --run my-final --workers 8`
3. Measure it: `python -m testagent.evaluate --run my-final --mode final`
4. Also measure the unchanged starter kit the same way, for the comparison in your report.
5. Copy `REPORT_TEMPLATE.md` to `REPORT.md` and fill it in.
6. Run `python -m testagent.package --run my-final --team <team>` and upload the zip.

For one continuous local benchmark, use the driver instead of running the stages by hand:

```bash
python scripts/final_run.py --run my-final --workers 8
```

It freezes `student/` and `settings.yaml` into `runs/<run>/` (an existing run name is refused), gives
each problem its own process group and the configured 900 s wall deadline (a blocked model call or
retry is interrupted at that deadline), merges each finished problem immediately and evaluates all of
them once at the end. `--only lcb-3487` is a smoke test. Launching pauses when the OpenRouter key
balance cannot cover another per-problem reservation; problems it cannot start are reported as unrun
and the exit code is nonzero. A timeout ends the local wait, not the provider's request, so spend can
be understated; such usage files say `billing_uncertain`. `scripts/` is a local convenience and is not
part of the graded submission, so local numbers are measurements, not the course's result.

Be ready to run your final configuration live and to answer questions about your design.

## What you can change

Only what is inside `student/`:

| Path | What it is |
|---|---|
| `student/final.yaml` | Your final configuration: `strategy: separate` or `strategy: combined` |
| `student/prompts/blackbox.yaml` | System and task prompt for black-box mode |
| `student/prompts/whitebox.yaml` | System and task prompt for white-box mode |
| `student/prompts/combined.yaml` | System and task prompt for combined mode (only if you use it) |
| `student/tools/blackbox/` | Command line tools for black-box mode |
| `student/tools/whitebox/` | Command line tools for white-box mode |
| `student/tools/combined/` | Command line tools for combined mode (it also sees the black-box and white-box tools) |
| `student/tools/shared/` | Tools available in every mode |

Do not change `testagent/`, `settings.yaml` or the model. We copy your `student/` folder into a
clean copy of this repository, so anything you change elsewhere is ignored.

Tools are ordinary command line programs. mini-swe-agent gives the model a single `bash` tool, so
the agent uses your tools by running them in the shell. MCP servers and extra function-calling tools
are not supported. Read [student/tools/README.md](student/tools/README.md) for the rules a tool must
follow. The most important ones: a tool must not call an LLM or the network, and it must print help
text for `--help`, because that text is how the agent learns about it.

Check your folder at any time with `python -m testagent.submission student`.

## The final configuration

`student/final.yaml` says how your final suite is built:

- `strategy: separate` runs your black-box agent, then your white-box agent (which starts from the
  black-box tests), and merges the two suites into one.
- `strategy: combined` runs one combined agent that does both jobs in a single run. It sees the full
  problem statement and the code, and uses `prompts/combined.yaml`.

`python -m testagent.final` builds the final suite either way and writes it to
`runs/<run>/final/<problem>/`. If you already have black-box and white-box runs, merge them with
`python -m testagent.final --run <run> --merge-only`.

### Call budget

Each suite may call the function under test a limited number of times. Only files named
`test_*.py` in `tests/` are part of a suite (other files such as `conftest.py` are ignored). Calls made
outside any test, for example at module level, are spent first. Then tests spend their calls, including
calls made in their fixtures, in order: files alphabetically (so `test_blackbox.py` comes before
`test_whitebox.py`), tests in file order. Any test past the budget is not measured. The prompts can use
`{{max_calls}}` for the budget of the current mode.

| Suite | Calls to the function under test |
|---|---|
| Black-box | 25 |
| White-box, combined and final | 50 |

Without a budget, an agent could generate thousands of random inputs and use the real code as the
oracle, which tells us nothing about testing skill. Choosing a small set of tests that finds bugs is
the actual task. Wrong tests also use up budget, so they cost you twice.

## Setup

You need Python 3.10 to 3.13 (3.12 is recommended). With [uv](https://docs.astral.sh/uv/):

```bash
uv venv -p 3.12 .venv
source .venv/bin/activate
uv pip install -e .
cp .env.example .env
```

Put your OpenRouter key in `.env`. About $10 of credit is far more than this project needs: a run of
the starter agent over all development problems in both modes costs about $1.50.

Check that everything works without spending anything (this takes a few minutes):

```bash
python -m pytest
```

How long things take: one agent run over all 61 problems takes about 15 to 30 minutes with
`--workers 8`. Measuring all 61 problems takes 30 to 70 minutes depending on your cores (use
`--workers`). While iterating, use `--only` with a handful of problems; `--max-mutants 10` makes a
measurement faster but changes the mutation numbers, so do not use it for the numbers you report.

## Running single modes

```bash
python -m testagent.run --mode blackbox --run try1 --workers 8
python -m testagent.run --mode whitebox --run try1 --workers 8
python -m testagent.run --mode combined --run try2 --workers 8
```

White-box mode starts from the black-box tests of the same run, so run black-box first. Useful options:

- `--only lcb-3610 lcb-3607` runs only those problems.
- `--student path/to/folder` uses another `student/` folder, which is handy for comparing two designs.
- `--keep-workspace` keeps the temporary folder the agent worked in.

Results are written to `runs/<run>/<mode>/<problem>/`: the test files, `trajectory.json` (every
message, command and output, plus token counts) and `usage.json` (exit status, calls, tokens, cost).

## Measuring

```bash
python -m testagent.evaluate --run try1 --mode blackbox
python -m testagent.evaluate --run try1 --mode whitebox
python -m testagent.evaluate --run my-final --mode final
```

For each problem the evaluator first rejects any test file that could read the code under test (for
example by importing `inspect`, `os` or `pathlib`, calling `open`, or mentioning `solution.py`). It
then runs your tests three times in random order on the correct solution. A test that fails is a
**wrong oracle** and a test that changes outcome is **flaky**; both are removed. The call budget is
applied to the remaining tests, and then it reports:

| Metric | Meaning |
|---|---|
| **Bug detection rate** | Share of the problem's realistic bugs that your tests catch. Every bug passes all the worked examples in the problem statement. |
| **Hard-mutant score** | Share of hard mutants your tests kill. Mutants are small automatic code changes; hard ones are those that very few strong tests detect. |
| **Path coverage** | Share of the known execution paths (which way each decision went, and whether each loop ran zero, one or many times) that your tests take. |
| Oracle precision | Share of your tests that were valid. |
| Mutation score, branch coverage | All mutants and branches. Close to the maximum for almost any reasonable suite on these problems, so treat full branch coverage as a requirement. |
| Smells, tokens, cost | For information. |

There is no combined score. Bug detection rate, hard-mutant score and path coverage are the three
main metrics. For white-box and final suites the evaluator also reports your black-box tests alone,
so you can see what white-box testing added.

## What the agent sees

Each problem gets a fresh temporary folder:

```
SPEC.md                  the problem statement and signature (no worked examples in black-box mode)
solution.py              a stub in black-box mode, the real code in white-box and combined mode
tests/test_blackbox.py   written in black-box mode
tests/test_whitebox.py   written in white-box mode
tests/test_combined.py   written in combined mode
```

Tests import the code with `from solution import Solution`.

The prompt templates are [Jinja](https://jinja.palletsprojects.com/) and can use these variables:

| Variable | Value |
|---|---|
| `{{spec}}` | Contents of SPEC.md |
| `{{signature}}` | The `def` line of the method under test |
| `{{func_name}}` | Name of the method under test |
| `{{test_file}}` | File the agent must write, for example `tests/test_blackbox.py` |
| `{{tools}}` | The `--help` text of every tool available in this mode |
| `{{max_calls}}` | The call budget of this mode (25 for black-box, 50 otherwise) |
| `{{mode}}`, `{{problem_id}}`, `{{title}}` | As named |

The agent finishes by running `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`. It is also stopped when it
reaches a limit below. Whatever tests it has written by then are kept.

## Limits per problem

| Limit | Black-box | White-box | Combined |
|---|---|---|---|
| Tokens (prompt plus completion) | 100,000 | 150,000 | 250,000 |
| Model calls | 40 | 50 | 90 |
| Cost (a safety net; the token limit is reached first) | $0.05 | $0.05 | $0.05 |
| Wall clock | 15 minutes | 15 minutes | 15 minutes |
| Single command | 120 seconds | 120 seconds | 120 seconds |
| A single test when measured | 30 seconds | 30 seconds | 30 seconds |

A test that runs longer than 30 seconds fails on its own; the rest of the suite is still measured. Any
problem the evaluator runs into (a run that did not finish, calls made outside tests) is listed under
`warnings` in its output instead of silently lowering a score.

## Data

`data/dev/` holds the 61 development (training) problems: the LeetCode problems from the LiveCodeBench
[release v5](https://github.com/livecodebench/livecodebench) (October 2024 to January 2025), except
one with a floating point answer. Each has a reference solution that passes all of LiveCodeBench's
test cases, realistic bugs, and the data the evaluator needs for mutants and paths.

Know the limits of this data:

- Problems differ a lot. Bugs per problem range from 1 to 8 (about 6 on average), and hard mutants
  from 0 to 42; 19 problems have none. Look at results per problem, not only at averages.
- These are public LeetCode problems, so the model may have seen them during training.
- Runs vary. The same agent can differ by several bugs between two runs on 20 problems, so compare
  designs over at least two runs before concluding that a change helped.

## Model settings

`settings.yaml` asks OpenRouter for the fastest provider that supports tool calling and serves the
model at fp8 precision, so every team (and our own runs) uses the same precision. It caps each response
at 8192 tokens and uses the sampling settings Qwen recommends for its reasoning mode. Temperature 0
makes this model loop in its reasoning, so do not use it.

## Running in Docker

The agent runs shell commands. To keep it away from the rest of your machine, run it in a container:

```bash
docker build -t testing-agents-starter .
mkdir -p runs && chmod a+w runs
docker run --rm --env-file .env -v "$PWD/student:/submission:ro" -v "$PWD/runs:/home/agent/harness/runs" \
    testing-agents-starter testagent.final --run my-final --student /submission --workers 8
```

## Rules

- Change only the `student/` folder. Everything else is replaced by a clean copy when we run your work.
- Your agent and your tools must not read the reference solutions, bugs or mutant data in `data/`, the
  harness internals, or anything outside the task folder, and must not call any LLM or network service.
  The evaluator also blocks tests that read the code under test, and we read every tool you submit.
- You submit exactly one final configuration, and we run it unchanged.
