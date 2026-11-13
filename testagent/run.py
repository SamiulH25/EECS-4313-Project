"""Run your agent on the projects.

Usage:
    python -m testagent.run --run my-run
    python -m testagent.run --run my-run --only cachetools
    python -m testagent.run --run my-run --student path/to/another/student

For each project the harness makes a fresh workspace (the project's code, its existing tests and an
empty tests_new/ folder), builds the budgeted model and a shell in that workspace, and calls
`solve(task, model, env)` from student/agent.py. Whatever is in tests_new/ at the end is kept.
"""

import argparse
import importlib.util
import json
import shutil
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml
from minisweagent.environments.local import LocalEnvironment

from testagent import REPO_ROOT, toolbox
from testagent.budget import build_model
from testagent.projects import DATA_DIR, NEW_TESTS, Project, collect_tests, create_workspace, load_projects

SETTINGS_PATH = REPO_ROOT / "settings.yaml"
STUDENT_DIR = REPO_ROOT / "student"
RUNS_DIR = REPO_ROOT / "runs"


@dataclass
class Task:
    """What solve() gets to know about the project."""

    project: str
    package: str
    summary: str
    workspace: Path
    new_tests_dir: str
    tools: str
    token_limit: int
    step_limit: int
    max_tests: int
    trajectory_path: Path
    student_dir: Path


def load_settings(path: Path = SETTINGS_PATH) -> dict:
    """Read the fixed course settings."""
    return yaml.safe_load(Path(path).read_text())


def load_student(student: Path):
    """Import student/agent.py as a module."""
    spec = importlib.util.spec_from_file_location("student_agent", student / "agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_project(project: Project, out_dir: Path, *, settings: dict, student: Path, keep_workspace: bool = False) -> dict:
    """Run the student's agent on one project and save its tests and usage to out_dir."""
    limits = settings["limits"]
    out_dir.mkdir(parents=True, exist_ok=True)
    workspace = create_workspace(project)
    tools_dir = student / "tools"
    model = build_model(settings, limits["token_limit"])
    env = LocalEnvironment(cwd=str(workspace), env=toolbox.environment(tools_dir),
                           timeout=limits["command_timeout_seconds"])
    task = Task(project.name, project.package, project.summary, workspace, NEW_TESTS,
                toolbox.describe(workspace, tools_dir), limits["token_limit"], limits["step_limit"],
                settings["evaluation"]["max_tests"], out_dir / "trajectory.json", student)
    started = time.time()
    try:
        load_student(student).solve(task, model, env)
        exit_status = "Submitted"
    except Exception as error:
        exit_status = f"Error: {type(error).__name__}: {error}"
        (out_dir / "error.txt").write_text(traceback.format_exc())
    tests = collect_tests(workspace, out_dir / NEW_TESTS)
    usage = {"project": project.name, "exit_status": exit_status, "seconds": round(time.time() - started),
             **model.usage(), "test_files": [str(t.relative_to(out_dir / NEW_TESTS)) for t in tests]}
    (out_dir / "usage.json").write_text(json.dumps(usage, indent=2))
    if keep_workspace:
        usage["workspace"] = str(workspace)
    else:
        shutil.rmtree(workspace.parent, ignore_errors=True)
    return usage


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", required=True, help="name of the run; results go to runs/<run>/<project>/")
    parser.add_argument("--projects", default=str(DATA_DIR), help="folder of projects")
    parser.add_argument("--only", nargs="*", help="project names to run")
    parser.add_argument("--student", default=str(STUDENT_DIR), help="folder with agent.py, prompts/ and tools/")
    parser.add_argument("--workers", type=int, default=1, help="projects to run in parallel")
    parser.add_argument("--keep-workspace", action="store_true", help="keep the temporary workspaces")
    args = parser.parse_args(argv)

    run_dir = RUNS_DIR / args.run
    student = Path(args.student).resolve()
    settings = load_settings()
    projects = load_projects(Path(args.projects), args.only)

    def run_one(project: Project) -> dict:
        usage = run_project(project, run_dir / project.name, settings=settings, student=student,
                            keep_workspace=args.keep_workspace)
        print(f"{project.name}: {usage['exit_status']}  calls={usage['model_calls']}  tokens={usage['total_tokens']}"
              f"  cost=${usage['cost_usd']:.4f}  files={usage['test_files']}", flush=True)
        return usage

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        rows = list(pool.map(run_one, projects))
    summary = {"run": args.run, "student": str(student), "projects": rows,
               "cost_usd": round(sum(r["cost_usd"] for r in rows), 6),
               "total_tokens": sum(r["total_tokens"] for r in rows)}
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "usage.json").write_text(json.dumps(summary, indent=2))
    print(f"Done. {len(rows)} projects, {summary['total_tokens']} tokens, ${summary['cost_usd']:.4f}.")


if __name__ == "__main__":
    main()
