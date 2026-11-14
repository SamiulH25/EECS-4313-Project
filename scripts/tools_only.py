"""Dev only: run the tool sequence (no model) on a fresh buggy workspace and store tests_new/ as a run.

usage: python scripts/tools_only.py <run> <project> [student dir]
Then: python -m testagent.evaluate --run <run> --projects data/dev
"""
import json, shutil, subprocess, sys
from pathlib import Path
from testagent import toolbox
from testagent.projects import create_workspace, load_projects

run, name = sys.argv[1], sys.argv[2]
student = Path(sys.argv[3] if len(sys.argv) > 3 else "student").resolve()
project = load_projects(Path("data/dev"), [name])[0]
ws = create_workspace(project)
import os
sys.path.insert(0, str(student))
from types import SimpleNamespace
from tagent.session import init_state
init_state(ws, SimpleNamespace(project=project.name, package=project.package, summary=project.summary, max_tests=300), student)
env = {**os.environ, **toolbox.environment(student / "tools")}


def sh(cmd):
    p = subprocess.run(cmd, shell=True, cwd=ws, env=env, capture_output=True, text=True, timeout=130)
    out = p.stdout + p.stderr
    keep = [l for l in out.splitlines() if l.startswith(("Fuzzer", "Wrote", "Mined", "Confirmed", "Repair-check", "tests_new", "All fuzz", "Traceback", "  CONFIRMED"))] or out.strip().splitlines()[-1:]
    print(f"== {cmd}: " + " | ".join(keep)[:400], flush=True)
    return out


sh("survey")
sh("doc_examples")
for _ in range(6):
    out = sh("fuzz")
    for _ in range(4):
        sh("confirm")
    if "All fuzzing passes are done" in out:
        break
sh("fuzz --long")
for _ in range(8):
    if "queue: empty" in sh("confirm"):
        break
sh("finalize")
st = json.loads((ws / ".tagent" / "state.json").read_text())
print("fixes", len(st.get("fixes", [])))
dst = Path("runs") / run / name / "tests_new"
shutil.rmtree(dst, ignore_errors=True)
shutil.copytree(ws / "tests_new", dst)
shutil.rmtree(ws.parent, ignore_errors=True)
