"""Contract fuzzer: deterministic test generation without the model.

Run inside the workspace:  python -m tagent.contracts <request.json> <result.json>

For every public class and function of the package it
  1. finds constructor calls that work (parameter names and defaults suggest values),
  2. probes what protocol the object follows (mapping, set, sequence, memoizing decorator, key function)
     and whether it keeps its contents sorted (detected from behaviour, never assumed),
  3. runs random operation sequences and checks them against
       - a reference model built from Python's own types (dict, list, set, bisect, functools.lru_cache),
         when the object behaves like one on a probe (differential testing), and
       - observer-consistency contracts that hold for any correct implementation of the protocol
         (`k in m` agrees with `m[k]`, `len` agrees with iteration, `del` raises iff the key is absent ...),
  4. shrinks every failing sequence with delta debugging (ddmin) and writes it as a plain pytest test.

The tests only use the public API. Whether a failure is a bug is decided later by the repair check.
"""

from __future__ import annotations

import bisect
import functools
import importlib
import inspect
import itertools
import json
import pkgutil
import random
import signal
import sys
import time
import types
from collections import abc

# ---------------------------------------------------------------------------------------------------
# Helpers that are copied verbatim to the top of the generated test file (and exec'd while fuzzing).
HELPERS = r'''
import bisect
import functools
import itertools


class _Clock:
    def __init__(self):
        self.t = 0

    def __call__(self):
        return self.t


def _neg(x):
    return -x

def _half(x):
    return x // 2


def _far(*args):
    nums = [a for a in args if isinstance(a, (int, float)) and not isinstance(a, bool)]
    return (nums[-1] if nums else 0) + 10


_MISS = object()


def _outcome(f):
    try:
        return ("ok", f())
    except Exception as e:  # noqa: BLE001
        return ("raises", type(e).__name__)


def _same(f, g, what):
    a, b = _outcome(f), _outcome(g)
    assert a == b, f"{what}: got {a!r}, reference {b!r}"


def _same_effect(f, g, what):
    a, b = _outcome(f), _outcome(g)
    assert a[0] == b[0] and (a[0] == "ok" or a == b), f"{what}: got {a!r}, reference {b!r}"


def _sorted(xs, key):
    return sorted(xs, key=key)


def _ins(ref, key, *xs):
    ref.extend(xs)
    ref.sort(key=key)


def _disc(ref, x):
    if x in ref:
        ref.remove(x)


def _rem(ref, x):
    ref.remove(x)


def _bis(ref, key, x, side):
    keys = [key(v) if key else v for v in ref]
    k = key(x) if key else x
    return bisect.bisect_left(keys, k) if side == "left" else bisect.bisect_right(keys, k)


def _check_map(m, ref, key, ordered, probe):
    assert len(m) == len(ref), f"len {len(m)} != {len(ref)}"
    ks = list(m)
    if ordered:
        assert ks == sorted(ref, key=key), f"iteration order {ks}"
    else:
        assert sorted(ks) == sorted(ref), f"iteration {ks}"
    assert sorted(m.keys()) == sorted(ref.keys()), "keys()"
    assert sorted(m.values()) == sorted(ref.values()), "values()"
    assert sorted(m.items()) == sorted(ref.items()), "items()"
    for k in probe:
        assert (k in m) == (k in ref), f"{k} in m"
        assert m.get(k, "?") == ref.get(k, "?"), f"get({k})"


def _cons_map(m, probe):
    # per-key observers first: aggregate ones (len, currsize) may clean up stale entries
    for k in probe:
        a = k in m
        r = _outcome(lambda: m[k])
        assert (r[0] == "ok") == a, f"{k} in m is {a} but m[{k}] gives {r}"
        b = m.get(k, _MISS) is not _MISS
        assert a == b, f"{k} in m is {a} but get() says {b}"
    ks = list(m)
    assert len(ks) == len(set(ks)), f"duplicate keys {ks}"
    for k in ks:
        assert k in m, f"iterated key {k} not in m"
    assert len(m) == len(ks), f"len {len(m)} but iteration gives {ks}"
    sz, mx = getattr(m, "currsize", None), getattr(m, "maxsize", None)
    if isinstance(sz, (int, float)) and isinstance(mx, (int, float)):
        assert sz <= mx, f"currsize {sz} > maxsize {mx}"


def _c_set(m, k, v, probe):
    r = _outcome(lambda: m.__setitem__(k, v))
    if r[0] == "ok":
        assert k in m, f"{k} missing right after m[{k}] = {v}"
        assert m[k] == v, f"m[{k}] is {m[k]} right after m[{k}] = {v}"
    _cons_map(m, probe)


def _c_get(m, k, probe):
    a = k in m
    r = _outcome(lambda: m[k])
    assert (r[0] == "ok") == a, f"{k} in m was {a} but m[{k}] gives {r}"
    _cons_map(m, probe)


def _c_del(m, k, probe):
    a = k in m
    r = _outcome(lambda: m.__delitem__(k))
    assert (r[0] == "ok") == a, f"{k} in m was {a} but del m[{k}] gives {r}"
    assert k not in m, f"{k} still in m after del"
    _cons_map(m, probe)


def _c_pop(m, k, probe):
    before = m.get(k, _MISS)
    r = m.pop(k, "D")
    assert r == (before if before is not _MISS else "D"), f"pop({k}) gave {r}, get() before gave {before}"
    assert k not in m, f"{k} still in m after pop"
    _cons_map(m, probe)


def _c_popitem(m, probe):
    n = len(m)
    r = _outcome(m.popitem)
    if r[0] == "ok":
        k, v = r[1]
        assert k not in m, f"popped key {k} still in m"
        assert len(m) == n - 1, f"len {len(m)} after popitem from {n}"
    else:
        assert n == 0, f"popitem raised {r} with {n} items"
    _cons_map(m, probe)


def _c_setdefault(m, k, v, probe):
    before = m.get(k, _MISS)
    r = _outcome(lambda: m.setdefault(k, v))
    if r[0] == "ok":
        assert r[1] == (before if before is not _MISS else v), f"setdefault({k}) gave {r[1]}, before {before}"
    _cons_map(m, probe)


def _c_clear(m, probe):
    m.clear()
    assert len(m) == 0 and list(m) == [], "not empty after clear()"
    _cons_map(m, probe)


def _check_seq(s, ref, key, probe):
    if key is _half:
        # equal keys: the order among equal-key values is unspecified, so compare keys and contents only
        assert [key(v) for v in s] == sorted(key(v) for v in ref), f"key order {list(s)}"
        assert sorted(s) == sorted(ref), f"contents {list(s)} != {ref}"
        assert len(s) == len(ref), f"len {len(s)} != {len(ref)}"
        for x in probe:
            assert (x in s) == (x in ref), f"{x} in s"
            if hasattr(s, "count"):
                assert s.count(x) == ref.count(x), f"count({x})"
            if hasattr(s, "index"):
                o = _outcome(lambda: s.index(x))
                assert o[0] == ("ok" if x in ref else "raises"), f"index({x}) gives {o}"
                if o[0] == "ok":
                    assert s[o[1]] == x, f"index({x}) = {o[1]} but s[{o[1]}] = {s[o[1]]}"
        return
    assert list(s) == ref, f"contents {list(s)} != {ref}"
    assert len(s) == len(ref), f"len {len(s)} != {len(ref)}"
    n = len(ref)
    for i in range(-n, n):
        assert s[i] == ref[i], f"s[{i}]"
    for a, b, c in ((None, None, -1), (1, -1, None), (0, n, 2), (-2, None, None), (n // 2, None, -1)):
        assert list(s[a:b:c]) == ref[a:b:c], f"s[{a}:{b}:{c}]"
    assert list(reversed(s)) == ref[::-1], "reversed"
    for x in probe:
        assert (x in s) == (x in ref), f"{x} in s"
        if hasattr(s, "count"):
            assert s.count(x) == ref.count(x), f"count({x})"
        if hasattr(s, "index"):
            assert _outcome(lambda: s.index(x)) == _outcome(lambda: ref.index(x)), f"index({x})"
        for side in ("left", "right"):
            f = getattr(s, "bisect_" + side, None)
            if f is not None:
                assert f(x) == _bis(ref, key, x, side), f"bisect_{side}({x})"


def _check_set(s, ref, key, ordered, probe):
    if ordered:
        assert list(s) == sorted(ref, key=key), f"contents {list(s)}"
    else:
        assert sorted(s) == sorted(ref), f"contents {sorted(s)}"
    assert len(s) == len(ref), f"len {len(s)} != {len(ref)}"
    for x in probe:
        assert (x in s) == (x in ref), f"{x} in s"


def _memo_info(g):
    i = g.cache_info()
    return (i.hits, i.misses, i.currsize)
'''

_H = {}
exec(compile(HELPERS, "<contracts-helpers>", "exec"), _H)

PROBE = list(range(8))
_INIT = [5, 1, 4, 1, 3]

# ---------------------------------------------------------------------------------------------------
# discovery


def _public_path(pkg: str, top, mod, name: str, obj) -> str | None:
    if getattr(top, name, None) is obj:
        return f"{pkg}.{name}"
    if any(part.startswith("_") for part in mod.__name__.split(".")):
        return None
    return f"{mod.__name__}.{name}"


def discover(pkg: str) -> list[tuple[str, object]]:
    top = importlib.import_module(pkg)
    mods = [top]
    for info in pkgutil.walk_packages(getattr(top, "__path__", []), pkg + "."):
        try:
            mods.append(importlib.import_module(info.name))
        except Exception:  # noqa: BLE001
            pass
    seen, out = set(), []
    for mod in mods:
        for name, obj in sorted(vars(mod).items()):
            if name.startswith("_") or id(obj) in seen:
                continue
            if not (inspect.isclass(obj) or inspect.isfunction(obj)):
                continue
            if not str(getattr(obj, "__module__", "")).startswith(pkg):
                continue
            path = _public_path(pkg, top, mod, name, obj)
            if path:
                seen.add(id(obj))
                out.append((path, obj))
    return out


# ---------------------------------------------------------------------------------------------------
# construction


def _param_options(p: inspect.Parameter, big: bool) -> list[str | None]:
    """Code strings to pass for a parameter (None means: leave it out)."""
    n = p.name.lower()
    has_default = p.default is not inspect.Parameter.empty
    d = p.default if has_default else None
    if "timer" in n or "clock" in n:
        return ["clock"]
    if "ttl" in n or "timeout" in n or "expire" in n:
        return ["10"]
    if n == "key":
        return [None, "_neg", "_half"] if has_default else ["_neg", "_half"]
    if isinstance(d, bool) or n in ("typed",):
        return [None]
    if (isinstance(d, int) and not isinstance(d, bool)) or (
            not has_default and any(s in n for s in ("size", "max", "cap", "limit", "len"))):
        return ["100" if big else "3"]
    if n in ("iterable", "items", "data", "seq", "values", "initial", "iter", "it", "elements"):
        return [None, repr(_INIT)] if has_default else ["[]", repr(_INIT)]
    if has_default:
        return [None]
    return ["_far", "100" if big else "3", "[]", "{}"]


def ctor_variants(path: str, cls, big: bool) -> list[str]:
    try:
        sig = inspect.signature(cls)
    except (TypeError, ValueError):
        return [f"{path}()"]
    pos_only, named, var_pos = [], [], False
    for p in sig.parameters.values():
        if p.kind is p.VAR_POSITIONAL:
            var_pos = True
        elif p.kind is p.VAR_KEYWORD:
            continue
        elif p.kind is p.POSITIONAL_ONLY:
            pos_only.append(p)
        else:
            named.append(p)
    opts = [_param_options(p, big) for p in pos_only + named]
    out = []
    for combo in itertools.islice(itertools.product(*opts), 24):
        args, kwargs, ok = [], [], True
        for p, v in zip(pos_only + named, combo):
            if v is None:
                if p.kind is p.POSITIONAL_ONLY:
                    ok = False
                continue
            if p.kind is p.POSITIONAL_ONLY:
                args.append(v)
            else:
                kwargs.append(f"{p.name}={v}")
        if ok:
            out.append(f"{path}({', '.join(args + kwargs)})")
    if var_pos:
        out += [f"{path}(_neg)"]
    seen, uniq = set(), []
    for c in out or [f"{path}()"]:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


class Timeout(Exception):
    pass


def _alarm(signum, frame):
    raise Timeout()


def _run(code: str, seconds: float = 2.0):
    """Exec test code; returns None on success or (exception type, message) on failure."""
    ns = dict(_H)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        exec(compile(code, "<contract>", "exec"), ns)
        return None
    except Timeout:
        return ("Timeout", "")
    except RecursionError:
        return ("RecursionError", "")
    except BaseException as e:  # noqa: BLE001
        return (type(e).__name__, str(e).split("\n")[0][:200])
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


def _eval(code: str, ns: dict):
    signal.setitimer(signal.ITIMER_REAL, 2.0)
    try:
        return eval(code, ns)  # noqa: S307 - our own generated expression
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


# ---------------------------------------------------------------------------------------------------
# targets


class Target:
    def __init__(self, path, kind, ctor, prelude, ops, key="None", ordered=False, extra=None):
        self.path, self.kind, self.ctor, self.prelude, self.ops = path, kind, ctor, prelude, ops
        self.key, self.ordered, self.extra = key, ordered, extra or {}

    def name(self):
        return self.path.split(".")[-1]


def _is_sorted_like(lst, vals):
    if lst == sorted(vals):
        return "None"
    if lst == sorted(vals, key=lambda x: -x):
        return "_neg"
    if lst != sorted(vals) and sorted(lst) == sorted(vals) and [x // 2 for x in lst] == sorted(x // 2 for x in vals):
        return "_half"
    return None


def classify(path: str, cls, modules: set) -> list[Target]:
    targets = []
    for big in (True, False):
        for ctor in ctor_variants(path, cls, big):
            ns = dict(_H)
            ns["clock"] = _H["_Clock"]()
            for m in modules:
                ns[m.split(".")[0]] = importlib.import_module(m.split(".")[0])
                importlib.import_module(m)
            try:
                obj = _eval(ctor, ns)
            except BaseException:  # noqa: BLE001
                continue
            t = _classify_obj(path, ctor, obj, ns, big)
            if t:
                targets.append(t)
    # keep a few distinct variants per class and kind
    out, seen = [], set()
    for t in targets:
        k = (t.kind, t.key, t.ordered, "[" in t.ctor and "5, 1" in t.ctor, t.extra.get("regime"))
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out[:6]


def _try(f):
    try:
        return f()
    except BaseException:  # noqa: BLE001
        return _MISSING


_MISSING = object()


def _classify_obj(path, ctor, obj, ns, big):
    has = lambda *names: all(hasattr(obj, n) for n in names)  # noqa: E731
    uses_clock = "clock" in ctor
    init = repr(_INIT) in ctor
    if isinstance(obj, abc.MutableMapping) or has("__getitem__", "__setitem__", "__delitem__", "keys", "__iter__", "__len__"):
        fresh = _try(lambda: _eval(ctor, ns))
        if fresh is _MISSING or init:
            return None
        ok = _try(lambda: [fresh.__setitem__(k, k * 10) for k in (5, 1, 4, 3, 0)])
        dictlike = ok is not _MISSING and _try(lambda: len(fresh) == 5 and all(fresh[k] == k * 10 for k in (5, 1, 4, 3, 0))) is True
        if big and dictlike:
            key = _is_sorted_like(_try(lambda: list(fresh)), [5, 1, 4, 3, 0])
            if key == "_half":
                return None
            return Target(path, "map-ref", ctor, None, None, key=key or "None", ordered=key is not None,
                          extra={"regime": "ref", "clock": uses_clock})
        if ok is _MISSING or _try(lambda: fresh[0] == 0) is not True:
            return None
        if not big:
            return Target(path, "map-cons", ctor, None, None, extra={"regime": "cons", "clock": uses_clock,
                                                                    "methods": _zero_arg_methods(obj)})
        return None
    if has("add", "__contains__", "__iter__", "__len__") and (has("discard") or has("remove")):
        fresh = _try(lambda: _eval(ctor, ns))
        if fresh is _MISSING:
            return None
        base = list(_INIT) if init else []
        _try(lambda: [fresh.add(v) for v in (5, 1, 4, 1, 3)])
        vals = base + [5, 1, 4, 1, 3]
        lst = _try(lambda: list(fresh))
        if lst is _MISSING:
            return None
        key = _is_sorted_like(lst, vals)
        dkey = _is_sorted_like(lst, sorted(set(vals)))
        indexable = _try(lambda: fresh[0] == lst[0]) is True
        if key is not None:
            return Target(path, "seq-sorted", ctor, None, None, key=key, ordered=True,
                          extra={"regime": "ref", "init": init, "indexable": indexable,
                                 "cmp": _try(lambda: fresh == lst) is True})
        if dkey is not None and dkey != "_half":
            return Target(path, "set-sorted", ctor, None, None, key=dkey, ordered=True,
                          extra={"regime": "ref", "init": init, "indexable": indexable})
        if sorted(lst) == sorted(set(vals)):
            return Target(path, "set-plain", ctor, None, None, extra={"regime": "ref", "init": init})
    return None


def _zero_arg_methods(obj) -> list[str]:
    out = []
    for name in dir(type(obj)):
        if name.startswith("_") or name in ("clear", "popitem", "copy", "keys", "values", "items"):
            continue
        attr = getattr(type(obj), name, None)
        if not callable(attr):
            continue
        try:
            sig = inspect.signature(attr)
        except (TypeError, ValueError):
            continue
        params = [p for p in sig.parameters.values() if p.name != "self"]
        if all(p.default is not inspect.Parameter.empty or p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
               for p in params):
            out.append(name)
    return out[:4]


# ---------------------------------------------------------------------------------------------------
# operation generators: each returns one code chunk (the op plus its check)


def _k(r):
    return r.randrange(8)


def ops_map_ref(t: Target, r: random.Random) -> str:
    k, v = _k(r), r.randrange(100)
    pairs = [(_k(r), r.randrange(100)) for _ in range(r.randrange(1, 4))]
    choice = r.choice(["set", "set", "set", "get", "del", "pop", "popd", "getm", "setdefault", "update",
                       "updlist", "contains", "clear", "popitem", "keys_set"])
    code = {
        "set": f"_same(lambda: m.__setitem__({k}, {v}), lambda: ref.__setitem__({k}, {v}), 'm[{k}] = {v}')",
        "get": f"_same(lambda: m[{k}], lambda: ref[{k}], 'm[{k}]')",
        "del": f"_same(lambda: m.__delitem__({k}), lambda: ref.__delitem__({k}), 'del m[{k}]')",
        "pop": f"_same(lambda: m.pop({k}), lambda: ref.pop({k}), 'pop({k})')",
        "popd": f"_same(lambda: m.pop({k}, None), lambda: ref.pop({k}, None), 'pop({k}, None)')",
        "getm": f"_same(lambda: m.get({k}), lambda: ref.get({k}), 'get({k})')",
        "setdefault": f"_same(lambda: m.setdefault({k}, {v}), lambda: ref.setdefault({k}, {v}), 'setdefault({k}, {v})')",
        "update": f"_same(lambda: m.update({dict(pairs)!r}), lambda: ref.update({dict(pairs)!r}), 'update')",
        "updlist": f"_same(lambda: m.update({pairs!r}), lambda: ref.update({pairs!r}), 'update(list)')",
        "contains": f"assert ({k} in m) == ({k} in ref), '{k} in m'",
        "clear": "_same(lambda: m.clear(), lambda: ref.clear(), 'clear()')",
        "popitem": "_c_popitem_ref(m, ref)",
        "keys_set": f"assert set(m.keys() & {{{k}, {(k + 3) % 8}}}) == set(ref.keys() & {{{k}, {(k + 3) % 8}}}), 'keys() &'",
    }[choice]
    if choice == "popitem":
        code = ("r = _outcome(m.popitem)\n"
                "if r[0] == 'ok':\n    assert ref.pop(r[1][0]) == r[1][1], f'popitem gave {r}'\n"
                "else:\n    assert not ref, f'popitem raised {r}'")
    if t.ordered and r.random() < 0.3:
        meth = r.choice(["bisect_left", "bisect_right", "index"])
        code += (f"\nif hasattr(m, {meth!r}):\n"
                 f"    _same(lambda: m.{meth}({k}), lambda: "
                 + (f"_bis(sorted(ref, key={t.key}), {t.key}, {k}, {meth.split('_')[1]!r})" if meth != "index"
                    else f"sorted(ref, key={t.key}).index({k})")
                 + f", '{meth}({k})')")
    return code + f"\n_check_map(m, ref, {t.key}, {t.ordered}, range(8))"


def ops_map_cons(t: Target, r: random.Random) -> str:
    k, v = _k(r), r.randrange(100)
    opts = ["set", "set", "set", "get", "del", "pop", "popitem", "setdefault"]
    if t.extra.get("clock"):
        opts += ["tick", "tick", "tick"]
    if t.extra.get("methods"):
        opts += ["meth"]
    if r.random() < 0.05:
        opts += ["clear"]
    c = r.choice(opts)
    if c == "set":
        return f"_c_set(m, {k}, {v}, range(8))"
    if c == "get":
        return f"_c_get(m, {k}, range(8))"
    if c == "del":
        return f"_c_del(m, {k}, range(8))"
    if c == "pop":
        return f"_c_pop(m, {k}, range(8))"
    if c == "popitem":
        return "_c_popitem(m, range(8))"
    if c == "setdefault":
        return f"_c_setdefault(m, {k}, {v}, range(8))"
    if c == "clear":
        return "_c_clear(m, range(8))"
    if c == "tick":
        return f"clock.t += {r.choice([1, 1, 4, 5, 10, 10])}"
    name = r.choice(t.extra["methods"])
    return f"_outcome(lambda: m.{name}())\n_cons_map(m, range(8))"


def ops_seq_sorted(t: Target, r: random.Random) -> str:
    K = t.key
    x = r.randrange(8)
    xs = [r.randrange(8) for _ in range(r.randrange(1, 4))]
    i = r.randrange(-6, 6)
    a, b = r.randrange(-5, 6), r.randrange(-5, 7)
    opts = ["add", "add", "add", "discard", "remove", "update", "pop", "popi", "delitem", "delslice", "clear",
            "islice", "irange_none", "iadd", "add_op", "mul", "cmp", "index_range"]
    if K == "_half":
        opts = ["add", "add", "add", "discard", "discard", "remove", "update", "iadd", "clear"]
    if r.random() < BIG_P:
        # Containers often switch to a different internal layout above some size: grow past a few
        # thousand elements, then sweep operations over every value so each internal boundary is hit.
        lo, n = r.randrange(0, 50), r.randrange(1500, 2700)
        bulk = (f"_same(lambda: s.update(range({lo}, {lo + n})), lambda: _ins(ref, {K}, *range({lo}, {lo + n})), "
                f"'update(range({lo}, {lo + n}))')")
        return bulk + "\n" + _sweep(t, r, lo, lo + n) + f"\n_check_seq(s, ref, {K}, range(8))"
    c = r.choice(opts)
    code = {
        "add": f"_same(lambda: s.add({x}), lambda: _ins(ref, {K}, {x}), 'add({x})')",
        "discard": f"_same(lambda: s.discard({x}), lambda: _disc(ref, {x}), 'discard({x})')",
        "remove": f"_same(lambda: s.remove({x}), lambda: _rem(ref, {x}), 'remove({x})')",
        "update": f"_same(lambda: s.update({xs!r}), lambda: _ins(ref, {K}, *{xs!r}), 'update({xs!r})')",
        "pop": "_same(lambda: s.pop(), lambda: ref.pop(), 'pop()')",
        "popi": f"_same(lambda: s.pop({i}), lambda: ref.pop({i}), 'pop({i})')",
        "delitem": f"_same(lambda: s.__delitem__({i}), lambda: ref.__delitem__({i}), 'del s[{i}]')",
        "delslice": f"_same(lambda: s.__delitem__(slice({a}, {b})), lambda: ref.__delitem__(slice({a}, {b})), 'del s[{a}:{b}]')",
        "clear": "_same(lambda: s.clear(), lambda: ref.clear(), 'clear()')",
        "index_range": (f"if hasattr(s, 'index'):\n    _same(lambda: s.index({x}, {a}, {b}), "
                        f"lambda: ref.index({x}, {a}, {b}), 'index({x}, {a}, {b})')\n"
                        f"    _same(lambda: s.index({x}, {a}), lambda: ref.index({x}, {a}), 'index({x}, {a})')"),
        "islice": (f"if hasattr(s, 'islice'):\n    _same(lambda: list(s.islice({abs(a)}, {abs(b)})), "
                   f"lambda: ref[{abs(a)}:{abs(b)}], 'islice({abs(a)}, {abs(b)})')\n"
                   f"    _same(lambda: list(s.islice({abs(a)}, {abs(b)}, reverse=True)), "
                   f"lambda: ref[{abs(a)}:{abs(b)}][::-1], 'islice({abs(a)}, {abs(b)}, reverse=True)')"),
        "irange_none": ("if hasattr(s, 'irange'):\n    _same(lambda: list(s.irange()), lambda: list(ref), 'irange()')\n"
                        "    _same(lambda: list(s.irange(reverse=True)), lambda: ref[::-1], 'irange(reverse=True)')"),
        "iadd": f"_same(lambda: s.__iadd__({xs!r}) is s, lambda: _ins(ref, {K}, *{xs!r}) is None, 's += {xs!r}')",
        "add_op": f"_same(lambda: list(s + {xs!r}), lambda: _sorted(ref + {xs!r}, {K}), 's + {xs!r}')",
        "mul": f"_same(lambda: list(s * 2), lambda: _sorted(ref * 2, {K}), 's * 2')",
        "cmp": ("" if not t.extra.get("cmp") else
                "".join(f"_same(lambda: s {op} {sorted(xs)!r}, lambda: ref {op} {sorted(xs)!r}, 's {op} {sorted(xs)!r}')\n"
                        for op in ("==", "!=", "<", "<=", ">", ">="))),
    }[c] or "pass"
    if t.key == "_neg" and c in ("add_op", "mul"):
        code = "pass"
    return code + f"\n_check_seq(s, ref, {K}, range(8))"


BIG_P = 0.08


def _sweep(t: Target, r: random.Random, lo: int, hi: int) -> str:
    """One operation applied across the whole value range of a large sorted sequence."""
    K = t.key
    step = r.choice([1, 1, 3, 7])
    kind = r.choice(["discard", "remove", "index", "irange", "slice", "count"])
    vals = f"range({lo + r.randrange(step)}, {hi + 2}, {step})"
    if kind == "discard":
        return (f"for _v in {vals}:\n    s.discard(_v)\n    _disc(ref, _v)\n"
                f"assert sorted(s) == sorted(ref) and len(s) == len(ref), f'after discard sweep: len {{len(s)}} != {{len(ref)}}'")
    if kind == "remove":
        return (f"for _v in {vals}:\n    _same(lambda: s.remove(_v), lambda: _rem(ref, _v), f'remove({{_v}})')\n"
                f"assert sorted(s) == sorted(ref) and len(s) == len(ref), f'after remove sweep: len {{len(s)}} != {{len(ref)}}'")
    if kind == "count":
        return (f"for _v in {vals}:\n    if hasattr(s, 'count'):\n"
                f"        assert s.count(_v) == ref.count(_v), f'count({{_v}})'\n"
                f"    assert (_v in s) == (_v in ref), f'{{_v}} in s'")
    if kind == "index" and K in ("None", "_ident"):
        a, b = r.choice([(None, None), (-r.randrange(1, 900), None), (r.randrange(0, 900), -r.randrange(1, 900)),
                         (-r.randrange(900, 3000), -r.randrange(1, 50))])
        return (f"for _v in {vals}:\n    if hasattr(s, 'index'):\n"
                f"        _same(lambda: s.index(_v, {a}, {b}), lambda: ref.index(_v, {a}, {b}), f'index({{_v}}, {a}, {b})')")
    if kind == "irange":
        w = r.randrange(1, 1200)
        a = lo + r.randrange(0, hi - lo)
        lines = [f"for _a in range({a % 997}, {hi + 2}, 997):"]
        if K in ("None", "_ident"):
            lines += [f"    if hasattr(s, 'irange'):",
                      f"        _same(lambda: list(s.irange(_a, _a + {w})), lambda: [v for v in ref if _a <= v <= _a + {w}], f'irange({{_a}}, {{_a + {w}}})')",
                      f"        _same(lambda: list(s.irange(_a, _a + {w}, (False, False))), lambda: [v for v in ref if _a < v < _a + {w}], f'irange({{_a}}, {{_a + {w}}}, (False, False))')",
                      f"        _same(lambda: list(s.irange(_a, _a + {w}, reverse=True)), lambda: [v for v in ref if _a <= v <= _a + {w}][::-1], f'irange({{_a}}, {{_a + {w}}}, reverse=True)')"]
        else:
            lines += [f"    if hasattr(s, 'irange_key'):",
                      f"        _same(lambda: sorted(s.irange_key(_a // 2, _a // 2 + {w})), lambda: sorted(v for v in ref if _a // 2 <= {K}(v) <= _a // 2 + {w}), f'irange_key({{_a // 2}}, {{_a // 2 + {w}}})')",
                      f"        _same(lambda: sorted(s.irange_key(-_a, -_a + {w})), lambda: sorted(v for v in ref if -_a <= {K}(v) <= -_a + {w}), f'irange_key({{-_a}}, {{-_a + {w}}})')",
                      f"        _same(lambda: sorted(s.irange_key(_a, _a + {w}, (False, False))), lambda: sorted(v for v in ref if _a < {K}(v) < _a + {w}), f'irange_key({{_a}}, {{_a + {w}}}, (False, False))')"]
        return "\n".join(lines)
    # slices and negative indexes across the whole length
    K = "(lambda v: v)" if K in ("None", "_ident") else K
    return (f"_n = len(ref)\nfor _i in range(-_n, _n, {r.choice([1, 13, 97])}):\n"
            f"    assert {K}(s[_i]) == {K}(sorted(ref, key={K})[_i]), f's[{{_i}}]'\n"
            f"for _a, _b in ((5, -5), (-1500, None), (999, 2001), (-2001, -999)):\n"
            f"    assert [{K}(v) for v in s[_a:_b]] == [{K}(v) for v in sorted(ref, key={K})[_a:_b]], f's[{{_a}}:{{_b}}]'")


def ops_set(t: Target, r: random.Random) -> str:
    K, ordered = t.key, t.ordered
    x = r.randrange(8)
    other = sorted({r.randrange(8) for _ in range(r.randrange(0, 4))})
    o = "{" + ", ".join(map(str, other)) + "}" if other else "set()"
    norm = f"lambda z: sorted(z, key={K})" if ordered else "lambda z: sorted(z)"
    c = r.choice(["add", "add", "add", "discard", "remove", "or", "and", "sub", "xor", "union", "inter", "diff",
                  "symdiff", "ior", "iand", "isub", "ixor", "update", "iupd", "dupd", "supd", "cmp", "disjoint",
                  "issub", "issup", "pop", "clear", "index", "count"])
    if c in ("or", "and", "sub", "xor"):
        op = {"or": "|", "and": "&", "sub": "-", "xor": "^"}[c]
        code = (f"_same(lambda: ({norm})(s {op} {o}), lambda: ({norm})(ref {op} {o}), 's {op} {o}')")
        if ordered:
            code += f"\n_same(lambda: list(s {op} {o}), lambda: sorted(ref {op} {o}, key={K}), 'order of s {op} {o}')"
    elif c in ("union", "inter", "diff", "symdiff"):
        meth = {"union": "union", "inter": "intersection", "diff": "difference", "symdiff": "symmetric_difference"}[c]
        code = (f"if hasattr(s, {meth!r}):\n    _same(lambda: ({norm})(s.{meth}({other!r})), "
                f"lambda: ({norm})(ref.{meth}({other!r})), '{meth}({other!r})')")
    elif c in ("ior", "iand", "isub", "ixor"):
        op = {"ior": "|=", "iand": "&=", "isub": "-=", "ixor": "^="}[c]
        code = (f"s_before, ref_before = s, ref\ns {op} {o}\nref {op} {o}\n"
                f"assert s is s_before, 's {op} {o} rebinds s'")
    elif c in ("update", "iupd", "dupd", "supd"):
        meth = {"update": "update", "iupd": "intersection_update", "dupd": "difference_update",
                "supd": "symmetric_difference_update"}[c]
        code = (f"if hasattr(s, {meth!r}):\n    _same_effect(lambda: s.{meth}({other!r}), lambda: ref.{meth}({other!r}), "
                f"'{meth}({other!r})')")
    elif c == "cmp":
        code = "".join(f"_same(lambda: s {op} {o}, lambda: ref {op} {o}, 's {op} {o}')\n"
                       for op in ("==", "!=", "<", "<=", ">", ">=")).rstrip()
    elif c == "disjoint":
        code = f"_same(lambda: s.isdisjoint({other!r}), lambda: ref.isdisjoint({other!r}), 'isdisjoint')"
    elif c == "issub":
        code = (f"if hasattr(s, 'issubset'):\n    _same(lambda: s.issubset({other!r}), lambda: ref.issubset({other!r}), "
                f"'issubset({other!r})')")
    elif c == "issup":
        code = (f"if hasattr(s, 'issuperset'):\n    _same(lambda: s.issuperset({other!r}), "
                f"lambda: ref.issuperset({other!r}), 'issuperset({other!r})')")
    elif c == "pop" and ordered and t.extra.get("indexable"):
        i = r.choice([-1, 0, 1, -2])
        code = (f"_same(lambda: s.pop({i}), lambda: _spop(ref, {i}, {K}), 'pop({i})')")
    elif c == "index" and ordered and t.extra.get("indexable"):
        i = r.randrange(-4, 4)
        code = (f"_same(lambda: s[{i}], lambda: sorted(ref, key={K})[{i}], 's[{i}]')\n"
                f"if hasattr(s, 'index'):\n    _same(lambda: s.index({x}), lambda: sorted(ref, key={K}).index({x}), 'index({x})')\n"
                f"for side in ('left', 'right'):\n    if hasattr(s, 'bisect_' + side):\n"
                f"        _same(lambda: getattr(s, 'bisect_' + side)({x}), lambda: _bis(sorted(ref, key={K}), {K}, {x}, side), "
                f"'bisect_' + side + '({x})')")
    elif c == "clear":
        code = "_same(lambda: s.clear(), lambda: ref.clear(), 'clear()')"
    elif c == "count":
        code = (f"if hasattr(s, 'count'):\n    _same(lambda: s.count({x}), lambda: 1 if {x} in ref else 0, "
                f"'count({x})')")
    elif c in ("pop", "index"):
        code = f"_same(lambda: s.add({x}), lambda: ref.add({x}), 'add({x})')"
    else:
        meth = c
        code = f"_same(lambda: s.{meth}({x}), lambda: ref.{meth}({x}), '{meth}({x})')"
    return code + f"\n_check_set(s, ref, {K}, {ordered}, range(8))"


HELPERS += r'''

def _spop(ref, i, key):
    v = sorted(ref, key=key)[i]
    ref.remove(v)
    return v
'''
_H.clear()
exec(compile(HELPERS, "<contracts-helpers>", "exec"), _H)


def prelude(t: Target) -> list[str]:
    lines = []
    if "clock" in t.ctor:
        lines.append("clock = _Clock()")
    if t.kind.startswith("map"):
        lines.append(f"m = {t.ctor}")
        if t.kind == "map-ref":
            lines.append("ref = {}")
    elif t.kind == "seq-sorted":
        lines.append(f"s = {t.ctor}")
        lines.append(f"ref = _sorted({_INIT!r}, {t.key})" if t.extra.get("init") else "ref = []")
    elif t.kind.startswith("set"):
        lines.append(f"s = {t.ctor}")
        lines.append(f"ref = set({_INIT!r})" if t.extra.get("init") else "ref = set()")
    return lines


GEN = {"map-ref": ops_map_ref, "map-cons": ops_map_cons, "seq-sorted": ops_seq_sorted,
       "set-sorted": ops_set, "set-plain": ops_set}


# ---------------------------------------------------------------------------------------------------
# memoizing decorators and key functions


def _memo_forms(path: str, fn) -> list[dict]:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return []
    params = sig.parameters
    shared = [n for n in ("maxsize", "typed") if n in params]
    forms = []
    required = [p for p in params.values() if p.default is inspect.Parameter.empty
                and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)]
    if not required:
        forms.append({"code": f"{path}(_f)", "ref": "functools.lru_cache(_g)", "maxsize": True})
        forms.append({"code": f"{path}()(_f)", "ref": "functools.lru_cache()(_g)", "maxsize": True})
        if "maxsize" in shared:
            for n in (2, 3):
                forms.append({"code": f"{path}(maxsize={n})(_f)", "ref": f"functools.lru_cache(maxsize={n})(_g)",
                              "maxsize": True, "evicts": True})
            forms.append({"code": f"{path}(maxsize=None)(_f)", "ref": "functools.lru_cache(maxsize=None)(_g)",
                          "maxsize": True})
        if "typed" in shared:
            forms.append({"code": f"{path}(typed=True)(_f)", "ref": "functools.lru_cache(typed=True)(_g)",
                          "maxsize": True, "typed": True})
    else:
        flags = [p.name for p in params.values() if p.default is False]
        # a plain dict as the store behaves like an unbounded cache; None like a cache that keeps nothing
        for val, ref, cmp_max in (("{}", "functools.lru_cache(maxsize=None)(_g)", False),
                                  ("None", "functools.lru_cache(maxsize=0)(_g)", True)):
            req = ", ".join(val for _ in required)
            for extra in ([""] + [f", {f}=True" for f in flags]):
                forms.append({"code": f"{path}({req}{extra})(_f)", "ref": ref, "maxsize": cmp_max})
    good = []
    for form in forms:
        code = ("calls, calls2 = [], []\n"
                "def _f(x):\n    calls.append(x)\n    return x * 10\n"
                "def _g(x):\n    calls2.append(x)\n    return x * 10\n"
                f"g = {form['code']}\nh = {form['ref']}\n"
                "assert g(1) == 10 and g(1) == 10\nassert callable(g.cache_info)\n_memo_info(g)\n")
        if _run(_imports([path]) + code) is None:
            good.append(form)
    return good


def memo_program(path: str, form: dict, r: random.Random, stdlib_name: bool) -> list[str]:
    if form.get("evicts") and not stdlib_name:
        return ["pass"]
    domain = 8 if form.get("evicts") else 5
    head = ("calls, calls2 = [], []\n"
            "def _f(x):\n    calls.append(x)\n    return x * 10\n"
            "def _g(x):\n    calls2.append(x)\n    return x * 10\n"
            f"g = {form['code']}\nh = {form['ref']}")
    chunks = [head]
    if r.random() < 0.25 and not form.get("typed"):
        # capacity contract: a cache that reports maxsize M holds M results without evicting any
        chunks.append("M = g.cache_info().maxsize\n"
                      "if isinstance(M, int) and 0 < M <= 512:\n"
                      "    for i in range(M):\n        g(100 + i)\n"
                      "    assert g.cache_info().currsize == M, f'currsize {g.cache_info().currsize} after {M} distinct calls'\n"
                      "    for i in range(M):\n        g(100 + i)\n"
                      "    assert g.cache_info().hits == M, f'only {g.cache_info().hits} hits re-reading {M} cached calls'\n"
                      "    g(100 + M)\n"
                      "    assert g.cache_info().currsize == M, f'currsize {g.cache_info().currsize} with maxsize {M}'")
        return chunks
    if r.random() < 0.3:
        chunks.append("if hasattr(g, 'cache_parameters'):\n"
                      "    p = g.cache_parameters()\n"
                      "    assert p.get('maxsize') == g.cache_info().maxsize, "
                      "f'cache_parameters() says maxsize {p.get(\"maxsize\")}, cache_info() says {g.cache_info().maxsize}'\n"
                      + ("    assert p == h.cache_parameters(), f'cache_parameters() {p} != {h.cache_parameters()}'\n"
                         if form.get("maxsize") else ""))
    if form.get("maxsize") and r.random() < 0.3:
        chunks.append("assert g.cache_info().maxsize == h.cache_info().maxsize, "
                      "f'maxsize {g.cache_info().maxsize} != {h.cache_info().maxsize}'")
    for _ in range(r.randrange(3, 12)):
        x = r.randrange(domain)
        if form.get("typed") and r.random() < 0.3:
            x = f"{x}.0"
        c = r.random()
        if c < 0.85:
            chunks.append(f"assert g({x}) == h({x}), 'g({x})'\n"
                          f"assert _memo_info(g) == _memo_info(h), f'after g({x}): {{_memo_info(g)}} != {{_memo_info(h)}}'\n"
                          f"assert len(calls) == len(calls2), f'function ran {{len(calls)}} times, expected {{len(calls2)}}'")
        else:
            chunks.append("g.cache_clear()\nh.cache_clear()\n"
                          "assert _memo_info(g) == _memo_info(h), f'after cache_clear: {_memo_info(g)}'")
    return chunks


def hash_program(path: str, r: random.Random, lo: int = 0) -> list[str]:
    chunks = []
    for _ in range(r.randrange(2, 6)):
        args = ", ".join(str(r.randrange(5)) for _ in range(r.randrange(lo, lo + 4)))
        if r.random() < 0.3:
            args += (", " if args else "") + f"k={r.randrange(3)}"
        chunks.append(
            f"a = {path}({args})\nb = {path}({args})\n"
            f"assert a == b and hash(a) == hash(b), 'same arguments ({args})'\n"
            "if isinstance(a, tuple):\n"
            "    assert hash(a) == hash(tuple(a)), f'hash(a) != hash(tuple(a)) for {a!r}'\n"
            "    assert a + (9,) == tuple(a) + (9,), 'a + (9,)'\n"
            "    assert (9,) + a == (9,) + tuple(a), '(9,) + a'")
    return chunks


def _min_positional(fn) -> int:
    try:
        return sum(1 for p in inspect.signature(fn).parameters.values()
                   if p.default is inspect.Parameter.empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD))
    except (TypeError, ValueError):
        return 0


def _hash_ok(path: str) -> bool:
    return _run(_imports([path]) + f"a = {path}(1, 2)\nhash(a)\nassert a == {path}(1, 2)\n") is None


# ---------------------------------------------------------------------------------------------------
# fuzzing, shrinking and emitting


def _imports(paths) -> str:
    mods = set()
    for p in paths:
        parts = p.split(".")
        mods.add(".".join(parts[:-1]) if len(parts) > 1 else parts[0])
    return "".join(f"import {m}\n" for m in sorted(mods))


def _signature(fail) -> str:
    typ, msg = fail
    # the assertion label up to the first concrete value
    label = msg.split(":")[0] if ":" in msg else msg
    label = label.split("(")[0].split("[")[0]
    label = "".join(ch for ch in label if not ch.isdigit())
    return f"{typ}:{label[:60]}"


def ddmin(head: str, chunks: list[str], sig: str, head_chunks: int = 0, until: float | None = None) -> list[str]:
    """Delta debugging over the op chunks: the smallest list that still fails with the same signature
    (or the smallest found before `until`)."""
    fixed, items = chunks[:head_chunks], chunks[head_chunks:]

    def fails(sub):
        res = _run(head + "\n".join(fixed + sub))
        return res is not None and _signature(res) == sig

    n = 2
    while len(items) >= 2 and (until is None or time.time() < until):
        size = max(1, len(items) // n)
        parts = [items[i:i + size] for i in range(0, len(items), size)]
        reduced = False
        for i in range(len(parts)):
            comp = [x for j, p in enumerate(parts) if j != i for x in p]
            if fails(comp):
                items, n, reduced = comp, max(n - 1, 2), True
                break
        if not reduced:
            if n >= len(items):
                break
            n = min(len(items), n * 2)
    return fixed + items


def fuzz(pkg: str, deadline: float, seed: int, per_sig: int = 2, max_tests: int = 60, long: int = 0, span: int = 8) -> dict:
    r = random.Random(seed)
    stdlib = set(dir(functools))
    found = []
    stats = {"targets": [], "programs": 0}
    try:
        objs = discover(pkg)
    except Exception as e:  # noqa: BLE001
        return {"tests": [], "stats": {"error": f"{type(e).__name__}: {e}"}}
    t0 = time.time()
    jobs = []   # (label, head, chunk generator)
    for path, obj in objs:
        if time.time() - t0 > deadline * 0.3:
            break
        if inspect.isclass(obj):
            try:
                ts = classify(path, obj, {path.rsplit('.', 1)[0]})
            except Exception:  # noqa: BLE001
                ts = []
            for t in ts:
                stats["targets"].append(f"{t.kind}:{t.ctor}:{t.key}")
                gen = GEN[t.kind]
                head = _imports([path]) + "\n".join(prelude(t)) + "\n"
                jobs.append((f"{t.kind}:{t.name()}", head, (lambda rr, t=t, gen=gen: [gen(t, rr) for _ in range(rr.randrange(4, 16))]), 0))
        elif inspect.isfunction(obj):
            forms = _memo_forms(path, obj)
            std = path.rsplit(".", 1)[-1] in stdlib
            forms = [f for f in forms if std or not f.get("evicts")]
            for form in forms:
                stats["targets"].append(f"memo:{form['code']}")
                head = _imports([path])
                jobs.append((f"memo:{path}", head, (lambda rr, form=form, std=std, path=path: memo_program(path, form, rr, std)), 1))
            if not forms and _hash_ok(path):
                stats["targets"].append(f"hash:{path}")
                head = _imports([path])
                lo = _min_positional(obj)
                jobs.append((f"hash:{path}", head, (lambda rr, path=path, lo=lo: hash_program(path, rr, lo)), 0))
    per_job = {}
    sigs = {}
    end = t0 + deadline
    if long:
        # Long regression sequences: programs that pass on the code with the repairs accepted so far.
        # Each drives many paths, so it guards against more single defects than a shrunk failure does.
        for label, head, make, nfixed in jobs:
            if (nfixed and span >= 8) or time.time() > end:
                continue
            for _ in range(long):
                for _try_ in range(5):
                    chunks = make(r) if nfixed else [c for _k in range(span) for c in make(r)]
                    stats["programs"] += 1
                    if _run(head + "\n".join(chunks)) is None and _run(head + "\n".join(chunks)) is None:
                        found.append({"label": label, "sig": "long", "body": "\n".join(chunks), "head": head})
                        per_job[label] = per_job.get(label, 0) + 1
                        break
        stats["found"] = per_job
        return {"tests": found, "stats": stats}
    while jobs and time.time() < end and len(found) < max_tests:
        for label, head, make, nfixed in list(jobs):
            if time.time() > end or len(found) >= max_tests:
                break
            for _ in range(10):
                chunks = make(r)
                stats["programs"] += 1
                res = _run(head + "\n".join(chunks))
                if res is None or res[0] in ("Timeout", "RecursionError"):
                    continue
                sig = _signature(res)
                if sigs.get((label, sig, "*"), 0) >= 4 * per_sig:
                    continue
                small = ddmin(head, chunks, sig, nfixed, until=end + 5)
                code = head + "\n".join(small)
                # must fail the same way every time
                if any(_signature(_run(code) or ("pass", "")) != sig for _ in range(3)):
                    continue
                # distinct bugs often share an assertion; tell them apart by the operation that failed
                last = small[-1].splitlines()[0] if small else ""
                key = (label, sig, "".join(ch for ch in last if not ch.isdigit())[:80])
                if sigs.get(key, 0) >= per_sig:
                    continue
                sigs[(label, sig, "*")] = sigs.get((label, sig, "*"), 0) + 1
                sigs[key] = sigs.get(key, 0) + 1
                per_job[label] = per_job.get(label, 0) + 1
                found.append({"label": label, "sig": sig, "body": "\n".join(small), "imports": head.split("\n" + "\n".join(small[:0]))[0]})
                found[-1]["head"] = head
    stats["found"] = per_job
    return {"tests": found, "stats": stats}


def render(tests: list[dict]) -> str:
    imports = set()
    for t in tests:
        for line in t["head"].splitlines():
            if line.startswith("import "):
                imports.add(line)
    out = ["# Generated by the contract fuzzer: random operation sequences checked against a reference",
           "# model or protocol contracts, shrunk with delta debugging.", *sorted(imports), HELPERS.strip(), ""]
    for i, t in enumerate(tests):
        name = "".join(ch if ch.isalnum() else "_" for ch in t["label"].split(":")[-1].split(".")[-1]).lower()
        body_lines = [ln for ln in t["head"].splitlines() if ln and not ln.startswith("import ")]
        body_lines += t["body"].splitlines()
        out.append(f"\ndef test_contract_{name}_{i:02d}():")
        out += ["    " + ln for ln in body_lines]
    return "\n".join(out) + "\n"


def shadow_package(pkg: str, fixes: list[str]) -> str | None:
    """Copy the package with the repairs accepted so far applied and put the copy first on sys.path,
    so this pass looks past bugs that earlier tests already expose (the tests still run on the real code)."""
    import ast
    import importlib.util
    import shutil
    import tempfile
    from pathlib import Path
    if not fixes:
        return None
    spec = importlib.util.find_spec(pkg)
    if not spec or not spec.origin:
        return None
    pkg_dir = Path(spec.origin).parent
    base = pkg_dir.parent
    import atexit
    # scratch copy inside the workspace (hidden, so pytest does not collect it), removed on exit
    tmp = Path(tempfile.mkdtemp(prefix=".tagent_shadow_", dir=Path.cwd()))
    atexit.register(shutil.rmtree, tmp, True)
    shutil.copytree(pkg_dir, tmp / pkg_dir.name, ignore=shutil.ignore_patterns("__pycache__"))
    by_file: dict[str, list] = {}
    for site in fixes:
        try:
            rel, a, b, repl = site.split(":", 3)
            by_file.setdefault(rel, []).append((int(a), int(b), repl))
        except ValueError:
            continue
    cwd = Path.cwd()
    for rel, edits in by_file.items():
        src = (cwd / rel)
        try:
            dst = tmp / src.resolve().relative_to(base.resolve())
            text = src.read_text()
        except (ValueError, OSError):
            continue
        new = text
        for a, b, repl in sorted(edits, key=lambda e: -e[0]):
            new = new[:a] + repl + new[b:]
        try:
            ast.parse(new)
        except SyntaxError:
            continue
        dst.write_text(new)
    for name in list(sys.modules):
        if name == pkg or name.startswith(pkg + "."):
            del sys.modules[name]
    sys.path.insert(0, str(tmp))
    return str(tmp)


def main():
    req = json.loads(open(sys.argv[1]).read())
    sys.setrecursionlimit(3000)
    # _run and _eval stop a program after a few seconds with SIGALRM; without this handler the
    # signal would kill the whole fuzzing pass instead of skipping one slow program.
    signal.signal(signal.SIGALRM, _alarm)
    try:
        shadow_package(req["package"], req.get("fixes") or [])
    except Exception:  # noqa: BLE001 - fall back to the real code
        pass
    res = fuzz(req["package"], float(req.get("deadline", 60)), int(req.get("seed", 1)),
               per_sig=int(req.get("per_sig", 2)), max_tests=int(req.get("max_tests", 60)),
               long=int(req.get("long", 0)), span=int(req.get("span", 8)))
    res["source"] = render(res["tests"]) if res["tests"] else ""
    with open(sys.argv[2], "w") as fh:
        json.dump({"source": res["source"], "stats": res["stats"],
                   "labels": [(t["label"], t["sig"]) for t in res["tests"]]}, fh)


if __name__ == "__main__":
    main()
