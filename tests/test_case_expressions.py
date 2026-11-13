"""Deterministic tests for restricted case expressions evaluator."""

import multiprocessing
import sys
from pathlib import Path

import pytest

# Ensure student whitebox tools directory is on sys.path
whitebox_dir = Path(__file__).resolve().parent.parent / "student" / "tools" / "whitebox"
if str(whitebox_dir) not in sys.path:
    sys.path.insert(0, str(whitebox_dir))

import _case_expressions
from _case_expressions import CaseExpressionError, evaluate_cases


def test_list_repetition_and_comprehensions():
    res1 = evaluate_cases('[{"nums": [1] * 3, "k": 1}]')
    assert res1 == [{"nums": [1, 1, 1], "k": 1}]

    res2 = evaluate_cases('[{"nums": [0] * n, "k": n} for n in [1, 2]]')
    assert res2 == [{"nums": [0], "k": 1}, {"nums": [0, 0], "k": 2}]


def test_comb_gcd_isqrt_sizing():
    res1 = evaluate_cases('[{"count": comb(5, 2), "arr": [0] * comb(4, 2)}]')
    assert res1 == [{"count": 10, "arr": [0, 0, 0, 0, 0, 0]}]

    res2 = evaluate_cases('[{"g": gcd(18, 24), "root": isqrt(49)}]')
    assert res2 == [{"g": 6, "root": 7}]


def test_nested_matrices_preserved():
    res = evaluate_cases('[{"matrix": [[i * 2 + j for j in range(2)] for i in range(2)]}]')
    assert res == [{"matrix": [[0, 1], [2, 3]]}]

    cube = evaluate_cases('[{"cube": [[[0, 1]], [[2, 3]]]}]')
    assert cube == [{"cube": [[[0, 1]], [[2, 3]]]}]


def test_forbidden_attribute_access():
    with pytest.raises(CaseExpressionError):
        evaluate_cases("[x.__class__ for x in [1]]")
    with pytest.raises(CaseExpressionError):
        evaluate_cases("[x.imag for x in [1]]")
    with pytest.raises(CaseExpressionError):
        evaluate_cases('["abc".upper()]')


def test_forbidden_dunder_names():
    with pytest.raises(CaseExpressionError):
        evaluate_cases("[__name__]")
    with pytest.raises(CaseExpressionError):
        evaluate_cases("[__builtins__]")
    with pytest.raises(CaseExpressionError):
        evaluate_cases('[{"a": __doc__}]')


def test_forbidden_open_and_import():
    with pytest.raises(CaseExpressionError):
        evaluate_cases('[open("/etc/passwd")]')
    with pytest.raises(CaseExpressionError):
        evaluate_cases('[__import__("sys")]')
    with pytest.raises(CaseExpressionError):
        evaluate_cases("import os")


def test_forbidden_arbitrary_calls_and_builtins():
    with pytest.raises(CaseExpressionError):
        evaluate_cases("[(lambda x: x)(1)]")
    with pytest.raises(CaseExpressionError):
        evaluate_cases('[eval("[1]")]')
    with pytest.raises(CaseExpressionError):
        evaluate_cases('[getattr(1, "real")]')
    with pytest.raises(CaseExpressionError):
        evaluate_cases("[type(1)]")
    with pytest.raises(CaseExpressionError):
        evaluate_cases("[globals()]")
    with pytest.raises(CaseExpressionError):
        evaluate_cases("[f() for f in [len]]")


def test_non_list_rejected():
    with pytest.raises(CaseExpressionError):
        evaluate_cases('{"nums": [1]}')
    with pytest.raises(CaseExpressionError):
        evaluate_cases('(x for x in [{"nums": [1]}])')
    with pytest.raises(CaseExpressionError):
        evaluate_cases("123")


def test_nonfinite_rejected():
    with pytest.raises(CaseExpressionError):
        evaluate_cases('[{"x": 1e308 * 1e308}]')
    with pytest.raises(CaseExpressionError):
        evaluate_cases('[{"x": float("nan")}]')


def test_nonserializable_and_non_json_native_rejected():
    with pytest.raises(CaseExpressionError):
        evaluate_cases('[{"nums": (1, 2)}]')
    with pytest.raises(CaseExpressionError):
        evaluate_cases("[(1, 2)]")
    with pytest.raises(CaseExpressionError):
        evaluate_cases('[{"nums": {1, 2}}]')
    with pytest.raises(CaseExpressionError):
        evaluate_cases('[{"nums": range(5)}]')
    with pytest.raises(CaseExpressionError):
        evaluate_cases('[{1: "a"}]')


def test_cpu_consuming_generator_terminated_without_lingering_worker(monkeypatch):
    monkeypatch.setattr(_case_expressions, "MAX_WALL_SECONDS", 0.2)
    with pytest.raises(CaseExpressionError):
        evaluate_cases("[x for x in range(10**14) if all(y >= 0 for y in range(10**14))]")
    assert not multiprocessing.active_children()


def test_alloc_bomb_handled_without_lingering_worker():
    with pytest.raises(CaseExpressionError):
        evaluate_cases("[0] * (500 * 1024 * 1024)")
    assert not multiprocessing.active_children()


def test_output_size_cap_rejected(monkeypatch):
    monkeypatch.setattr(_case_expressions, "MAX_OUTPUT_BYTES", 1024)
    with pytest.raises(CaseExpressionError):
        evaluate_cases('["x" * 2048]')
    assert not multiprocessing.active_children()


def test_empty_and_syntax_error_rejected():
    with pytest.raises(CaseExpressionError):
        evaluate_cases("")
    with pytest.raises(CaseExpressionError):
        evaluate_cases("   ")
    with pytest.raises(CaseExpressionError):
        evaluate_cases("def f(): pass")


@pytest.mark.parametrize(
    "limit_kind",
    [_case_expressions.resource.RLIMIT_AS, _case_expressions.resource.RLIMIT_CPU],
)
def test_resource_limit_setup_failure_rejects_evaluation(monkeypatch, limit_kind):
    original = _case_expressions.resource.setrlimit

    def reject_limit(kind, limits):
        if kind == limit_kind:
            raise OSError("Limit setup unavailable")
        original(kind, limits)

    monkeypatch.setattr(_case_expressions.resource, "setrlimit", reject_limit)
    with pytest.raises(CaseExpressionError):
        evaluate_cases('[{"x": 1}]')
    assert not multiprocessing.active_children()
