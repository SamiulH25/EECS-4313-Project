"""Measure the tests your agent wrote.

Usage:
    python -m testagent.evaluate --run my-run
    python -m testagent.evaluate --run my-run --only cachetools --workers 8

For each project the evaluator
1. rejects any file in tests_new/ that could read the code under test (it imports os, inspect and
   similar modules, calls open, and so on),
2. runs the remaining tests twice, in two random orders, on the correct code. A test that fails or
   changes outcome is a false alarm: it reports a bug that is not there,
3. runs the tests that passed on the correct code once for each bug, with only that bug added. The
   bug is found if at least one of them fails.

score = bugs found - false_alarm_weight * false alarms. Only the first max_tests tests count.
"""

import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from testagent import REPO_ROOT, bugs
from testagent.projects import DATA_DIR, NEW_TESTS, Project, load_projects
from testagent.run import RUNS_DIR, load_settings

FORBIDDEN_MODULES = {"inspect", "os", "io", "pathlib", "importlib", "linecache", "dis", "subprocess", "shutil",
                     "glob", "marshal", "ast", "tokenize", "pkgutil", "runpy", "sys", "gc", "ctypes", "mmap"}
FORBIDDEN_NAMES = {"open", "exec", "eval", "compile", "globals", "vars", "__import__", "breakpoint"}
FORBIDDEN_ATTRS = {"__code__", "__file__", "__globals__", "__loader__", "__spec__", "__path__", "modules",
                   "getsource", "getsourcelines", "read_text", "read_bytes", "co_code", "f_code"}


def source_access(path: Path) -> str:
    """Why a test file may read the code under test, or '' if it looks clean."""
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        return ""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            names = {(node.module or "").split(".")[0]}
        else:
            names = set()
        hit = names & FORBIDDEN_MODULES
        if hit:
            return f"imports {sorted(hit)[0]}"
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            return f"uses {node.id}"
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRS:
            return f"uses .{node.attr}"
    return ""


def pytest_run(tree: Path, package: str, settings: dict, *, seed: str = "", only: list[str] | None = None,
               stop_first: bool = False) -> dict:
    """Run tests_new/ in tree and return {nodeid: outcome}, or {'<timeout>': 'failed'}."""
    evaluation = settings["evaluation"]
    out = tree / ".outcomes.json"
    out.unlink(missing_ok=True)
    env = os.environ | {
        "PYTHONPATH": os.pathsep.join([str(tree), str(REPO_ROOT)]),
        "PYTHONDONTWRITEBYTECODE": "1",
        "TESTAGENT_PACKAGE": package,
        "TESTAGENT_SOURCE": str(tree / package),
        "TESTAGENT_TESTS": str(tree / NEW_TESTS),
        "TESTAGENT_MAX_TESTS": str(evaluation["max_tests"]),
        "TESTAGENT_TEST_TIMEOUT": str(evaluation["test_timeout_seconds"]),
        "TESTAGENT_OUTCOMES": str(out),
        "TESTAGENT_SEED": seed,
        "HYPOTHESIS_STORAGE_DIRECTORY": str(tree / ".hypothesis"),
    }
    if only is not None:
        (tree / ".only.json").write_text(json.dumps(only))
        env["TESTAGENT_ONLY"] = str(tree / ".only.json")
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "testagent.guard",
               "--rootdir", str(tree), "-c", os.devnull, NEW_TESTS]
    if stop_first:
        command.insert(4, "-x")
    try:
        subprocess.run(command, cwd=tree, env=env, capture_output=True, timeout=evaluation["suite_timeout_seconds"])
    except subprocess.TimeoutExpired:
        return {"<timeout>": "failed"}
    return json.loads(out.read_text()) if out.exists() else {"<crash>": "failed"}


def evaluate_project(project: Project, tests_dir: Path, settings: dict, workers: int = 4) -> dict:
    """Score one project's new tests."""
    evaluation = settings["evaluation"]
    project_bugs = project.bugs()
    result = {"project": project.name, "bugs": len(project_bugs), "warnings": [], "rejected_files": {}}
    work = Path(tempfile.mkdtemp(prefix=f"eval-{project.name}-"))
    clean = work / "clean"
    shutil.copytree(project.code, clean, ignore=shutil.ignore_patterns("__pycache__"))
    files = sorted(tests_dir.rglob("*.py")) if tests_dir.is_dir() else []
    for path in files:
        relative = path.relative_to(tests_dir)
        reason = source_access(path)
        if reason:
            result["rejected_files"][str(relative)] = reason
            continue
        dest = clean / NEW_TESTS / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(path, dest)
    if result["rejected_files"]:
        result["warnings"].append(f"{len(result['rejected_files'])} file(s) rejected because they could read the code")
    if not (clean / NEW_TESTS).is_dir():
        result["warnings"].append("no test files to measure")
        (clean / NEW_TESTS).mkdir()

    first = pytest_run(clean, project.package, settings, seed="1")
    second = pytest_run(clean, project.package, settings, seed="2")
    if "<timeout>" in first or "<timeout>" in second:
        result["warnings"].append(f"the suite took longer than {evaluation['suite_timeout_seconds']} s on the correct code")
    collection = sorted(k for k in first if k.startswith("<collection>"))
    if collection:
        result["warnings"].append(f"{len(collection)} file(s) could not be collected: {', '.join(collection[:3])}")
    tests = sorted({k for k in first | second if not k.startswith("<")})
    valid = [t for t in tests if first.get(t) == "passed" and second.get(t) == "passed"]
    false_alarms = [t for t in tests if "failed" in (first.get(t), second.get(t))
                    or first.get(t) != second.get(t)]

    def found(bug: bugs.Bug) -> bool:
        tree = work / f"bug-{bug.id}"
        shutil.copytree(clean, tree)
        bugs.apply(tree, [bug])
        outcomes = pytest_run(tree, project.package, settings, only=valid, stop_first=True)
        shutil.rmtree(tree, ignore_errors=True)
        return any(v == "failed" for v in outcomes.values())

    if valid:
        with ThreadPoolExecutor(max(1, workers)) as pool:
            hits = list(pool.map(found, project_bugs))
    else:
        hits = [False] * len(project_bugs)
    shutil.rmtree(work, ignore_errors=True)
    found_ids = [b.id for b, hit in zip(project_bugs, hits) if hit]
    weight = evaluation["false_alarm_weight"]
    result.update({
        "tests": len(tests),
        "valid_tests": len(valid),
        "false_alarms": len(false_alarms),
        "false_alarm_tests": false_alarms,
        "bugs_found": len(found_ids),
        "found": found_ids,
        "detection_rate": round(len(found_ids) / len(project_bugs), 4) if project_bugs else 0.0,
        "score": round(len(found_ids) - weight * len(false_alarms), 2),
    })
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", required=True)
    parser.add_argument("--projects", default=str(DATA_DIR))
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--workers", type=int, default=4, help="bugs measured in parallel")
    args = parser.parse_args(argv)

    settings = load_settings()
    run_dir = RUNS_DIR / args.run
    rows = []
    for project in load_projects(Path(args.projects), args.only):
        row = evaluate_project(project, run_dir / project.name / NEW_TESTS, settings, args.workers)
        rows.append(row)
        print(f"{project.name}: found {row['bugs_found']}/{row['bugs']} bugs, {row['false_alarms']} false alarms, "
              f"{row['valid_tests']}/{row['tests']} valid tests, score {row['score']}", flush=True)
        for warning in row["warnings"]:
            print(f"  warning: {warning}")
    total_bugs = sum(r["bugs"] for r in rows)
    summary = {
        "run": args.run,
        "bugs": total_bugs,
        "bugs_found": sum(r["bugs_found"] for r in rows),
        "false_alarms": sum(r["false_alarms"] for r in rows),
        "detection_rate": round(sum(r["bugs_found"] for r in rows) / total_bugs, 4) if total_bugs else 0.0,
        "score": round(sum(r["score"] for r in rows), 2),
        "projects": rows,
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "evaluation.json").write_text(json.dumps(summary, indent=2))
    print(f"Total: {summary['bugs_found']}/{summary['bugs']} bugs found (rate {summary['detection_rate']}), "
          f"{summary['false_alarms']} false alarms, score {summary['score']}. Saved to {run_dir / 'evaluation.json'}")


if __name__ == "__main__":
    main()
