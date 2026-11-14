"""Dev only: which bugs each test file of a run finds (the evaluator reads data/ as the oracle).

usage: python scripts/attribute.py <run> <project> [data/dev]
"""
import shutil, sys, tempfile
from pathlib import Path
from testagent.evaluate import evaluate_project, load_settings
from testagent.projects import load_projects

run, name = sys.argv[1], sys.argv[2]
folder = Path(sys.argv[3] if len(sys.argv) > 3 else "data/dev")
project = load_projects(folder, [name])[0]
src = Path("runs") / run / name / "tests_new"
settings = load_settings()
for f in sorted(src.glob("test_*.py")):
    tmp = Path(tempfile.mkdtemp(dir="runs"))
    for c in src.glob("conftest.py"):
        shutil.copy(c, tmp / c.name)
    shutil.copy(f, tmp / f.name)
    r = evaluate_project(project, tmp, settings, 8)
    print(f.name, r["bugs_found"], "FA", r["false_alarms"], sorted(r["found"]), flush=True)
    shutil.rmtree(tmp, ignore_errors=True)
