"""Bugs as small text edits to a project's source files.

A bug replaces one span of bytes in one file. Spans never overlap, so any set of bugs can be applied
together, and a single bug can be applied to the correct code on its own.
"""

import ast
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

COMPARE = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.Eq: "==", ast.NotEq: "!=",
           ast.In: "in", ast.NotIn: "not in", ast.Is: "is", ast.IsNot: "is not"}
COMPARE_SWAPS = {"<": "<=", "<=": "<", ">": ">=", ">=": ">", "==": "!=", "!=": "==",
                 "in": "not in", "not in": "in", "is": "is not", "is not": "is"}
BINOP = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.FloorDiv: "//", ast.Div: "/", ast.Mod: "%"}
BINOP_SWAPS = {"+": "-", "-": "+", "*": "+", "//": "*", "/": "*", "%": "//"}
BOOL_SWAPS = {"and": "or", "or": "and"}


@dataclass
class Bug:
    """One edit: in `file`, bytes start:end hold `original` and become `replacement`."""

    id: str
    file: str
    line: int
    operator: str
    start: int
    end: int
    original: str
    replacement: str
    function: str = ""


def _line_offsets(data: bytes) -> list[int]:
    offsets = [0]
    for index, byte in enumerate(data):
        if byte == 10:
            offsets.append(index + 1)
    return offsets


def _between(data: bytes, offsets: list[int], left: ast.AST, right: ast.AST) -> tuple[int, int]:
    return offsets[left.end_lineno - 1] + left.end_col_offset, offsets[right.lineno - 1] + right.col_offset


def _token_span(data: bytes, start: int, end: int, token: str) -> tuple[int, int] | None:
    segment = data[start:end].decode()
    stripped = segment.replace("(", " ").replace(")", " ").replace("\\\n", " ")
    if " ".join(stripped.split()) != token:
        return None
    at = segment.find(token)
    return start + len(segment[:at].encode()), start + len(segment[:at].encode()) + len(token.encode())


def _functions(tree: ast.AST) -> list[tuple[int, int, str]]:
    found = []

    def visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                if not isinstance(child, ast.ClassDef):
                    found.append((child.lineno, child.end_lineno, name))
                visit(child, name + ".")
            else:
                visit(child, prefix)

    visit(tree, "")
    return found


def candidates(path: Path, relpath: str) -> list[Bug]:
    """Every bug the operators can make in one source file."""
    data = path.read_bytes()
    tree = ast.parse(data)
    offsets = _line_offsets(data)
    functions = _functions(tree)
    docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                  if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.body and isinstance(n.body[0], ast.Expr)}
    edits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for index, op in enumerate(node.ops):
                left = node.left if index == 0 else node.comparators[index - 1]
                token = COMPARE.get(type(op))
                if token:
                    span = _token_span(data, *_between(data, offsets, left, node.comparators[index]), token)
                    if span:
                        edits.append((node.lineno, "comparison", span, token, COMPARE_SWAPS[token]))
        elif isinstance(node, ast.BinOp) and type(node.op) in BINOP:
            token = BINOP[type(node.op)]
            span = _token_span(data, *_between(data, offsets, node.left, node.right), token)
            if span:
                edits.append((node.lineno, "arithmetic", span, token, BINOP_SWAPS[token]))
        elif isinstance(node, ast.AugAssign) and type(node.op) in BINOP:
            token = BINOP[type(node.op)] + "="
            span = _token_span(data, *_between(data, offsets, node.target, node.value), token)
            if span:
                edits.append((node.lineno, "arithmetic", span, token, BINOP_SWAPS[token[:-1]] + "="))
        elif isinstance(node, ast.BoolOp):
            token = "and" if isinstance(node.op, ast.And) else "or"
            span = _token_span(data, *_between(data, offsets, node.values[0], node.values[1]), token)
            if span:
                edits.append((node.lineno, "boolean", span, token, BOOL_SWAPS[token]))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            start = offsets[node.lineno - 1] + node.col_offset
            end = offsets[node.operand.lineno - 1] + node.operand.col_offset
            if data[start:end].decode().strip() == "not":
                edits.append((node.lineno, "remove_not", (start, end), data[start:end].decode(), ""))
        elif isinstance(node, ast.Constant) and type(node.value) is int and id(node) not in docstrings:
            start = offsets[node.lineno - 1] + node.col_offset
            end = offsets[node.end_lineno - 1] + node.end_col_offset
            text = data[start:end].decode()
            if text.isdigit():
                edits.append((node.lineno, "constant", (start, end), text, str(node.value + 1)))
                if node.value > 0:
                    edits.append((node.lineno, "constant", (start, end), text, str(node.value - 1)))
    bugs = []
    for line, operator, (start, end), original, replacement in edits:
        function = next((name for first, last, name in reversed(functions) if first <= line <= last), "")
        if not function:
            continue
        digest = hashlib.sha1(f"{relpath}:{start}:{replacement}".encode()).hexdigest()[:8]
        bugs.append(Bug(f"{Path(relpath).stem}-{line}-{digest}", relpath, line, operator, start, end,
                        original, replacement, function))
    return bugs


def apply(root: Path, bugs: list[Bug]) -> None:
    """Apply bugs in place to the project at root."""
    by_file: dict[str, list[Bug]] = {}
    for bug in bugs:
        by_file.setdefault(bug.file, []).append(bug)
    for relpath, file_bugs in by_file.items():
        path = root / relpath
        data = path.read_bytes()
        for bug in sorted(file_bugs, key=lambda b: b.start, reverse=True):
            if data[bug.start:bug.end].decode() != bug.original:
                raise ValueError(f"bug {bug.id} does not match {relpath}")
            data = data[:bug.start] + bug.replacement.encode() + data[bug.end:]
        path.write_bytes(data)


def load(path: Path) -> list[Bug]:
    return [Bug(**item) for item in json.loads(Path(path).read_text())]


def save(path: Path, bugs: list[Bug]) -> None:
    Path(path).write_text(json.dumps([asdict(b) for b in bugs], indent=1))
