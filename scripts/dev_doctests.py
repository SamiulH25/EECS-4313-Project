"""Dev only: measure the LLM-free doctest miner with the official evaluator. No model calls.

Usage: .venv/bin/python scripts/dev_doctests.py [--projects data/dev] [--only cachetools]
"""

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "student"))

from minisweagent.environments.local import LocalEnvironment  # noqa: E402

from tagent import doctests  # noqa: E402
from tagent.runner import Runner, triage_auto  # noqa: E402
from testagent import toolbox  # noqa: E402
from testagent.evaluate import evaluate_project  # noqa: E402
from testagent.projects import create_workspace, load_projects  # noqa: E402
from testagent.run import load_settings  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--projects", default=str(ROOT / "data/dev"))
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args()
    settings = load_settings()
    for project in load_projects(Path(args.projects), args.only):
        ws = create_workspace(project)
        env = LocalEnvironment(cwd=str(ws), env=toolbox.environment(ROOT / "student" / "tools"), timeout=120)
        runner = Runner(env, ws, ROOT / "student", project.package)
        cand = ws / "_cand"
        runner.ensure_conftest(cand)
        src, n = doctests.build_file(ws, project.package)
        (cand / "test_docs.py").write_text(src)
        tests, coll, out = runner.results(["_cand/test_docs.py"], repeats=2)
        failing = [t for t in tests if t.outcome == "failed"]
        keep = [t for t in failing if triage_auto(t, "test_docs.py", project.package) == "judge"]
        print(f"{project.name}: mined {n}, ran {len(tests)}, failing {len(failing)}, kept {len(keep)}, collection errors {len(coll)}")
        for c in coll[:2]:
            print("  COLLECT:", c[:500])
        for t in failing:
            tag = "KEEP" if t in keep else "drop"
            print(f"  {tag} {t.name}: {(t.crash or ['', '', t.text[:100]])[2][:110]!r}")
        # write kept tests only
        import ast
        tree = ast.parse(src)
        names = {t.func for t in keep}
        tree.body = [n for n in tree.body if not (isinstance(n, ast.FunctionDef) and n.name.startswith("test")
                                                    and n.name not in names)]
        out_dir = Path(tempfile.mkdtemp()) / "tests_new"
        out_dir.mkdir()
        (out_dir / "test_docs.py").write_text(ast.unparse(tree))
        (out_dir / "conftest.py").write_text((cand / "conftest.py").read_text())
        res = evaluate_project(project, out_dir, settings, workers=8)
        print(json.dumps({k: res[k] for k in ("bugs", "bugs_found", "false_alarms", "false_alarm_tests", "found", "score")}, indent=1))
        shutil.rmtree(ws.parent, ignore_errors=True)


if __name__ == "__main__":
    main()
