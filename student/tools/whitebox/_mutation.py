"""Single-point AST mutations of solution.py, shared by mutants and probe."""

import ast
import copy
import difflib
import hashlib
from dataclasses import dataclass

# Keep the tool standalone: task rules forbid importing harness internals.
BINOP_SWAPS = {
    ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Add,
    ast.FloorDiv: ast.Mult, ast.Div: ast.Mult, ast.Mod: ast.FloorDiv,
}
COMPARE_SWAPS = {
    ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.In: ast.NotIn, ast.NotIn: ast.In,
    ast.Is: ast.IsNot, ast.IsNot: ast.Is,
}
BOOLOP_SWAPS = {ast.And: ast.Or, ast.Or: ast.And}


@dataclass
class Mutant:
    id: str
    operator: str
    line: int
    source: str


def _sites(tree: ast.AST) -> list[tuple[ast.AST, str, object]]:
    sites = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.BinOp, ast.AugAssign)) and type(node.op) in BINOP_SWAPS:
            sites.append((node, "arithmetic", BINOP_SWAPS[type(node.op)]))
        elif isinstance(node, ast.Compare):
            for index, op in enumerate(node.ops):
                if type(op) in COMPARE_SWAPS:
                    sites.append((node, "comparison", (index, COMPARE_SWAPS[type(op)])))
        elif isinstance(node, ast.BoolOp) and type(node.op) in BOOLOP_SWAPS:
            sites.append((node, "boolean", BOOLOP_SWAPS[type(node.op)]))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            sites.append((node, "remove_not", None))
        elif isinstance(node, ast.Constant) and type(node.value) is int:
            sites.append((node, "constant_plus_one", node.value + 1))
            sites.append((node, "constant_minus_one", node.value - 1))
        elif isinstance(node, (ast.If, ast.While)):
            sites.append((node, "negate_condition", None))
        elif isinstance(node, ast.Break):
            sites.append((node, "break_to_continue", None))
        elif isinstance(node, ast.Continue):
            sites.append((node, "continue_to_break", None))
        elif isinstance(node, ast.Return) and node.value is not None:
            sites.append((node, "return_none", None))
        # Realistic-bug operators: forgotten calls, wrong running extreme, lost ordering or modulo.
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            sites.append((node, "delete_call", None))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in ("max", "min") and len(node.args) == 2 and not node.keywords:
                sites.append((node, "keep_arg", 0))
                sites.append((node, "keep_arg", 1))
                sites.append((node, "swap_extreme", None))
            elif node.func.id == "sorted" and len(node.args) == 1:
                sites.append((node, "keep_arg", 0))
        if isinstance(node, ast.Call) and node.keywords and any(k.arg in ("key", "reverse") for k in node.keywords):
            sites.append((node, "drop_sort_keywords", None))
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            sites.append((node, "drop_modulo", None))
        if (isinstance(node, (ast.List, ast.Tuple)) and isinstance(node.ctx, ast.Load)
                and 2 <= len(node.elts) <= 8 and not any(isinstance(e, ast.Starred) for e in node.elts)):
            for index in range(len(node.elts)):  # e.g. a direction list missing one move
                sites.append((node, "drop_element", index))
    return sites


class _Apply(ast.NodeTransformer):
    def __init__(self, target_index: int, operator: str, payload: object):
        self.target_index = target_index
        self.operator = operator
        self.payload = payload

    def generic_visit(self, node):
        node = super().generic_visit(node)
        if getattr(node, "_mutation_id", None) != self.target_index:
            return node
        op = self.operator
        if op in ("arithmetic", "boolean"):
            node.op = self.payload()
        elif op == "comparison":
            index, new_op = self.payload
            node.ops[index] = new_op()
        elif op == "remove_not":
            return node.operand
        elif op in ("constant_plus_one", "constant_minus_one"):
            return ast.copy_location(ast.Constant(self.payload), node)
        elif op == "negate_condition":
            node.test = ast.UnaryOp(op=ast.Not(), operand=node.test)
        elif op == "break_to_continue":
            return ast.copy_location(ast.Continue(), node)
        elif op == "continue_to_break":
            return ast.copy_location(ast.Break(), node)
        elif op == "return_none":
            node.value = ast.Constant(None)
        elif op == "delete_call":
            return ast.copy_location(ast.Pass(), node)
        elif op == "keep_arg":
            return node.args[self.payload]
        elif op == "swap_extreme":
            node.func = ast.Name(id="min" if node.func.id == "max" else "max", ctx=ast.Load())
        elif op == "drop_sort_keywords":
            node.keywords = [k for k in node.keywords if k.arg not in ("key", "reverse")]
        elif op == "drop_modulo":
            return node.left
        elif op == "drop_element":
            del node.elts[self.payload]
        return node


def generate_mutants(source: str, limit: int = 0) -> list[Mutant]:
    original_tree = ast.parse(source)
    seen = {ast.unparse(original_tree)}
    mutants = []
    for index, (node, operator, payload) in enumerate(_sites(original_tree)):
        tree = copy.deepcopy(original_tree)
        target = _sites(tree)[index][0]
        target._mutation_id = index
        mutated = _Apply(index, operator, payload).visit(tree)
        ast.fix_missing_locations(mutated)
        try:
            text = ast.unparse(mutated)
            compile(text, "<mutant>", "exec")
        except (SyntaxError, ValueError):
            continue
        if text in seen:
            continue
        seen.add(text)
        mutant_id = hashlib.sha1(text.encode()).hexdigest()[:10]
        mutants.append(Mutant(mutant_id, operator, getattr(node, "lineno", 0), text + "\n"))
        if limit and len(mutants) >= limit:
            break
    return mutants


def changed_line(original: list[str], mutated: list[str]) -> str:
    """The first line where mutant and original differ, as text."""
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, original, mutated).get_opcodes():
        if tag != "equal":
            return mutated[j1].strip() if j1 < j2 else "(removed) " + original[i1].strip()
    return "(no textual change)"


def describe(source: str, mutant) -> str:
    """Short label for logs: operator, line and the changed text."""
    original = ast.unparse(ast.parse(source)).splitlines()
    return f"{mutant.operator}@L{mutant.line}: {changed_line(original, mutant.source.splitlines())[:90]}"
