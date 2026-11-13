"""Property runner: executes the model's `prop_<name>(r)` functions on many random inputs.

Run inside the workspace:  python -m tagent.props <request.json> <result.json>

The model states a documented rule once, as a function of a seeded `random.Random`; this tool explores the
input space (property-based testing), records which inputs break the rule and whether the failure is an
assertion (the rule does not hold) or a crash inside the property itself (wrong use of the API).
The agent turns failing seeds into ordinary deterministic tests.
"""

from __future__ import annotations

import importlib.util
import json
import random
import signal
import sys
import time
import traceback
from pathlib import Path


class _Timeout(Exception):
    pass


def _alarm(signum, frame):
    raise _Timeout()


def _where(tb, pkg: str, test_file: str) -> str:
    frames = traceback.extract_tb(tb)
    for f in reversed(frames):
        if f"/{pkg}/" in f.filename.replace("\\", "/") or f.filename.startswith(pkg):
            return "package"
        if f.filename.endswith(Path(test_file).name):
            return "property"
    return "other"


def run(test_file: str, pkg: str, names: list[str], seeds: int, deadline: float) -> dict:
    sys.path.insert(0, str(Path.cwd()))
    spec = importlib.util.spec_from_file_location("_tagent_props_mod", test_file)
    mod = importlib.util.module_from_spec(spec)
    signal.signal(signal.SIGALRM, _alarm)
    try:
        spec.loader.exec_module(mod)
    except BaseException as e:  # noqa: BLE001
        return {"error": f"import failed: {type(e).__name__}: {e}"[:300], "props": {}}
    out = {}
    end = time.time() + deadline
    per = max(2.0, deadline / max(1, len(names)))
    for name in names:
        fn = getattr(mod, name, None)
        res = {"runs": 0, "fails": [], "first": "", "where": "", "kinds": {}}
        out[name] = res
        if not callable(fn):
            res["first"] = "not defined"
            continue
        stop = min(end, time.time() + per)
        for seed in range(seeds):
            if time.time() > stop:
                break
            signal.setitimer(signal.ITIMER_REAL, 2.0)
            try:
                fn(random.Random(seed))
                ok = True
            except _Timeout:
                ok, kind, where, msg = False, "Timeout", "other", "took more than 2 s"
            except BaseException as e:  # noqa: BLE001
                ok = False
                kind = type(e).__name__
                where = _where(e.__traceback__, pkg, test_file)
                if kind == "AssertionError":
                    where = "assert"
                msg = (str(e).split("\n")[0] or kind)[:200]
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
            res["runs"] += 1
            if not ok:
                res["fails"].append(seed)
                res["kinds"][f"{kind}@{where}"] = res["kinds"].get(f"{kind}@{where}", 0) + 1
                if not res["first"]:
                    res["first"], res["where"] = f"{kind}: {msg}", where
    return {"props": out}


def main():
    req = json.loads(Path(sys.argv[1]).read_text())
    res = run(req["file"], req["package"], req["props"], int(req.get("seeds", 200)), float(req.get("deadline", 60)))
    Path(sys.argv[2]).write_text(json.dumps(res))


if __name__ == "__main__":
    main()
