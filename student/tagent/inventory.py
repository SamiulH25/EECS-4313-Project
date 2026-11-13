"""Static map of a project: units of code to test, their risk points, and the matching documentation.

No model calls. This decides *where* the agent looks (risk-based test selection) and gives the model
the boundary points of every comparison, constant and arithmetic operation it has to probe
(white-box boundary value analysis).
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

MAX_UNIT_LINES = 170


@dataclass
class Unit:
    uid: str
    module: str              # dotted module name
    path: str                # workspace-relative file
    cls: str | None          # class name, if the unit is (part of) a class
    members: list[str]       # function / method names in this unit
    spans: list[tuple[int, int]]
    source: str = ""         # numbered source of the unit
    outline: str = ""        # signatures of the class (incl. project base classes)
    risks: list[str] = field(default_factory=list)
    import_hint: str = ""
    weight: float = 0.0

    @property
    def title(self) -> str:
        who = f"{self.cls}." if self.cls else ""
        return f"{self.module}: {who}{', '.join(self.members[:8])}{'...' if len(self.members) > 8 else ''}"


def _numbered(lines: list[str], start: int, end: int) -> str:
    return "\n".join(f"{i:4d}| {lines[i - 1]}" for i in range(start, end + 1))


def _span(node) -> tuple[int, int]:
    start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
    return start, node.end_lineno


def _sig(node: ast.AST) -> str:
    try:
        args = ast.unparse(node.args)
    except Exception:  # noqa: BLE001
        args = "..."
    doc = ast.get_docstring(node) or ""
    first = doc.strip().splitlines()[0][:90] if doc.strip() else ""
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return f"{prefix} {node.name}({args})" + (f"  # {first}" if first else "")


_OPS = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.Eq: "==", ast.NotEq: "!=",
        ast.Is: "is", ast.IsNot: "is not", ast.In: "in", ast.NotIn: "not in"}


def risk_points(tree: ast.AST, src_lines: list[str], spans: list[tuple[int, int]], limit: int = 45) -> list[str]:
    """Comparisons, constants, arithmetic and boolean logic inside the spans: the places where a small
    slip (off-by-one, wrong operator) changes behaviour, with the boundary to test for each."""
    def inside(n):
        return any(a <= getattr(n, "lineno", -1) <= b for a, b in spans)

    # The enclosing function of every line (innermost wins), so a risk point inside a nested helper
    # says how it is reached, e.g. `cached.decorator.getinfo`.
    owner: dict[int, str] = {}

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = f"{prefix}.{child.name}" if prefix else child.name
                for ln in range(child.lineno, (child.end_lineno or child.lineno) + 1):
                    owner[ln] = q
                walk(child, q)
            else:
                walk(child, prefix)
    walk(tree, "")

    # 0/1 are everywhere, but as a call argument or an assigned value (a reset, a reported count)
    # they are behaviour a test can observe.
    small = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            small.update(id(a) for a in node.args if isinstance(a, ast.Constant))
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            small.add(id(node.value))

    seen = set()
    out = []
    for node in ast.walk(tree):
        if not inside(node):
            continue
        line = node.lineno
        text = ""
        hint = ""
        if isinstance(node, ast.Compare):
            ops = [_OPS.get(type(o), "?") for o in node.ops]
            text = ast.unparse(node)
            if any(o in ("<", "<=", ">", ">=") for o in ops):
                hint = "test both sides AND the equal case (off-by-one: < vs <=)"
            elif any(o in ("==", "!=") for o in ops):
                hint = "test equal and not-equal cases"
            else:
                hint = "test present and absent"
        elif isinstance(node, ast.BoolOp):
            text = ast.unparse(node)
            hint = "test each operand true/false separately (and vs or)"
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.FloorDiv,
                                                                  ast.Mod, ast.Div, ast.LShift, ast.RShift,
                                                                  ast.BitAnd, ast.BitOr, ast.Pow)):
            text = ast.unparse(node)
            hint = "check the exact value (wrong operator / off-by-one)"
        elif isinstance(node, ast.AugAssign):
            text = ast.unparse(node)
            hint = "check the exact count/amount after repeated calls"
        elif isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool) \
                and node.value not in (0, 1, -1):
            text = repr(node.value)
            hint = "documented default/limit? test at, just below and just above it"
        elif isinstance(node, ast.Constant) and id(node) in small and type(node.value) is int and node.value in (0, 1):
            seg = src_lines[line - 1].strip()
            text = seg if len(seg) <= 90 else repr(node.value)
            hint = f"the value {node.value} here is observable: check it exactly"
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            text = ast.unparse(node)
            hint = "test the condition both ways"
        elif isinstance(node, ast.Subscript) and isinstance(node.slice, (ast.Slice, ast.UnaryOp, ast.BinOp)):
            text = ast.unparse(node)
            hint = "check first/last element and empty/one-element cases"
        if not text or len(text) > 90:
            continue
        key = (line, text)
        if key in seen:
            continue
        seen.add(key)
        where = f" in `{owner[line]}`" if line in owner and "." in owner[line] else ""
        out.append((line, f"L{line}{where}: `{text}` -> {hint}"))
    out.sort()
    return [t for _, t in out][:limit]


def suspicious_lines(text: str) -> dict[int, str]:
    """Lines whose code deviates from similar code elsewhere in the same file (belief analysis:
    redundant code reveals the programmer's intent, and a lone deviation is a likely slip)."""
    from .repair import _FLIP, comparison_profile  # local import keeps inventory light

    out: dict[int, str] = {}
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return out
    starts = [0]
    for line in text.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))

    def line_of(offset):
        lo, hi = 0, len(starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if starts[mid] <= offset:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    profile = comparison_profile(text)
    for me in profile:
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
        if len(votes) >= 3:
            top = max(set(votes), key=votes.count)
            if votes.count(top) / len(votes) >= 0.6 and me["op"] is not top:
                out[line_of(me["start"])] = "operator differs from similar comparisons elsewhere"
    # Statements of the same shape whose numeric constants disagree (e.g. one copy of a repeated
    # line uses 127 where the others use 128).
    from .repair import constant_shapes, plurality
    shapes: dict[str, list[tuple[tuple, int]]] = {}
    for c in constant_shapes(text):
        shapes.setdefault(c["key"], []).append((c["consts"], c["line"]))
    for key, vals in shapes.items():
        if len(vals) >= 3 and len({c for c, _ in vals}) > 1:
            top = plurality([c for c, _ in vals])
            for c, ln in vals:
                if c != top:
                    out[ln] = "constant differs from the same code elsewhere; check the documented value"
    # Parameter defaults that differ between sibling functions (same parameter name).
    defaults: dict[str, list[tuple[object, int]]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args.args[-len(node.args.defaults):] if node.args.defaults else []
            pairs = list(zip(args, node.args.defaults)) + [
                (a, d) for a, d in zip(node.args.kwonlyargs, node.args.kw_defaults) if d is not None]
            for a, d in pairs:
                if isinstance(d, ast.Constant) and type(d.value) in (int, float):
                    defaults.setdefault(a.arg, []).append((d.value, d.lineno))
    for name, vals in defaults.items():
        if len(vals) >= 3 and len({v for v, _ in vals}) > 1:
            for v, ln in vals:
                out[ln] = f"default {name}={v} differs between sibling functions; check the documented default"
    return out


def _exports(workspace: Path, package: str) -> set[str]:
    init = workspace / package / "__init__.py"
    names = set()
    if not init.exists():
        return names
    try:
        tree = ast.parse(init.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return names
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                names.add(a.asname or a.name)
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
    return names


def build_units(workspace: Path, package: str) -> list[Unit]:
    workspace = Path(workspace)
    exports = _exports(workspace, package)
    classes: dict[str, ast.ClassDef] = {}
    parsed = []
    for path in sorted((workspace / package).rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        rel = path.relative_to(workspace)
        dotted = ".".join(rel.with_suffix("").parts)
        if dotted.endswith(".__init__"):
            dotted = dotted[: -len(".__init__")]
        parsed.append((path, rel, dotted, text, tree))
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                classes[node.name] = node

    def outline(cls: ast.ClassDef, depth=0) -> list[str]:
        lines = []
        bases = [ast.unparse(b) for b in cls.bases]
        lines.append(f"{'  ' * depth}class {cls.name}({', '.join(bases)}):")
        for n in cls.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                lines.append(f"{'  ' * depth}    {_sig(n)}")
            elif isinstance(n, ast.Assign) and all(isinstance(t, ast.Name) for t in n.targets):
                s = ast.unparse(n)
                if len(s) < 80:
                    lines.append(f"{'  ' * depth}    {s}")
        for b in cls.bases:
            name = ast.unparse(b).split(".")[-1]
            if name in classes and depth < 2 and classes[name] is not cls:
                lines.append(f"{'  ' * depth}  # inherited from {name}:")
                lines.extend(outline(classes[name], depth + 1))
        return lines

    units: list[Unit] = []
    for path, rel, dotted, text, tree in parsed:
        src_lines = text.splitlines()
        stem = dotted.split(".")[-1]
        suspicious = suspicious_lines(text)
        free: list[ast.AST] = []

        def emit(cls_name, members_nodes, cls_node=None):
            if not members_nodes:
                return
            spans = [_span(n) for n in members_nodes]
            code_lines = sum(b - a + 1 for a, b in spans)
            if code_lines < 3:
                return
            parts = []
            if cls_node is not None:
                head_end = cls_node.body[0].lineno - 1 if cls_node.body else cls_node.lineno
                doc_node = cls_node.body[0] if cls_node.body and isinstance(cls_node.body[0], ast.Expr) else None
                if doc_node is not None:
                    head_end = doc_node.end_lineno
                head_end = min(head_end, cls_node.lineno + 60)
                parts.append(_numbered(src_lines, _span(cls_node)[0], head_end))
                parts.append("    ...")
            for a, b in spans:
                parts.append(_numbered(src_lines, a, b))
            uid = f"u{len(units):02d}_{stem}_{(cls_name or 'funcs')}".lower()
            uid = re.sub(r"\W+", "_", uid)[:48]
            public = cls_name or (members_nodes[0].name if members_nodes else "")
            if public in exports:
                imp = f"from {package} import {public}"
            else:
                imp = f"from {dotted} import {public}" if public else f"import {dotted}"
            if not cls_name:
                names = [n.name for n in members_nodes if not n.name.startswith("_")]
                exported = [n for n in names if n in exports]
                imp = (f"from {package} import {', '.join(exported)}" if exported and len(exported) == len(names)
                       else f"from {dotted} import {', '.join(names) or '...'}")
            risks = risk_points(tree, src_lines, spans)
            sus = [f"L{ln}: SUSPICIOUS - {why}" for ln, why in sorted(suspicious.items())
                   if any(a <= ln <= b for a, b in spans)]
            risks = sus + risks
            u = Unit(uid=uid, module=dotted, path=str(rel), cls=cls_name,
                     members=[n.name for n in members_nodes], spans=spans,
                     source="\n".join(parts), risks=risks, import_hint=imp)
            if cls_node is not None:
                text_outline = "\n".join(outline(cls_node))[:3500]
                own = {n.name for n in cls_node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
                variants = []
                for b in cls_node.bases:
                    base = classes.get(ast.unparse(b).split(".")[-1])
                    if base is not None and base is not cls_node:
                        inherited = {n.name for n in base.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
                        over = sorted(own & inherited - {"__init__", "__repr__"})
                        if over:
                            variants.append(f"{cls_node.name} overrides {', '.join(over[:20])} of {base.name}")
                subs = [c.name for c in classes.values() if any(ast.unparse(b).split(".")[-1] == cls_node.name
                                                              for b in c.bases)]
                if subs:
                    variants.append(f"subclasses of {cls_node.name}: {', '.join(subs)}")
                if variants:
                    text_outline += ("\n# Variants of the same interface (differential oracle: configure them to be "
                                     "equivalent, e.g. identity key / huge size / frozen timer, and compare):\n# "
                                     + "\n# ".join(variants))
                u.outline = text_outline
            pub = sum(1 for m in u.members if not m.startswith("_") or m in ("__init__", "__getitem__", "__setitem__",
                                                                             "__delitem__", "__contains__", "__len__",
                                                                             "__iter__", "__eq__", "__lt__"))
            u.weight = len(risks) + 0.5 * pub + (2 if (cls_name or public) in exports else 0) + 4 * len(sus)
            units.append(u)

        def chunk(nodes):
            groups, cur, size = [], [], 0
            for n in nodes:
                a, b = _span(n)
                ln = b - a + 1
                if cur and size + ln > MAX_UNIT_LINES:
                    groups.append(cur)
                    cur, size = [], 0
                cur.append(n)
                size += ln
            if cur:
                groups.append(cur)
            return groups

        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                methods = [n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
                for group in chunk(methods):
                    emit(node.name, group, node)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                free.append(node)
        for group in chunk(free):
            emit(None, group)
    return units


# ---------------------------------------------------------------- documentation and existing tests

_NOISE = re.compile(r"^\s*(\.\. (image|figure)::|:target:|:alt:|:width:|:height:|\.\. _[^:]+: http)")
_DIRECTIVE = re.compile(r"^\s*\.\.\s+(?:auto|py:)?(?:class|function|method|decorator|attribute|data|exception|module)::\s*([\w.]+)", re.M)


def doc_sections(workspace: Path) -> list[tuple[str, str]]:
    files = [p for p in Path(workspace).glob("*") if p.suffix.lower() in (".rst", ".md", ".txt") and p.is_file()]
    d = Path(workspace) / "docs"
    if d.is_dir():
        files += [p for p in d.rglob("*") if p.suffix.lower() in (".rst", ".md", ".txt")]
    sections = []
    for p in sorted(files):
        if p.name.lower().startswith(("changes", "changelog", "history", "license", "authors", "contributing")):
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        text = "\n".join(line for line in text.splitlines() if not _NOISE.match(line))
        paras = re.split(r"\n\s*\n", text)
        cur = ""
        for para in paras:
            starts_api = bool(_DIRECTIVE.match(para))
            if cur and (len(cur) + len(para) > 900 or starts_api):
                sections.append((p.name, cur))
                cur = ""
            cur += para.strip("\n") + "\n\n"
        if cur.strip():
            sections.append((p.name, cur))
    return sections


def docs_for(unit: Unit, sections: list[tuple[str, str]], budget_chars: int = 4500) -> str:
    keys = [unit.cls] if unit.cls else []
    keys += [m for m in unit.members if not m.startswith("_")]
    keys += [m.strip("_") for m in unit.members if m.startswith("__") and m.endswith("__")]
    keys = [k for k in dict.fromkeys(keys) if k and len(k) > 2]
    if not keys:
        return ""
    scored = []
    for i, (name, sec) in enumerate(sections):
        score = 0.0
        for k in keys:
            n = len(re.findall(rf"\b{re.escape(k)}\b", sec))
            score += n * (3 if k == unit.cls else 1)
        named = {m.split(".")[-1] for m in _DIRECTIVE.findall(sec)}
        if named & set(keys):
            score += 12
        if re.search(rf"^\s*\.\.\s+(?:py:)?(?:auto)?module::\s*{re.escape(unit.module)}\s*$", sec, re.M):
            score += 15                  # the module's introduction explains its shared conventions
        if score:
            scored.append((score, i, name, sec))
    scored.sort(key=lambda t: (-t[0], t[1]))
    chosen: dict[int, str] = {}
    used = 0
    for score, i, name, sec in scored:
        group = [i]
        if {m.split(".")[-1] for m in _DIRECTIVE.findall(sec)} & set(keys) and i + 1 < len(sections) \
                and sections[i + 1][0] == name and not _DIRECTIVE.match(sections[i + 1][1]):
            group.append(i + 1)          # the paragraphs that follow an API directive explain it
        for j in group:
            if j in chosen:
                continue
            text = sections[j][1]
            if used + len(text) > budget_chars:
                continue
            chosen[j] = f"[{sections[j][0]}]\n{text.strip()}"
            used += len(text)
    return "\n\n".join(chosen[j] for j in sorted(chosen))


def existing_tests_for(workspace: Path, unit: Unit, max_lines: int = 45) -> str:
    tdir = Path(workspace) / "tests"
    if not tdir.is_dir():
        return ""
    keys = [unit.cls] if unit.cls else [m for m in unit.members if not m.startswith("_")]
    keys = [k for k in keys if k]
    best = ("", 0)
    for p in sorted(tdir.rglob("test*.py")):
        text = p.read_text(encoding="utf-8", errors="replace")
        n = sum(text.count(k) for k in keys)
        if n > best[1]:
            best = (str(p.relative_to(workspace)), n)
    if not best[0]:
        return ""
    lines = (Path(workspace) / best[0]).read_text(encoding="utf-8", errors="replace").splitlines()
    # header (imports, helpers) + first test touching a key
    head = []
    for ln in lines[:40]:
        head.append(ln)
        if re.match(r"\s*(def test|class Test)", ln):
            head.pop()
            break
    first = next((i for i, ln in enumerate(lines) if re.match(r"\s*def test", ln) and
                  any(k in "\n".join(lines[i:i + 15]) for k in keys)), None)
    body = lines[first:first + 18] if first is not None else []
    snippet = "\n".join(head[:max_lines - len(body)] + ["..."] + body)
    return f"# {best[0]}\n{snippet}"
