"""Exercise student tools in isolated task folders without model calls."""

import ast
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from testagent import REPO_ROOT

TOOLS = REPO_ROOT / "student" / "tools" / "whitebox"


def test_probe_tests_preserve_inputs_and_use_fresh_instances(tmp_path):
    (tmp_path / "solution.py").write_text(
        "class Solution:\n"
        "    def f(self, xs):\n"
        "        self.calls = getattr(self, 'calls', 0) + 1\n"
        "        xs.append(self.calls)\n"
        "        return len(xs) + self.calls\n"
    )
    result = subprocess.run(
        [sys.executable, str(TOOLS / "probe")],
        input='[{"xs": [0]}, {"xs": [1, 2]}]\n',
        cwd=tmp_path, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_generated.py").write_text(
        "from solution import Solution\n" + result.stdout
    )
    executed = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"],
        cwd=tmp_path, capture_output=True, text=True, timeout=20,
    )
    assert executed.returncode == 0, executed.stdout + executed.stderr


@pytest.mark.parametrize(
    "bad_input",
    ["not an expression", '[{"x": 2}, {"x": 0}]', '[{"x": 2}, {"wrong": 1}]', '[{"x": 2}, []]', '{"x": 2}'],
)
def test_probe_rejects_partial_batches(tmp_path, bad_input):
    (tmp_path / "solution.py").write_text(
        "class Solution:\n    def f(self, x):\n        return 10 // x\n"
    )
    result = subprocess.run(
        [sys.executable, str(TOOLS / "probe")],
        input=bad_input,
        cwd=tmp_path, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    assert result.stdout == "", "Failed batches must not append partial tests"


@pytest.mark.parametrize("wrong_oracle", [False, True])
def test_mutants_require_a_passing_original_suite(tmp_path, wrong_oracle):
    (tmp_path / "solution.py").write_text(
        "class Solution:\n    def f(self, x):\n        return x + 1\n"
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_example.py").write_text(
        "from solution import Solution\n"
        + "".join(
            f"def test_{i}():\n    assert Solution().f({i}) == {99 if wrong_oracle else i + 1}\n"
            for i in range(4)
        )
    )
    result = subprocess.run(
        [sys.executable, str(TOOLS / "mutants"), "--max", "1"],
        cwd=tmp_path, capture_output=True, text=True, timeout=45,
    )
    if wrong_oracle:
        assert result.returncode != 0
        assert "no mutants scored" in result.stdout
        assert "killed," not in result.stdout
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 mutants tested, 1 killed, 0 survived" in result.stdout


def test_probe_saves_valid_cases_and_enforces_shared_budget_across_batches(tmp_path):
    (tmp_path / "solution.py").write_text(
        "class Solution:\n    def f(self, x):\n        return 10 // x\n"
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_blackbox.py").write_text(
        "from solution import Solution\n"
        "def test_inherited():\n    assert Solution().f(1) == 10\n"
    )
    command = [
        sys.executable, str(TOOLS / "probe"), "--output", "tests/test_whitebox.py",
        "--max-calls", "4",
    ]
    first = subprocess.run(
        command, input='[{"x": 1}, {"x": 2}, {"x": 0}, {"x": 4}]', cwd=tmp_path,
        capture_output=True, text=True, timeout=10,
    )
    assert first.returncode != 0, "Invalid inputs must be reported even when valid cases are saved"
    assert "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" not in first.stdout
    second = subprocess.run(
        command, input='[{"x": 2}, {"x": 5}, {"x": 10}]', cwd=tmp_path,
        capture_output=True, text=True, timeout=10,
    )
    assert second.returncode == 0, second.stderr
    lines = second.stdout.strip().splitlines()
    assert lines[0] == "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "--junitxml=results.xml", "-p", "no:cacheprovider"],
        cwd=tmp_path, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = ET.parse(tmp_path / "results.xml")
    assert report.find("testsuite").attrib["tests"] == "4"
    calls = [
        ast.literal_eval(kw.value)
        for node in ast.walk(ast.parse((tests / "test_whitebox.py").read_text()))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "f"
        for kw in node.keywords if kw.arg == "x"
    ]
    assert calls == [2, 4, 5]


def test_probe_named_mapping_key_order_dedup_and_arg_validation(tmp_path):
    (tmp_path / "solution.py").write_text(
        "class Solution:\n"
        "    def solve(self, grid, k):\n"
        "        return sum(sum(row) for row in grid) + k\n"
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_existing.py").write_text(
        "from solution import Solution\n"
        "def test_base():\n"
        "    assert Solution().solve([[1, 2]], 2) == 5\n"
    )
    batch_input = (
        '[\n'
        '  {"k": 2, "grid": [[1, 2]]},\n'
        '  {"grid": [[3, 4]]},\n'
        '  {"grid": [[3, 4]], "k": 1, "extra": 99},\n'
        '  {"grid": [[3, 4]], "k": 5},\n'
        '  {"k": 5, "grid": [[3, 4]]},\n'
        ']'
    )
    command = [
        sys.executable, str(TOOLS / "probe"), "--output", "tests/test_whitebox.py",
        "--func", "solve",
    ]
    res = subprocess.run(
        command, input=batch_input, cwd=tmp_path,
        capture_output=True, text=True, timeout=10,
    )
    assert res.returncode != 0
    assert "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" not in res.stdout
    executed = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "--junitxml=results.xml", "-p", "no:cacheprovider"],
        cwd=tmp_path, capture_output=True, text=True, timeout=20,
    )
    assert executed.returncode == 0, executed.stdout + executed.stderr
    assert ET.parse(tmp_path / "results.xml").find("testsuite").attrib["tests"] == "2"
    (tmp_path / "solution.py").write_text(
        "class Solution:\n"
        "    def solve(self, grid, k):\n"
        "        return 0 if grid == [[3, 4]] else sum(sum(row) for row in grid) + k\n"
    )
    rejected = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_whitebox.py", "-q", "-p", "no:cacheprovider"],
        cwd=tmp_path, capture_output=True, text=True, timeout=20,
    )
    assert rejected.returncode == 1, rejected.stdout + rejected.stderr


def test_probe_rejects_empty_output_batch(tmp_path):
    (tmp_path / "solution.py").write_text(
        "class Solution:\n    def f(self, x):\n        return x + 1\n"
    )
    result = subprocess.run(
        [sys.executable, str(TOOLS / "probe"), "--output", "tests/test_generated.py"],
        input="", cwd=tmp_path, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    assert not (tmp_path / "tests" / "test_generated.py").exists()
    assert "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" not in result.stdout

    empty_list_result = subprocess.run(
        [sys.executable, str(TOOLS / "probe"), "--output", "tests/test_generated.py"],
        input="[]", cwd=tmp_path, capture_output=True, text=True, timeout=10,
    )
    assert empty_list_result.returncode != 0
    assert not (tmp_path / "tests" / "test_generated.py").exists()
    assert "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" not in empty_list_result.stdout


def test_probe_output_mode_submits_automatically_on_clean_batch(tmp_path):
    (tmp_path / "solution.py").write_text(
        "class Solution:\n    def f(self, x):\n        return x * 2\n"
    )
    command = [
        sys.executable, str(TOOLS / "probe"), "--output", "tests/test_direct.py",
        "--max-calls", "4",
    ]
    result = subprocess.run(
        command, input='[{"x": 1}, {"x": 2}]', cwd=tmp_path,
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert lines[0] == "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"


def test_probe_rejects_slow_cases_and_keeps_fast_ones(tmp_path):
    (tmp_path / "solution.py").write_text(
        "import time\n"
        "class Solution:\n"
        "    def f(self, x):\n"
        "        try:\n"
        "            while x == 0:\n"
        "                time.sleep(0.01)\n"
        "        except Exception:\n"
        "            return -1\n"
        "        return x\n"
    )
    result = subprocess.run(
        [sys.executable, str(TOOLS / "probe"), "--output", "tests/test_combined.py"],
        input='[{"x": 1}, {"x": 0}, {"x": 2}]', cwd=tmp_path,
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode != 0
    assert "Too slow" in result.stderr
    assert "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" not in result.stdout
    source = (tmp_path / "tests" / "test_combined.py").read_text()
    assert "f(x=1)" in source and "f(x=2)" in source and "f(x=0)" not in source


def test_probe_no_submit_seeds_then_large_batch_submits_despite_rejects(tmp_path):
    (tmp_path / "solution.py").write_text(
        "class Solution:\n    def f(self, x):\n        return 10 // x\n"
    )
    command = [sys.executable, str(TOOLS / "probe"), "--output", "tests/test_combined.py"]
    seed = subprocess.run(
        command + ["--no-submit"], input='[{"x": 1}, {"x": 2}]', cwd=tmp_path,
        capture_output=True, text=True, timeout=20,
    )
    assert seed.returncode == 0, seed.stderr
    assert "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" not in seed.stdout
    main = subprocess.run(
        command, input="[{'x': x} for x in range(0, 25)]", cwd=tmp_path,
        capture_output=True, text=True, timeout=30,
    )
    assert main.returncode == 0, main.stdout + main.stderr
    assert main.stdout.splitlines()[0] == "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"


def test_probe_accepts_expression_argument_and_summarizes_seed(tmp_path):
    (tmp_path / "solution.py").write_text(
        "class Solution:\n    def f(self, x):\n        return x > 0\n"
    )
    result = subprocess.run(
        [sys.executable, str(TOOLS / "probe"), "--output", "tests/test_combined.py", "--no-submit",
         '[{"x": 1}, {"x": 2}, {"x": 3}, {"x": 4}]'],
        cwd=tmp_path, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "Saved 4 tests" in result.stderr
    assert "most common True x4" in result.stderr and "Most answers are identical" in result.stderr


def test_probe_augments_with_reordered_input_that_kills_a_mutant(tmp_path):
    (tmp_path / "solution.py").write_text(
        "class Solution:\n"
        "    def f(self, nums):\n"
        "        nums.sort()\n"
        "        return nums[0] - nums[-1]\n"
    )
    result = subprocess.run(
        [sys.executable, str(TOOLS / "probe"), "--output", "tests/test_combined.py", "--max-calls", "4"],
        input='[{"nums": [1, 2, 3]}, {"nums": [2, 5, 9]}]', cwd=tmp_path,
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines()[0] == "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
    calls = [
        ast.literal_eval(kw.value)
        for node in ast.walk(ast.parse((tmp_path / "tests" / "test_combined.py").read_text()))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "f"
        for kw in node.keywords
    ]
    assert calls[:2] == [[1, 2, 3], [2, 5, 9]]
    assert any(c != sorted(c) for c in calls[2:]), calls


@pytest.mark.parametrize("broken", [
    '[{"n": 2, "r": [[1, 2], [3, 4]]}, {"n": 3, "r": [[5, 6]]}',
    '[{"n": 2, "r": [[1, 2], [3, 4]]}, {"n": 3, "r": [[5, 6]]]',
])
def test_probe_repairs_unbalanced_closing_brackets(tmp_path, broken):
    (tmp_path / "solution.py").write_text(
        "class Solution:\n    def f(self, n, r):\n        return n + len(r)\n"
    )
    result = subprocess.run(
        [sys.executable, str(TOOLS / "probe"), "--output", "tests/test_combined.py", "--no-submit", broken],
        cwd=tmp_path, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "repaired unbalanced closing brackets" in result.stderr
    assert "Saved 2 tests" in result.stderr


def test_probe_does_not_auto_submit_a_tiny_suite(tmp_path):
    (tmp_path / "solution.py").write_text(
        "class Solution:\n    def f(self, x):\n        return x * 2\n"
    )
    result = subprocess.run(
        [sys.executable, str(TOOLS / "probe"), "--output", "tests/test_combined.py", '[{"x": 1}, {"x": 2}]'],
        cwd=tmp_path, capture_output=True, text=True, timeout=20,
    )
    assert "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" not in result.stdout
    assert "send one more batch of at least 18 new cases" in result.stderr
