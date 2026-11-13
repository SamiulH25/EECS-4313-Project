"""Dev only: replay a recorded run's model replies through the current pipeline, then evaluate.

No model calls: every `ask` returns the command recorded under the same tag in the old trajectory.
This measures changes to everything except the prompts.

Usage: .venv/bin/python scripts/replay.py --from-run v1-25 --projects <dir> [--only cachetools] [--out replay-x]
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
STUDENT = ROOT / "student"
if "--student" in sys.argv:
    STUDENT = Path(sys.argv[sys.argv.index("--student") + 1]).resolve()
sys.path.insert(0, str(STUDENT))

from minisweagent.environments.local import LocalEnvironment  # noqa: E402

from tagent import pipeline as P  # noqa: E402
from testagent import toolbox  # noqa: E402
from testagent.evaluate import evaluate_project  # noqa: E402
from testagent.projects import collect_tests, create_workspace, load_projects  # noqa: E402
from testagent.run import RUNS_DIR, Task, load_settings  # noqa: E402


class FakeModel:
    total_tokens = 0
    tokens_left = 10 ** 9


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-run", required=True)
    ap.add_argument("--projects", default=str(ROOT / "data/dev"))
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--out", default="replay")
    ap.add_argument("--student", default=str(STUDENT))
    ap.add_argument("--no-llm", action="store_true", help="skip every model call (tools only)")
    args = ap.parse_args()
    settings = load_settings()
    total = {"found": 0, "fa": 0, "bugs": 0}
    for project in load_projects(Path(args.projects), args.only):
        traj = json.loads((RUNS_DIR / args.from_run / project.name / "trajectory.json").read_text())["calls"]
        recorded = {}
        for c in traj:
            if c.get("command") is not None and c["tag"] not in recorded:
                recorded[c["tag"]] = c["command"]
        ws = create_workspace(project)
        env = LocalEnvironment(cwd=str(ws), env=toolbox.environment(STUDENT / "tools"), timeout=120)
        out_dir = RUNS_DIR / args.out / project.name
        out_dir.mkdir(parents=True, exist_ok=True)
        task = Task(project.name, project.package, project.summary, ws, "tests_new", "", 400000, 120, 300,
                    out_dir / "trajectory.json", STUDENT)
        pipe = P.Pipeline(task, FakeModel(), env)
        orig_ask = pipe.llm.ask

        def ask(system, user, tag, _rec=recorded):
            return None if args.no_llm else _rec.get(tag)
        pipe.llm.ask = ask
        pipe.run()
        collect_tests(ws, out_dir / "tests_new")
        res = evaluate_project(project, out_dir / "tests_new", settings, workers=8)
        print(f"{project.name}: found {res['bugs_found']}/{res['bugs']} {res['found']}, false alarms {res['false_alarms']}, "
              f"valid {res['valid_tests']}/{res['tests']}, score {res['score']}")
        for t in res["false_alarm_tests"][:40]:
            print("   FA", t)
        total["found"] += res["bugs_found"]
        total["fa"] += res["false_alarms"]
        total["bugs"] += res["bugs"]
        (out_dir / "evaluation.json").write_text(json.dumps(res, indent=1))
        shutil.rmtree(out_dir / "cands", ignore_errors=True)
        if (ws / "_cand").is_dir():
            shutil.copytree(ws / "_cand", out_dir / "cands", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.rmtree(ws.parent, ignore_errors=True)
    print(f"TOTAL found {total['found']}/{total['bugs']}, FA {total['fa']}, score {total['found'] - 0.5 * total['fa']}")


if __name__ == "__main__":
    main()
