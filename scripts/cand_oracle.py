"""Dev only (reads data/): which candidate tests are real bug detectors, and what did the pipeline do with them?

Usage: .venv/bin/python scripts/cand_oracle.py --run replay-v4c --project cachetools
"""
import argparse
import copy
import json
import shutil
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from testagent import bugs  # noqa: E402
from testagent.evaluate import pytest_run  # noqa: E402
from testagent.projects import load_projects  # noqa: E402
from testagent.run import RUNS_DIR, load_settings  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--project", required=True)
    ap.add_argument("--projects", default=str(ROOT / "data/dev"))
    args = ap.parse_args()
    settings = copy.deepcopy(load_settings())
    settings["evaluation"]["max_tests"] = 100000
    project = load_projects(Path(args.projects), [args.project])[0]
    cands = RUNS_DIR / args.run / args.project / "cands"
    work = Path(tempfile.mkdtemp(prefix="oracle-"))
    clean = work / "clean"
    shutil.copytree(project.code, clean, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(cands, clean / "tests_new")
    for _ in range(6):
        first = pytest_run(clean, project.package, settings, seed="1")
        bad = [k.split("::", 1)[1] for k in first if k.startswith("<collection>")]
        if not bad:
            break
        for b in bad:
            (clean / b).unlink(missing_ok=True)
    second = pytest_run(clean, project.package, settings, seed="2")
    tests = sorted(k for k in first if not k.startswith("<"))
    valid = [t for t in tests if first.get(t) == "passed" and second.get(t) == "passed"]
    pbugs = project.bugs()

    def run_bug(bug):
        tree = work / f"bug-{bug.id}"
        shutil.copytree(clean, tree)
        bugs.apply(tree, [bug])
        out = pytest_run(tree, project.package, settings, only=valid)
        shutil.rmtree(tree, ignore_errors=True)
        return bug.id, [t for t, v in out.items() if v == "failed" and not t.startswith("<")]

    with ThreadPoolExecutor(8) as pool:
        det = dict(pool.map(run_bug, pbugs))
    traj = json.loads((RUNS_DIR / args.run / args.project / "trajectory.json").read_text())["calls"]
    kept = {(c["file"].split("/")[-1], k) for c in traj if c["tag"] == "kept" for k in c.get("kept", [])}
    out = {"valid": len(valid), "tests": len(tests), "bugs": {}}
    for bid, ts in det.items():
        rows = []
        for t in ts:
            f = t.split("::")[0].split("/")[-1]
            fn = t.split("::")[-1].split("[")[0]
            rows.append(("KEPT " if (f, fn) in kept else "drop ") + t)
        out["bugs"][bid] = rows
        print(bid, len(ts), "detectors;", sum(r.startswith("KEPT") for r in rows), "kept")
        for r in rows[:6]:
            print("   ", r)
    (RUNS_DIR / args.run / args.project / "oracle.json").write_text(json.dumps(out, indent=1))
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
