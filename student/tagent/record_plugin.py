"""Pytest plugin: record every test's outcome, failure text and traceback frames to TAGENT_OUT.

Also shuffles test order (TAGENT_SEED) and stops a test after TAGENT_TIMEOUT seconds, like the
course evaluator does, so we see order dependence and slow tests before they are measured.
"""

import json
import os
import random
import signal

import pytest

_records: dict = {}


def _put(nodeid, rec):
    old = _records.get(nodeid)
    if old and old.get("outcome") == "failed":
        return
    _records[nodeid] = rec


def pytest_collectreport(report):
    if report.failed:
        _records["<collection>" + report.nodeid] = {"outcome": "failed", "when": "collect",
                                                     "text": str(report.longrepr)[-3000:]}


def pytest_collection_modifyitems(session, config, items):
    seed = os.environ.get("TAGENT_SEED", "")
    if seed:
        random.Random(int(seed)).shuffle(items)


def _alarm(signum, frame):
    raise TimeoutError("test took too long")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    limit = float(os.environ.get("TAGENT_TIMEOUT", "8"))
    old = signal.signal(signal.SIGALRM, _alarm)
    signal.setitimer(signal.ITIMER_REAL, limit)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def pytest_runtest_logreport(report):
    if report.when != "call" and not report.failed:
        return
    rec = {"outcome": report.outcome, "when": report.when}
    if report.failed:
        rec["text"] = report.longreprtext[-4000:]
        frames = []
        crash = None
        try:
            chain = getattr(report.longrepr, "chain", None)
            reprtb, reprcrash = (chain[-1][0], chain[-1][1]) if chain else (report.longrepr.reprtraceback,
                                                                          report.longrepr.reprcrash)
            for entry in reprtb.reprentries:
                loc = getattr(entry, "reprfileloc", None)
                if loc is not None:
                    frames.append([loc.path, loc.lineno, loc.message])
            if reprcrash is not None:
                crash = [reprcrash.path, reprcrash.lineno, reprcrash.message]
        except Exception:  # noqa: BLE001 - best effort only
            pass
        rec["frames"] = frames
        rec["crash"] = crash
    _put(report.nodeid, rec)


def pytest_sessionfinish(session, exitstatus):
    out = os.environ.get("TAGENT_OUT")
    if out:
        with open(out, "w") as fh:
            json.dump(_records, fh)
