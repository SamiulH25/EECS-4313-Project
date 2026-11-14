"""Dev check: run the agentic tools by hand on a fresh workspace (no model). Usage: smoke_tools.py <student> <project> <cmd>..."""
import subprocess, sys, time
from pathlib import Path
from types import SimpleNamespace
from testagent import projects, toolbox

student = Path(sys.argv[1]).resolve()
proj = [p for p in projects.load_projects(only=[sys.argv[2]])][0]
ws = projects.create_workspace(proj)
sys.path.insert(0, str(student))
from tagent.session import init_state
init_state(ws, SimpleNamespace(project=proj.name, package=proj.package if hasattr(proj, "package") else proj.name,
                               summary="", max_tests=300), student)
env = toolbox.environment(student / "tools")
print("workspace", ws)
for cmd in sys.argv[3:]:
    t = time.time()
    r = subprocess.run(cmd, shell=True, cwd=ws, env=env, text=True, capture_output=True, timeout=130)
    print(f"===== {cmd}  ({time.time() - t:.0f}s, rc {r.returncode})\n{r.stdout[-3500:]}{r.stderr[-1500:]}")
