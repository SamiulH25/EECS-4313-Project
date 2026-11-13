"""Run candidate test files in the workspace and classify each test's result."""

from __future__ import annotations

import ast
import json
import re
import shlex
import uuid
from dataclasses import dataclass, field
from pathlib import Path

FORBIDDEN_MODULES = {"inspect", "os", "io", "pathlib", "importlib", "linecache", "dis", "subprocess", "shutil",
                     "glob", "marshal", "ast", "tokenize", "pkgutil", "runpy", "sys", "gc", "ctypes", "mmap",
                     "tempfile", "socket", "requests", "urllib", "multiprocessing"}
FORBIDDEN_NAMES = {"open", "exec", "eval", "compile", "globals", "vars", "__import__", "breakpoint"}
FORBIDDEN_ATTRS = {"__code__", "__file__", "__globals__", "__loader__", "__spec__", "__path__", "modules",
                   "getsource", "getsourcelines", "read_text", "read_bytes", "co_code", "f_code"}

CONFTEST = '''"""Make property-based tests deterministic so they behave the same on every run."""
from hypothesis import HealthCheck, settings

settings.register_profile("tagent", derandomize=True, database=None, deadline=None, max_examples=150,
                          suppress_health_check=list(HealthCheck), print_blob=False)
settings.load_profile("tagent")
'''


def static_problem(src: str) -> str:
    """Why the evaluator would reject this file, or ''."""
    try:
        tree = ast.parse(src)
    except SyntaxError as error:
        return f"syntax error on line {error.lineno}: {error.msg}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names = {(node.module or "").split(".")[0]}
        else:
            names = set()
        hit = names & FORBIDDEN_MODULES
        if hit:
            return f"imports {sorted(hit)[0]} (not allowed)"
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            return f"uses {node.id} (not allowed)"
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRS:
            return f"uses .{node.attr} (not allowed)"
    return ""


def _node_problem(node) -> bool:
    return any(static_problem_node(n) for n in ast.walk(node))


def static_problem_node(node) -> str:
    if isinstance(node, ast.Import):
        names = {a.name.split(".")[0] for a in node.names}
    elif isinstance(node, ast.ImportFrom):
        names = {(node.module or "").split(".")[0]}
    else:
        names = set()
    if names & FORBIDDEN_MODULES:
        return "module"
    if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
        return "name"
    if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRS:
        return "attr"
    return ""


def _blocks(src: str) -> list[str]:
    """Split a module into top-level blocks (a decorated def/class stays with its decorators)."""
    blocks, cur = [], []
    for line in src.splitlines(keepends=True):
        top = line[:1] not in (" ", "\t", "\n", "\r", "#", ")", "]", "}") and line.strip() != ""
        if top and cur and not (cur and all(l.lstrip().startswith("@") or not l.strip() for l in cur)):
            blocks.append("".join(cur))
            cur = []
        cur.append(line)
    if cur:
        blocks.append("".join(cur))
    return blocks


def _parses(src: str) -> bool:
    try:
        ast.parse(src)
        return True
    except SyntaxError:
        return False


def salvage(src: str) -> str:
    """Keep the parts of a generated test file that are valid: drop top-level blocks that break the
    syntax, and drop tests/helpers/imports that use something the evaluator forbids."""
    if not _parses(src):
        kept = ""
        for b in _blocks(src):
            if _parses(kept + b):
                kept += b
        src = kept
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return ""
    bad_names = set()
    body = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)) and static_problem_node(node):
            bad_names.update((a.asname or a.name).split(".")[0] for a in node.names)
            continue
        body.append(node)

    def uses_bad(n):
        return _node_problem(n) or any(isinstance(x, ast.Name) and x.id in bad_names for x in ast.walk(n))
    out = []
    for node in body:
        if isinstance(node, ast.ClassDef):
            node.body = [n for n in node.body if not uses_bad(n)] or [ast.Pass()]
            if uses_bad(node):
                continue
        elif uses_bad(node):
            continue
        out.append(node)
    tree.body = out
    try:
        return ast.unparse(tree) + "\n"
    except Exception:  # noqa: BLE001
        return ""


@dataclass
class TestResult:
    nodeid: str
    outcome: str                 # passed / failed / skipped
    text: str = ""
    frames: list = field(default_factory=list)
    crash: list | None = None
    flaky: bool = False

    @property
    def name(self) -> str:
        return self.nodeid.split("::")[-1]

    @property
    def func(self) -> str:
        return self.name.split("[")[0]


class Runner:
    def __init__(self, env, workspace: Path, student_dir: Path, package: str):
        self.env = env
        self.ws = Path(workspace)
        self.student_dir = Path(student_dir)
        self.package = package

    def ensure_conftest(self, folder: Path):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "conftest.py").write_text(CONFTEST)

    def run(self, rel_paths: list[str], seed: int = 1, timeout_per_test: float = 8.0,
            command_timeout: int = 110, coverage: bool = False) -> tuple[dict, str]:
        """Run pytest on the given workspace-relative paths. Returns (records, raw output tail)."""
        out = f".tagent_out_{uuid.uuid4().hex[:8]}.json"
        cov_out = out.replace("_out_", "_cov_")
        paths = " ".join(shlex.quote(p) for p in rel_paths)
        cov = (f"TAGENT_COV_PKG={shlex.quote(self.package)} TAGENT_COV_OUT={cov_out} " if coverage else "")
        plugin = "-p tagent.cov_plugin " if coverage else ""
        cmd = (f"{cov}TAGENT_OUT={out} TAGENT_SEED={seed} TAGENT_TIMEOUT={timeout_per_test} "
               f"PYTHONPATH={shlex.quote(str(self.student_dir))}:. "
               f"HYPOTHESIS_STORAGE_DIRECTORY=.hypothesis "
               f"timeout {command_timeout} python -m pytest -q -p no:cacheprovider -p tagent.record_plugin {plugin}"
               f"--tb=short --rootdir . -c /dev/null {paths} 2>&1 | tail -30")
        result = self.env.execute({"command": cmd}, timeout=command_timeout + 10)
        path = self.ws / out
        records = {}
        if path.exists():
            try:
                records = json.loads(path.read_text())
            except json.JSONDecodeError:
                records = {}
            path.unlink(missing_ok=True)
        self.last_coverage = {}
        cpath = self.ws / cov_out
        if cpath.exists():
            try:
                self.last_coverage = json.loads(cpath.read_text())
            except json.JSONDecodeError:
                pass
            cpath.unlink(missing_ok=True)
        return records, result.get("output", "")[-3000:]

    def results(self, rel_paths: list[str], repeats: int = 2, coverage: dict | None = None
                ) -> tuple[list[TestResult], list[str], str]:
        """Run `repeats` times in different orders. Returns (tests, collection errors, output).
        If `coverage` is a dict, the first run's executed arcs are merged into it."""
        runs = []
        output = ""
        for i in range(repeats):
            recs, output = self.run(rel_paths, seed=i + 1, coverage=(coverage is not None and i == 0))
            if coverage is not None and i == 0:
                for f, arcs in (self.last_coverage or {}).items():
                    coverage.setdefault(f, set()).update(tuple(a) for a in arcs)
            runs.append(recs)
        first = runs[0] if runs else {}
        collection = [v.get("text", k) for k, v in first.items() if k.startswith("<collection>")]
        tests = []
        for nodeid, rec in first.items():
            if nodeid.startswith("<"):
                continue
            outcomes = {r.get(nodeid, {}).get("outcome") for r in runs}
            tr = TestResult(nodeid, rec.get("outcome", "failed"), rec.get("text", ""), rec.get("frames", []),
                            rec.get("crash"))
            if len(outcomes) > 1:
                tr.flaky = True
                # Prefer a failure record when one run failed.
                for r in runs:
                    if r.get(nodeid, {}).get("outcome") == "failed":
                        x = r[nodeid]
                        tr.outcome, tr.text, tr.frames, tr.crash = "failed", x.get("text", ""), x.get("frames", []), x.get("crash")
            tests.append(tr)
        if not first and not collection:
            collection = [output]
        return tests, collection, output


TEST_BUG_EXC = ("NameError", "SyntaxError", "ImportError", "ModuleNotFoundError", "UnboundLocalError",
                "fixture", "IndentationError")


def triage_auto(tr: TestResult, test_file: str, package: str) -> str:
    """Classify a failing test without the model.

    Returns 'drop' (the test itself is broken or unreliable) or 'judge' (looks like it may be a bug).
    """
    if tr.flaky:
        return "drop"
    text = tr.text or ""
    if "TimeoutError" in text or "took too long" in text:
        return "drop"
    if tr.crash and len(tr.crash) >= 3:
        msg = tr.crash[2] or ""
        if any(msg.startswith(e) or f"{e}:" in msg.split("\n")[0] for e in TEST_BUG_EXC):
            return "drop"
        if "SourceAccessError" in msg:
            return "drop"
    # Where was the exception raised?  If the innermost frame is in the test file and it is not an
    # assertion (or pytest.raises/fail), the test code itself crashed.
    frames = tr.frames or []
    if frames:
        inner = frames[-1]
        path = str(inner[0])
        msg = (tr.crash[2] if tr.crash else "") or ""
        in_test = path.endswith(Path(test_file).name)
        in_pkg = (f"{package}/" in path or path.startswith(package))
        if in_test and not in_pkg:
            is_assert = msg.startswith("AssertionError") or msg.startswith("assert ") or "Failed: DID NOT RAISE" in msg \
                or msg.startswith("Failed") or "Falsifying" in text
            if not is_assert:
                if re.match(r"(TypeError|AttributeError|KeyError|IndexError|ValueError)", msg) and \
                        any(f"{package}/" in str(f[0]) for f in frames):
                    return "judge"
                return "drop"
    return "judge"
