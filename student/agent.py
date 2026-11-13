"""Your agent. You may change everything in this file.

The harness calls solve(task, model, env) once per project:
- task: what the project is, where its workspace is, your tools' help text and the limits
  (see testagent/run.py, class Task).
- model: the course model with the project's token budget. Call model.query(messages) as often as
  you like; it raises LimitsExceeded when the budget is used up. It is the only model you may use.
- env: a shell in the workspace. env.execute({"command": "ls"}) runs a command there.

Write your new tests into tests_new/ in the workspace. Whatever is there when solve() returns is kept.

The starter agent below is plain mini-swe-agent: one loop with one prompt. Replace it with whatever
design you like (several phases, several conversations, your own loop), as long as every model call
goes through `model`.
"""

from pathlib import Path

import yaml
from minisweagent.agents.default import DefaultAgent


def solve(task, model, env) -> None:
    prompts = yaml.safe_load((Path(task.student_dir) / "prompts" / "agent.yaml").read_text())
    agent = DefaultAgent(
        model,
        env,
        system_template=prompts["system_template"],
        instance_template=prompts["instance_template"],
        step_limit=task.step_limit,
        cost_limit=0,
        output_path=task.trajectory_path,
    )
    agent.run(
        project=task.project,
        package=task.package,
        summary=task.summary,
        new_tests_dir=task.new_tests_dir,
        max_tests=task.max_tests,
        tools=task.tools,
    )
