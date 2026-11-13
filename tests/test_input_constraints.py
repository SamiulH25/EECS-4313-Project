"""Probe enforces recognizable SPEC.md constraints before calling the oracle."""

import subprocess
import sys

from testagent import REPO_ROOT

PROBE = REPO_ROOT / "student" / "tools" / "whitebox" / "probe"


def run_probe(work, spec, solution, batch, *args):
    (work / "solution.py").write_text(solution)
    if spec is not None:
        (work / "SPEC.md").write_text(spec)
    return subprocess.run(
        [sys.executable, str(PROBE), *args],
        input=batch, cwd=work, capture_output=True, text=True, timeout=20,
    )


EVENTS_SPEC = """# Buttons

Constraints:

1 <= events.length <= 1000
events[i] == [index_i, time_i]
1 <= index_i, time_i <= 10^5
"""


def test_probe_blocks_bound_component_fields(tmp_path):
    solution = "class Solution:\n    def f(self, events):\n        return len(events)\n"
    for batch in ('[{"events": [[0, 5]]}]', '[{"events": [[1, 100001]]}]', '[{"events": []}]'):
        result = run_probe(tmp_path, EVENTS_SPEC, solution, batch)
        assert result.returncode == 1, result.stdout + result.stderr
        assert result.stdout == ""
        assert "violates SPEC constraint" in result.stderr

    accepted = run_probe(tmp_path, EVENTS_SPEC, solution, '[{"events": [[1, 1], [100000, 100000]]}]')
    assert accepted.returncode == 0, accepted.stderr
    assert accepted.stdout.count("def test_") == 1


LENGTH_SPEC = """# Shift

Constraints:

1 <= s.length == t.length <= 10^5
nextCost.length == 26
"""


def test_probe_enforces_chained_equal_lengths_and_constant_width(tmp_path):
    solution = "class Solution:\n    def f(self, s, t, nextCost):\n        return 0\n"
    blocked = [
        '[{"s": "ab", "t": "abc", "nextCost": [0] * 26}]',
        '[{"s": "a", "t": "a", "nextCost": [0] * 25}]',
        '[{"s": "", "t": "", "nextCost": [0] * 26}]',
    ]
    for batch in blocked:
        result = run_probe(tmp_path, LENGTH_SPEC, solution, batch)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "violates SPEC constraint" in result.stderr
    accepted = run_probe(
        tmp_path, LENGTH_SPEC, solution,
        '[{"s": "a", "t": "b", "nextCost": [0] * 26}]',
    )
    assert accepted.returncode == 0, accepted.stderr


DIGIT_SPEC = """# Transform

Constraints:

1 <= n, m < 10^4
n and m consist of the same number of digits.
"""


def test_probe_enforces_same_digit_width(tmp_path):
    solution = "class Solution:\n    def f(self, n, m):\n        return 0\n"
    for batch, expected in (
        ('[{"n": 999, "m": 1000}]', "same number of digits"),
        ('[{"n": 1000, "m": 10000}]', "violates SPEC constraint"),
    ):
        result = run_probe(tmp_path, DIGIT_SPEC, solution, batch)
        assert result.returncode == 1, result.stdout + result.stderr
        assert expected in result.stderr
    accepted = run_probe(tmp_path, DIGIT_SPEC, solution, '[{"n": 9999, "m": 1000}]')
    assert accepted.returncode == 0, accepted.stderr


COINS_SPEC = """# Coins

Constraints:

1 <= coins.length <= 10^5
1 <= k <= 10^9
coins[i] == [l_i, r_i, c_i]
1 <= l_i <= r_i <= 10^9
1 <= c_i <= 1000
The given segments are non-overlapping.
"""


def test_probe_blocks_overlapping_closed_segments_and_bad_fields(tmp_path):
    solution = "class Solution:\n    def f(self, coins, k):\n        return 0\n"
    for batch, expected in (
        ('[{"coins": [[1, 5, 1], [4, 6, 1]], "k": 2}]', "entries overlap"),
        ('[{"coins": [[1, 5, 1], [5, 6, 1]], "k": 2}]', "entries overlap"),
        ('[{"coins": [[1, 5, 0]], "k": 2}]', "violates SPEC constraint"),
        ('[{"coins": [[6, 5, 1]], "k": 2}]', "violates SPEC constraint"),
    ):
        result = run_probe(tmp_path, COINS_SPEC, solution, batch)
        assert result.returncode == 1, result.stdout + result.stderr
        assert result.stdout == ""
        assert expected in result.stderr
    accepted = run_probe(
        tmp_path, COINS_SPEC, solution,
        '[{"coins": [[5, 6, 4], [1, 3, 2], [8, 10, 1]], "k": 2}]',
    )
    assert accepted.returncode == 0, accepted.stderr


def test_probe_keeps_valid_cases_when_another_case_violates(tmp_path):
    solution = "class Solution:\n    def f(self, coins, k):\n        return 0\n"
    result = run_probe(
        tmp_path, COINS_SPEC, solution,
        '[{"coins": [[1, 2, 1]], "k": 2}, {"coins": [[1, 5, 1], [4, 6, 1]], "k": 2}]',
        "--output", "tests/test_whitebox.py", "--max-calls", "4",
    )
    assert result.returncode == 1
    assert "entries overlap" in result.stderr
    assert "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" not in result.stdout
    saved = (tmp_path / "tests" / "test_whitebox.py").read_text()
    assert saved.count("def test_") == 1


RECTANGLES_SPEC = """# Cuts

Constraints:

3 <= n <= 10^9
3 <= rectangles.length <= 10^5
0 <= rectangles[i][0] < rectangles[i][2] <= n
0 <= rectangles[i][1] < rectangles[i][3] <= n
No two rectangles overlap.
"""


def test_probe_rectangles_overlap_by_area_not_by_touching_edges(tmp_path):
    solution = "class Solution:\n    def f(self, n, rectangles):\n        return False\n"
    for batch, expected in (
        ('[{"n": 4, "rectangles": [[0, 0, 3, 3], [2, 2, 4, 4], [0, 0, 1, 1]]}]', "rectangles overlap"),
        ('[{"n": 4, "rectangles": [[0, 0, 5, 2], [0, 2, 2, 4], [3, 2, 5, 3]]}]', "violates SPEC constraint"),
    ):
        result = run_probe(tmp_path, RECTANGLES_SPEC, solution, batch)
        assert result.returncode == 1, result.stdout + result.stderr
        assert expected in result.stderr
    accepted = run_probe(
        tmp_path, RECTANGLES_SPEC, solution,
        '[{"n": 4, "rectangles": [[0, 0, 2, 2], [2, 0, 4, 2], [0, 2, 2, 4]]}]',
    )
    assert accepted.returncode == 0, accepted.stderr


MATRIX_SPEC = """# Grid

Constraints:

2 <= n == grid.length == grid[i].length <= 5
0 <= grid[i][j] < 16
"""


def test_probe_enforces_nested_dimensions_and_leaf_bounds(tmp_path):
    solution = "class Solution:\n    def f(self, n, grid):\n        return 0\n"
    for batch, expected in (
        ('[{"n": 2, "grid": [[1], [2]]}]', "violates SPEC constraint"),
        ('[{"n": 2, "grid": [[16, 0], [0, 0]]}]', "violates SPEC constraint"),
        ('[{"n": 6, "grid": [[0, 0], [0, 0]]}]', "violates SPEC constraint"),
    ):
        result = run_probe(tmp_path, MATRIX_SPEC, solution, batch)
        assert result.returncode == 1, result.stdout + result.stderr
        assert expected in result.stderr
    accepted = run_probe(
        tmp_path, MATRIX_SPEC, solution,
        '[{"n": 2, "grid": [[15, 0], [0, 15]]}]',
    )
    assert accepted.returncode == 0, accepted.stderr


def test_probe_does_not_guess_unsupported_constraint_semantics(tmp_path):
    spec = """# Scores

Constraints:

travelScore[i][i] == 0
0 <= parent[i] <= n - 1 for all i >= 1.
1 < 2 * k <= nums.length
"""
    solution = (
        "class Solution:\n"
        "    def f(self, travelScore, parent, nums, k):\n"
        "        return 0\n"
    )
    batch = (
        '[{"travelScore": [[0, 9], [9, 0]], "parent": [-1, 0], "nums": [], "k": 1}]'
    )
    result = run_probe(tmp_path, spec, solution, batch)
    assert result.returncode == 0, result.stderr


def test_probe_without_spec_keeps_working(tmp_path):
    solution = "class Solution:\n    def f(self, x):\n        return x\n"
    result = run_probe(tmp_path, None, solution, '[{"x": -1}]')
    assert result.returncode == 0, result.stderr
    assert "def test_probe_1" in result.stdout


def test_list_constraints_suggests_only_legal_integer_boundaries(tmp_path):
    import re

    tool = REPO_ROOT / "student" / "tools" / "blackbox" / "list_constraints"
    for constraint, low, high in (
        ("-10^6 <= x <= 10^6", -1000000, 1000000),
        ("1 <= x < 10^4", 1, 9999),
        ("0 < x < 2", 1, 1),
        ("-2 * 10^3 < x <= -10^2", -1999, -100),
    ):
        (tmp_path / "SPEC.md").write_text("Constraints:\n" + constraint)
        result = subprocess.run(
            [sys.executable, str(tool)], cwd=tmp_path, capture_output=True, text=True, timeout=5,
        )
        assert result.returncode == 0, result.stderr
        values = [int(value) for value in re.findall(r"-?\d+", result.stdout)]
        assert min(values) == low
        assert max(values) == high
        assert all(low <= value <= high for value in values)
