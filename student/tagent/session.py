"""State shared by the command line tools in student/tools.

The model drives the work by running those tools in its shell. Each tool is a separate process, so
everything the tools learn (accepted repairs, guards, kept tests, the queue of failing tests waiting
for the repair check) is kept in `.tagent/state.json` in the workspace.

Every tool call must finish within the course's 120 s command limit, so slow work (the mutation-based
repair check) is queued and done in slices: a tool does as much as fits and reports what is left.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from . import doctests, inventory
from .pipeline import (CONTRACT_GUARDS, CONTRACT_LONG, CONTRACT_MAX_VERIFY, CONTRACT_SEEDS, PROP_SEEDS,
                       USE_CONTRACTS, USE_DOCTESTS, Pipeline, prune, reads_private)
from .runner import CONFTEST, TestResult, salvage, static_problem, triage_auto

STATE = Path(".tagent") / "state.json"
TOOL_BUDGET = 110.0          # seconds one tool call may use (course limit per command: 120 s)
RETRIES = 2                 # times a rejected test is checked again after new repairs are accepted
DRY_LIMIT = 3               # rounds on one unit without a new confirmed bug before it is closed
CHECK_SECONDS = 88.0         # time one slice of the repair check (5 tests) may need, worst case


class LocalEnv:
    """The tools run inside the workspace shell already; commands they start run there too."""

    def __init__(self, ws: Path):
        self.ws = ws

    def execute(self, action: dict, timeout: int | None = None) -> dict:
        try:
            r = subprocess.run(action["command"], shell=True, cwd=self.ws, text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=timeout or 120)
            return {"output": r.stdout, "returncode": r.returncode}
        except subprocess.TimeoutExpired as e:
            out = e.output.decode() if isinstance(e.output, bytes) else (e.output or "")
            return {"output": out, "returncode": -1}


def init_state(ws: Path, task, student: Path):
    """Called once by the agent loop before the model starts."""
    path = ws / STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"task": {"project": task.project, "package": task.package,
                                         "summary": task.summary, "max_tests": task.max_tests,
                                         "student_dir": str(student)}}))
    (ws / "_cand").mkdir(exist_ok=True)
    (ws / "_cand" / "conftest.py").write_text(CONFTEST)


def find_workspace(here: Path) -> Path:
    """The workspace holding the tools' state: here, a parent of here, or a folder just below here
    (the model sometimes cd's around)."""
    for d in [here, *here.parents]:
        if (d / STATE).exists():
            return d
    for d in sorted(here.glob("*/")):
        if (d / STATE).exists():
            return d
    return here


class Session(Pipeline):
    """The pipeline's operations, run one tool call at a time on persisted state."""

    def __init__(self, ws: Path | None = None):
        ws = Path(ws).resolve() if ws else find_workspace(Path.cwd().resolve())
        data = json.loads((ws / STATE).read_text())
        t = data["task"]
        task = SimpleNamespace(workspace=ws, package=t["package"], project=t["project"], summary=t["summary"],
                               student_dir=Path(t["student_dir"]), max_tests=t["max_tests"],
                               trajectory_path=ws / ".tagent" / "tool_log.json")
        super().__init__(task, None, LocalEnv(ws))
        self.started = time.time()
        self.data = data
        self.kept = {k: set(v) for k, v in data.get("kept", {}).items()}
        self.sources = data.get("sources", {})
        self.location_count = Counter(data.get("location_count", {}))
        self.history = data.get("history", {})
        self.fixes = data.get("fixes", [])
        self.shadow_fixes = data.get("shadow_fixes", [])
        self.contract_info = data.get("contract_info", {"targets": [], "located": []})
        self.unit_stats = data.get("unit_stats", {})
        self.guards = data.get("guards", [])
        self.arcs = {f: {tuple(a) for a in v} for f, v in data.get("arcs", {}).items()}
        self.pending = data.get("pending", [])
        self.retry = data.get("retry", [])
        self.flags = data.get("flags", {})

    # ------------------------------------------------------------------ persistence
    def save(self):
        self.data.update(kept={k: sorted(v) for k, v in self.kept.items()}, sources=self.sources,
                         location_count=dict(self.location_count), history=self.history, fixes=self.fixes,
                         shadow_fixes=self.shadow_fixes, contract_info=self.contract_info,
                         unit_stats=self.unit_stats, guards=self.guards[-1500:],
                         arcs={f: sorted(v) for f, v in self.arcs.items()}, pending=self.pending, retry=self.retry,
                         flags=self.flags)
        tmp = self.ws / ".tagent" / "state.tmp"
        tmp.write_text(json.dumps(self.data))
        tmp.replace(self.ws / STATE)

    def left(self) -> float:
        return TOOL_BUDGET - (time.time() - self.started)

    def repair_check(self, tests, fixes, deadline: float = 65, baseline_only: bool = False, guards=None, hot=None):
        """One slice, sized so the whole tool call stays inside the command limit."""
        import shlex
        deadline = max(20.0, min(deadline, self.left() - 40))
        tag = f"{int(time.time() * 1000) % 10 ** 8}"
        req, out = self.ws / f".tagent_req_{tag}.json", self.ws / f".tagent_res_{tag}.json"
        req.write_text(json.dumps({"package": self.pkg, "tests": tests, "fixes": fixes, "guards": guards or [],
                                   "hot": hot or {}, "deadline": 0 if baseline_only else deadline,
                                   "baseline_only": baseline_only}))
        limit = int(max(30, self.left() - 2))
        cmd = (f"PYTHONPATH={shlex.quote(str(self.student))} timeout {limit} python -m tagent.repair "
               f"{req.name} {out.name} > /dev/null 2>&1")
        self.runner.env.execute({"command": cmd}, timeout=limit + 5)
        res = {}
        if out.exists():
            try:
                res = json.loads(out.read_text())
            except json.JSONDecodeError:
                res = {}
        req.unlink(missing_ok=True)
        out.unlink(missing_ok=True)
        return res

    # ------------------------------------------------------------------ units
    def units_list(self):
        if not hasattr(self, "_units"):
            self._units = inventory.build_units(self.ws, self.pkg)
            self.sections = inventory.doc_sections(self.ws)
        return self._units

    def unit(self, uid: str):
        units = self.units_list()
        uid = uid.strip().lower()
        exact = next((u for u in units if u.uid == uid), None)
        if exact:
            return exact
        m = re.match(r"u?(\d+)", uid)
        if m:
            return next((u for u in units if int(re.match(r"u(\d+)", u.uid).group(1)) == int(m.group(1))), None)
        return next((u for u in units if uid in u.uid or uid in u.title.lower()), None)

    def gate(self) -> str:
        """The automatic tools come first: the model's own tests build on what they confirm."""
        if not self.flags.get("docs_done"):
            return "doc_examples"
        if len(self.flags.get("fuzz_seeds", [])) < len(CONTRACT_SEEDS):
            return "fuzz"
        if not self.flags.get("long_done") and self.fixes:
            return "fuzz --long"
        return ""

    def gate_message(self) -> str:
        nxt = self.gate()
        return (f"Not yet: the automatic tools come first, because your tests build on what they confirm. "
                f"Run `{nxt}` now (then keep going until `fuzz` says fuzzing is complete).")

    def suggest(self) -> str:
        """The next unit worth the model's attention: many risk points, few rounds so far."""
        units = [u for u in self.units_list() if u.weight >= 1 and u.risks]
        if not units:
            return ""
        def score(u):
            st = self.unit_stats.get(u.uid, {})
            return (st.get("dry", 0) >= DRY_LIMIT, st.get("rounds", 0), -len(u.risks))
        fresh = sorted(units, key=score)[:3]
        return ("Units with the least testing so far: " +
                ", ".join(f"{u.uid} ({len(u.risks)} risk points, "
                          f"{self.unit_stats.get(u.uid, {}).get('rounds', 0)} rounds)" for u in fresh) +
                ". Use `unit <id>` to study one.")

    def unit_for_file(self, rel: str):
        units = self.units_list()
        name = Path(rel).stem.removeprefix("test_")
        m = re.match(r"u(\d+)", name)
        if m:
            return self.unit(m.group(0))
        best = None
        for u in units:
            if name.startswith(u.uid) and (best is None or len(u.uid) > len(best.uid)):
                best = u
        return best

    # ------------------------------------------------------------------ the repair-check queue
    def enqueue(self, rel: str, tests: list[TestResult], kind: str, uid: str = ""):
        for t in tests:
            self.pending.append({"rel": rel, "kind": kind, "uid": uid, "test": asdict(t)})

    def drain(self, report: list[str], min_left: float = CHECK_SECONDS + 8) -> int:
        """Run the repair check on queued failing tests while the time allows. Returns tests confirmed."""
        confirmed = 0
        while self.pending and self.left() > min_left:
            rel, kind = self.pending[0]["rel"], self.pending[0]["kind"]
            batch = [p for p in self.pending if p["rel"] == rel][:5]
            for p in batch:
                self.pending.remove(p)
            cands = [TestResult(**p["test"]) for p in batch]
            path = self.ws / rel
            if not path.exists():
                continue
            src = path.read_text()
            group = (lambda f: re.sub(r"^test_contract_|_\d+$", "", f)) if kind == "contract" else None
            nfix = len(self.fixes)
            keep, located = self.verify(rel, cands, cap=(kind != "doc"), group=group)
            if kind == "contract":
                for sites in (self.last_explained or {}).values():
                    if sites and sites[0] not in self.shadow_fixes and \
                            not self._overlaps_any(sites[0], self.shadow_fixes + self.fixes):
                        self.shadow_fixes.append(sites[0])
                self.contract_info["located"] += list(located.values())
                if keep:
                    self.guards += [f"{rel}::{t.nodeid.split('::', 1)[1]}" for t in cands
                                    if t.func in keep and "::" in t.nodeid]
            if kind == "doc":
                keep = {t.func for t in cands}           # documented examples are trusted oracles
            explained = self.last_explained or {}
            uid = batch[0].get("uid") or ""
            notes = self.history.setdefault(uid, []) if uid else []
            for t in cands:
                tid = f"{rel}::{t.nodeid.split('::', 1)[1]}" if "::" in t.nodeid else t.nodeid
                if t.func in keep and located.get(t.func, "").startswith("(earlier"):
                    report.append(f"  CONFIRMED {rel}::{t.func}  (but it shows a bug that is ALREADY confirmed: no new "
                                  "score, so test other methods and units)")
                    notes.append(f"- {t.func}: shows an already confirmed bug")
                elif t.func in keep:
                    if uid:
                        self.unit_stats.setdefault(uid, {"rounds": 0, "kept": 0})["dry"] = 0
                    where = located.get(t.func) or ("a documented example" if kind == "doc" else "an earlier repair")
                    report.append(f"  CONFIRMED {rel}::{t.func}  (bug near {where})")
                    notes.append(f"- {t.func}: FOUND A BUG near {where}")
                elif explained.get(tid):
                    report.append(f"  dropped   {rel}::{t.func}  (that code site already has enough confirmed tests: "
                                  "this bug is found, move on to other code)")
                    notes.append(f"- {t.func}: shows an already confirmed bug")
                else:
                    entry = next((p for p in batch if p["test"].get("nodeid") == t.nodeid), None)
                    if entry is not None and entry.get("tries", 0) < RETRIES and kind != "doc":
                        # It may need a second repair that a later check accepts: try it again then.
                        self.retry.append(dict(entry, tries=entry.get("tries", 0) + 1))
                    report.append(f"  rejected  {rel}::{t.func}  (no single small code repair explains it: its "
                                  "expectation is probably wrong; re-read the docs)")
                    notes.append(f"- {t.func}: failed, but its expectation looks wrong (no small fix explains it)")
            if len(self.fixes) > nfix and self.retry:
                # new repairs were accepted: tests rejected earlier get another chance on top of them
                self.pending += self.retry
                self.retry = []
            if keep:
                old = set(self.kept.get(rel, set()))
                self.kept[rel] = old | keep
                self.sources[rel] = src
                confirmed += len(keep)
                if uid:
                    st = self.unit_stats.setdefault(uid, {"rounds": 0, "kept": 0})
                    st["kept"] = st.get("kept", 0) + len(keep)
            self.save()
        return confirmed

    def queue_line(self) -> str:
        if not self.pending:
            return "Repair-check queue: empty."
        files = Counter(p["rel"] for p in self.pending)
        return (f"Repair-check queue: {len(self.pending)} failing tests still waiting "
                f"({', '.join(f'{k}: {v}' for k, v in files.items())}). Run `confirm` to check them.")

    def totals(self) -> str:
        n = sum(len(v) for v in self.kept.values())
        return (f"Confirmed bug-revealing tests so far: {n} in {len(self.kept)} files; "
                f"distinct repaired code sites: {len(self.fixes)}.")

    # ------------------------------------------------------------------ tool operations
    def op_doc_examples(self) -> str:
        if not USE_DOCTESTS:                     # ablation: documentation examples switched off
            return "This tool is switched off in this configuration. Run `fuzz` next."
        if self.flags.get("docs_done"):
            return "Documentation examples were already mined. " + self.queue_line()
        self.flags["docs_done"] = True
        src, n = doctests.build_file(self.ws, self.pkg)
        if not n:
            self.save()
            return "No runnable examples found in the documentation."
        rel = "_cand/test_docs.py"
        (self.ws / rel).write_text(src)
        if static_problem(src):
            self.save()
            return f"Mined {n} examples but the file is not usable ({static_problem(src)})."
        tests, coll, _ = self.runner.results([rel], repeats=3)
        failing = [t for t in tests if t.outcome == "failed"]
        cands = [t for t in failing if triage_auto(t, rel, self.pkg) == "judge"]
        self._add_guards(rel, tests)
        self.enqueue(rel, cands, "doc")
        report = [f"Mined {n} documentation examples into {rel}: {len(tests)} ran, "
                  f"{len(tests) - len(failing)} pass, {len(cands)} fail on this code (documented results the code "
                  f"does not produce: strong bug evidence)."]
        self.drain(report)
        report += [self.totals(), self.queue_line(), "Next: run `fuzz`."]
        self.save()
        return "\n".join(report)

    def op_fuzz(self, seed: int | None = None, long: bool = False) -> str:
        if not USE_CONTRACTS:                    # ablation: model-based fuzzing switched off
            return "All fuzzing passes are done (the fuzzer is switched off in this configuration). Now write your own tests."
        done = self.flags.setdefault("fuzz_seeds", [])
        if long:
            if self.flags.get("long_done"):
                return "The long regression sequences were already written. " + self.queue_line()
            if not self.fixes:
                return "No repair has been accepted yet: confirm some fuzzer findings first (`confirm`)."
        elif seed is None:
            free = [s for s in CONTRACT_SEEDS if s not in done]
            if not free:
                return ("All fuzzing passes are done. " +
                        ("Run `fuzz --long` once, then write your own tests. " if not self.flags.get("long_done")
                         else "Now write your own tests. " + self.suggest() + " ") + self.queue_line())
            seed = free[0]
        fixes = list(self.fixes) if long else list(self.fixes) + [f for f in self.shadow_fixes if f not in self.fixes]
        res = self._fuzz_request({"package": self.pkg, "deadline": 42, "seed": 99 if long else seed,
                                  "per_sig": 2, "max_tests": 60, "fixes": fixes,
                                  "long": CONTRACT_LONG if long else 0}, timeout=70)
        if long:
            self.flags["long_done"] = True
        else:
            done.append(seed)
        for t in (res.get("stats") or {}).get("targets") or []:
            if t not in self.contract_info["targets"]:
                self.contract_info["targets"].append(t)
        src = res.get("source") or ""
        targets = (res.get("stats") or {}).get("targets") or []
        head = (f"Fuzzer pass {'long' if long else seed}: drove {len(targets)} targets "
                f"({', '.join(sorted({t.split(':')[0] for t in targets})) or 'none'}) against reference models.")
        if not src or static_problem(src):
            self.save()
            return head + " It produced no failing program." + ("\n" + self.queue_line())
        rel = f"_cand/test_contracts_{'long' if long else seed}.py"
        (self.ws / rel).write_text(src)
        tests, coll, _ = self.runner.results([rel], repeats=2)
        failing = [t for t in tests if t.outcome == "failed"]
        cands = [t for t in failing if triage_auto(t, rel, self.pkg) == "judge"]
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
        kinds = Counter(str(lbl[0]) for lbl in labels)
        report = [head, f"Wrote {rel}: {len(tests)} shrunk programs, {len(failing)} fail on this code "
                        f"(by target: {', '.join(f'{k} x{v}' for k, v in kinds.most_common(8))}). "
                        f"{len(picked)} queued for the repair check."]
        self.enqueue(rel, picked, "contract")
        self.drain(report)
        report += [self.totals(), self.queue_line()]
        if not long and len(done) < len(CONTRACT_SEEDS):
            report.append(f"Run `fuzz` again now ({len(CONTRACT_SEEDS) - len(done)} passes left); each builds on "
                          "the repairs confirmed so far.")
        elif long:
            report.append("Fuzzing is complete. Now write your own tests. " + self.suggest())
        self.save()
        return "\n".join(report)

    def _fuzz_request(self, req_data: dict, timeout: int) -> dict:
        tag = f"{int(time.time() * 1000) % 10 ** 8}"
        req, out = self.ws / f".tagent_creq_{tag}.json", self.ws / f".tagent_cres_{tag}.json"
        req.write_text(json.dumps(req_data))
        cmd = (f"PYTHONPATH={self.student} timeout {timeout - 5} python -m tagent.contracts "
               f"{req.name} {out.name} > /dev/null 2>&1")
        self.runner.env.execute({"command": cmd}, timeout=timeout)
        res = {}
        if out.exists():
            try:
                res = json.loads(out.read_text())
            except json.JSONDecodeError:
                res = {}
        req.unlink(missing_ok=True)
        out.unlink(missing_ok=True)
        return res

    def ensure_guards(self):
        """Short reference-checked sequences that pass on the current code, used only as repair guards."""
        if self.flags.get("guards_done") or not CONTRACT_GUARDS:
            return
        self.flags["guards_done"] = True
        res = self._fuzz_request({"package": self.pkg, "deadline": 15, "seed": 77, "fixes": list(self.fixes),
                                  "long": CONTRACT_GUARDS, "span": 2}, timeout=25)
        src = res.get("source") or ""
        if not src or static_problem(src):
            return
        rel = "_cand/test_contracts_guard.py"
        (self.ws / rel).write_text(src)
        tests, _, _ = self.runner.results([rel], repeats=1)
        self.guards = [f"{rel}::{t.nodeid.split('::', 1)[1]}" for t in tests if "::" in t.nodeid] + self.guards

    def op_run_tests(self, rel: str) -> str:
        path = self.ws / rel
        if not path.exists():
            return f"{rel} does not exist. Write it first (cat > {rel} <<'EOF' ... EOF)."
        rel = str(path.resolve().relative_to(self.ws))
        if not rel.startswith("_cand/") or not Path(rel).name.startswith("test_"):
            return ("Put your test files in _cand/ and name them test_<unit id>_<n>.py, e.g. "
                    "_cand/test_u03_cache_lru_1.py. The tools copy confirmed tests into tests_new/ at the end.")
        if self.gate():
            return self.gate_message()
        if "file body omitted" in path.read_text():
            return ("The file holds a placeholder, not code. Write the complete test code in the heredoc, "
                    f"then run `run_tests {rel}`.")
        u = self.unit_for_file(rel)
        if u is not None and self.unit_stats.get(u.uid, {}).get("dry", 0) >= DRY_LIMIT:
            return (f"{u.uid} has had {DRY_LIMIT} rounds in a row without a new confirmed bug, so it is closed. "
                    "Test another unit. " + self.suggest())
        self.ensure_guards()
        body = path.read_text()
        if u is not None:
            body = self._with_imports(u, body)
        problem = static_problem(body)
        report = []
        if problem:
            saved = salvage(body)
            if saved and not static_problem(saved) and re.search(r"^\s*def test_|^def prop_", saved, re.M):
                report.append(f"Some parts of the file had problems and were dropped ({problem.splitlines()[0][:200]}).")
                body = self._with_imports(u, saved) if u else saved
            else:
                return f"The file cannot be used: {problem[:1500]}\nFix it and run `run_tests {rel}` again."
        path.write_text(body)
        if re.search(r"^def prop_\w+\(\s*r\s*\)", body, re.M):
            body, notes = self.run_props(rel, body, deadline=30)
            path.write_text(body)
            report += ["Properties (each run on random inputs; failing inputs became test_<prop>_seed<N> tests):"]
            report += ["  " + n.lstrip("- ") for n in notes]
        tests, coll, out = self.runner.results([rel], repeats=2, coverage=self.arcs)
        if coll:
            return ("\n".join(report) + "\nThe file does not import or collect:\n" + "\n".join(coll)[-2000:] +
                    f"\nFix it and run `run_tests {rel}` again.")
        failing = [t for t in tests if t.outcome == "failed"]

        def private(func):
            m = re.match(r"test_(prop_\w+)_seed\d+$", func)
            return reads_private(body, func) or bool(m and reads_private(body, m.group(1)))

        cands, crashed, privs, flaky = [], [], [], []
        for t in failing:
            verdict = triage_auto(t, rel, self.pkg)
            if verdict != "judge":
                (flaky if t.flaky else crashed).append(t)
            elif private(t.func):
                privs.append(t)
            else:
                cands.append(t)
        passed = [t for t in tests if t.outcome == "passed"]
        self._add_guards(rel, tests)
        uid = u.uid if u else ""
        if uid:
            st = self.unit_stats.setdefault(uid, {"rounds": 0, "kept": 0})
            st["rounds"] = st.get("rounds", 0) + 1
            st["dry"] = st.get("dry", 0) + 1
            notes = self.history.setdefault(uid, [])
            notes += [f"- {t.func}: passed (behaviour already correct)" for t in passed]
        report.append(f"Ran {rel}: {len(tests)} tests, {len(passed)} pass (no bug shown there), "
                      f"{len(failing)} fail.")
        for t in crashed[:8]:
            msg = ((t.crash or [None, None, ""])[2] or t.text[-200:] or "").strip().splitlines()
            report.append(f"  CRASHED {t.func}: {msg[0][:180] if msg else '?'}  -> the test uses the API wrongly; "
                          "fix or delete it")
        for t in flaky[:4]:
            report.append(f"  FLAKY {t.func}: not deterministic, it will be dropped")
        for t in privs[:4]:
            report.append(f"  IGNORED {t.func}: reads private attributes; test only the public API")
        for t in cands:
            msg = (t.text or "").strip().splitlines()
            report.append(f"  FAILS {t.func}: {msg[-1][:160] if msg else ''}")
        if cands:
            report.append(f"{len(cands)} failing tests queued for the repair check (a test is kept only if one "
                          "small change to the library code makes it pass without breaking passing tests).")
        self.enqueue(rel, cands, "model", uid)
        self.drain(report)
        report += [self.totals(), self.queue_line(), self.suggest()]
        self.save()
        return "\n".join(report)

    def run_props(self, rel: str, body: str, deadline: int = 90):
        import shlex
        names = re.findall(r"^def (prop_\w+)\(\s*r\s*\)", body, re.M)
        tag = f"{int(time.time() * 1000) % 10 ** 8}"
        req, out = self.ws / f".tagent_preq_{tag}.json", self.ws / f".tagent_pres_{tag}.json"
        req.write_text(json.dumps({"file": rel, "package": self.pkg, "props": names, "seeds": PROP_SEEDS,
                                   "deadline": deadline}))
        cmd = (f"PYTHONPATH={shlex.quote(str(self.student))} timeout {deadline + 15} python -m tagent.props "
               f"{req.name} {out.name} > /dev/null 2>&1")
        self.runner.env.execute({"command": cmd}, timeout=deadline + 20)
        res = {}
        if out.exists():
            try:
                res = json.loads(out.read_text())
            except json.JSONDecodeError:
                res = {}
        req.unlink(missing_ok=True)
        out.unlink(missing_ok=True)
        return self._props_notes(body, res)

    def _props_notes(self, body: str, res: dict):
        notes, added = [], []
        for name, r in (res.get("props") or {}).items():
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
            for seed in fails[:2]:
                added.append(f"\n\ndef test_{name}_seed{seed}():\n    {name}(random.Random({seed}))\n")
        if added:
            if not re.search(r"^import random\b", body, re.M):
                body = "import random\n" + body
            body = body.rstrip() + "".join(added) + "\n"
        return body, notes

    def op_confirm(self) -> str:
        if not self.pending:
            return "Nothing is waiting for the repair check. " + self.totals()
        report = []
        self.drain(report)
        report += [self.totals(), self.queue_line()]
        self.save()
        return "\n".join(report)

    def op_survey(self) -> str:
        units = sorted([u for u in self.units_list() if u.weight >= 1], key=lambda u: -u.weight)
        lines = [f"Project {self.task.project} ({self.pkg}): {len(units)} units, highest risk first.",
                 "unit id | what | risk points | fuzzer | your rounds / confirmed tests | uncovered branches"]
        for u in units:
            names = {w for w in re.findall(r"[A-Za-z_]\w*", u.title) if len(w) > 2}
            fuzz = sorted({t.split(":")[0] for t in self.contract_info["targets"]
                           if any(re.search(rf"\b{re.escape(n)}\b", t) for n in names)})
            st = self.unit_stats.get(u.uid, {})
            gaps = len(self.uncovered(u)) if st.get("rounds") else len(u.risks)
            lines.append(f"{u.uid} | {u.title[:55]} | {len(u.risks)} | {','.join(fuzz) or 'not driven'} | "
                         f"{st.get('rounds', 0)} / {st.get('kept', 0)} | {gaps}")
        lines += ["", self.totals(), self.queue_line(),
                  "Use `unit <unit id>` to see a unit's docs, code, risk points and what is still untested."]
        return "\n".join(lines)

    def op_unit(self, uid: str, part: str = "all") -> str:
        if self.gate():
            return self.gate_message()
        u = self.unit(uid)
        if u is None:
            return f"Unknown unit {uid}. Run `survey` for the list."
        rounds = self.unit_stats.get(uid, {}).get("rounds", 0)
        out = [f"# {u.uid}: {u.title}", f"Import: {u.import_hint}",
               f"Write tests to _cand/test_{u.uid}_{rounds + 1}.py, then run `run_tests _cand/test_{u.uid}_{rounds + 1}.py`."]
        if part not in ("all", "docs", "code"):
            return "\n".join(out[:2] + [self._method_view(u, part)])
        if part in ("all", "docs"):
            out += ["", "## Documentation (correct behaviour)", inventory.docs_for(u, self.sections, 3500) or "(none found)"]
        if part in ("all", "code"):
            out += ["", "## Signatures", (u.outline or "(module-level functions)")[:2500],
                    "", "## Code (may contain bugs; never derive expected values from it)", u.source[:6000]]
            if len(u.source) > 6000:
                out.append(f"... (code cut here; `unit {u.uid} <method>` shows one method in full: "
                           f"{', '.join(u.members[:40])})")
        out += ["", "## Risk points (where an injected bug would sit)", "\n".join(u.risks[:25]) or "(none)"]
        gaps = self.uncovered(u) if rounds else []
        if gaps:
            out += ["", "## Branches no test has exercised yet", "\n".join(f"- {g}" for g in gaps)]
        notes = self.history.get(uid) or []
        if notes:
            out += ["", "## Your earlier tests for this unit (do not repeat them)", "\n".join(notes[-25:])]
        if rounds == 0 and part == "all":
            ex = inventory.existing_tests_for(self.ws, u)
            if ex:
                out += ["", "## How the existing tests use it (style only)", ex[:1500]]
        return "\n".join(out)

    def _method_view(self, u, name: str) -> str:
        """One method of a unit with its docstring, numbered lines and the risk points inside it."""
        import ast
        try:
            text = (self.ws / u.path).read_text()
            tree = ast.parse(text)
        except (OSError, SyntaxError):
            return f"Cannot read {u.path}."
        lines = text.splitlines()
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and (u.cls is None or node.name == u.cls):
                found += [(node.name, c) for c in node.body
                          if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef)) and c.name == name]
        if not found:
            found = [("", n) for n in ast.walk(tree)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
        if not found:
            return (f"No method `{name}` in {u.uid}. Its members: {', '.join(u.members)}. "
                    "Parts: docs, code, all, or a member name.")
        cls, node = found[0]
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        body = "\n".join(f"{i:4d}| {lines[i - 1]}" for i in range(start, node.end_lineno + 1))
        pool = [r for x in self.units_list() if x.path == u.path for r in x.risks]
        risks = list(dict.fromkeys(r for r in pool if (m := re.match(r"L(\d+)", r))
                                   and start <= int(m.group(1)) <= node.end_lineno))
        return "\n".join([f"## {cls + '.' if cls else ''}{name} in {u.path} (may contain bugs; take expected "
                          "values from its docstring and the docs, never from the code)", body[:5000],
                          "", "## Risk points in it", "\n".join(risks) or "(none)"])

    def op_finalize(self) -> str:
        if self.pending:
            report = []
            self.drain(report)
        self.assemble()
        files = sorted((self.ws / "tests_new").glob("test_*.py"))
        n = sum(len(re.findall(r"^\s*def test_", p.read_text(), re.M)) for p in files)
        self.flags["finalized"] = True
        self.save()
        msg = f"tests_new/ now holds {n} confirmed tests in {len(files)} files."
        if self.pending:
            msg += f" {len(self.pending)} failing tests were never checked (run `confirm`, then `finalize` again)."
        return msg
