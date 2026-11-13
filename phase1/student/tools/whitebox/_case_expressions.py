"""Restricted expression evaluator for test case inputs."""

import ast
import json
import math
import multiprocessing
import resource

# Fixed resource limits: memory, CPU, wall-clock timeout, and serialized payload size.
MAX_MEMORY_BYTES = 512 * 1024 * 1024  # 512 MiB
MAX_CPU_SECONDS = 5
MAX_WALL_SECONDS = 10.0
MAX_OUTPUT_BYTES = 32 * 1024 * 1024  # 32 MiB serialized payload cap


class CaseExpressionError(Exception):
    """Raised when case expression parsing, validation, or evaluation fails."""



SAFE_FUNCTIONS = {
    "range": range,
    "list": list,
    "tuple": tuple,
    "dict": dict,
    "set": set,
    "enumerate": enumerate,
    "zip": zip,
    "reversed": reversed,
    "sorted": sorted,
    "len": len,
    "min": min,
    "max": max,
    "sum": sum,
    "abs": abs,
    "int": int,
    "str": str,
    "bool": bool,
    "all": all,
    "any": any,
    "next": next,
    "comb": math.comb,
    "gcd": math.gcd,
    "isqrt": math.isqrt,
}
SAFE_CALL_NAMES = frozenset(SAFE_FUNCTIONS)

ALLOWED_NODES = (
    ast.Expression, ast.Constant, ast.List, ast.Tuple, ast.Dict, ast.Set,
    ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.comprehension,
    ast.UnaryOp, ast.UAdd, ast.USub, ast.Not, ast.Invert,
    ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.LShift, ast.RShift, ast.BitOr, ast.BitXor, ast.BitAnd,
    ast.BoolOp, ast.And, ast.Or, ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE,
    ast.Gt, ast.GtE, ast.Is, ast.IsNot, ast.In, ast.NotIn,
    ast.Subscript, ast.Slice, ast.IfExp, ast.Name, ast.Load, ast.Store,
    ast.Call, ast.keyword, ast.Starred,
)


def _validate_ast(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, ALLOWED_NODES):
            raise CaseExpressionError(f"Disallowed expression node: {type(node).__name__}")
        if isinstance(node, ast.Name):
            if node.id.startswith("__") or node.id.endswith("__"):
                raise CaseExpressionError(f"Dunder identifier '{node.id}' is not allowed")
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise CaseExpressionError("Only direct named function calls are allowed")
            if node.func.id not in SAFE_CALL_NAMES:
                raise CaseExpressionError(f"Call to '{node.func.id}' is not allowed")
            for kw in node.keywords:
                if kw.arg and (kw.arg.startswith("__") or kw.arg.endswith("__")):
                    raise CaseExpressionError(f"Dunder keyword '{kw.arg}' is not allowed")


def _check_json_native(obj) -> None:
    # Stack-based traversal rejects non-native JSON types before IPC serialization
    stack = [obj]
    while stack:
        item = stack.pop()
        if item is None or isinstance(item, (str, bool)):
            continue
        if isinstance(item, int):
            continue
        if isinstance(item, float):
            if not math.isfinite(item):
                raise CaseExpressionError("Non-finite float values are not allowed")
            continue
        if isinstance(item, list):
            stack.extend(item)
            continue
        if isinstance(item, dict):
            for k, v in item.items():
                if not isinstance(k, str):
                    raise CaseExpressionError(f"Dict keys must be strings, got {type(k).__name__}")
                stack.append(v)
            continue
        raise CaseExpressionError(
            f"Output contains non-JSON-native type '{type(item).__name__}'; "
            "convert containers to list() and dict()"
        )


def _set_resource_limits() -> None:
    for kind, ceiling in [(resource.RLIMIT_AS, MAX_MEMORY_BYTES),
                          (resource.RLIMIT_CPU, MAX_CPU_SECONDS)]:
        soft, hard = resource.getrlimit(kind)
        limit = min([ceiling] + [v for v in (soft, hard) if v != resource.RLIM_INFINITY])
        try:
            resource.setrlimit(kind, (limit, limit))
        except (ValueError, OSError) as error:
            raise CaseExpressionError("Cannot enforce expression resource limits") from error


def _worker_entry(expr_text: str, conn) -> None:
    try:
        _set_resource_limits()
        try:
            tree = ast.parse(expr_text, mode="eval")
        except SyntaxError as e:
            conn.send(("error", f"Syntax error: {e.msg}"))
            return
        except ValueError as e:
            conn.send(("error", f"Invalid expression: {e}"))
            return

        _validate_ast(tree)

        code = compile(tree, "<case_expression>", "eval")
        result = eval(code, {"__builtins__": {}, **SAFE_FUNCTIONS})

        if not isinstance(result, list):
            conn.send(("error", f"Top-level expression must evaluate to a list, got {type(result).__name__}"))
            return

        _check_json_native(result)

        encoded = json.dumps(result, allow_nan=False)
        if len(encoded.encode("utf-8")) > MAX_OUTPUT_BYTES:
            conn.send(("error", f"Output exceeds maximum payload size of {MAX_OUTPUT_BYTES} bytes"))
            return

        conn.send(("ok", encoded))
    except CaseExpressionError as e:
        conn.send(("error", str(e)))
    except MemoryError:
        conn.send(("error", "Memory limit exceeded"))
    except ZeroDivisionError as e:
        conn.send(("error", f"Division by zero: {e}"))
    except Exception as e:
        conn.send(("error", f"Evaluation failed: {type(e).__name__}: {e}"))
    finally:
        try:
            conn.close()
        except Exception:
            pass


def evaluate_cases(text: str) -> list:
    if not isinstance(text, str):
        raise CaseExpressionError("Input must be a string expression")
    text = text.strip()
    if not text:
        raise CaseExpressionError("Expression cannot be empty")

    ctx = multiprocessing.get_context("fork")
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    worker = ctx.Process(target=_worker_entry, args=(text, child_conn))
    try:
        worker.start()
        child_conn.close()
        if not parent_conn.poll(MAX_WALL_SECONDS):
            raise CaseExpressionError(f"Expression evaluation exceeded timeout ({MAX_WALL_SECONDS}s)")
        try:
            status, payload = parent_conn.recv()
        except (EOFError, OSError) as error:
            raise CaseExpressionError("Expression worker terminated before returning a result") from error
        if status != "ok":
            raise CaseExpressionError(payload)
        return json.loads(payload)
    finally:
        child_conn.close()
        parent_conn.close()
        if worker.pid is not None:
            worker.join(timeout=0.1)
            if worker.is_alive():
                worker.kill()
                worker.join()
