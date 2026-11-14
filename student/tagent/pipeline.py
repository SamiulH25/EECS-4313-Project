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
from .runner import salvage, CONFTEST, Runner, TestResult, static_problem, strip_pointless_settings, triage_auto

WORKERS = 4
JOB_RESERVE = 30_000        # do not start a unit job with fewer tokens left than this
JOB_EST = 18_000            # typical tokens of one unit job (generation + repairs); reserved per running job
MAX_PER_LOCATION = 2        # kept tests per repaired location
USE_JUDGE = False           # the LLM judge was too permissive; the repair check replaces it
USE_BOUNDARY_VOTE = False   # the 9B model voted the wrong way; sibling evidence is used instead
# Technique switches (turned off only in ablation copies made by scripts/make_variant.sh)
USE_DOCTESTS = True         # mine documentation examples into one-check tests
USE_REPAIR_FILTER = True    # keep a failing test only if a single small code repair explains it
USE_CONTRACTS = True        # model-free contract fuzzing (reference models, observer consistency, ddmin)
CONTRACT_SEEDS = (1, 2, 3, 4)  # one fuzzing pass (about 90 s) per seed, each on top of the repairs found so far
CONTRACT_MAX_VERIFY = 15
CONTRACT_GUARDS = 6        # short passing sequences per driven object used only as repair guards
CONTRACT_LONG = 4          # long regression sequences per driven object, written after the shrunk passes    # failing contract tests per pass sent to the repair check (spread over failure kinds)
USE_PLANNER = True          # the model plans each round from the tools' report
USE_PROPS = True            # the model may state documented rules as properties; a tool explores inputs
PROP_SEEDS = 200            # random inputs per property
ROUNDS = 4
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
        # getattr(obj, "_Class__field") / hasattr / vars-style access by name
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and \
                node.func.id in ("getattr", "hasattr", "setattr") and len(node.args) >= 2 and \
                isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str) and \
                node.args[1].value.startswith("_") and not node.args[1].value.endswith("__"):
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


PROPS_MODE = """Write PROPERTIES first, then a few plain tests.
A property is a top-level function `def prop_<rule>(r):` (r is a `random.Random`). It states ONE documented
rule for ANY input: use r to choose sizes, keys, values and a short random sequence of operations, build a
fresh object inside the function, and assert the rule (compare against a tiny reference model in plain
Python, or against another documented way of observing the same thing). A tool runs every property on 200
random inputs and turns the inputs that break the rule into tests, so state rules, not examples.
Write 3 to 6 properties for the documented behaviour that the risk points control (eviction order,
expiry times, counters, bounds, ordering), then {{n_tests}} plain tests for edge cases."""

TESTS_MODE = """Write {{n_tests}} focused pytest tests."""

_FLIPS = {("<", "<="), ("<=", "<"), (">", ">="), (">=", ">")}


def is_boundary_flip(info: dict) -> bool:
    """A comparison repair that only moves the boundary in or out (< vs <=, > vs >=)."""
    if info.get("kind") != "comparison":
        return False
    orig, repl = info.get("orig", ""), (info.get("repl", "") or "").strip()
    if repl.startswith("(") and repl.endswith(")"):
        repl = repl[1:-1]
    a = re.findall(r"<=|>=|==|!=|<|>", orig)
    b = re.findall(r"<=|>=|==|!=|<|>", repl)
    if len(a) != len(b) or not a:
        return False
    diff = [(x, y) for x, y in zip(a, b) if x != y]
    if len(diff) != 1 or diff[0] not in _FLIPS:
        return False
    norm = lambda t: re.sub(r"\s+|[()]", "", re.sub(r"<=|>=|==|!=|<|>", "?", t))  # noqa: E731
    return norm(orig) == norm(repl)


SLOW_TEST = 1.5          # seconds; slower tests are not submitted (see assemble)


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
        self.boundary_prompt = _load_prompt(self.student / "prompts" / "boundary.md")
        self.cand = self.ws / "_cand"
        self.runner.ensure_conftest(self.cand)
        self.lock = threading.Lock()
        self.kept: dict[str, set[str]] = {}       # candidate file -> kept test names
        self.sources: dict[str, str] = {}         # candidate file -> source
        self.location_count: Counter = Counter()
        self.history: dict[str, list[str]] = {}   # unit uid -> notes for later rounds
        self.fixes: list[str] = []                # accepted repair sites (approximates the correct code)
        self.shadow_fixes: list[str] = []         # plus first explanations of ambiguous failures (fuzzing only)
        self.last_explained: dict = {}
        self.contract_info: dict = {"targets": [], "located": []}   # what the fuzzer covered and found
        self.unit_stats: dict[str, dict] = {}     # uid -> rounds, kept, last summary (planner input)
        self.plan_prompt = _load_prompt(self.student / "prompts" / "plan.md")
        self.guards: list[str] = []               # passing tests that a trusted repair must keep passing
        self.arcs: dict[str, set] = {}            # package file -> executed (from_line, to_line) arcs
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

    # ------------------------------------------------------------------ phase 2: contract fuzzing (no model)
    def contract_phase(self):
        passes = list(CONTRACT_SEEDS) + ([0] if CONTRACT_LONG else [])
        for seed in passes:
            tag = uuid.uuid4().hex[:8]
            req, out = self.ws / f".tagent_creq_{tag}.json", self.ws / f".tagent_cres_{tag}.json"
            long = CONTRACT_LONG if seed == 0 else 0
            with self.lock:
                # long sequences must pass on the code with only the confidently accepted repairs
                fixes = list(self.fixes) if long else \
                    list(self.fixes) + [f for f in self.shadow_fixes if f not in self.fixes]
            if long and not fixes:
                continue
            req.write_text(json.dumps({"package": self.pkg, "deadline": 60, "seed": 99 if long else seed,
                                       "per_sig": 2, "max_tests": 60, "fixes": fixes, "long": long}))
            cmd = (f"PYTHONPATH={shlex.quote(str(self.student))} timeout 112 python -m tagent.contracts "
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
            src = res.get("source") or ""
            self.llm.note("contracts", seed=seed, stats=res.get("stats"), labels=res.get("labels"))
            with self.lock:
                for t in (res.get("stats") or {}).get("targets") or []:
                    if t not in self.contract_info["targets"]:
                        self.contract_info["targets"].append(t)
            if not src or static_problem(src):
                if src:
                    self.llm.note("contracts", seed=seed, error=static_problem(src))
                continue
            rel = f"_cand/test_contracts_{seed or 'long'}.py"
            (self.ws / rel).write_text(src)
            tests, coll, _ = self.runner.results([rel], repeats=2)
            failing = [t for t in tests if t.outcome == "failed"]
            cands = [t for t in failing if triage_auto(t, rel, self.pkg) == "judge"]
            # round-robin over (target, failure kind) so one bug does not use up the repair-check time
            labels = res.get("labels") or []
            groups: dict = {}
            for t in cands:
                m = re.search(r"_(\d+)$", t.func)
                g = tuple(labels[int(m.group(1))]) if m and int(m.group(1)) < len(labels) else (t.func,)
                groups.setdefault(g, []).append(t)
            picked = []
            while len(picked) < CONTRACT_MAX_VERIFY and any(groups.values()):
                for g in list(groups):
                    if groups[g] and len(picked) < CONTRACT_MAX_VERIFY:
                        picked.append(groups[g].pop(0))
            keep, located = self.verify(rel, picked, group=lambda f: re.sub(r"^test_contract_|_\d+$", "", f))
            with self.lock:
                self.contract_info["located"] += list(located.values())
            with self.lock:
                for sites in (self.last_explained or {}).values():
                    if sites and sites[0] not in self.shadow_fixes and not self._overlaps_any(sites[0], self.shadow_fixes + self.fixes):
                        self.shadow_fixes.append(sites[0])
            self.llm.note("kept", file=rel, kept=sorted(keep), located=located, collection=coll[:1])
            if keep:
                with self.lock:
                    self.sources[rel] = src
                    self.kept[rel] = keep
                    # reference-checked tests that pass once the accepted repairs are applied guard later
                    # repairs (the repair tool re-runs guards with those repairs and keeps the passing ones)
                    for t in tests:
                        if t.func in keep and "::" in t.nodeid:
                            self.guards.append(f"{rel}::{t.nodeid.split('::', 1)[1]}")

    def contract_guards(self):
        """Short reference-checked sequences that pass on the current code: never submitted, only used as
        guards so a repair that 'explains' a wrong model test must not break the reference model's behaviour."""
        tag = uuid.uuid4().hex[:8]
        req, out = self.ws / f".tagent_creq_{tag}.json", self.ws / f".tagent_cres_{tag}.json"
        with self.lock:
            fixes = list(self.fixes)
        req.write_text(json.dumps({"package": self.pkg, "deadline": 40, "seed": 77, "fixes": fixes,
                                   "long": CONTRACT_GUARDS, "span": 2}))
        cmd = (f"PYTHONPATH={shlex.quote(str(self.student))} timeout 60 python -m tagent.contracts "
               f"{req.name} {out.name} > /dev/null 2>&1")
        self.runner.env.execute({"command": cmd}, timeout=70)
        res = {}
        if out.exists():
            try:
                res = json.loads(out.read_text())
            except json.JSONDecodeError:
                res = {}
        req.unlink(missing_ok=True)
        out.unlink(missing_ok=True)
        src = res.get("source") or ""
        if not src or static_problem(src):
            return
        rel = "_cand/test_contracts_guard.py"
        (self.ws / rel).write_text(src)
        tests, _, _ = self.runner.results([rel], repeats=1)
        ids = [f"{rel}::{t.nodeid.split('::', 1)[1]}" for t in tests if "::" in t.nodeid]
        self.llm.note("contract_guards", count=len(ids), passing=sum(t.outcome == "passed" for t in tests))
        with self.lock:
            self.guards = ids + self.guards

    # ------------------------------------------------------------------ phase 3: model-written tests
    def llm_phase(self):
        self.units = inventory.build_units(self.ws, self.pkg)
        self.sections = inventory.doc_sections(self.ws)
        order = sorted([u for u in self.units if u.weight >= 1], key=lambda u: -u.weight)
        self.llm.note("units", units=[(u.uid, round(u.weight, 1), len(u.risks)) for u in order])
        stop = threading.Event()
        self.focus: dict[str, tuple[str, str]] = {}
        self.inflight = 0
        for rnd in range(1, ROUNDS + 1):
            if stop.is_set() or self.llm.tokens_left < JOB_RESERVE:
                break
            default = order if rnd <= 2 else order[: max(1, len(order) // 2)]
            plan = self.plan(rnd, order, default) if USE_PLANNER else [(u, "tests", "") for u in default]
            if not plan:
                break
            jobs: queue.Queue = queue.Queue()
            for u, action, focus in plan:
                self.focus[u.uid] = (action, focus)
                jobs.put(u)

            def worker(rnd=rnd, jobs=jobs):
                while not stop.is_set():
                    with self.lock:
                        # keep the jobs already running from pushing the total past the budget
                        if self.llm.tokens_left - self.inflight * JOB_EST < max(JOB_RESERVE, JOB_EST):
                            stop.set()
                            return
                        try:
                            u = jobs.get_nowait()
                        except queue.Empty:
                            return
                        self.inflight += 1
                    try:
                        self.unit_job(u, rnd)
                    except LimitsExceeded:
                        stop.set()
                        return
                    except Exception as error:  # noqa: BLE001
                        self.llm.note("job-error", unit=u.uid, error=f"{error}\n{traceback.format_exc()[-1500:]}")
                    finally:
                        with self.lock:
                            self.inflight -= 1

            threads = [threading.Thread(target=worker, daemon=True) for _ in range(WORKERS)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

    # ------------------------------------------------------------------ planner
    def _unit_report(self, u) -> str:
        names = {w for w in re.findall(r"[A-Za-z_]\w*", u.title) if len(w) > 2}
        fuzz = sorted({t.split(":")[0] for t in self.contract_info["targets"]
                       if any(re.search(rf"\b{re.escape(n)}\b", t) for n in names)})
        bugs = 0
        for loc in self.contract_info["located"]:
            m = re.match(r"(.+) line (\d+)", loc)
            if m and m.group(1).endswith(u.path) and any(a <= int(m.group(2)) <= b for a, b in u.spans):
                bugs += 1
        st = self.unit_stats.get(u.uid, {})
        gaps = len(self.uncovered(u)) if st.get("rounds") else len(u.risks)
        return (f"- {u.uid} | {u.title[:60]} | risk points {len(u.risks)} | fuzzer: "
                f"{', '.join(fuzz) if fuzz else 'could not drive it'}, bugs it confirmed here {bugs} | "
                f"model rounds so far {st.get('rounds', 0)}, bugs confirmed {st.get('kept', 0)} | "
                f"unexercised branches/risk points {gaps} | last round: {st.get('last', '-')}")

    def plan(self, rnd: int, order: list, default: list) -> list:
        """One model call: choose the units for this round and what to do with each."""
        report = "\n".join(self._unit_report(u) for u in order)
        user = _fill(self.plan_prompt[1], project=self.task.project, summary=self.task.summary, round=str(rnd),
                     rounds=str(ROUNDS), report=report, tokens=str(self.llm.tokens_left))
        text = heredoc_body(self.llm.ask(self.plan_prompt[0], user, f"plan:r{rnd}")) or ""
        by_id = {u.uid: u for u in order}
        plan, seen = [], set()
        for line in text.splitlines():
            parts = [x.strip(" `*-") for x in line.split("|")]
            if len(parts) < 2 or parts[0] not in by_id or parts[0] in seen:
                continue
            action = parts[1].lower()
            if action not in ("props", "tests", "skip"):
                continue
            seen.add(parts[0])
            if action != "skip":
                plan.append((by_id[parts[0]], action, (parts[2] if len(parts) > 2 else "")[:300]))
        self.llm.note("plan", round=rnd, plan=[(u.uid, a, f) for u, a, f in plan], raw=text[:3000])
        if len(plan) < min(3, len(default)):
            # unusable or too timid a plan: fall back to risk order, keeping what the planner chose
            chosen = {u.uid for u, _, _ in plan}
            plan += [(u, "props" if USE_PROPS else "tests", "") for u in default if u.uid not in chosen]
        return plan

    def uncovered(self, u) -> list[str]:
        """Branch outcomes in the unit that no generated test has exercised (coverage-guided rounds)."""
        with self.lock:
            arcs = set(self.arcs.get(u.path, set()))
        if not arcs:
            return []
        lines = {b for _, b in arcs} | {a for a, _ in arcs}
        try:
            text = (self.ws / u.path).read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(text)
        except (OSError, SyntaxError):
            return []
        src = text.splitlines()
        out = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.If, ast.While)) or not any(a <= node.lineno <= b for a, b in u.spans):
                continue
            test_lines = set(range(node.test.lineno, node.test.end_lineno + 1))
            body = set(range(node.body[0].lineno, node.body[-1].end_lineno + 1))
            cond = src[node.lineno - 1].strip()[:90]
            if not test_lines & lines:
                out.append(f"L{node.lineno}: `{cond}` is never reached")
                continue
            true_taken = node.body[0].lineno in lines
            false_taken = any(a in test_lines and b not in body and b not in test_lines for a, b in arcs)
            if not true_taken:
                out.append(f"L{node.lineno}: `{cond}` is never true")
            elif not false_taken:
                out.append(f"L{node.lineno}: `{cond}` is never false")
        for r in u.risks:
            m = re.match(r"L(\d+):", r)
            if m and int(m.group(1)) not in lines and not any(o.startswith(f"L{m.group(1)}:") for o in out):
                out.append(f"{r.split(' -> ')[0]} is never executed")
        return out[:25]

    def _gen_user(self, u, rnd: int, rel: str) -> str:
        prev = ""
        notes = self.history.get(u.uid) or []
        if notes:
            prev = ("\n## Tests already written for this unit in earlier rounds\n"
                    "Do not repeat them; target other methods, other risk points and other boundaries.\n"
                    + "\n".join(notes[-30:]) + "\n")
        gaps = self.uncovered(u) if rnd > 1 else []
        if gaps:
            prev += ("\n## Branches and risk points that NO test has exercised yet (coverage)\n"
                     "Write tests that drive these, with expected values from the documentation:\n"
                     + "\n".join(f"- {g}" for g in gaps) + "\n")
        action, focus = getattr(self, "focus", {}).get(u.uid, ("props" if USE_PROPS else "tests", ""))
        if focus:
            prev += f"\n## Focus for this round (from the planner)\n{focus}\n"
        n_tests = "6 to 10" if action == "props" and USE_PROPS else "10 to 14"
        mode = (PROPS_MODE if action == "props" and USE_PROPS else TESTS_MODE).replace("{{n_tests}}", n_tests)
        return _fill(self.gen_prompt[1], project=self.task.project, summary=self.task.summary, title=u.title,
                     mode=mode,
                     import_hint=u.import_hint,
                     # later rounds: the notes carry what was learnt, so the reference material is trimmed
                     docs=inventory.docs_for(u, self.sections, 4500 if rnd == 1 else 3000) or "(none found)",
                     outline=u.outline or "(module-level functions)", source=u.source,
                     risks="\n".join(u.risks) or "(none detected)",
                     existing=(inventory.existing_tests_for(self.ws, u) or "# (none)") if rnd == 1
                     else "# (see round 1; omitted)",
                     previous=prev, n_tests=n_tests, path=rel)

    def _repair(self, rel: str, body: str, problem: str, u=None) -> str | None:
        ref = ""
        if u is not None:
            ref = ("Reference: how the API is documented (use exactly these call forms; never invent arguments):\n"
                   f"{inventory.docs_for(u, self.sections, 2500) or '(none)'}\n\nSignatures:\n{u.outline[:2500]}\n\n")
        user = (f"{ref}This pytest file has a problem:\n```\n{problem[-2500:]}\n```\n\nThe file:\n```python\n{body}\n```\n"
                "Rewrite the complete file so that it imports, collects and runs cleanly: fix the way the "
                "crashing tests call the API, keep what each test checks and its expected values. Never import os, sys, inspect, pathlib, io, "
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
            return strip_pointless_settings(body)
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
        return strip_pointless_settings("\n".join(src[:insert_at] + lines + src[insert_at:]) + "\n")

    def unit_job(self, u, rnd: int):
        rel = f"_cand/test_{u.uid}_r{rnd}.py"
        body = heredoc_body(self.llm.ask(self.gen_prompt[0], self._gen_user(u, rnd, rel), f"gen:{u.uid}:r{rnd}"))
        if not body:
            return
        body = self._with_imports(u, body)
        problem = static_problem(body)
        if problem:
            saved = salvage(body)
            n_tests = len(re.findall(r"^\s*def test_", saved, re.M))
            if saved and not static_problem(saved) and n_tests >= 3:
                body = self._with_imports(u, saved)       # keep the valid tests; no model call needed
            else:
                fixed = self._repair(rel, body, problem, u)
                if fixed and static_problem(fixed):
                    fixed = salvage(fixed)
                if not fixed or static_problem(fixed):
                    if not saved or static_problem(saved) or not n_tests:
                        return
                    fixed = saved
                body = self._with_imports(u, fixed)
        path = self.ws / rel
        path.write_text(body)
        prop_notes: list[str] = []
        if USE_PROPS and re.search(r"^def prop_\w+\(\s*r\s*\)", body, re.M):
            body, prop_notes = self.run_props(rel, body)
            path.write_text(body)
        tests, coll, out = self.runner.results([rel], repeats=2, coverage=self.arcs)
        broken = [t for t in tests if t.outcome == "failed" and triage_auto(t, rel, self.pkg) == "drop"]
        if coll or len(broken) >= 2:
            problem = "\n".join(coll)[-2500:] if coll else (
                "These tests crash inside the test code itself (wrong use of the API):\n" +
                "\n".join(f"{t.func}: {(t.crash or ['', '', t.text[-300:]])[2][:300]}" for t in broken[:6]))
            fixed = self._repair(rel, body, problem, u)
            if not fixed or static_problem(fixed):
                if coll:
                    return
            else:
                old = (body, tests)
                body = self._with_imports(u, fixed)
                path.write_text(body)
                tests, coll, out = self.runner.results([rel], repeats=2, coverage=self.arcs)
                if coll:
                    if old[1]:
                        body, tests = old            # the rewrite broke the file: keep the original
                        path.write_text(body)
                        coll = []
                    else:
                        return
        failing = [t for t in tests if t.outcome == "failed"]
        def private(func):
            m = re.match(r"test_(prop_\w+)_seed\d+$", func)
            return reads_private(body, func) or bool(m and reads_private(body, m.group(1)))
        cands = [t for t in failing if triage_auto(t, rel, self.pkg) == "judge" and not private(t.func)]
        passed = [t.func for t in tests if t.outcome == "passed"]
        self._add_guards(rel, tests)
        self.llm.note("ran", file=rel, tests=len(tests), passed=len(passed), failing=len(failing),
                      candidates=[t.name for t in cands])
        keep, located = self.verify(rel, cands, unit=u)
        verdicts = {}
        if USE_JUDGE and keep:
            judged = [t for t in cands if t.func in keep]
            for i in range(0, len(judged), 5):
                verdicts.update(self.judge(u, body, judged[i:i + 5]))
            keep = {f for f in keep if verdicts.get(f, ("BUG",))[0] == "BUG"}
        with self.lock:
            notes = self.history.setdefault(u.uid, [])
            notes += prop_notes
            st = self.unit_stats.setdefault(u.uid, {"rounds": 0, "kept": 0})
            st["rounds"] += 1
            st["kept"] += len(keep)
            st["last"] = (f"{len(tests)} tests, {len(passed)} passed, {len(failing)} failed, "
                          f"{len(keep)} confirmed as bugs")
            for t in tests:
                if t.func in keep:
                    notes.append(f"- {t.func}: FOUND A BUG near {located.get(t.func, '?')}")
                elif t.outcome == "passed":
                    notes.append(f"- {t.func}: passed (behaviour already correct)")
                elif t in cands:
                    notes.append(f"- {t.func}: failed, but its expectation looks wrong (no small fix explains it)")
                elif t.outcome == "failed":
                    msg = ((t.crash or [None, None, ""])[2] or t.text[-200:] or "").strip().splitlines()
                    msg = msg[0][:160] if msg else "?"
                    notes.append(f"- {t.func}: CRASHED inside the test code ({msg}); "
                                 "the test used the API wrongly: check the signatures and docs")
            if keep:
                self.sources[rel] = body
                self.kept[rel] = keep
        self.llm.note("kept", file=rel, kept=sorted(keep), located=located, verdicts=verdicts)

    # ------------------------------------------------------------------ model-stated properties
    def run_props(self, rel: str, body: str) -> tuple[str, list[str]]:
        """Run every `prop_<name>(r)` on PROP_SEEDS random inputs; failing inputs become plain tests."""
        names = re.findall(r"^def (prop_\w+)\(\s*r\s*\)", body, re.M)
        tag = uuid.uuid4().hex[:8]
        req, out = self.ws / f".tagent_preq_{tag}.json", self.ws / f".tagent_pres_{tag}.json"
        req.write_text(json.dumps({"file": rel, "package": self.pkg, "props": names, "seeds": PROP_SEEDS,
                                   "deadline": 90}))
        cmd = (f"PYTHONPATH={shlex.quote(str(self.student))} timeout 112 python -m tagent.props "
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
        props = res.get("props") or {}
        self.llm.note("props", file=rel, error=res.get("error"),
                      props={k: {"runs": v.get("runs"), "fails": len(v.get("fails") or []), "first": v.get("first"),
                                 "kinds": v.get("kinds")} for k, v in props.items()})
        notes, added = [], []
        for name, r in props.items():
            runs, fails = r.get("runs", 0), r.get("fails") or []
            if not runs:
                notes.append(f"- {name}: did not run ({r.get('first') or res.get('error') or 'error'})")
                continue
            if not fails:
                notes.append(f"- {name}: held for all {runs} random inputs (no bug there)")
                continue
            not_assert = not str(r.get("first", "")).startswith("AssertionError")
            if not_assert and (r.get("where") in ("property", "other") or len(fails) == runs):
                notes.append(f"- {name}: CRASHED inside the property itself on {len(fails)}/{runs} inputs "
                             f"({r.get('first')}); fix how it uses the API")
                continue
            if len(fails) == runs:
                notes.append(f"- {name}: failed for EVERY input ({r.get('first')}); either the rule is wrong "
                             "(re-read the docs) or the code is broken everywhere")
            else:
                notes.append(f"- {name}: failed for {len(fails)}/{runs} random inputs ({r.get('first')})")
            # the smallest failing seeds become deterministic tests
            for seed in fails[:2]:
                added.append(f"\n\ndef test_{name}_seed{seed}():\n    {name}(random.Random({seed}))\n")
        if added:
            if not re.search(r"^import random\b", body, re.M):
                body = "import random\n" + body
            body = body.rstrip() + "".join(added) + "\n"
        return body, notes

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

    def verify(self, rel: str, cands: list[TestResult], cap: bool = True, unit=None,
               group=None) -> tuple[set[str], dict]:
        """Keep failing tests that a single small repair (on top of the repairs accepted so far) makes
        pass without breaking the existing suite. Returns (kept function names, name -> site)."""
        if not cands:
            return set(), {}
        if not USE_REPAIR_FILTER:            # ablation: every auto-triaged failing test is kept
            return {t.func for t in cands}, {t.func: "(unchecked)" for t in cands}
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
            for key in ("errors", "guard_errors", "guard_rejected", "suite_ok"):
                res.setdefault(key, {}).update(part.get(key) or {})
            res.setdefault("sibling_rejected", []).extend(part.get("sibling_rejected") or [])
            res.setdefault("pairs", {}).update(part.get("pairs") or {})
        self.llm.note("repair", file=rel, notes=res.get("notes"), explained=res.get("explained"),
                      baseline_passing=[t for t, ok in (res.get("baseline") or {}).items() if ok],
                      errors=res.get("errors"), guard_errors=res.get("guard_errors"),
                      guard_rejected=res.get("guard_rejected"), suite_ok=res.get("suite_ok"),
                      sibling_rejected=res.get("sibling_rejected"))
        self.last_explained = res.get("explained") or {}
        keep, located = set(), {}
        # Boundary flips (< vs <=) are where the model most often guesses wrongly at an undocumented
        # edge: ask several independent focused questions before trusting one.
        votes: dict[str, bool] = {}
        if unit is not None and USE_BOUNDARY_VOTE:
            for tid in by_id:
                sites = (res.get("explained") or {}).get(tid) or []
                if (res.get("baseline") or {}).get(tid) or not sites:
                    continue
                infos = [(res.get("sites") or {}).get(x) or {} for x in sites]
                if all(is_boundary_flip(i) for i in infos):
                    key = sites[0]
                    if key not in votes:
                        votes[key] = self.boundary_vote(unit, infos[0])
        with self.lock:
            for tid, t in by_id.items():
                if (res.get("baseline") or {}).get(tid):
                    if not self.fixes:
                        continue        # passes in-process with no repair at all: runner disagreement
                    sites = ["(earlier repair)"]
                else:
                    sites = (res.get("explained") or {}).get(tid) or []
                if not sites:
                    continue
                if votes.get(sites[0]) is False:
                    continue
                site = sites[0]
                loc = ":".join(site.split(":", 3)[:3])
                if site == "(earlier repair)":
                    # Explained by a repair accepted earlier: count it against the place it fails at.
                    pkg_frames = [f for f in (t.frames or []) if f"{self.pkg}/" in str(f[0])]
                    f = pkg_frames[-1] if pkg_frames else None
                    loc = f"earlier:{str(f[0])[str(f[0]).find(self.pkg):]}:{f[1]}" if f else f"earlier:{rel}:{t.func}"
                if group:
                    # contract tests: one long operation sequence often exposes several bugs on other
                    # paths, so allow the cap per (location, driven class) to keep the classes diverse
                    loc = f"{loc}|{group(t.func)}"
                if cap and self.location_count[loc] >= MAX_PER_LOCATION:
                    continue
                self.location_count[loc] += 1
                keep.add(t.func)
                info = (res.get("sites") or {}).get(site, {})
                located[t.func] = f"{info.get('file', '?')} line {info.get('line', '?')}" if info else site
                if site != "(earlier repair)" and len(sites) == 1 and not self._overlaps(site):
                    self.fixes.append(site)
                elif tid in (res.get("pairs") or {}):
                    # two bugs on one path, repaired together
                    for x in res["pairs"][tid]:
                        if not self._overlaps(x):
                            self.fixes.append(x)
        return keep, located

    def boundary_vote(self, u, info: dict) -> bool:
        """True when at least 2 of 3 independent questions say the repaired comparison is intended."""
        file = self.ws / info.get("file", "")
        if not file.exists():
            return True
        lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
        ln = int(info.get("line", 0))
        if not 1 <= ln <= len(lines):
            return True
        current = lines[ln - 1].strip()
        orig, repl = info.get("orig", ""), info.get("repl", "")
        changed = current.replace(orig, repl.strip("()") if repl.startswith("(") else repl, 1) \
            if orig and orig in current else repl
        if changed == current:
            return True
        a, b = max(1, ln - 14), min(len(lines), ln + 6)
        context = "\n".join(f"{i:4d}| {lines[i - 1]}" for i in range(a, b + 1))
        names = set(re.findall(r"[A-Za-z_]\w{2,}", orig)) - {"self", "None", "True", "False", "not", "and"}
        sib = [f"{i:4d}| {t.strip()}" for i, t in enumerate(lines, 1)
               if i != ln and re.search(r"[<>]=?|==", t) and names & set(re.findall(r"[A-Za-z_]\w{2,}", t))
               and not t.strip().startswith("#")][:14]
        docs = inventory.docs_for(u, self.sections, 3000) or "(none found)"
        questions = [
            "Which version is the intended behaviour at the exact boundary value?",
            "Which version is consistent with what the documentation says happens at the boundary?",
            "A reviewer thinks one version is an injected off-by-one bug. Which version is the CORRECT one?",
        ]
        yes = no = 0
        for k, q in enumerate(questions):
            swap = k % 2 == 1                     # alternate the order to cancel position bias
            va, vb = (changed, current) if swap else (current, changed)
            user = _fill(self.boundary_prompt[1], project=self.task.project, summary=self.task.summary,
                         docs=docs, file=info.get("file", ""), line=ln, context=context,
                         siblings="\n".join(sib) or "(none)", a=va, b=vb, question=q)
            cmd = self.llm.ask(self.boundary_prompt[0], user, f"vote:{info.get('file')}:{ln}:{k}") or ""
            m = re.search(r"VERDICT:\s*(A|B|UNSURE)", cmd.upper())
            pick = m.group(1) if m else "UNSURE"
            if pick == ("A" if swap else "B"):
                yes += 1
            else:
                no += 1
            if yes >= 2 or no >= 2:
                break
        self.llm.note("vote", file=info.get("file"), line=ln, current=current, changed=changed, yes=yes, no=no)
        return yes >= 2

    def _add_guards(self, rel: str, tests: list[TestResult]):
        with self.lock:
            for t in tests:
                if t.outcome == "passed" and not t.flaky and "::" in t.nodeid:
                    self.guards.append(f"{rel}::{t.nodeid.split('::', 1)[1]}")

    @staticmethod
    def _overlaps_any(site: str, others: list[str]) -> bool:
        try:
            rel, a, b, _ = site.split(":", 3)
            a, b = int(a), int(b)
        except ValueError:
            return True
        for o in others:
            try:
                r2, c, d, _ = o.split(":", 3)
            except ValueError:
                continue
            if r2 == rel and int(c) < b and a < int(d):
                return True
        return False

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
        """Write tests_new/ from the confirmed tests. A test that runs longer than SLOW_TEST seconds is
        left out: under the evaluator's load it can hit the time limit on the correct code (a false
        alarm), and its "detections" there are timeouts rather than real failures."""
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
            if t.outcome == "failed" and not t.flaky and t.duration < SLOW_TEST:
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
                if USE_DOCTESTS:
                    self.doctest_phase()
            except Exception as error:  # noqa: BLE001
                self.llm.note("doctest-error", error=f"{error}\n{traceback.format_exc()[-1500:]}")
            try:
                if USE_CONTRACTS:
                    self.contract_phase()
            except Exception as error:  # noqa: BLE001
                self.llm.note("contract-error", error=f"{error}\n{traceback.format_exc()[-1500:]}")
            try:
                if USE_CONTRACTS and CONTRACT_GUARDS:
                    self.contract_guards()
            except Exception as error:  # noqa: BLE001
                self.llm.note("contract-error", error=f"{error}\n{traceback.format_exc()[-1500:]}")
            try:
                self.llm_phase()
            except LimitsExceeded:
                pass
            except Exception as error:  # noqa: BLE001
                self.llm.note("llm-phase-error", error=f"{error}\n{traceback.format_exc()[-1500:]}")
        finally:
            self.assemble()
