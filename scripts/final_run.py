"""Run every development problem through one frozen student configuration, one process per problem.

Local driver only: what is graded is the course's own run of `student/` on held-out problems. This
script exists so a full local benchmark survives a single problem stalling:

    python scripts/final_run.py --run my-final --workers 8
    python scripts/final_run.py --run smoke --only lcb-3487 --workers 1

`student/` and `settings.yaml` are copied into `runs/<run>/` before anything starts and an existing
run directory is never reused. Every problem then runs in its own process group with the configured
wall deadline, and its suite is merged as soon as that process returns. Evaluation runs once at the
end over whatever finished. A parent-side process-group kill at wall + command timeout + 60 s is the
last resort for a child that still has not returned. The OpenRouter key balance is checked before
launching and while problems are in flight; if the balance cannot cover another per-problem
reservation the remaining problems are reported as unrun and the exit code is nonzero.

The per-problem reservation is the fixed cost limit from `settings.yaml`, not a provider-side cap:
cancelling a blocked request stops the wait, not the remote generation, so a timed-out problem's
recorded spend can understate the bill (`billing_uncertain` in its usage.json says so). The balance
endpoint can also lag provider-side spend, so treat it as a guardrail, not an exact ledger.
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from testagent import REPO_ROOT  # loads the repo .env before mini-swe-agent loads its global one
from testagent.final import load_strategy, merge_problem
from testagent.problems import Problem, load_problems
from testagent.run import RUNS_DIR, build_model, load_settings, run_problem
from testagent.workspace import TEST_FILES

from minisweagent.exceptions import TimeExceeded

SCRIPT = Path(__file__).resolve()
DEV_PROBLEMS = REPO_ROOT / "data" / "dev"
BALANCE_URL = "https://openrouter.ai/api/v1/auth/key"
BALANCE_REFRESH_SECONDS = 15.0
STARTUP_BALANCE_TIMEOUT = 15
CANCEL_GRACE_SECONDS = 5.0


def key_balance(timeout: float = 15) -> dict | None:
    """Remaining credit on the OpenRouter key, without ever keeping or printing the key itself."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return None
    request = urllib.request.Request(BALANCE_URL, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.load(response).get("data", {})
    except Exception as error:  # an advisory check must not block a run
        return {"error": f"{type(error).__name__}: {error}"}
    limit, usage = _number(data.get("limit")), _number(data.get("usage"))
    remaining = limit - usage if limit is not None and usage is not None else None
    return {"limit": limit, "usage": usage, "remaining": remaining}


def _number(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def budget_slots(balance: dict | None, reservation: float, in_flight: int) -> int | None:
    """How many more problems the key balance allows, or None when the balance is unknown."""
    if not balance or balance.get("remaining") is None:
        return None
    available = float(balance["remaining"]) - in_flight * reservation
    return max(0, int(available // reservation))


class QueryTimeout(KeyboardInterrupt):
    """Signal payload for a blocked model query.

    It derives from KeyboardInterrupt so the model's tenacity retry policy (`abort_exceptions`
    includes KeyboardInterrupt) re-raises it instead of sleeping through a backoff.
    """


def _raise_query_timeout(signum, frame):
    raise QueryTimeout("model query exceeded the problem wall deadline")


def _time_exceeded() -> TimeExceeded:
    return TimeExceeded(
        {"role": "exit", "content": "TimeExceeded", "extra": {"exit_status": "TimeExceeded", "submission": ""}}
    )


class DeadlineModel:
    """A model whose every query is cut off at one absolute per-problem deadline.

    The timer runs in the child's main thread and interrupts a blocked HTTP call or retry sleep;
    the model's own retry loop aborts on KeyboardInterrupt. Everything else is delegated untouched.
    """

    def __init__(self, model, deadline: float):
        self.model = model
        self.deadline = deadline
        self.timeouts = 0

    @property
    def timed_out(self) -> bool:
        return self.timeouts > 0

    def query(self, messages: list[dict], **kwargs) -> dict:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            self.timeouts += 1
            raise _time_exceeded()
        previous = signal.signal(signal.SIGALRM, _raise_query_timeout)
        signal.setitimer(signal.ITIMER_REAL, remaining)
        try:
            return self.model.query(messages, **kwargs)
        except QueryTimeout:
            self.timeouts += 1
            raise _time_exceeded() from None
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)

    def __getattr__(self, name):
        return getattr(self.model, name)


def freeze_run(run_dir: Path, student: Path, settings_path: Path) -> None:
    """Copy the student configuration and settings into the run; never reuse a name."""
    run_dir.mkdir(parents=True, exist_ok=False)
    try:
        shutil.copytree(student, run_dir / "student", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(settings_path, run_dir / "settings.yaml")
    except BaseException:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise


def run_problem_child(
    problem: Problem,
    modes: list[str],
    run_dir: Path,
    *,
    student: Path,
    settings: dict,
    model: DeadlineModel,
) -> dict[str, dict]:
    """Run one problem's mode(s) in this process and record where its suite ended up."""
    results = {}
    for mode in modes:
        out_dir = run_dir / mode / problem.id
        blackbox_tests = run_dir / "blackbox" / problem.id / "tests" / TEST_FILES["blackbox"]
        timeouts_before = model.timeouts
        usage = run_problem(
            problem,
            mode,
            out_dir,
            model=model,
            settings=settings,
            prompts_dir=student / "prompts",
            tools_root=student / "tools",
            blackbox_tests=blackbox_tests if mode == "whitebox" else None,
        )
        if model.timeouts > timeouts_before:
            # The provider may still bill a request whose client wait we cut off.
            usage["billing_uncertain"] = True
            (out_dir / "usage.json").write_text(json.dumps(usage, indent=2))
        results[mode] = usage
        print(
            f"[{mode}] {problem.id}: {usage['exit_status']}  calls={usage['api_calls']}"
            f"  tokens={usage['total_tokens']}  cost=${usage['cost_usd']:.4f}"
            f"  files={usage['test_files']}  billing_uncertain={usage.get('billing_uncertain', False)}",
            flush=True,
        )
    return results


def stop_group(process: subprocess.Popen, grace: float = CANCEL_GRACE_SECONDS) -> None:
    """Terminate a child's whole process group, escalating to SIGKILL for anything left."""
    if process.poll() is not None:
        return
    try:
        pgid = os.getpgid(process.pid)
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    try:  # the leader may be gone while a command it started still ignores SIGTERM
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass


def _has_tests(run_dir: Path, problem_id: str, modes: list[str]) -> bool:
    return any(path for mode in modes for path in (run_dir / mode / problem_id / "tests").glob("test_*.py"))


def _mode_results(run_dir: Path, problem_id: str, modes: list[str]) -> dict[str, dict]:
    results = {}
    for mode in modes:
        path = run_dir / mode / problem_id / "usage.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        results[mode] = {
            key: data.get(key)
            for key in ("exit_status", "api_calls", "total_tokens", "cost_usd", "billing_uncertain")
        }
    return results


def _write_status(run_dir: Path, records: list[dict]) -> None:
    target = run_dir / "status.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({"problems": records}, indent=2))
    os.replace(temporary, target)


def _log_event(run_dir: Path, event: dict) -> None:
    event["at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(run_dir / "progress.jsonl", "a") as log:
        log.write(json.dumps(event) + "\n")


def _child_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Run one problem for final_run.py")
    parser.add_argument("--run", required=True)
    parser.add_argument("--problem", required=True)
    parser.add_argument("--modes", default="combined")
    args = parser.parse_args(argv)
    run_dir = RUNS_DIR / args.run
    settings = load_settings(run_dir / "settings.yaml")
    problems = load_problems(DEV_PROBLEMS, [args.problem])
    if not problems:
        print(f"unknown problem {args.problem}", file=sys.stderr)
        return 2
    deadline = time.monotonic() + float(settings["limits"]["wall_time_limit_seconds"])
    model = DeadlineModel(build_model(settings), deadline)
    run_problem_child(
        problems[0], args.modes.split(","), run_dir, student=run_dir / "student", settings=settings, model=model
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--child" in argv:
        return _child_main([arg for arg in argv if arg != "--child"])

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", required=True, help="new run name; results go to runs/<run>/")
    parser.add_argument("--workers", type=int, default=8, help="problems to run in parallel")
    parser.add_argument("--only", nargs="*", help="problem ids to run (default: every development problem)")
    args = parser.parse_args(argv)

    if Path(args.run).name != args.run or args.run in (".", ".."):
        print(f"--run must be a plain directory name under {RUNS_DIR}")
        return 2
    problems = load_problems(DEV_PROBLEMS, args.only)
    if not problems:
        print(f"No problems matched {args.only or DEV_PROBLEMS}")
        return 2
    if args.only and len(problems) != len(set(args.only)):
        print(f"Warning: only {len(problems)} of {len(set(args.only))} requested problems exist")
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("OPENROUTER_API_KEY is not set (expected in the environment or .env)")
        return 2

    run_dir = RUNS_DIR / args.run
    try:
        freeze_run(run_dir, REPO_ROOT / "student", REPO_ROOT / "settings.yaml")
    except FileExistsError:
        print(f"{run_dir} already exists; choose a new --run name (refusing to overwrite)")
        return 2

    settings = load_settings(run_dir / "settings.yaml")
    strategy = load_strategy(run_dir / "student")
    modes = ["blackbox", "whitebox"] if strategy == "separate" else ["combined"]
    limits = settings["limits"]
    reservation = float(limits["cost_limit"])
    wall = float(limits["wall_time_limit_seconds"])
    outer_timeout = wall + float(limits["command_timeout_seconds"]) + 60
    workers = max(1, args.workers)
    (run_dir / "logs").mkdir()

    records = [{"id": p.id, "state": "pending", "modes": {}, "tests": [], "seconds": 0.0} for p in problems]
    by_id = {record["id"]: record for record in records}
    pending = list(problems)
    active: dict[str, dict] = {}
    before = key_balance(timeout=STARTUP_BALANCE_TIMEOUT)
    last_balance = time.monotonic()
    print(
        f"[run] {args.run}: {len(problems)} problems, strategy={strategy}, modes={modes},"
        f" workers={workers}, wall={wall:.0f}s, reservation=${reservation:.2f}"
    )
    print(f"[budget] before: {_balance_line(before)}")
    _write_status(run_dir, records)

    def refresh_balance() -> None:
        nonlocal before, last_balance
        if time.monotonic() - last_balance < BALANCE_REFRESH_SECONDS:
            return
        latest = key_balance()
        if latest is not None and (latest.get("remaining") is not None or before is None):
            before = latest
        last_balance = time.monotonic()

    def finish(problem: Problem, child: dict, returncode: int, killed: bool) -> None:
        record = by_id[problem.id]
        state = "ok" if returncode == 0 else ("killed" if killed else "crashed")
        files = []
        if state == "ok" or _has_tests(run_dir, problem.id, modes):
            files = merge_problem(run_dir, problem.id, modes)
        results = _mode_results(run_dir, problem.id, modes)
        record.update(
            state=state,
            exit_code=returncode,
            seconds=round(time.monotonic() - child["started"], 1),
            modes=results,
            tests=files,
            billing_uncertain=any(mode.get("billing_uncertain") for mode in results.values()),
        )
        if state != "ok":
            # What an interrupted child already spent is unknown, so never claim zero.
            record["billing_uncertain"] = True
            record["reason"] = (
                f"child exited {returncode}" + (" after the outer wall timeout" if killed else "") + "; spend unknown"
            )
        merged_usage = run_dir / "final" / problem.id / "usage.json"
        if record["billing_uncertain"] and merged_usage.exists():
            data = json.loads(merged_usage.read_text())
            data["billing_uncertain"] = True
            merged_usage.write_text(json.dumps(data, indent=2))
        child["log"].close()
        statuses = ",".join(f"{mode}:{data.get('exit_status')}" for mode, data in results.items()) or "no usage"
        spent = sum(float(data.get("cost_usd") or 0) for data in results.values())
        spent_text = f"${spent:.4f}" + (" (spend unknown)" if record["billing_uncertain"] else "")
        done = sum(1 for item in records if item["state"] in ("ok", "killed", "crashed"))
        print(
            f"[done {done}/{len(records)}] {problem.id}: {state} {record['seconds']:.0f}s"
            f" [{statuses}] tests={files} cost={spent_text}",
            flush=True,
        )
        _log_event(run_dir, {"event": "finish", "problem": problem.id, "state": state, "seconds": record["seconds"]})
        _write_status(run_dir, records)

    cancelled = False
    evaluation_code = 0

    def cancel(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, cancel)  # parent cancellation must reach the finally below
    try:
        while pending or active:
            refresh_balance()

            while pending and len(active) < workers:
                slots = budget_slots(before, reservation, len(active))
                if slots is not None and slots <= 0:
                    break
                problem = pending.pop(0)
                log = open(run_dir / "logs" / f"{problem.id}.log", "wb")
                command = [
                    sys.executable, str(SCRIPT), "--child", "--run", args.run,
                    "--problem", problem.id, "--modes", ",".join(modes),
                ]
                process = subprocess.Popen(
                    command, cwd=str(REPO_ROOT), stdout=log, stderr=subprocess.STDOUT, start_new_session=True
                )
                active[problem.id] = {
                    "process": process, "log": log, "problem": problem, "started": time.monotonic(),
                    "deadline": time.monotonic() + outer_timeout,
                }
                _log_event(run_dir, {"event": "start", "problem": problem.id})
                print(f"[start] {problem.id}: {len(records) - len(pending)}/{len(records)}", flush=True)

            if not active and pending:
                reason = f"budget: {_balance_line(before)} cannot cover a ${reservation:.2f} reservation"
                for problem in pending:
                    by_id[problem.id].update(state="unrun", reason=reason)
                    _log_event(run_dir, {"event": "unrun", "problem": problem.id, "reason": reason})
                print(f"[budget] unrun: {' '.join(p.id for p in pending)}")
                pending.clear()
                _write_status(run_dir, records)
                break

            progressed = False
            for problem_id, child in list(active.items()):
                returncode = child["process"].poll()
                killed = False
                if returncode is None and time.monotonic() >= child["deadline"]:
                    stop_group(child["process"])
                    returncode = child["process"].poll()
                    killed = True
                if returncode is None:
                    continue
                active.pop(problem_id)
                progressed = True
                finish(child["problem"], child, returncode, killed)
                refresh_balance()
            if not progressed and active:
                time.sleep(0.2)
    except KeyboardInterrupt:
        cancelled = True
        for child in active.values():
            stop_group(child["process"])
            child["log"].close()
        active.clear()
        for problem in pending:
            by_id[problem.id].update(state="cancelled", reason="interrupted")
        print("[cancel] stopped every child process group")
    finally:
        for child in active.values():
            stop_group(child["process"])
            child["log"].close()
        after = key_balance(timeout=5)
        (run_dir / "budget.json").write_text(json.dumps({"before": before, "after": after}, indent=2))
        _write_status(run_dir, records)
        print(f"[budget] after: {_balance_line(after)}")

    merged = [record["id"] for record in records if (run_dir / "final" / record["id"]).exists()]
    if merged and not cancelled:
        print(f"[evaluate] {len(merged)} merged suites")
        evaluation_code = subprocess.call(
            [sys.executable, "-m", "testagent.evaluate", "--run", args.run, "--mode", "final",
             "--workers", str(workers), "--only", *merged],
            cwd=str(REPO_ROOT),
        )

    bad = [record for record in records if record["state"] not in ("ok",)]
    for record in records:
        if record["state"] != "ok":
            print(f"[{record['state']}] {record['id']}: {record.get('reason', '')}")
    print(f"[summary] ok={len(records) - len(bad)} not-ok={len(bad)} merged={len(merged)} run={run_dir}")
    if cancelled:
        return 130
    return 1 if bad or evaluation_code else 0


def _balance_line(balance: dict | None) -> str:
    """Describe a balance snapshot without exposing anything secret."""
    if balance is None:
        return "OpenRouter key balance unavailable (no key)"
    if balance.get("error"):
        return f"OpenRouter key balance unavailable ({balance['error']})"
    if balance.get("remaining") is None:
        return f"OpenRouter key has no explicit limit (usage ${float(balance.get('usage') or 0):.4f})"
    return (
        f"remaining ${float(balance['remaining']):.4f}"
        f" (limit ${float(balance['limit']):.2f}, used ${float(balance['usage']):.4f})"
    )


if __name__ == "__main__":
    raise SystemExit(main())
