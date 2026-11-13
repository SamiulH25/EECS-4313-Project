"""Specification mining from documentation examples (technique: spec mining / doc-derived oracles).

Docstrings and the project's .rst/.md docs often contain interactive examples (`>>> expr` followed by
the expected output). Those expected outputs were written by the project's authors against the
correct code, so they are oracles that do not depend on the (possibly buggy) code we can run.

For every example that shows an expected output or exception, we emit one pytest test that replays
the earlier examples of the same docstring as setup and then checks only that example. One check
per test means a failure on an early example cannot hide an unverified later one.
"""

from __future__ import annotations

import ast
import doctest
import re
from dataclasses import dataclass, field
from pathlib import Path

HEADER = '''"""Tests mined from documentation examples (expected outputs written by the project's authors)."""
import builtins
import contextlib
import doctest

import pytest


class _Out:
    def __init__(self):
        self.parts = []

    def write(self, text):
        self.parts.append(text)
        return len(text)

    def flush(self):
        pass


_FLAGS = doctest.ELLIPSIS | doctest.NORMALIZE_WHITESPACE


def _match(got, want, flags=0):
    checker = doctest.OutputChecker()
    assert checker.check_output(want, got, _FLAGS | flags), (
        "documented output differs\\n--- expected ---\\n" + want + "--- got ---\\n" + got)
'''


@dataclass
class Example:
    source: str
    want: str
    exc_msg: str | None
    options: dict
    lineno: int


@dataclass
class DocBlock:
    owner: str            # e.g. "sortedlist.SortedList.add" or "README.rst"
    module: str | None    # dotted module whose globals the examples may use
    examples: list = field(default_factory=list)


def _docstrings(path: Path, dotted: str):
    """Yield (qualname, docstring) for a module, its classes, functions and methods."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return
    doc = ast.get_docstring(tree, clean=False)
    if doc:
        yield dotted, doc

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}.{child.name}"
                d = ast.get_docstring(child, clean=False)
                if d:
                    yield name, d
                if isinstance(child, ast.ClassDef):
                    yield from walk(child, name)
    yield from walk(tree, dotted)


def _parse_examples(text: str) -> list[Example]:
    try:
        raw = doctest.DocTestParser().get_examples(text)
    except ValueError:
        return []
    out = []
    for ex in raw:
        out.append(Example(ex.source, ex.want, ex.exc_msg, dict(ex.options), ex.lineno))
    return out


def collect_blocks(workspace: Path, package: str) -> list[DocBlock]:
    blocks: list[DocBlock] = []
    pkg_dir = workspace / package
    for path in sorted(pkg_dir.rglob("*.py")):
        rel = path.relative_to(workspace).with_suffix("")
        dotted = ".".join(rel.parts)
        if dotted.endswith(".__init__"):
            dotted = dotted[: -len(".__init__")]
        for qual, doc in _docstrings(path, dotted):
            examples = _parse_examples(doc)
            if examples:
                blocks.append(DocBlock(qual, dotted, examples))
    doc_files = [p for p in workspace.glob("*") if p.suffix.lower() in (".rst", ".md", ".txt")]
    doc_dir = workspace / "docs"
    if doc_dir.is_dir():
        doc_files += [p for p in doc_dir.rglob("*") if p.suffix.lower() in (".rst", ".md", ".txt")]
    for path in sorted(doc_files):
        text = path.read_text(encoding="utf-8", errors="replace")
        examples = _parse_examples(text)
        if examples:
            blocks.append(DocBlock(str(path.relative_to(workspace)), None, examples))
    return blocks


def _valid(src: str) -> bool:
    try:
        ast.parse(src)
        return True
    except SyntaxError:
        return False


def _names(src: str) -> set[str]:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return set()
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}


def _module_globals(workspace: Path, dotted: str) -> set[str]:
    rel = Path(*dotted.split("."))
    for cand in (workspace / rel.with_suffix(".py"), workspace / rel / "__init__.py"):
        if cand.exists():
            try:
                tree = ast.parse(cand.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                return set()
            names = set()
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.add(node.name)
                elif isinstance(node, ast.Assign):
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            names.add(t.id)
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    for a in node.names:
                        if a.name != "*":
                            names.add((a.asname or a.name).split(".")[0])
            return names
    return set()


def _indent(src: str, pad: str) -> str:
    return "\n".join(pad + line if line.strip() else line for line in src.rstrip("\n").splitlines())


SAFE_STDLIB = {"time", "math", "functools", "operator", "collections", "itertools", "random", "string",
                "re", "json", "decimal", "fractions", "datetime", "heapq", "bisect", "copy", "pickle",
                "statistics", "textwrap", "enum", "dataclasses", "typing", "array", "struct", "weakref"}

_BAD_WANT =re.compile(r"0x[0-9a-fA-F]{4,}|object at |<function |<bound method ")
_NONDET = re.compile(r"\brandom\b|\btime\(|datetime|\bid\(|hash\(|getrandbits|uuid")


def _exc_name(exc_msg: str) -> str:
    head = exc_msg.strip().split(":", 1)[0].strip()
    name = head.rsplit(".", 1)[-1]
    if re.fullmatch(r"[A-Za-z_]\w*", name or ""):
        return name
    return "Exception"


def _safe_ident(text: str) -> str:
    return re.sub(r"\W+", "_", text).strip("_")[:60] or "x"


def block_tests(block: DocBlock, block_index: int) -> list[tuple[str, str]]:
    """Return (test_name, test_source) pairs for one docstring/doc file."""
    tests = []
    setup: list[str] = []
    for i, ex in enumerate(block.examples):
        if any(v for k, v in ex.options.items() if k == doctest.SKIP):
            break
        if not _valid(ex.source):
            break  # the rest of this block may depend on it
        checkable = (ex.want.strip() or ex.exc_msg) and not _BAD_WANT.search(ex.want)
        if checkable and _NONDET.search(ex.source + "\n".join(setup)):
            checkable = False
        if checkable:
            name = f"test_doc_{block_index:03d}_{_safe_ident(block.owner.split('.')[-1])}_{i:02d}"
            body = ["    " + line for s in setup for line in s.rstrip("\n").splitlines()]
            if ex.exc_msg is not None:
                exc = _exc_name(ex.exc_msg)
                exc_ref = f"getattr(builtins, {exc!r}, Exception)"
                body.append(f"    with pytest.raises({exc_ref}):")
                body.append(_indent(ex.source, "        "))
            else:
                try:
                    tree = ast.parse(ex.source)
                    single_expr = len(tree.body) == 1 and isinstance(tree.body[0], ast.Expr)
                except SyntaxError:
                    single_expr = False
                body.append("    _o = _Out()")
                body.append("    with contextlib.redirect_stdout(_o):")
                if single_expr:
                    body.append(f"        _v = ({ex.source.strip()})")
                    body.append("        if _v is not None:")
                    body.append("            print(repr(_v))")
                else:
                    body.append(_indent(ex.source, "        "))
                body.append(f"    _match(''.join(_o.parts), {ex.want!r})")
            src = f"def {name}():\n    # from {block.owner}, example {i + 1}\n" + "\n".join(body) + "\n"
            if _valid(src):
                tests.append((name, src))
        # becomes setup for later examples
        if ex.exc_msg is not None:
            setup.append("try:\n" + _indent(ex.source, "    ") + "\nexcept Exception:\n    pass\n")
        else:
            setup.append(ex.source)
    return tests


def build_file(workspace: Path, package: str, max_tests: int = 400) -> tuple[str, int]:
    """Source of a test module with every mined doc example, and the number of tests in it."""
    blocks = collect_blocks(workspace, package)
    imports: dict[str, set[str]] = {}
    chunks = []
    count = 0
    for bi, block in enumerate(blocks):
        tests = block_tests(block, bi)
        if not tests:
            continue
        if block.module:
            used = set().union(*(_names(ex.source) for ex in block.examples))
            avail = _module_globals(workspace, block.module)
            wanted = used & avail
            if wanted:
                imports.setdefault(block.module, set()).update(wanted)
        for _, src in tests:
            chunks.append(src)
            count += 1
            if count >= max_tests:
                break
        if count >= max_tests:
            break
    lines = [HEADER]
    # Doc files rely on the documentation's own setup (for example Sphinx testsetup), which usually
    # imports the package's public names, so make those available everywhere.
    lines.append(f"try:\n    from {package} import *  # noqa: F401,F403\nexcept ImportError:\n    pass")
    used_all = set()
    for block in blocks:
        for ex in block.examples:
            used_all |= _names(ex.source)
    for mod in sorted(used_all & SAFE_STDLIB):
        lines.append(f"import {mod}")
    # Import each needed module-level name with an alias-free `from x import y`.
    seen = set()
    for module in sorted(imports):
        for name in sorted(imports[module]):
            if name in seen:
                continue
            seen.add(name)
            lines.append(f"try:\n    from {module} import {name}\nexcept ImportError:\n    pass")
    lines.append("")
    lines.extend(chunks)
    return "\n\n".join(lines) + "\n", count
