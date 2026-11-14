"""Our phase 2 agent: the model drives testing tools from its shell.

The harness calls solve(task, model, env) once per project. The model runs in one conversation
(tagent/loop.py) and decides what to do; the tools in student/tools do the mechanical work and keep
their state in the workspace (tagent/session.py). Every model call goes through `model`.
"""

import sys
import traceback
from pathlib import Path


def solve(task, model, env) -> None:
    student = Path(task.student_dir).resolve()
    if str(student) not in sys.path:
        sys.path.insert(0, str(student))
    from tagent.loop import AgentLoop
    from tagent.pipeline import _fill, _load_prompt
    from tagent.session import Session, init_state

    ws = Path(task.workspace)
    init_state(ws, task, student)
    system, user = _load_prompt(student / "prompts" / "agent.md")
    instance = _fill(user, project=task.project, package=task.package, summary=task.summary,
                     max_tests=str(task.max_tests), tools=task.tools)
    loop = AgentLoop(task, model, env, system, instance)
    try:
        loop.run()
    except Exception as error:  # noqa: BLE001
        loop.note("loop-error", error=f"{error}\n{traceback.format_exc()[-1500:]}")
    finally:
        # Whatever the model managed, the confirmed tests end up in tests_new/ (finalize is idempotent).
        try:
            if Session(ws).kept:
                env.execute({"command": "finalize"}, timeout=120)
        except Exception as error:  # noqa: BLE001
            loop.note("finalize-error", error=str(error))
