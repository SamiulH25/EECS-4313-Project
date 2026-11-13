"""Search-based input augmentation for probe.

Builds candidate inputs from the suite already saved: random inputs with the same
shapes, local edits (change, swap, insert, delete, reorder) and scaled or negated
copies for value-like parameters. Candidates must pass probe's constraint check and
run cleanly on solution.py, which also supplies every expected value. A greedy cover
then keeps candidates that
  * kill a local mutant of solution.py that no saved test kills (weight 3),
  * take a new branch transition or loop-count class in solution.py (weight 1),
  * add a new pair of input/output features such as "k == 1" with "mixed signs" (0.5),
then up to SCALE_CASES inputs at the stated limits (largest legal scalars, long lists
spanning the full value range) and the lowest and highest numeric answers from
scaled copies: modulo, overflow and sentinel bugs only show at scale.
"""

import copy
import random
import re
import sys
import time

from _input_constraints import _number
from _log import short
from _mutation import describe, generate_mutants

MAX_MUTANTS = 200
MIN_LENGTH = 3
EXTREME_CASES = 2
SCALE_CASES = 5
COVER_PASSES = 3
BIG_LENGTH = 1000  # long enough for counts past 10^9+7, short enough for 1 s and a readable file
BIG_CELLS = 1000  # grids at the limits: about 31 x 31
FUZZ_MAX_LENGTH = 8  # random inputs stay small: fast, readable, and enough for most decisions
FUZZ_SECONDS = 0.25
STALE_CANDIDATES = 1500  # stop searching after this many candidates in a row add nothing
VALUE_BOUND = 10**6  # element bounds at least this large describe values, not indices or sizes
WEIGHTS = {"kill": 3.0, "arc": 1.0, "pair": 0.5}
_ELEMENT_BOUND_RE = re.compile(r"(-?[\d^* ]+?)\s*<=\s*([A-Za-z_]\w*)\[i\](?:\[j\])?\s*<=\s*([\d^* ]+)")
# Constraint lines that restrict order or structure: never reorder or rebuild a parameter they name.
_ORDER_RULE_RE = re.compile(
    r"sort|ascending|descending|increasing|decreasing|parent|tree|leading|generated such that", re.I
)
_STRUCTURE_RULE_RE = re.compile(r"parent|tree|edge|graph|connected|generated such that|permutation|prime", re.I)
_DISTINCT_RULE_RE = re.compile(r"distinct|unique", re.I)
_BOUND_RE = re.compile(  # low <(=) [alias ==] name[i][j](.length) <(=) high
    r"(-?\d[\d^* ]*?)\s*(<=|<)\s*(?:[A-Za-z_]\w*\s*==\s*)?([A-Za-z_]\w*)((?:\[[a-z0-9]\])*)(\.length)?"
    r"\s*(<=|<)\s*(-?\d[\d^* ]*)"
)


def _rule_names(spec: str, pattern) -> set:
    section = spec.split("Constraints", 1)[1] if "Constraints" in spec else spec
    names = set()
    for line in section.splitlines():
        if pattern.search(line):
            names.update(re.findall(r"[A-Za-z_]\w*", line))
    return names


def order_locked(spec: str) -> set:
    """Names appearing on an order-restricting line of the Constraints section."""
    return _rule_names(spec, _ORDER_RULE_RE)


def value_bounds(spec: str) -> dict:
    """{parameter: (low, high)} for list elements with large explicit bounds."""
    bounds = {}
    for low, name, high in _ELEMENT_BOUND_RE.findall(spec):
        low, high = _number(low.strip()), _number(high.strip())
        if low is not None and high is not None and max(abs(low), abs(high)) >= VALUE_BOUND:
            bounds[name] = (low, high)
    return bounds


def spec_bounds(spec: str):
    """Bounds from `low <= x <= high` lines: (values, lengths, row widths), each {name: (low, high)}."""
    section = spec.split("Constraints", 1)[1] if "Constraints" in spec else spec
    values, lengths, widths = {}, {}, {}
    for low, low_op, name, index, length, high_op, high in _BOUND_RE.findall(section):
        low, high = _number(low.strip()), _number(high.strip())
        if low is None or high is None:
            continue
        low, high = low + (low_op == "<"), high - (high_op == "<")
        target = (widths if index else lengths) if length else values
        target.setdefault(name, (low, high))
    return values, lengths, widths


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _map_ints(value, fn):
    if isinstance(value, list):
        return [_map_ints(item, fn) for item in value]
    return fn(value) if _is_int(value) else value


def _ints(value):
    if isinstance(value, list):
        for item in value:
            yield from _ints(item)
    elif _is_int(value):
        yield value


def _magnitude(value) -> int:
    """Size of an answer: |number|, or the largest |number| inside a list."""
    return max((abs(x) for x in _ints(value)), default=0)


def rescalings(args: dict, bounds: dict) -> list:
    out = []
    for name, (low, high) in bounds.items():
        value = args.get(name)
        peak = _magnitude(value)
        if not isinstance(value, list) or peak == 0:
            continue
        clamp = lambda x: max(low, min(high, x))
        factor = max(high, -low) // peak
        if factor > 1:
            out.append({**args, name: _map_ints(value, lambda x: clamp(x * factor))})
        if low < 0:
            out.append({**args, name: _map_ints(value, lambda x: clamp(-x))})
    return out


def reorderings(args: dict, locked: set, rng: random.Random) -> list:
    out = []
    for name, value in args.items():
        if name in locked or not isinstance(value, (list, str)) or len(value) < MIN_LENGTH:
            continue
        mixed = list(value)
        rng.shuffle(mixed)
        if isinstance(value, str):
            mixed = "".join(mixed)
        for new in (value[::-1], value[1:] + value[:1], mixed):
            if new != value:
                out.append({**args, name: new})
    return out


class Shape:
    """What one parameter looks like across the saved inputs."""

    def __init__(self, values, bound=None, length_bound=None, distinct=False, width_bound=None):
        self.values, self.bound, self.length_bound, self.distinct = values, bound, length_bound, distinct
        self.width_bound = width_bound
        ints = [x for v in values for x in _ints(v)]
        self.low, self.high = (min(ints), max(ints)) if ints else (0, 1)
        self.kind = self._kind(values)
        lengths = [len(v) for v in values if isinstance(v, (list, str))]
        self.min_len, self.max_len = (min(lengths), max(lengths)) if lengths else (0, 0)
        self.alphabet = sorted({c for v in values if isinstance(v, str) for c in v}
                               | {c for v in values if isinstance(v, list) for s in v if isinstance(s, str) for c in s})
        widths = {len(row) for v in values if isinstance(v, list) for row in v if isinstance(row, list)}
        self.width = widths.pop() if len(widths) == 1 else None
        self.max_width = max((len(row) for v in values if isinstance(v, list) for row in v if isinstance(row, list)),
                             default=1)

    @staticmethod
    def _kind(values):
        if all(isinstance(v, bool) for v in values):
            return "bool"
        if all(_is_int(v) for v in values):
            return "int"
        if all(isinstance(v, str) for v in values):
            return "str"
        if all(isinstance(v, list) and all(_is_int(x) for x in v) for v in values):
            return "ints"
        if all(isinstance(v, list) and all(isinstance(x, str) for x in v) for v in values):
            return "strs"
        if all(isinstance(v, list) and v and all(isinstance(r, list) and all(_is_int(x) for x in r) for r in v)
               for v in values):
            return "rows" if any(len(v) > 1 for v in values) else "seed"
        return "seed"

    def number(self, rng):
        low, high = self.low, self.high
        if rng.random() < 0.3:
            return rng.choice([x for x in (low, high, 0, 1, -1, (low + high) // 2) if low <= x <= high] or [low])
        small_low, small_high = max(low, -10), min(high, 10)
        if small_low <= small_high and rng.random() < 0.6:
            return rng.randint(small_low, small_high)
        return rng.randint(low, high)

    def big(self, rng, mode="random"):
        """A value at the stated limits ("random", or uniform "low"/"high"), or None when unknown."""
        top = self.length_bound[1] if self.length_bound else max(self.max_len, 1)
        size = max(self.min_len, min(top, BIG_LENGTH))
        if self.kind == "str":  # strings need only a length bound
            if not self.alphabet or not self.length_bound:
                return None
            letters = {"low": self.alphabet[:1], "high": self.alphabet[-1:]}.get(mode, self.alphabet)
            return "".join(rng.choice(letters) for _ in range(size))
        if not self.bound:
            return None
        low, high = self.bound
        pick = {"low": lambda: low, "high": lambda: high}.get(mode, lambda: rng.randint(low, high))
        if self.kind == "int":
            return {"low": low}.get(mode, high)
        if self.kind == "rows" and not self.width:  # a grid: as square as BIG_CELLS allows
            rows = min(top, max(1, int(BIG_CELLS ** 0.5)))
            cols = min(self.width_bound[1] if self.width_bound else self.max_width, max(1, BIG_CELLS // rows))
            return [[pick() for _ in range(cols)] for _ in range(rows)]
        if self.kind != "ints":
            return None
        if self.distinct:
            return rng.sample(range(low, high + 1), size) if high - low + 1 >= size and mode == "random" else None
        out = [pick() for _ in range(size)]
        if mode == "random":
            out[:2] = [high, low][:size]  # both limits appear at least once
            rng.shuffle(out)
        return out

    def length(self, rng):
        return rng.randint(self.min_len, max(self.min_len, min(self.max_len, FUZZ_MAX_LENGTH)))

    def random(self, rng):
        kind = self.kind
        if kind == "bool":
            return rng.random() < 0.5
        if kind == "int":
            return self.number(rng)
        if kind == "str" and self.alphabet:
            letters = self.alphabet[:rng.randint(1, len(self.alphabet))]
            return "".join(rng.choice(letters) for _ in range(self.length(rng)))
        if kind == "ints":
            values = [self.number(rng) for _ in range(self.length(rng))]
            return list(dict.fromkeys(values)) if self.distinct else values
        if kind == "strs" and self.alphabet:
            return ["".join(rng.choice(self.alphabet) for _ in range(rng.randint(1, 4)))
                    for _ in range(self.length(rng))]
        if kind == "rows":
            if self.width:  # fixed-width records such as [start, end] or [x1, y1, x2, y2]
                cols = self.width
            else:  # a grid: one random width for every row
                cols = rng.randint(1, min(self.max_width, FUZZ_MAX_LENGTH))
            return [[self.number(rng) for _ in range(cols)] for _ in range(max(1, self.length(rng)))]
        return copy.deepcopy(rng.choice(self.values))

    def edit(self, value, rng):
        """One local change: element value, swap, insert, delete or duplicate."""
        if _is_int(value):
            return self.number(rng) if rng.random() < 0.5 else value + rng.choice([-1, 1])
        if isinstance(value, str):
            chars = list(value)
            if chars and self.alphabet:
                chars[rng.randrange(len(chars))] = rng.choice(self.alphabet)
            return "".join(chars)
        if not isinstance(value, list) or not value:
            return value
        new = copy.deepcopy(value)
        i, j = rng.randrange(len(new)), rng.randrange(len(new))
        move = rng.choice(["set", "swap", "insert", "delete", "dup"])
        if move == "swap":
            new[i], new[j] = new[j], new[i]
        elif move == "delete" and len(new) > max(1, self.min_len):
            del new[i]
        elif move == "dup":
            new.insert(j, copy.deepcopy(new[i]))
        elif move == "insert" and self.kind == "ints":
            new.insert(i, self.number(rng))
        elif isinstance(new[i], list) and new[i]:
            k = rng.randrange(len(new[i]))
            if _is_int(new[i][k]):
                new[i][k] = self.number(rng)
        elif _is_int(new[i]):
            new[i] = self.number(rng)
        return new


def features(args: dict, value) -> set:
    """Coarse input/output classes; pairs of them are diversity goals."""
    out = set()
    for name, v in args.items():
        if _is_int(v):
            out.add(f"{name}:" + ("neg" if v < 0 else "0" if v == 0 else "1" if v == 1 else "pos"))
        elif isinstance(v, (list, str)):
            out.add(f"{name}:len" + ("0" if not v else "1" if len(v) == 1 else "n"))
            flat = list(_ints(v)) if isinstance(v, list) else []
            if flat:
                out.add(f"{name}:" + ("allneg" if max(flat) < 0 else "mixed" if min(flat) < 0 else "nonneg"))
            if isinstance(v, list) and len(v) > 2 and all(_is_int(x) for x in v):
                out.add(f"{name}:" + ("asc" if v == sorted(v) else "desc" if v == sorted(v, reverse=True) else "unsorted"))
            if len(v) != len(set(map(repr, v))):
                out.add(f"{name}:dups")
    if isinstance(value, bool):
        out.add(f"out:{value}")
    elif _is_int(value):
        out.add("out:" + ("neg" if value < 0 else "0" if value == 0 else "1" if value == 1 else "pos"))
    elif isinstance(value, (list, str)):
        out.add("out:len" + ("0" if not value else "1" if len(value) == 1 else "n"))
    return {(a, b) for a in out for b in out if a < b and a.split(":")[0] != b.split(":")[0]}


def traced(call, filename):
    """Run `call`; return (value, branch transitions and loop-count classes inside `filename`)."""
    arcs, hits = set(), {}

    def tracer(frame, event, arg):
        if frame.f_code.co_filename != filename:
            return None
        last = [frame.f_code.co_firstlineno]

        def local(frame, event, arg):
            if event == "line":
                arcs.add((last[0], frame.f_lineno))
                hits[frame.f_lineno] = hits.get(frame.f_lineno, 0) + 1
                last[0] = frame.f_lineno
            return local
        return local

    previous = sys.gettrace()
    sys.settrace(tracer)
    try:
        value = call()
    finally:
        sys.settrace(previous)
    return value, arcs | {("hits", line, min(count, 3)) for line, count in hits.items()}


def augment(source, func, reference, suite, spec, admissible, run, slots, deadline, filename="solution.py",
            log=lambda text: None):
    """Up to `slots` (args, expected) pairs chosen by the greedy cover described above.

    `admissible(args)` returns False for duplicates or recognizable constraint violations;
    `run(call, seconds)` runs one call under a time limit and raises on timeout;
    `log(text)` receives a detailed trace of every phase.
    """
    started = time.monotonic()
    log(f"augment: {len(suite)} saved inputs, {slots} free slots, {deadline - started:.1f}s left")
    if slots <= 0 or not suite:
        log("augment: nothing to do")
        return []

    def outcome(cls, args, seconds=FUZZ_SECONDS):
        try:
            return "ok", run(lambda: getattr(cls(), func)(**copy.deepcopy(args)), seconds)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as error:  # includes probe's timeout: a hang or crash differs
            return "error", type(error).__name__

    def reference_goals(args, seconds=FUZZ_SECONDS):
        try:
            value, arcs = run(lambda: traced(lambda: getattr(reference(), func)(**copy.deepcopy(args)), filename),
                              seconds)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
            return None
        return value, arcs, features(args, value)

    mutants, labels = [], []
    generated_mutants = generate_mutants(source, limit=MAX_MUTANTS)
    for mutant in generated_mutants:
        namespace = {}
        try:
            exec(compile(mutant.source, "<mutant>", "exec"), namespace)
            mutants.append(namespace["Solution"])
            labels.append(describe(source, mutant))
        except Exception:
            continue
    operators = {}
    for mutant in generated_mutants:
        operators[mutant.operator] = operators.get(mutant.operator, 0) + 1
    log(f"mutants: {len(generated_mutants)} generated, {len(mutants)} loaded; by operator {operators}")

    suite = sorted(suite, key=lambda args: len(repr(args)))  # cheap inputs settle most mutants
    covered_arcs, covered_pairs, expected = set(), set(), []
    for args in suite:
        goals = reference_goals(args, 1.0)
        if goals:
            expected.append((args, ("ok", goals[0])))
            covered_arcs |= goals[1]
            covered_pairs |= goals[2]
    log(f"suite: {len(expected)}/{len(suite)} inputs traced; {len(covered_arcs)} transitions/loop classes, "
        f"{len(covered_pairs)} feature pairs covered")
    survivors = []
    for index, cls in enumerate(mutants):
        if time.monotonic() > deadline:
            log("deadline reached while scoring mutants; no augmentation")
            return []
        if all(outcome(cls, args) == result for args, result in expected):
            survivors.append(index)
    log(f"survivors: {len(survivors)}/{len(mutants)} mutants survive the saved suite")
    for index in survivors:
        log(f"  survivor m{index} {labels[index]}")

    rng = random.Random(0)
    names = list(suite[0])
    stated, lengths, widths = spec_bounds(spec)
    distinct = _rule_names(spec, _DISTINCT_RULE_RE)
    shapes = {name: Shape([args[name] for args in suite if name in args], stated.get(name), lengths.get(name),
                          name in distinct, widths.get(name)) for name in names}
    locked, bounds = order_locked(spec), value_bounds(spec)
    frozen = _rule_names(spec, _STRUCTURE_RULE_RE)  # rebuilt structures would rarely stay legal
    log(f"bounds: values {stated} lengths {lengths} widths {widths}")
    for name, shape in shapes.items():
        log(f"shape {name}: kind={shape.kind} observed=[{shape.low}, {shape.high}] len=[{shape.min_len}, "
            f"{shape.max_len}] width={shape.width} frozen={name in frozen} order_locked={name in locked} "
            f"distinct={shape.distinct}")

    scale = []  # inputs at the stated limits; kills are not computed (slow mutants would look killed)
    for args in sorted(suite, key=lambda a: -len(repr(a)))[:4]:
        for name, mode in [(n, m) for n in [n for n in names if n not in frozen] + [None]
                           for m in ("random", "low", "high")]:
            if time.monotonic() > deadline:
                break
            new = dict(args)
            for target in ([name] if name else [n for n in names if n not in frozen]):
                value = shapes[target].big(rng, mode)
                if value is not None:
                    new[target] = value
            if new == args:
                continue
            tag = f"scale {name or 'all'}/{mode}"
            if not admissible(new):
                log(f"{tag}: rejected by constraints or duplicate")
                continue
            result = outcome(reference, new, 1.0)  # untraced: tracing would slow big inputs tenfold
            if result[0] != "ok":
                log(f"{tag}: reference {result[1]}")
            elif all(repr(e[0]) != repr(new) for e in scale):
                scale.append((new, result[1]))
                log(f"{tag}: ok, answer {short(result[1], 60)}, {len(repr(new))} chars")
    rescaled = [r for args in suite for r in rescalings(args, bounds)]

    def generated():
        for args in suite:
            yield from reorderings(args, locked, rng)
        yield from rescaled
        while True:
            base = copy.deepcopy(rng.choice(suite))
            if rng.random() < 0.5:
                for _ in range(rng.randint(1, 3)):
                    name = rng.choice(names)
                    if name not in frozen:
                        base[name] = shapes[name].edit(base[name], rng)
            else:
                for name in names:
                    if name not in frozen and shapes[name].kind != "seed":
                        base[name] = shapes[name].random(rng)
            yield base

    candidates, extremes, seen, stale = [], [], set(), 0
    stats = {"generated": 0, "duplicate": 0, "inadmissible": 0, "reference_error": 0, "useless": 0, "useful": 0}
    any_kill = set()
    rescaled_keys = {repr(r) for r in rescaled}
    stop = "generator exhausted"
    search_started = time.monotonic()
    for new in generated():
        if time.monotonic() > deadline:
            stop = "deadline"
            break
        if stale > STALE_CANDIDATES:
            stop = f"{STALE_CANDIDATES} candidates in a row added nothing"
            break
        stale += 1
        stats["generated"] += 1
        key = repr(new)
        if key in seen:
            stats["duplicate"] += 1
            continue
        seen.add(key)
        if not admissible(new):
            stats["inadmissible"] += 1
            continue
        goals = reference_goals(new)
        if goals is None:
            stats["reference_error"] += 1
            continue
        value, arcs, pairs = goals
        kills = frozenset(i for i in survivors if outcome(mutants[i], new) != ("ok", value))
        entry = (new, value, kills, frozenset(arcs - covered_arcs), frozenset(pairs - covered_pairs))
        if (kills - any_kill) or entry[3] or entry[4]:
            stale = 0
        any_kill |= kills
        if kills or entry[3] or entry[4]:
            candidates.append(entry)
            stats["useful"] += 1
        else:
            stats["useless"] += 1
        if key in rescaled_keys:
            extremes.append(entry)
    log(f"search: stopped ({stop}) after {time.monotonic() - search_started:.1f}s; {stats}")
    log(f"search: candidates kill {len(any_kill)}/{len(survivors)} survivors; "
        f"unkilled: {[f'm{i}' for i in survivors if i not in any_kill]}")

    chosen, reasons = [], []
    killed, arcs_done, pairs_done = set(), set(), set()

    def gain(entry):
        return (WEIGHTS["kill"] * len(entry[2] - killed) + WEIGHTS["arc"] * len(entry[3] - arcs_done)
                + WEIGHTS["pair"] * len(entry[4] - pairs_done))

    scale_picks = []
    for entry in sorted(scale, key=lambda e: -_magnitude(e[1])):  # largest answers first, no repeated answer
        if len(scale_picks) < SCALE_CASES and all(repr(entry[1]) != repr(p[1]) for p in scale_picks):
            scale_picks.append(entry)
    budget = slots - min(EXTREME_CASES, len(extremes)) - len(scale_picks)
    for cover_pass in range(COVER_PASSES):  # later passes cover every goal again with different inputs
        killed, arcs_done, pairs_done = set(), set(), set()
        while candidates and len(chosen) < budget:
            best = max(candidates, key=lambda e: (gain(e), -len(repr(e[0]))))
            if gain(best) <= 0:
                break
            reasons.append(f"pass {cover_pass + 1}: kills {sorted(f'm{i}' for i in best[2] - killed)} "
                           f"+{len(best[3] - arcs_done)} transitions +{len(best[4] - pairs_done)} pairs")
            chosen.append(best)
            killed |= best[2]
            arcs_done |= best[3]
            pairs_done |= best[4]
            candidates.remove(best)

    # Large magnitudes expose sentinel, overflow and missing-modulo bugs that no local mutant mimics.
    remaining = [e for e in extremes if e not in chosen]
    numeric = [e for e in remaining if _is_int(e[1])]
    if numeric:  # the lowest and the highest answer: sentinels fail on one side, overflow on the other
        picks = [min(numeric, key=lambda e: e[1]), max(numeric, key=lambda e: e[1])]
    else:
        picks = sorted(remaining, key=lambda e: -_magnitude(e[1]))
    picks = [e for i, e in enumerate(picks) if e not in picks[:i]]
    for entry in picks[:max(0, min(EXTREME_CASES, slots - len(chosen) - len(scale_picks)))]:
        chosen.append(entry)
        reasons.append(f"extreme answer {short(entry[1], 40)}")
    out = [(entry[0], entry[1]) for entry in chosen]
    for entry in scale_picks[:max(0, slots - len(chosen))]:
        out.append(entry)
        reasons.append(f"at stated limits, answer {short(entry[1], 40)}")
    final_kills = set().union(*(e[2] for e in chosen)) if chosen else set()
    log(f"chosen: {len(out)} of {slots} slots ({len(chosen) - min(len(picks), EXTREME_CASES)} cover, "
        f"{len(out) - len(chosen)} scale); kills {len(final_kills)}/{len(survivors)} survivors; "
        f"still surviving: {[labels[i] for i in survivors if i not in final_kills][:12]}")
    for (args, value), reason in zip(out, reasons):
        log(f"  + {reason} | {short(args, 140)} -> {short(value, 40)}")
    log(f"augment: done in {time.monotonic() - started:.1f}s")
    return out
