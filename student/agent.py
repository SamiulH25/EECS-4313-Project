"""Our phase 2 agent. See tagent/pipeline.py for the loop.

The harness calls solve(task, model, env) once per project. Every model call goes through `model`.
"""

import sys
from pathlib import Path


def solve(task, model, env) -> None:
    student = str(Path(task.student_dir).resolve())
    if student not in sys.path:
        sys.path.insert(0, student)
    from tagent.pipeline import Pipeline

    Pipeline(task, model, env).run()
