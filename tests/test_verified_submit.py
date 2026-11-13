"""Unit regressions for run_tests --submit --max-calls mode."""

import subprocess
import sys
from pathlib import Path

import pytest

from testagent import REPO_ROOT

RUN_TESTS = REPO_ROOT / "student" / "tools" / "whitebox" / "run_tests"


def make_workspace(folder: Path, *, code: str | None = None, tests: dict[str, str] | None = None):
    (folder / "solution.py").write_text(
        code or "class Solution:\n    def add(self, a, b):\n        return a + b\n"
    )
    test_dir = folder / "tests"
    test_dir.mkdir(exist_ok=True)
    if tests is not None:
        for name, content in tests.items():
            (test_dir / name).write_text(content)


def test_submit_accepts_valid_full_suite(tmp_path):
    make_workspace(
        tmp_path,
        tests={
            "test_solution.py": (
                "from solution import Solution\n"
                "def test_1():\n    assert Solution().add(1, 2) == 3\n"
                "def test_2():\n    assert Solution().add(0, 0) == 0\n"
            )
        },
    )
    result = subprocess.run(
        [sys.executable, str(RUN_TESTS), "--submit", "--max-calls", "10"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"Expected 0, got {result.returncode}: {result.stdout}\n{result.stderr}"
    lines = result.stdout.strip().splitlines()
    assert lines[0] == "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"


@pytest.mark.parametrize(
    "case,tests,args",
    [
        (
            "failing_assertion",
            {
                "test_solution.py": (
                    "from solution import Solution\n"
                    "def test_fail():\n    assert Solution().add(1, 1) == 99\n"
                )
            },
            ["--submit", "--max-calls", "10"],
        ),
        (
            "budget_overflow",
            {
                "test_solution.py": (
                    "from solution import Solution\n"
                    "def test_1():\n    assert Solution().add(1, 1) == 2\n"
                    "def test_2():\n    assert Solution().add(2, 2) == 4\n"
                    "def test_3():\n    assert Solution().add(3, 3) == 6\n"
                )
            },
            ["--submit", "--max-calls", "2"],
        ),
        (
            "empty_suite_no_files",
            {},
            ["--submit", "--max-calls", "10"],
        ),
        (
            "empty_suite_no_tests",
            {"test_solution.py": "# empty test file\n"},
            ["--submit", "--max-calls", "10"],
        ),
        (
            "partial_suite_flag",
            {
                "test_solution.py": (
                    "from solution import Solution\n"
                    "def test_1():\n    assert Solution().add(1, 1) == 2\n"
                )
            },
            ["--submit", "--max-calls", "10", "-k", "test_1"],
        ),
        (
            "partial_suite_file_selector",
            {
                "test_solution.py": (
                    "from solution import Solution\n"
                    "def test_1():\n    assert Solution().add(1, 1) == 2\n"
                )
            },
            ["--submit", "--max-calls", "10", "tests/test_solution.py"],
        ),
        (
            "missing_max_calls",
            {
                "test_solution.py": (
                    "from solution import Solution\n"
                    "def test_1():\n    assert Solution().add(1, 1) == 2\n"
                )
            },
            ["--submit"],
        ),
        (
            "zero_max_calls",
            {
                "test_solution.py": (
                    "from solution import Solution\n"
                    "def test_1():\n    assert Solution().add(1, 1) == 2\n"
                )
            },
            ["--submit", "--max-calls", "0"],
        ),
        (
            "negative_max_calls",
            {
                "test_solution.py": (
                    "from solution import Solution\n"
                    "def test_1():\n    assert Solution().add(1, 1) == 2\n"
                )
            },
            ["--submit", "--max-calls", "-5"],
        ),
        (
            "bad_syntax",
            {"test_solution.py": "def test_syntax(:\n    assert True\n"},
            ["--submit", "--max-calls", "10"],
        ),
        (
            "source_access_forbidden",
            {"test_solution.py": "import os\ndef test_os():\n    assert True\n"},
            ["--submit", "--max-calls", "10"],
        ),
    ],
)
def test_submit_rejects_invalid_runs(tmp_path, case, tests, args):
    make_workspace(tmp_path, tests=tests)
    result = subprocess.run(
        [sys.executable, str(RUN_TESTS)] + args,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" not in result.stdout


def test_diagnostic_mode_preserved(tmp_path):
    make_workspace(
        tmp_path,
        tests={
            "test_solution.py": (
                "from solution import Solution\n"
                "def test_1():\n    assert Solution().add(1, 2) == 3\n"
                "def test_2():\n    assert Solution().add(0, 0) == 0\n"
            )
        },
    )
    result = subprocess.run(
        [sys.executable, str(RUN_TESTS), "-k", "test_1"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" not in result.stdout
    assert "1 passed" in result.stdout
