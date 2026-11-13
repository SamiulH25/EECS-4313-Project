"""Pytest plugin used for every measured run.

Enabled with `-p testagent.guard`. It
- keeps only the first TESTAGENT_MAX_TESTS tests in collection order,
- runs only the tests listed in TESTAGENT_ONLY (a JSON file), if set,
- shuffles test order (TESTAGENT_SEED),
- stops any single test after TESTAGENT_TEST_TIMEOUT seconds,
- stops test code from opening the project's source files or reading code objects from it, since
  such a test could detect a changed line without testing behaviour,
- writes each test's outcome to TESTAGENT_OUTCOMES.
"""

import json
import os
import random
import signal
import sys

import pytest

_outcomes: dict[str, str] = {}
_source_dir = ""
_tests_dir = ""
_package = ""


class SourceAccessError(Exception):
    """Raised when a test tries to read the code under test."""


def _from_test_code() -> bool:
    frame = sys._getframe(2)
    while frame is not None:
        filename = frame.f_code.co_filename
        if filename.startswith("<frozen importlib"):
            return False
        if os.path.realpath(filename).startswith(_tests_dir):
            return True
        frame = frame.f_back
    return False


def _audit(event, args):
    if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        if os.path.realpath(os.fsdecode(args[0])).startswith(_source_dir) and _from_test_code():
            raise SourceAccessError("tests may not open the project's source files")
    elif event == "object.__getattr__" and len(args) >= 2 and args[1] == "__code__":
        module = getattr(args[0], "__module__", None) or ""
        if (module == _package or module.startswith(_package + ".")) and _from_test_code():
            raise SourceAccessError("tests may not read code objects of the project")


def _timeout(signum, frame):
    raise TimeoutError(f"test took longer than {os.environ.get('TESTAGENT_TEST_TIMEOUT')} seconds")


def pytest_configure(config):
    global _source_dir, _tests_dir, _package
    _package = os.environ.get("TESTAGENT_PACKAGE", "")
    _source_dir = os.path.realpath(os.environ.get("TESTAGENT_SOURCE", _package or "src")) + os.sep
    _tests_dir = os.path.realpath(os.environ.get("TESTAGENT_TESTS", "tests_new")) + os.sep
    sys.addaudithook(_audit)


def pytest_collection_modifyitems(session, config, items):
    limit = int(os.environ.get("TESTAGENT_MAX_TESTS", "0") or 0)
    if limit and len(items) > limit:
        config.hook.pytest_deselected(items=items[limit:])
        del items[limit:]
    only = os.environ.get("TESTAGENT_ONLY")
    if only:
        keep = set(json.loads(open(only).read()))
        dropped = [item for item in items if item.nodeid not in keep]
        items[:] = [item for item in items if item.nodeid in keep]
        if dropped:
            config.hook.pytest_deselected(items=dropped)
    seed = os.environ.get("TESTAGENT_SEED", "")
    if seed:
        random.Random(int(seed)).shuffle(items)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    limit = int(os.environ.get("TESTAGENT_TEST_TIMEOUT", "0") or 0)
    if limit:
        signal.signal(signal.SIGALRM, _timeout)
        signal.alarm(limit)
    try:
        yield
    finally:
        if limit:
            signal.alarm(0)


def pytest_runtest_logreport(report):
    if report.failed:
        _outcomes[report.nodeid] = "failed"
    elif report.skipped:
        _outcomes.setdefault(report.nodeid, "skipped")
    elif report.when == "call" and report.passed:
        _outcomes.setdefault(report.nodeid, "passed")


def pytest_collectreport(report):
    if report.failed:
        _outcomes[f"<collection>::{report.nodeid}"] = "error"


def pytest_sessionfinish(session, exitstatus):
    path = os.environ.get("TESTAGENT_OUTCOMES")
    if path:
        with open(path, "w") as handle:
            json.dump(_outcomes, handle)
