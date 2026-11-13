"""Focused tests for the local full-run driver. They use scripted/slow models, so no API key is needed."""

import importlib.util
import json
import signal
import subprocess
import time

import pytest
from minisweagent.exceptions import TimeExceeded
from minisweagent.models.test_models import DeterministicModel, make_output

from testagent import REPO_ROOT
from testagent.problems import load_problem
from testagent.run import load_settings

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "example-peaks.json"


def load_runner():
    spec = importlib.util.spec_from_file_location("final_run", REPO_ROOT / "scripts" / "final_run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


final_run = load_runner()

SUITE = """cat > tests/test_combined.py <<'PYEOF'
from solution import Solution


def test_example_one():
    assert Solution().countPeaks([1, 3, 2]) == 1
PYEOF"""


def test_blocked_query_is_interrupted_and_suite_survives(tmp_path):
    problem = load_problem(FIXTURE)
    settings = load_settings()
    settings["limits"] = dict(settings["limits"], wall_time_limit_seconds=2, command_timeout_seconds=10)
    slow = DeterministicModel(
        outputs=[
            make_output("", [{"command": SUITE}], cost=0.001),
            make_output("", [{"command": "/sleep 60"}], cost=0.001),
        ],
        cost_per_call=0.001,
    )
    model = final_run.DeadlineModel(slow, deadline=time.monotonic() + 2)
    started = time.monotonic()
    usage = final_run.run_problem_child(
        problem, ["combined"], tmp_path, student=REPO_ROOT / "student", settings=settings, model=model
    )
    elapsed = time.monotonic() - started

    assert elapsed < 15, "the blocked query was not interrupted by the wall clock"
    assert model.timed_out
    assert usage["combined"]["exit_status"] == "TimeExceeded"
    assert usage["combined"]["billing_uncertain"] is True
    result = tmp_path / "combined" / problem.id
    assert (result / "tests" / "test_combined.py").exists()
    trajectory = json.loads((result / "trajectory.json").read_text())
    assert trajectory["info"]["exit_status"] == "TimeExceeded"
    saved = json.loads((result / "usage.json").read_text())
    assert saved["exit_status"] == "TimeExceeded"
    assert saved["billing_uncertain"] is True


def test_query_past_deadline_does_not_reach_the_model():
    class Uncallable:
        def query(self, *args, **kwargs):
            raise AssertionError("the model must not be queried after the deadline")

    model = final_run.DeadlineModel(Uncallable(), deadline=time.monotonic() - 1)
    with pytest.raises(TimeExceeded):
        model.query([])
    assert model.timed_out


def test_budget_slots_reserve_each_problem():
    assert final_run.budget_slots({"remaining": 0.12}, 0.05, 0) == 2
    assert final_run.budget_slots({"remaining": 0.12}, 0.05, 2) == 0
    assert final_run.budget_slots({"remaining": 0.04}, 0.05, 0) == 0
    assert final_run.budget_slots(None, 0.05, 0) is None
    assert final_run.budget_slots({"remaining": None}, 0.05, 0) is None
    assert final_run.budget_slots({"error": "offline"}, 0.05, 0) is None


def test_freeze_run_refuses_to_reuse_a_name(tmp_path):
    run_dir = tmp_path / "run"
    final_run.freeze_run(run_dir, REPO_ROOT / "student", REPO_ROOT / "settings.yaml")
    assert (run_dir / "settings.yaml").exists()
    assert (run_dir / "student" / "final.yaml").exists()
    with pytest.raises(FileExistsError):
        final_run.freeze_run(run_dir, REPO_ROOT / "student", REPO_ROOT / "settings.yaml")


def test_stop_group_kills_a_child_that_ignores_sigterm(tmp_path):
    marker = tmp_path / "started"
    process = subprocess.Popen(
        ["bash", "-c", f"trap '' TERM; echo ok > {marker}; while :; do sleep 60; done"],
        start_new_session=True,
    )
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert marker.exists()
    started = time.monotonic()
    final_run.stop_group(process, grace=0.5)
    assert process.poll() is not None
    assert process.returncode == -signal.SIGKILL
    assert time.monotonic() - started < 10
