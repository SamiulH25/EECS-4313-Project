"""Pytest plugin: record line-to-line arcs executed in the package under test (TAGENT_COV_PKG) and
write them to TAGENT_COV_OUT as {relative file: [[from_line, to_line], ...]}.

Used for coverage-guided generation: later rounds are told which risk points and branch outcomes
no test has exercised yet.
"""

import json
import os
import sys
import threading

_arcs: dict[str, set] = {}
_pkg_dir = ""
_root = ""


def _tracer(frame, event, arg):
    filename = frame.f_code.co_filename
    if not filename.startswith(_pkg_dir):
        return None
    rel = filename[len(_root):].lstrip(os.sep)
    arcs = _arcs.setdefault(rel, set())
    state = {"last": -frame.f_code.co_firstlineno}

    def local(frame, event, arg):
        if event == "line":
            arcs.add((state["last"], frame.f_lineno))
            state["last"] = frame.f_lineno
        elif event == "return":
            arcs.add((state["last"], -frame.f_code.co_firstlineno))
        return local

    return local


def pytest_configure(config):
    global _pkg_dir, _root
    pkg = os.environ.get("TAGENT_COV_PKG", "")
    if not pkg or not os.environ.get("TAGENT_COV_OUT"):
        return
    _root = os.path.realpath(os.getcwd())
    _pkg_dir = os.path.join(_root, pkg) + os.sep
    sys.settrace(_tracer)
    threading.settrace(_tracer)


def pytest_sessionfinish(session, exitstatus):
    out = os.environ.get("TAGENT_COV_OUT")
    if not out or not _pkg_dir:
        return
    sys.settrace(None)
    with open(out, "w") as fh:
        json.dump({f: sorted(a) for f, a in _arcs.items()}, fh)
