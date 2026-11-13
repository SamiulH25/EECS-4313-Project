"""Build the submission zip that you upload to eClass.

Usage:
    python -m testagent.package --run my-final --team team-07

The zip contains your student/ folder, REPORT.md, the tests your final agent wrote and the evaluation
of that run on all development projects. It is only built if everything required is there.
"""

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

from testagent import REPO_ROOT
from testagent.projects import DATA_DIR, NEW_TESTS, load_projects
from testagent.run import RUNS_DIR, STUDENT_DIR

REPORT = REPO_ROOT / "REPORT.md"
TEMPLATE = REPO_ROOT / "REPORT_TEMPLATE.md"
PLACEHOLDER = re.compile(r"<[^<>\n]+>")


def check_student(student: Path) -> list[str]:
    """Problems with a student folder that would stop us from running it."""
    errors = []
    agent = student / "agent.py"
    if not agent.exists():
        errors.append("missing agent.py")
    elif "def solve(" not in agent.read_text():
        errors.append("agent.py has no solve(task, model, env) function")
    for tool in (student / "tools").iterdir() if (student / "tools").is_dir() else []:
        if tool.is_file() and not tool.name.startswith((".", "_")) and tool.suffix not in (".md", ".py"):
            if not tool.stat().st_mode & 0o111:
                errors.append(f"tools/{tool.name} is not executable (chmod +x)")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", required=True, help="the run made by your final agent")
    parser.add_argument("--team", required=True, help="your team name, used in the file name")
    args = parser.parse_args(argv)

    run_dir = RUNS_DIR / args.run
    errors = [f"student/: {e}" for e in check_student(STUDENT_DIR)]
    if not REPORT.exists():
        errors.append("missing REPORT.md: copy REPORT_TEMPLATE.md to REPORT.md and fill it in")
    else:
        expected = set(PLACEHOLDER.findall(TEMPLATE.read_text())) if TEMPLATE.exists() else set()
        left = sorted(p for p in expected if p in REPORT.read_text())
        if left:
            errors.append(f"REPORT.md still has {len(left)} placeholder(s) from the template, for example {left[0]}")
    evaluation = run_dir / "evaluation.json"
    if not evaluation.exists():
        errors.append(f"missing {evaluation}: run `python -m testagent.evaluate --run {args.run}`")
    else:
        done = {p["project"] for p in json.loads(evaluation.read_text())["projects"]}
        missing = sorted({p.name for p in load_projects(DATA_DIR)} - done)
        if missing:
            errors.append(f"the evaluation does not cover: {', '.join(missing)}")
    if errors:
        for error in errors:
            print(f"ERROR {error}")
        print("Submission not built.")
        return 1

    target = REPO_ROOT / f"submission-{args.team}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(STUDENT_DIR.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                archive.write(path, Path("student") / path.relative_to(STUDENT_DIR))
        archive.write(REPORT, "REPORT.md")
        archive.write(evaluation, "results/evaluation.json")
        if (run_dir / "usage.json").exists():
            archive.write(run_dir / "usage.json", "results/usage.json")
        for path in sorted(run_dir.glob(f"*/{NEW_TESTS}/**/*.py")):
            archive.write(path, Path("results") / path.relative_to(run_dir))
    print(f"Built {target.name}. Upload this file to eClass.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
