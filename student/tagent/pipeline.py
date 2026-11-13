"""The agent's loop.

1. Mine documentation examples into tests (no model).                     -> doctests.py
2. Split the package into units ranked by risk (no model).                 -> inventory.py
3. For each unit, one short conversation writes a test file aimed at the unit's risk points, with
   boundary-value tests and hypothesis differential/model-based tests.
4. Run the file on the workspace code (which contains the bugs). A test that passes cannot reveal a
   bug and might encode one, so only failing tests go on. Broken or flaky tests are dropped by rule.
5. A separate judge conversation decides, from the docs, whether each failure is a library bug or a
   test mistake. Only BUG verdicts are kept, at most a few per suspected location.
6. Later rounds revisit units with feedback while budget remains; finally tests_new/ is assembled
   and re-checked.
"""

from __future__ import annotations

import ast
import json
import queue
import re
import shlex
import textwrap
import uuid
import threading
import traceback
from collections import Counter
from pathlib import Path

from minisweagent.exceptions import LimitsExceeded

from . import doctests, inventory
from .llm import LLM, heredoc_body
from .runner import CONFTEST, Runner, TestResult, static_problem, triage_auto

WORKERS = 4
JOB_RESERVE = 30_000        # do not start a unit job with fewer tokens left than this
MAX_PER_LOCATION = 2        # kept tests per repaired location
USE_JUDGE = False           # the LLM judge was too permissive; the repair check replaces it
MAX_TESTS = 300


def _load_prompt(path: Path) -> tuple[str, str]:
    text = path.read_text()
    system = text.split("# SYSTEM", 1)[1].split("# USER", 1)[0].strip()
    user = text.split("# USER", 1)[1].strip()
    return system, user


def _fill(template: str, **values) -> str:
    def rep(m):
        return str(values.get(m.group(1), ""))
    return re.sub(r"\{\{(\w+)\}\}", rep, template)


def _functions(src: str) -> dict[str, str]:
    """Top-level (and class-level) defs of a test module: name -> source segment."""
    out = {}
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out
    lines = src.splitlines()
    for node in tree.body:
        nodes = [node]
        if isinstance(node, ast.ClassDef):
            nodes += [n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for n in nodes:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = min([n.lineno] + [d.lineno for d in n.decorator_list])
                out.setdefault(n.name, "\n".join(lines[start - 1:n.end_lineno]))
    return out


def reads_private(src: str, func: str) -> bool:
    """True if the test (or a helper it defines) reads a private attribute such as obj._x."""
    code = textwrap.dedent(_functions(src).get(func, ""))
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr.startswith("_") and not (
                node.attr.startswith("__") and node.attr.endswith("__")):
            return True
    return False


def prune(src: str, keep: set[str]) -> str:
    """Remove every test function not in keep (top level and inside Test classes)."""
    tree = ast.parse(src)

    def is_test(n):
        return isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")

    body = []
    for node in tree.body:
        if is_test(node):
            if node.name in keep:
                body.append(node)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            node.body = [n for n in node.body if not is_test(n) or n.name in keep]
            if any(is_test(n) for n in node.body):
                body.append(node)
        else:
            body.append(node)
    tree.body = body
    return ast.unparse(tree) + "\n"


class Pipeline:
    def __init__(self, task, model, env):
        self.task = task
        self.ws = Path(task.workspace)
        self.pkg = task.package
        self.student = Path(task.student_dir)
        self.llm = LLM(model, task.trajectory_path)
        self.runner = Runner(env, self.ws, self.student, self.pkg)
        self.gen_prompt = _load_prompt(self.student / "prompts" / "generate.md")
        self.judge_prompt = _load_prompt(self.student / "prompts" / "judge.md")
        self.cand = self.ws / "_cand"
        self.runner.ensure_conftest(self.cand)
        self.lock = threading.Lock()
        self.kept: dict[str, set[str]] = {}       # candidate file -> kept test names
        self.sources: dict[str, str] = {}         # candidate file -> source
        self.location_count: Counter = Counter()
        self.history: dict[str, list[str]] = {}   # unit uid -> notes for later rounds
        self.fixes: list[str] = []                # accepted repair sites (approximates the correct code)
        self.guards: list[str] = []               # passing tests that a trusted repair must keep passing
        self.max_tests = getattr(task, "max_tests", MAX_TESTS) or MAX_TESTS

    # ------------------------------------------------------------------ phase 1: doc examples
    def doctest_phase(self):
        src, n = doctests.build_file(self.ws, self.pkg)
        if not n:
            return
        rel = "_cand/test_docs.py"
        (self.ws / rel).write_text(src)
        if static_problem(src):
            self.llm.note("doctests", error=static_problem(src))
            return
        tests, coll, _ = self.runner.results([rel], repeats=3)
        failing = [t for t in tests if t.outcome == "failed"]
        cands = [t for t in failing if triage_auto(t, rel, self.pkg) == "judge"]
        keep = {t.func for t in cands}
        self._add_guards(rel, tests)
        # Documented examples are trusted oracles; the repair check here only seeds the repairs.
        try:
            self.verify(rel, cands, cap=False)
        except Exception as error:  # noqa: BLE001
            self.llm.note("doctest-repair-error", error=str(error))
        self.llm.note("doctests", mined=n, ran=len(tests), failing=len(failing), kept=sorted(keep),
                      collection=coll[:1])
        if keep:
            with self.lock:
                self.sources[rel] = src
                self.kept[rel] = keep

    # ------------------------------------------------------------------ phase 2: model-written tests
    def llm_phase(self):
        self.units = inventory.build_units(self.ws, self.pkg)
        self.sections = inventory.doc_sections(self.ws)
        order = sorted([u for u in self.units if u.weight >= 1], key=lambda u: -u.weight)
        self.llm.note("units", units=[(u.uid, round(u.weight, 1), len(u.risks)) for u in order])
        jobs: queue.Queue = queue.Queue()
        for u in order:
            jobs.put((u, 1))
        state = {"round": 1}
        stop = threading.Event()

        def refill():
            with self.lock:
                state["round"] += 1
                rnd = state["round"]
                if rnd > 4:
                    return False
                # revisit the riskiest half first, then the rest
                for u in order[: max(1, len(order) // 2)] if rnd > 2 else order:
                    jobs.put((u, rnd))
                return True

        def worker():
            while not stop.is_set():
                if self.llm.tokens_left < JOB_RESERVE:
                    stop.set()
                    return
                try:
                    u, rnd = jobs.get_nowait()
                except queue.Empty:
                    if not refill():
                        return
                    continue
                try:
                    self.unit_job(u, rnd)
                except LimitsExceeded:
                    stop.set()
                    return
                except Exception as error:  # noqa: BLE001
                    self.llm.note("job-error", unit=u.uid, error=f"{error}\n{traceback.format_exc()[-1500:]}")

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(WORKERS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def _gen_user(self, u, rnd: int, rel: str) -> str:
        prev = ""
        notes = self.history.get(u.uid) or []
        if notes:
            prev = ("\n## Tests already written for this unit in earlier rounds\n"
                    "Do not repeat them; target other methods, other risk points and other boundaries.\n"
                    + "\n".join(notes[-40:]) + "\n")
        return _fill(self.gen_prompt[1], project=self.task.project, summary=self.task.summary, title=u.title,
                     import_hint=u.import_hint,
                     docs=inventory.docs_for(u, self.sections) or "(none found)",
                     outline=u.outline or "(module-level functions)", source=u.source,
                     risks="\n".join(u.risks) or "(none detected)",
                     existing=inventory.existing_tests_for(self.ws, u) or "# (none)",
                     previous=prev, n_tests="10 to 14", path=rel)

    def _repair(self, rel: str, body: str, problem: str) -> str | None:
        user = (f"This pytest file has a problem:\n```\n{problem[-2500:]}\n```\n\nThe file:\n```python\n{body}\n```\n"
                "Rewrite the complete file so that it imports and collects cleanly. Fix only the problem; "
                "keep the tests and their expected values. Never import os, sys, inspect, pathlib, io, "
                "subprocess or importlib and never open files.\n"
                f"Reply with exactly one bash tool call:\ncat > {rel} <<'EOF'\n<the complete file>\nEOF")
        return heredoc_body(self.llm.ask(self.gen_prompt[0], user, f"repair:{rel}"))

    def _with_imports(self, u, body: str) -> str:
        """Make sure the unit (and the package's public names) are importable in the test file."""
        lines = []
        if u.import_hint and "..." not in u.import_hint and u.import_hint not in body:
            lines.append(u.import_hint)
        if f"from {self.pkg} import *" not in body:
            lines.append(f"from {self.pkg} import *  # noqa: F401,F403")
        if not lines:
            return body
        # keep a leading module docstring / __future__ imports first
        try:
            tree = ast.parse(body)
        except SyntaxError:
            return "\n".join(lines) + "\n" + body
        insert_at = 0
        for node in tree.body:
            if (isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant)
                    and isinstance(node.value.value, str)) or \
                    (isinstance(node, ast.ImportFrom) and node.module == "__future__"):
                insert_at = node.end_lineno
            else:
                break
        src = body.splitlines()
        return "\n".join(src[:insert_at] + lines + src[insert_at:]) + "\n"

    def unit_job(self, u, rnd: int):
        rel = f"_cand/test_{u.uid}_r{rnd}.py"
        body = heredoc_body(self.llm.ask(self.gen_prompt[0], self._gen_user(u, rnd, rel), f"gen:{u.uid}:r{rnd}"))
        if not body:
            return
        body = self._with_imports(u, body)
        problem = static_problem(body)
        if problem:
            body = self._repair(rel, body, problem)
            if not body or static_problem(body):
                return
            body = self._with_imports(u, body)
        path = self.ws / rel
        path.write_text(body)
        tests, coll, out = self.runner.results([rel], repeats=2)
        broken = [t for t in tests if t.outcome == "failed" and triage_auto(t, rel, self.pkg) == "drop"]
        if coll or (tests and len(broken) >= max(2, len(tests) // 2)):
            problem = "\n".join(coll)[-2500:] if coll else (
                "Most tests crash inside the test code itself:\n" +
                "\n".join(f"{t.func}: {(t.crash or ['', '', t.text[-300:]])[2][:300]}" for t in broken[:6]))
            fixed = self._repair(rel, body, problem)
            if not fixed or static_problem(fixed):
                return
            body = self._with_imports(u, fixed)
            path.write_text(body)
            tests, coll, out = self.runner.results([rel], repeats=2)
            if coll:
                return
        failing = [t for t in tests if t.outcome == "failed"]
        cands = [t for t in failing if triage_auto(t, rel, self.pkg) == "judge" and not reads_private(body, t.func)]
        passed = [t.func for t in tests if t.outcome == "passed"]
        self._add_guards(rel, tests)
        self.llm.note("ran", file=rel, tests=len(tests), passed=len(passed), failing=len(failing),
                      candidates=[t.name for t in cands])
        keep, located = self.verify(rel, cands)
        verdicts = {}
        if USE_JUDGE and keep:
            judged = [t for t in cands if t.func in keep]
            for i in range(0, len(judged), 5):
                verdicts.update(self.judge(u, body, judged[i:i + 5]))
            keep = {f for f in keep if verdicts.get(f, ("BUG",))[0] == "BUG"}
        with self.lock:
            notes = self.history.setdefault(u.uid, [])
            for t in tests:
                if t.func in keep:
                    notes.append(f"- {t.func}: FOUND A BUG near {located.get(t.func, '?')}")
                elif t.outcome == "passed":
                    notes.append(f"- {t.func}: passed (behaviour already correct)")
                elif t in cands:
                    notes.append(f"- {t.func}: failed, but its expectation looks wrong (no small fix explains it)")
            if keep:
                self.sources[rel] = body
                self.kept[rel] = keep
        self.llm.note("kept", file=rel, kept=sorted(keep), located=located, verdicts=verdicts)

    # ------------------------------------------------------------------ mutation-based repair check
    def repair_check(self, tests: list[str], fixes: list[str], deadline: float = 65,
                     baseline_only: bool = False, guards: list[str] | None = None,
                     hot: dict | None = None) -> dict:
        tag = uuid.uuid4().hex[:8]
        req, out = self.ws / f".tagent_req_{tag}.json", self.ws / f".tagent_res_{tag}.json"
        req.write_text(json.dumps({"package": self.pkg, "tests": tests, "fixes": fixes, "guards": guards or [],
                                   "hot": hot or {},
                                   "deadline": 0 if baseline_only else deadline, "baseline_only": baseline_only}))
        cmd = (f"PYTHONPATH={shlex.quote(str(self.student))} timeout 112 python -m tagent.repair "
               f"{req.name} {out.name} > /dev/null 2>&1")
        self.runner.env.execute({"command": cmd}, timeout=120)
        res = {}
        if out.exists():
            try:
                res = json.loads(out.read_text())
            except json.JSONDecodeError:
                res = {}
        req.unlink(missing_ok=True)
        out.unlink(missing_ok=True)
        return res

    def verify(self, rel: str, cands: list[TestResult], cap: bool = True) -> tuple[set[str], dict]:
        """Keep failing tests that a single small repair (on top of the repairs accepted so far) makes
        pass without breaking the existing suite. Returns (kept function names, name -> site)."""
        if not cands:
            return set(), {}
        ids = [f"{rel}::{t.nodeid.split('::', 1)[1]}" if "::" in t.nodeid else t.nodeid for t in cands]
        by_id = dict(zip(ids, cands))
        res = {"baseline": {}, "explained": {}, "sites": {}, "notes": []}
        for i in range(0, len(ids), 5):
            chunk = ids[i:i + 5]
            hot: dict[str, list[int]] = {}
            for tid in chunk:
                for f in by_id[tid].frames or []:
                    path = str(f[0])
                    if f"{self.pkg}/" in path or path.startswith(self.pkg):
                        hot.setdefault(path[path.find(self.pkg):], []).append(int(f[1]))
            with self.lock:
                fixes = list(self.fixes)
                guards = list(self.guards)
            part = self.repair_check(chunk, fixes, guards=guards, hot=hot)
            for key in ("baseline", "explained", "sites"):
                res[key].update(part.get(key) or {})
            res["notes"] += part.get("notes") or ["(no result)"]
        self.llm.note("repair", file=rel, notes=res.get("notes"), explained=res.get("explained"),
                      baseline_passing=[t for t, ok in (res.get("baseline") or {}).items() if ok])
        keep, located = set(), {}
        with self.lock:
            for tid, t in by_id.items():
                if (res.get("baseline") or {}).get(tid):
                    sites = ["(earlier repair)"]
                else:
                    sites = (res.get("explained") or {}).get(tid) or []
                if not sites:
                    continue
                site = sites[0]
                loc = ":".join(site.split(":", 3)[:3])
                if cap and self.location_count[loc] >= MAX_PER_LOCATION:
                    continue
                self.location_count[loc] += 1
                keep.add(t.func)
                info = (res.get("sites") or {}).get(site, {})
                located[t.func] = f"{info.get('file', '?')} line {info.get('line', '?')}" if info else site
                if site != "(earlier repair)" and len(sites) == 1 and not self._overlaps(site):
                    self.fixes.append(site)
        return keep, located

    def _add_guards(self, rel: str, tests: list[TestResult]):
        with self.lock:
            for t in tests:
                if t.outcome == "passed" and not t.flaky and "::" in t.nodeid:
                    self.guards.append(f"{rel}::{t.nodeid.split('::', 1)[1]}")

    def _overlaps(self, site: str) -> bool:
        f, a, b = site.split(":", 3)[:3]
        a, b = int(a), int(b)
        for s in self.fixes:
            g, c, d = s.split(":", 3)[:3]
            if g == f and int(c) < b and a < int(d):
                return True
        return False

    # ------------------------------------------------------------------ judging
    def _code_context(self, u, cands: list[TestResult]) -> str:
        parts = [u.source]
        seen = set()
        for t in cands:
            for f in t.frames or []:
                path = str(f[0])
                if f"{self.pkg}/" not in path and not path.startswith(self.pkg):
                    continue
                rel = path[path.find(self.pkg):]
                key = (rel, int(f[1]) // 25)
                if key in seen:
                    continue
                seen.add(key)
                file = self.ws / rel
                if not file.exists():
                    continue
                lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
                ln = int(f[1])
                a, b = max(1, ln - 12), min(len(lines), ln + 8)
                if any(sa <= ln <= sb for sa, sb in u.spans):
                    continue
                parts.append(f"# {rel} around line {ln}\n" + "\n".join(f"{i:4d}| {lines[i - 1]}" for i in range(a, b + 1)))
        return "\n\n".join(parts)[:14000]

    def judge(self, u, body: str, cands: list[TestResult]) -> dict:
        funcs = _functions(body)
        blocks = []
        for t in cands:
            src = funcs.get(t.func, "")
            used = set(re.findall(r"\b([A-Za-z_]\w*)\b", src))
            helpers = [funcs[n] for n in used if n in funcs and n != t.func and not n.startswith("test")]
            helper_txt = ("\n# helpers used by the test\n" + "\n\n".join(helpers)) if helpers else ""
            fail = re.sub(r"\n{3,}", "\n\n", t.text or "")[-1800:]
            blocks.append(f"### {t.func}\n```python\n{src}{helper_txt}\n```\nFailure on the current code:\n```\n{fail}\n```")
        user = _fill(self.judge_prompt[1], project=self.task.project, summary=self.task.summary,
                     docs=inventory.docs_for(u, self.sections, 2200) or "(none found)",
                     source=self._code_context(u, cands), tests="\n\n".join(blocks))
        command = self.llm.ask(self.judge_prompt[0], user, f"judge:{u.uid}")
        text = heredoc_body(command) or command or ""
        out = {}
        names = {t.func for t in cands}
        for line in text.splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 2:
                continue
            name = parts[0].strip("`* -").split("::")[-1].split("[")[0]
            verdict = parts[1].upper().strip("* ")
            if name in names and verdict in ("BUG", "WRONG", "UNSURE"):
                out[name] = (verdict, parts[2] if len(parts) > 2 else "", parts[3] if len(parts) > 3 else "")
        return out

    # ------------------------------------------------------------------ final suite
    def assemble(self):
        out_dir = self.ws / "tests_new"
        out_dir.mkdir(exist_ok=True)
        for p in out_dir.rglob("*.py"):
            p.unlink()
        (out_dir / "conftest.py").write_text(CONFTEST)
        written = []
        with self.lock:
            items = sorted(self.kept.items())
        for rel, keep in items:
            try:
                src = prune(self.sources[rel], keep)
            except SyntaxError:
                continue
            if static_problem(src):
                continue
            name = "test_" + Path(rel).stem.removeprefix("test_") + ".py"
            (out_dir / name).write_text(src)
            written.append((name, keep))
        if not written:
            return
        # Re-run the assembled suite: keep only tests that still fail reliably on this code.
        tests, coll, _ = self.runner.results(["tests_new"], repeats=2)
        still = {}
        for t in tests:
            file = t.nodeid.split("::")[0].split("/")[-1]
            if t.outcome == "failed" and not t.flaky:
                still.setdefault(file, set()).add(t.func)
        bad_files = {c.split("::")[0].split("/")[-1] for c in coll}
        total = 0
        for name, keep in written:
            path = out_dir / name
            final = keep & still.get(name, set())
            if name in bad_files or not final or total >= self.max_tests:
                path.unlink(missing_ok=True)
                continue
            if total + len(final) > self.max_tests:
                final = set(sorted(final)[: self.max_tests - total])
            path.write_text(prune(path.read_text(), final))
            total += len(final)
        self.llm.note("assembled", files=sorted(p.name for p in out_dir.glob("test_*.py")), tests=total)

    def run(self):
        try:
            try:
                self.doctest_phase()
            except Exception as error:  # noqa: BLE001
                self.llm.note("doctest-error", error=f"{error}\n{traceback.format_exc()[-1500:]}")
            try:
                self.llm_phase()
            except LimitsExceeded:
                pass
            except Exception as error:  # noqa: BLE001
                self.llm.note("llm-phase-error", error=f"{error}\n{traceback.format_exc()[-1500:]}")
        finally:
            self.assemble()
