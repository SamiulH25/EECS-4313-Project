"""Mutation-based repair check (technique: mutation analysis used as a test oracle filter).

The projects' bugs are small slips: a wrong comparison, an off-by-one constant, a wrong arithmetic
or boolean operator. So if a failing test reveals a real bug, a single small mutation of a line the
test executes should make the test pass, without breaking the project's existing test suite.
If no such repair exists, the test's expectation is most likely wrong and it is dropped.

Run as:  python -m tagent.repair REQUEST.json RESULT.json      (from the workspace folder)

REQUEST: {"package": str, "tests": ["path::Class::name" | "path::name", ...], "fixes": [site...],
          "deadline": seconds, "suite": "tests"}
RESULT:  {"baseline": {test: passed}, "explained": {test: [site, ...]}, "sites": {site: info},
          "suite_ok": {site: bool}, "notes": [...]}

A site is "relpath:start:end:replacement" on the package's source text (character offsets).
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

CMP_ALTS = {ast.Lt: [ast.LtE, ast.Gt], ast.LtE: [ast.Lt, ast.GtE], ast.Gt: [ast.GtE, ast.Lt],
            ast.GtE: [ast.Gt, ast.LtE], ast.Eq: [ast.NotEq], ast.NotEq: [ast.Eq], ast.Is: [ast.IsNot],
            ast.IsNot: [ast.Is], ast.In: [ast.NotIn], ast.NotIn: [ast.In]}
BIN_ALTS = {ast.Add: [ast.Sub], ast.Sub: [ast.Add], ast.Mult: [ast.FloorDiv, ast.Add], ast.FloorDiv: [ast.Mult, ast.Div],
            ast.Div: [ast.Mult, ast.FloorDiv], ast.Mod: [ast.FloorDiv], ast.LShift: [ast.RShift],
            ast.RShift: [ast.LShift], ast.BitAnd: [ast.BitOr], ast.BitOr: [ast.BitAnd], ast.Pow: [ast.Mult]}


def _offsets(text: str) -> list[int]:
    starts = [0]
    for line in text.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    return starts


def mutation_sites(rel: str, text: str, lines: set[int] | None) -> list[dict]:
    """All small mutations of expressions that start on one of `lines` (all lines if None)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    starts = _offsets(text)
    out = []

    def pos(node):
        return starts[node.lineno - 1] + node.col_offset, starts[node.end_lineno - 1] + node.end_col_offset

    def hit(node):
        if lines is None:
            return True
        return any(ln in lines for ln in range(node.lineno, node.end_lineno + 1))

    def add(node, new_node, kind):
        try:
            repl = "(" + ast.unparse(new_node) + ")"
        except Exception:  # noqa: BLE001
            return
        a, b = pos(node)
        out.append({"site": f"{rel}:{a}:{b}:{repl}", "file": rel, "start": a, "end": b, "repl": repl,
                    "line": node.lineno, "kind": kind, "orig": text[a:b][:80]})

    for node in ast.walk(tree):
        if not hasattr(node, "lineno") or not hit(node):
            continue
        if isinstance(node, ast.Compare):
            for i, op in enumerate(node.ops):
                for alt in CMP_ALTS.get(type(op), []):
                    new = ast.Compare(left=node.left, ops=list(node.ops), comparators=node.comparators)
                    new.ops[i] = alt()
                    add(node, new, "comparison")
        elif isinstance(node, ast.BinOp):
            for alt in BIN_ALTS.get(type(node.op), []):
                if isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
                    continue
                add(node, ast.BinOp(left=node.left, op=alt(), right=node.right), "arithmetic")
        elif isinstance(node, ast.BoolOp):
            alt = ast.Or() if isinstance(node.op, ast.And) else ast.And()
            add(node, ast.BoolOp(op=alt, values=node.values), "boolean")
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            add(node, node.operand, "boolean")
        if isinstance(node, (ast.If, ast.While, ast.IfExp, ast.Assert)) and hit(node.test) and \
                not (isinstance(node.test, ast.UnaryOp) and isinstance(node.test.op, ast.Not)):
            add(node.test, ast.UnaryOp(op=ast.Not(), operand=node.test), "boolean")
        elif isinstance(node, ast.Constant) and type(node.value) is int:
            for d in (1, -1):
                add(node, ast.Constant(node.value + d), "constant")
        elif isinstance(node, ast.Constant) and type(node.value) is bool:
            add(node, ast.Constant(not node.value), "constant")
        elif isinstance(node, ast.AugAssign) and type(node.op) in (ast.Add, ast.Sub):
            alt = ast.Sub() if isinstance(node.op, ast.Add) else ast.Add()
            a, b = pos(node)
            new = ast.AugAssign(target=node.target, op=alt, value=node.value)
            try:
                repl = ast.unparse(new)
            except Exception:  # noqa: BLE001
                continue
            out.append({"site": f"{rel}:{a}:{b}:{repl}", "file": rel, "start": a, "end": b, "repl": repl,
                        "line": node.lineno, "kind": "arithmetic", "orig": text[a:b][:80]})
    return out


_FLIP = {ast.Lt: ast.Gt, ast.Gt: ast.Lt, ast.LtE: ast.GtE, ast.GtE: ast.LtE}
_NEG = {ast.Lt: ast.GtE, ast.GtE: ast.Lt, ast.Gt: ast.LtE, ast.LtE: ast.Gt, ast.Eq: ast.NotEq, ast.NotEq: ast.Eq,
        ast.Is: ast.IsNot, ast.IsNot: ast.Is, ast.In: ast.NotIn, ast.NotIn: ast.In}
_COMMON = {"self", "cls", "other", "key", "value", "len", "i", "j", "k", "x", "y", "n", "result", "item",
           "index", "pos", "idx", "start", "stop", "end", "args", "kwargs", "None"}


def _tokens(node) -> set[str]:
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute):
            out.add("." + n.attr)
        elif isinstance(n, ast.Name) and n.id not in _COMMON:
            out.add(n.id)
    return out


def comparison_profile(text: str) -> list[dict]:
    """Every single-operator comparison with its operand tokens and its effective operator
    (after an enclosing `not`), oriented left-to-right."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    negated = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not) and isinstance(n.operand, ast.Compare):
            negated.add(id(n.operand))
    starts = _offsets(text)
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Compare) and len(n.ops) == 1:
            op = type(n.ops[0])   # the raw token: code conventions are written in raw operators
            out.append({"start": starts[n.lineno - 1] + n.col_offset, "op": op,
                        "left": _tokens(n.left), "right": _tokens(n.comparators[0])})
    return out


def deviates_after(text: str, site: dict) -> bool:
    """True if `site` (a comparison repair) would make a comparison disagree with a clear majority of
    its siblings: comparisons in the same file over the same operands. A real injected bug makes one
    sibling deviate; repairing it restores agreement. A repair that creates deviation is suspicious."""
    profile = comparison_profile(text)
    me = next((c for c in profile if c["start"] == site["start"]), None)
    if me is None or me["op"] in (ast.Is, ast.IsNot, ast.In, ast.NotIn):
        return False   # identity/membership tests guard different actions; their polarity is not a convention
    mutated = text[: site["start"]] + site["repl"] + text[site["end"]:]
    after = next((c for c in comparison_profile(mutated) if c["start"] in (site["start"], site["start"] + 1)), None)
    if after is None:
        return False
    votes = []
    for c in profile:
        if c is me:
            continue
        same = bool(c["left"] & me["left"]) or bool(c["right"] & me["right"])
        crossed = bool(c["left"] & me["right"]) or bool(c["right"] & me["left"])
        if same and not crossed:
            votes.append(c["op"])
        elif crossed and not same:
            votes.append(_FLIP.get(c["op"], c["op"]))
    if len(votes) < 3:
        return False
    top, count = max(((o, votes.count(o)) for o in set(votes)), key=lambda t: t[1])
    return count / len(votes) >= 0.75 and me["op"] == top and after["op"] != top


_STRICT = {ast.Lt: True, ast.Gt: True, ast.LtE: False, ast.GtE: False}


def _related(a1: set, b1: set, a2: set, b2: set) -> bool:
    """Two comparisons talk about the same quantities: both operand pairs overlap, or one pair shares
    an attribute name (a field such as `.expires` is specific; a local name such as `i` is not)."""
    if (a1 & b1) and (a2 & b2):
        return True
    return any(t.startswith(".") for t in (a1 & b1) | (a2 & b2))


def boundary_unsupported(text: str, site: dict) -> bool:
    """For a repair that only moves a boundary in or out (< vs <=, > vs >=): True when the code's other
    comparisons over the same operands do not favour the repaired form. Code is consistent about
    whether a boundary value is included (e.g. an item is expired exactly at its expiry time), so a
    true repair moves a comparison towards the convention of its siblings. Ties count against."""
    profile = comparison_profile(text)
    me = next((c for c in profile if c["start"] == site["start"]), None)
    if me is None or me["op"] not in _STRICT:
        return False
    mutated = text[: site["start"]] + site["repl"] + text[site["end"]:]
    after = next((c for c in comparison_profile(mutated) if c["start"] in (site["start"], site["start"] + 1)), None)
    if after is None or after["op"] not in _STRICT or _STRICT[after["op"]] == _STRICT[me["op"]]:
        return False
    strict = loose = 0
    for c in profile:
        if c is me or c["op"] not in _STRICT:
            continue
        same = _related(c["left"], me["left"], c["right"], me["right"])
        crossed = _related(c["left"], me["right"], c["right"], me["left"])
        if same != crossed:
            if _STRICT[c["op"]]:
                strict += 1
            else:
                loose += 1
    if strict + loose < 2:
        return False
    want, against = (strict, loose) if _STRICT[after["op"]] else (loose, strict)
    return want <= against


class _Shape(ast.NodeTransformer):
    """Abstract a statement to its shape: names, attributes and numbers become placeholders."""

    def visit_Name(self, node):
        return ast.copy_location(ast.Name(id="_", ctx=node.ctx), node)

    def visit_Attribute(self, node):
        self.generic_visit(node)
        return ast.copy_location(ast.Attribute(value=node.value, attr="_", ctx=node.ctx), node)

    def visit_Constant(self, node):
        if type(node.value) in (int, float):
            return ast.copy_location(ast.Constant(value="#"), node)
        return node


def constant_shapes(text: str) -> list[dict]:
    """Statements (or if/while conditions) with numeric constants, grouped by shape."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    starts = _offsets(text)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Return, ast.Assign, ast.AugAssign, ast.Expr, ast.If, ast.While)):
            continue
        target = node.test if isinstance(node, (ast.If, ast.While)) else node
        consts = tuple(n.value for n in ast.walk(target) if isinstance(n, ast.Constant) and type(n.value) in (int, float))
        if not consts:
            continue
        try:
            key = ast.unparse(_Shape().visit(ast.parse(ast.unparse(target))))
        except Exception:  # noqa: BLE001
            continue
        if len(key) < 12:
            continue
        out.append({"key": key, "consts": consts, "line": node.lineno,
                    "start": starts[target.lineno - 1] + target.col_offset,
                    "end": starts[target.end_lineno - 1] + target.end_col_offset})
    return out


def plurality(values: list):
    """The clearly most common value (at least twice, at least half, more than any other), or None."""
    counts = sorted(((values.count(v), v) for v in set(values)), key=lambda t: -t[0])
    if not counts:
        return None
    top_n, top = counts[0]
    second = counts[1][0] if len(counts) > 1 else 0
    if top_n >= 2 and top_n > second and top_n * 2 >= len(values):
        return top
    return None


def constant_deviates_after(text: str, site: dict) -> bool:
    """True if a constant repair makes a statement disagree with the majority of same-shape statements."""
    before = constant_shapes(text)
    mine = [c for c in before if c["start"] <= site["start"] and site["end"] <= c["end"]]
    if not mine:
        return False
    me = min(mine, key=lambda c: c["end"] - c["start"])
    group = [c["consts"] for c in before if c["key"] == me["key"]]
    if len(group) < 3:
        return False
    top = plurality(group)
    # Rejecting a repair needs a strong majority: with every bug applied, two siblings can share the
    # same wrong value and win a plain plurality.
    if top is None or me["consts"] != top or group.count(top) < 3 or group.count(top) * 3 < 2 * len(group):
        return False
    mutated = text[: site["start"]] + site["repl"] + text[site["end"]:]
    shift = len(site["repl"]) - (site["end"] - site["start"])
    after = [c for c in constant_shapes(mutated) if c["start"] <= site["start"] + 1 and site["end"] + shift <= c["end"] + 1]
    if not after:
        return False
    return min(after, key=lambda c: c["end"] - c["start"])["consts"] != top


_NUM = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])")


def documented_numbers(workspace: Path, package: str) -> set[str]:
    """Numbers that appear in the documentation and the package's docstrings."""
    texts = []
    for p in list(workspace.glob("*")) + (list((workspace / "docs").rglob("*")) if (workspace / "docs").is_dir() else []):
        if p.is_file() and p.suffix.lower() in (".rst", ".md", ".txt"):
            texts.append(p.read_text(encoding="utf-8", errors="replace"))
    for p in (workspace / package).rglob("*.py"):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                d = ast.get_docstring(node)
                if d:
                    texts.append(d)
    return set(_NUM.findall("\n".join(texts)))


def contradicts_docs(site: dict, text: str, docnums: set[str]) -> bool:
    """A constant repair that moves a documented value (e.g. a default of 128 stated in the docs) to an
    undocumented one contradicts the specification."""
    orig = text[site["start"]:site["end"]].strip()
    new = site["repl"].strip("()")
    try:
        if abs(float(orig)) <= 2:
            return False
    except ValueError:
        return False
    return orig in docnums and new not in docnums


def apply_sites(root: Path, originals: dict[str, str], sites: list[dict]):
    """Write each file in `originals` with the given sites applied (later offsets first)."""
    by_file: dict[str, list[dict]] = {}
    for s in sites:
        by_file.setdefault(s["file"], []).append(s)
    for rel, text in originals.items():
        new = text
        for s in sorted(by_file.get(rel, []), key=lambda s: -s["start"]):
            new = new[: s["start"]] + s["repl"] + new[s["end"]:]
        (root / rel).write_text(new)


def parse_site(site: str) -> dict:
    rel, a, b, repl = site.split(":", 3)
    return {"site": site, "file": rel, "start": int(a), "end": int(b), "repl": repl}


class _Timeout(BaseException):
    pass


def _alarm(signum, frame):
    raise _Timeout()


def _param_sets(func) -> list[dict]:
    """The argument combinations of `@pytest.mark.parametrize` marks (all of them are run)."""
    combos = [{}]
    for mark in getattr(func, "pytestmark", []) or []:
        if getattr(mark, "name", "") != "parametrize" or not mark.args:
            continue
        names = mark.args[0]
        names = [n.strip() for n in names.split(",")] if isinstance(names, str) else list(names)
        values = mark.args[1] if len(mark.args) > 1 else mark.kwargs.get("argvalues", [])
        rows = []
        for v in values:
            v = getattr(v, "values", v)              # pytest.param(...)
            if len(names) == 1:
                v = v[0] if hasattr(values[0] if values else None, "values") else v
                rows.append({names[0]: v})
            else:
                rows.append(dict(zip(names, v)))
        combos = [dict(c, **r) for c in combos for r in rows] or combos
    return combos


def _fixture(mod, owner, name, stack, depth=0):
    fx = getattr(owner, name, None) if owner is not None else None
    if fx is None:
        fx = getattr(mod, name, None)
    raw = getattr(fx, "_get_wrapped_function", None)
    func = raw() if raw else getattr(fx, "__wrapped__", None)
    if func is None or depth > 5:
        raise TypeError(f"unsupported fixture {name}")
    sig = __import__("inspect").signature(func)
    args = {}
    for p in sig.parameters.values():
        if p.name in ("self", "cls"):
            continue
        if p.name == "request":
            raise TypeError("fixture uses request")
        args[p.name] = _fixture(mod, owner, p.name, stack, depth + 1)
    if owner is not None and "self" in sig.parameters:
        value = func(owner, **args)
    else:
        value = func(**args)
    if hasattr(value, "__next__") and hasattr(value, "send"):
        gen = value
        value = next(gen)
        stack.append(gen)
    return value


_CURRENT_ID = [""]   # the [...] part of the test id being run


def _call_with_params(func, mod, owner):
    """Call a test function, supplying parametrize arguments and simple fixtures like pytest does."""
    import inspect as _inspect
    base = getattr(func, "__func__", func)
    try:
        params = [p.name for p in _inspect.signature(func).parameters.values()]
    except (TypeError, ValueError):
        params = []
    if not params:
        func()
        return
    combos = _param_sets(base)
    if len(combos) > 1 and _CURRENT_ID[0]:
        def ident(v, n, i):
            if isinstance(v, (int, float, str, bool, type(None))) and not (isinstance(v, str) and not v.isprintable()):
                return str(v)
            return f"{n}{i}"
        ids = []
        for i, c in enumerate(combos):
            ids.append("-".join(ident(v, n, i) for n, v in c.items()))
        match = [c for c, x in zip(combos, ids) if x == _CURRENT_ID[0]]
        if len(match) == 1:
            combos = match
    for combo in combos:
        stack: list = []
        kwargs = dict(combo)
        for name in params:
            if name not in kwargs:
                kwargs[name] = _fixture(mod, owner, name, stack)
        try:
            func(**kwargs)
        finally:
            for gen in reversed(stack):
                try:
                    next(gen)
                except StopIteration:
                    pass


class InProcess:
    """Import the package from a scratch copy and run single test functions without pytest."""

    def __init__(self, root: Path, workspace: Path, package: str):
        self.root = root
        self.ws = workspace
        self.package = package
        self.counter = 0
        self.errors: dict[str, str] = {}
        sys.path[:] = [str(root), str(workspace)] + [p for p in sys.path if p not in ("", str(workspace))]

    def purge(self):
        for name in list(sys.modules):
            if name == self.package or name.startswith(self.package + ".") or name.startswith("_tagent_t"):
                del sys.modules[name]

    def run(self, tests: list[str], timeout: float = 4.0, trace_lines: dict | None = None,
            budget: float | None = None) -> dict[str, bool]:
        self.purge()
        modules = {}
        results = {}
        stop_at = time.time() + budget if budget else None
        for tid in tests:
            if stop_at and time.time() > stop_at:
                break
            path, *rest = tid.split("::")
            try:
                if path not in modules:
                    self.counter += 1
                    spec = importlib.util.spec_from_file_location(f"_tagent_t{self.counter}", self.ws / path)
                    mod = importlib.util.module_from_spec(spec)
                    sys.modules[spec.name] = mod
                    spec.loader.exec_module(mod)
                    modules[path] = mod
                mod = modules[path]
            except BaseException as e:  # noqa: BLE001
                self.errors[tid] = "import: " + f"{type(e).__name__}: {e}"[:200]
                results[tid] = False
                continue
            results[tid] = self._call(mod, rest, timeout, tid, trace_lines)
        return results

    def _call(self, mod, rest, timeout, tid, trace_lines) -> bool:
        name = rest[-1].split("[")[0]
        _CURRENT_ID[0] = rest[-1][len(name) + 1:-1] if "[" in rest[-1] else ""
        try:
            if len(rest) == 2:
                cls = getattr(mod, rest[0])
                if isinstance(cls, type) and issubclass(cls, unittest.TestCase):
                    def fn():
                        res = unittest.TestResult()
                        cls(name).run(res)
                        if res.errors or res.failures:
                            raise AssertionError("unittest failure")
                else:
                    def fn():
                        inst = cls()
                        if hasattr(inst, "setup_method"):
                            try:
                                inst.setup_method(getattr(inst, name))
                            except TypeError:
                                inst.setup_method()
                        _call_with_params(getattr(inst, name), mod, inst)
            else:
                target = getattr(mod, name)

                def fn():
                    _call_with_params(target, mod, None)
        except BaseException as e:  # noqa: BLE001
            self.errors[tid] = "lookup: " + f"{type(e).__name__}: {e}"[:200]
            return False
        tracer = None
        if trace_lines is not None:
            pkg_dir = str(self.root / self.package)
            hits = trace_lines.setdefault(tid, {})

            def tracer(frame, event, arg):
                fname = frame.f_code.co_filename
                if not fname.startswith(pkg_dir):
                    return None
                if event == "line":
                    rel = str(Path(fname).relative_to(self.root))
                    hits.setdefault(rel, set()).add(frame.f_lineno)
                return tracer
        old = signal.signal(signal.SIGALRM, _alarm)
        signal.setitimer(signal.ITIMER_REAL, timeout)
        try:
            if tracer:
                sys.settrace(tracer)
            fn()
            return True
        except BaseException as e:  # noqa: BLE001
            self.errors[tid] = f"{type(e).__name__}: {e}"[:200]
            return False
        finally:
            sys.settrace(None)
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old)


def suite_passes(root: Path, suite_dir: str, timeout: int) -> bool:
    if not (root / suite_dir).is_dir():
        return True
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "--rootdir", str(root),
                            "-c", "/dev/null", suite_dir], cwd=root, capture_output=True, timeout=timeout)
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def _terminate(signum, frame):
    raise SystemExit(1)


def main():
    signal.signal(signal.SIGTERM, _terminate)
    req = json.loads(Path(sys.argv[1]).read_text())
    out_path = Path(sys.argv[2])
    started = time.time()
    deadline = started + float(req.get("deadline", 90))
    ws = Path.cwd()
    pkg = req["package"]
    suite = req.get("suite", "tests")
    result = {"baseline": {}, "explained": {}, "sites": {}, "suite_ok": {}, "notes": []}

    def save():
        out_path.write_text(json.dumps(result))

    try:
        from hypothesis import HealthCheck, settings
        settings.register_profile("tagent", derandomize=True, database=None, deadline=None, max_examples=150,
                                  suppress_health_check=list(HealthCheck), print_blob=False)
        # Screening mutants with fewer examples is much faster; every repair that survives screening
        # is confirmed with the full profile before it is trusted.
        settings.register_profile("screen", derandomize=True, database=None, deadline=None, max_examples=25,
                                  suppress_health_check=list(HealthCheck), print_blob=False)
        settings.load_profile("tagent")
        profiles = settings
    except Exception:  # noqa: BLE001
        profiles = None

    def profile(name):
        if profiles is not None:
            profiles.load_profile(name)

    tmp = Path(tempfile.mkdtemp(prefix="tagent-repair-"))
    try:
        shutil.copytree(ws / pkg, tmp / pkg, ignore=shutil.ignore_patterns("__pycache__"))
        if (ws / suite).is_dir():
            shutil.copytree(ws / suite, tmp / suite, ignore=shutil.ignore_patterns("__pycache__"))
        originals = {str(p.relative_to(tmp)): p.read_text() for p in (tmp / pkg).rglob("*.py")}
        fixes = [parse_site(s) for s in req.get("fixes", [])]
        fixes = [f for f in fixes if f["file"] in originals]
        apply_sites(tmp, originals, fixes)
        base_texts = {rel: (tmp / rel).read_text() for rel in originals}
        runner = InProcess(tmp, ws, pkg)
        tests = list(req["tests"])
        lines: dict = {}
        baseline = runner.run(tests, trace_lines=lines)
        result["baseline"] = baseline
        result["errors"] = dict(runner.errors)
        # Guards: tests that pass on the current code (documentation examples and model-written tests
        # that agree with the code). A repair that breaks one of them is not trusted.
        guard_lines: dict = {}
        guards = []
        if req.get("guards") and not req.get("baseline_only"):
            pool = list(dict.fromkeys(req["guards"]))[-400:]
            guards = [g for g, ok in runner.run(pool, timeout=2.0, trace_lines=guard_lines,
                                                budget=min(25.0, max(5.0, deadline - time.time() - 30))).items() if ok]
            result["notes"].append(f"{len(guards)} guards")
            result["guard_errors"] = dict(list(runner.errors.items())[-8:])
        failing = [t for t in tests if not baseline.get(t)]
        save()
        if req.get("baseline_only"):
            return
        # Candidate mutations: on lines the failing tests executed.
        covered: dict[str, set[int]] = {}
        for t in failing:
            for rel, ls in lines.get(t, {}).items():
                covered.setdefault(rel, set()).update(ls)
        sites = []
        for rel, ls in covered.items():
            if rel in base_texts:
                sites += mutation_sites(rel, base_texts[rel], ls)
        result["notes"].append(f"{len(failing)} failing, {len(sites)} mutations on covered lines")
        explains: dict[str, list[str]] = {}
        docnums = documented_numbers(ws, pkg)
        hot = req.get("hot", {})   # file -> lines where the failures surfaced; try nearby repairs first

        def distance(site):
            lines_ = hot.get(site["file"]) or []
            return min((abs(site["line"] - h) for h in lines_), default=10 ** 6)
        sites.sort(key=distance)
        kept_sites = []
        for s in sites:
            if (s["kind"] == "comparison" and (deviates_after(base_texts[s["file"]], s)
                                               or boundary_unsupported(base_texts[s["file"]], s))) or \
                    (s["kind"] == "constant" and (constant_deviates_after(base_texts[s["file"]], s)
                                                  or contradicts_docs(s, base_texts[s["file"]], docnums))):
                result.setdefault("sibling_rejected", []).append(s["site"])
                continue
            kept_sites.append(s)
        sites = kept_sites
        for s in sites:
            if time.time() > deadline:
                result["notes"].append("deadline reached during mutation runs")
                break
            who = [t for t in failing if any(ln in lines.get(t, {}).get(s["file"], ())
                                              for ln in range(s["line"], s["line"] + 3))]
            if not who:
                continue
            apply_sites(tmp, base_texts, [s])
            profile("screen")
            res = runner.run(who, timeout=2.0)
            profile("tagent")
            passed = [t for t, ok in res.items() if ok]
            if passed:
                passed = [t for t, ok in runner.run(passed, timeout=4.0).items() if ok]
            if passed:
                near = [g for g in guards if any(ln in guard_lines.get(g, {}).get(s["file"], ())
                                                 for ln in range(s["line"], s["line"] + 3))][:80]
                broken = [g for g, ok in runner.run(near, timeout=3.0).items() if not ok] if near else []
                if broken:
                    result.setdefault("guard_rejected", {})[s["site"]] = broken[:5]
                    continue
                explains[s["site"]] = passed
                result["sites"][s["site"]] = {k: s[k] for k in ("file", "line", "kind", "orig", "repl")}
                save()
        apply_sites(tmp, base_texts, [])
        # The existing suite must still pass with the repair: check the strongest repairs first.
        order = sorted(explains, key=lambda k: -len(explains[k]))
        for site in order:
            if time.time() > deadline + 25:
                result["notes"].append("deadline reached during suite checks")
                break
            apply_sites(tmp, base_texts, [parse_site(site)])
            ok = suite_passes(tmp, suite, timeout=40)
            result["suite_ok"][site] = ok
            if ok:
                for t in explains[site]:
                    result["explained"].setdefault(t, []).append(site)
            save()
        apply_sites(tmp, base_texts, [])
    finally:
        save()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
