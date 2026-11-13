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
        out.append((line, f"L{line}: `{text}` -> {hint}"))
    out.sort()
    return [t for _, t in out][:limit]


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
            u = Unit(uid=uid, module=dotted, path=str(rel), cls=cls_name,
                     members=[n.name for n in members_nodes], spans=spans,
                     source="\n".join(parts), risks=risks, import_hint=imp)
            if cls_node is not None:
                u.outline = "\n".join(outline(cls_node))[:3500]
            pub = sum(1 for m in u.members if not m.startswith("_") or m in ("__init__", "__getitem__", "__setitem__",
                                                                             "__delitem__", "__contains__", "__len__",
                                                                             "__iter__", "__eq__", "__lt__"))
            u.weight = len(risks) + 0.5 * pub + (2 if (cls_name or public) in exports else 0)
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

def doc_sections(workspace: Path) -> list[tuple[str, str]]:
    files = [p for p in Path(workspace).glob("*") if p.suffix.lower() in (".rst", ".md", ".txt") and p.is_file()]
    d = Path(workspace) / "docs"
    if d.is_dir():
        files += [p for p in d.rglob("*") if p.suffix.lower() in (".rst", ".md", ".txt")]
    sections = []
    for p in sorted(files):
        text = p.read_text(encoding="utf-8", errors="replace")
        # split on blank-line separated paragraphs, then regroup into ~900 char sections
        paras = re.split(r"\n\s*\n", text)
        cur = ""
        for para in paras:
            if len(cur) + len(para) > 900 and cur:
                sections.append((p.name, cur))
                cur = ""
            cur += para.strip("\n") + "\n\n"
        if cur.strip():
            sections.append((p.name, cur))
    return sections


def docs_for(unit: Unit, sections: list[tuple[str, str]], budget_chars: int = 2600) -> str:
    keys = [unit.cls] if unit.cls else []
    keys += [m for m in unit.members if not m.startswith("__")]
    keys = [k for k in keys if k and len(k) > 2]
    if not keys:
        return ""
    scored = []
    for i, (name, sec) in enumerate(sections):
        score = 0
        for k in keys:
            n = len(re.findall(rf"\b{re.escape(k)}\b", sec))
            score += n * (3 if k == unit.cls else 1)
        if score:
            scored.append((score, i, name, sec))
    scored.sort(key=lambda t: (-t[0], t[1]))
    out, used = [], 0
    for _, i, name, sec in scored:
        if used + len(sec) > budget_chars:
            continue
        out.append((i, f"[{name}]\n{sec.strip()}"))
        used += len(sec)
    out.sort()
    return "\n\n".join(s for _, s in out)


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
