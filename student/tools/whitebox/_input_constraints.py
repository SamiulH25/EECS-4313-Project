"""Conservative checks for a recognizable subset of SPEC.md constraints.

Recognized and enforced:
  * numeric comparisons over parameters, ``x.length``, ``x[i].length``,
    ``x[i]``, ``x[i][j]`` and ``x[i][0]`` terms, including chained equalities
    such as ``n == nums.length == grid[i].length``;
  * component bindings such as ``events[i] == [index_i, time_i]`` so later
    ``1 <= index_i, time_i <= 10^5`` applies to those positions;
  * "x and y consist of the same number of digits";
  * one explicit non-overlap sentence for the single shaped list it can
    identify: closed intervals (adjacent positions) or rectangles.

Everything else, including prose that does not match this grammar, is ignored
rather than guessed. Values with an unexpected shape or type are never
rejected by an inferred reading.

shortcut: rectangle overlap is checked pairwise only up to 1000 rectangles.
"""

import re

_NUMBER = r"-?\d+(?:\s*\*\s*10\s*\^\s*\d+|\s*\^\s*\d+)?"
_NUMBER_RE = re.compile(_NUMBER + r"\Z")
_TERM_RE = re.compile(r"([A-Za-z_]\w*)((?:\[(?:\d+|[a-z])\])*)(\.length)?\Z")
_INDEX_RE = re.compile(r"\[(\d+|[a-z])\]")
_BINDING_RE = re.compile(r"([A-Za-z_]\w*)\[i\]\s*==?\s*\[([^\]]*)\]")
_DIGITS_RE = re.compile(
    r"([A-Za-z_]\w*)\s+and\s+([A-Za-z_]\w*)[^.]*?same\s+number\s+of\s+digits"
)
_OVERLAP_RE = re.compile(
    r"non[-\s]?overlap|(?:do|does)\s+not\s+overlap|no\s+two\s+\w+\s+overlap", re.I
)
_QUALIFIER_RE = re.compile(r"\b(?:for all|for each|for every|where|such that)\b", re.I)
_OPERATOR_RE = re.compile(r"<=|>=|==|<|>")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+")
_RECTANGLE_LIMIT = 1000
_OPERATORS = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
}


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _number(text: str):
    compact = text.replace(" ", "")
    if not _NUMBER_RE.match(compact):
        return None
    if "*10^" in compact:
        base, exponent = compact.split("*10^")
        return int(base) * 10 ** int(exponent)
    if "^" in compact:
        base, exponent = compact.split("^")
        magnitude = abs(int(base)) ** int(exponent)
        return -magnitude if base.startswith("-") else magnitude
    return int(compact)


def _constraint_lines(spec: str) -> list[str]:
    lines = spec.splitlines()
    start = None
    for index, line in enumerate(lines):
        if re.match(r"^\s*(?:#{1,6}\s*)?(?:\*\*)?Constraints\b", line, re.I):
            start = index + 1
    if start is None:
        return []
    cleaned = []
    for line in lines[start:]:
        line = _BULLET_RE.sub("", line).strip().strip("`").strip()
        if line:
            cleaned.append(line)
    return cleaned


def _term(text: str, components: dict):
    text = text.strip()
    value = _number(text)
    if value is not None:
        return ("num", value)
    match = _TERM_RE.match(text)
    if not match:
        return None
    name, index_text, length = match.groups()
    indices = tuple(
        item if item.isalpha() else int(item) for item in _INDEX_RE.findall(index_text)
    )
    if name in components:
        # A component alias such as l_i stands for one position of entries[i].
        if indices or length:
            return None
        param, position = components[name]
        return ("leaf", param, ("i", position))
    if length:
        if not indices:
            return ("len", name)
        if len(indices) == 1 and isinstance(indices[0], str):
            return ("rowlen", name)
        return None
    if not indices:
        return ("scalar", name)
    if not isinstance(indices[0], str) or any(isinstance(item, int) for item in indices[:-1]):
        return None
    letters = [item for item in indices if isinstance(item, str)]
    if len(letters) != len(set(letters)):
        # x[i][i] is a diagonal, not every cell; do not guess its meaning.
        return None
    return ("leaf", name, indices)


def _leaf_values(container, indices):
    def walk(node, position):
        if position == len(indices):
            return [node] if _is_int(node) else None
        index = indices[position]
        if not isinstance(node, (list, tuple)):
            return None
        if isinstance(index, str):
            values = []
            for child in node:
                found = walk(child, position + 1)
                if found is None:
                    return None
                values.extend(found)
            return values
        if index >= len(node):
            return None
        return walk(node[index], position + 1)

    return walk(container, 0)


def _values(term, arguments: dict):
    kind = term[0]
    if kind == "num":
        return [term[1]]
    name = term[1]
    if name not in arguments:
        return None
    value = arguments[name]
    if kind == "scalar":
        return [value] if _is_int(value) else None
    if kind == "len":
        return [len(value)] if isinstance(value, (str, list, tuple)) else None
    if kind == "rowlen":
        if not isinstance(value, (list, tuple)):
            return None
        if not all(isinstance(row, (str, list, tuple)) for row in value):
            return None
        return [len(row) for row in value]
    return _leaf_values(value, term[2])


class ConstraintSet:
    """Atoms and relations compiled once, checked per candidate input."""

    def __init__(self, atoms, digit_pairs, shapes, overlap_line):
        self.atoms = atoms
        self.digit_pairs = digit_pairs
        self.shapes = shapes
        self.overlap_line = overlap_line

    def violation(self, arguments: dict):
        for op, left, right, line in self.atoms:
            left_values, right_values = _values(left, arguments), _values(right, arguments)
            if left_values is None or right_values is None:
                continue
            if len(left_values) == 1 and len(right_values) > 1:
                left_values = left_values * len(right_values)
            elif len(right_values) == 1 and len(left_values) > 1:
                right_values = right_values * len(left_values)
            if len(left_values) != len(right_values):
                continue
            compare = _OPERATORS[op]
            if any(not compare(a, b) for a, b in zip(left_values, right_values)):
                return f"violates SPEC constraint `{line}`"
        for first, second in self.digit_pairs:
            left, right = arguments.get(first), arguments.get(second)
            if not (_is_int(left) and _is_int(right)):
                continue
            if len(str(abs(left))) != len(str(abs(right))):
                return f"`{first}` and `{second}` must have the same number of digits"
        if self.overlap_line:
            problem = _overlap_violation(self.shapes, arguments)
            if problem:
                return f"{problem} (constraint `{self.overlap_line}`)"
        return None


def _overlap_violation(shapes: dict, arguments: dict):
    candidates = []
    for param, pairs in shapes.items():
        entries = arguments.get(param)
        if not isinstance(entries, (list, tuple)) or not entries:
            continue
        if len(pairs) not in (1, 2):
            continue
        positions = [position for pair in pairs for position in pair]
        if len(set(positions)) != len(positions):
            continue
        width = max(positions) + 1
        if not all(
            isinstance(entry, (list, tuple))
            and len(entry) >= width
            and all(_is_int(value) for value in entry[:width])
            for entry in entries
        ):
            continue
        candidates.append((param, pairs, entries))
    if len(candidates) != 1:
        return None
    param, pairs, entries = candidates[0]
    if len(pairs) == 1:
        start, end = next(iter(pairs))
        if end != start + 1:
            return None
        ordered = sorted((entry[start], entry[end]) for entry in entries)
        for (_, previous_end), (next_start, _) in zip(ordered, ordered[1:]):
            if next_start <= previous_end:
                return f"`{param}` entries overlap"
        return None
    if len(entries) > _RECTANGLE_LIMIT:
        return None
    (a_start, a_end), (b_start, b_end) = sorted(pairs)
    rectangles = [
        (entry[a_start], entry[a_end], entry[b_start], entry[b_end])
        for entry in entries
    ]
    if any(a_start >= a_end or b_start >= b_end for a_start, a_end, b_start, b_end in rectangles):
        return None
    for index, (a_start, a_end, b_start, b_end) in enumerate(rectangles):
        for c_start, c_end, d_start, d_end in rectangles[index + 1:]:
            if a_start < c_end and c_start < a_end and b_start < d_end and d_start < b_end:
                return f"`{param}` rectangles overlap"
    return None


def compile_constraints(spec: str) -> ConstraintSet:
    lines = _constraint_lines(spec or "")
    if not lines:
        return ConstraintSet([], [], {}, None)
    text = "\n".join(lines)

    components = {}
    for match in _BINDING_RE.finditer(text):
        names = [name.strip() for name in match.group(2).split(",")]
        if len(names) < 2 or not all(re.fullmatch(r"[A-Za-z_]\w*", name) for name in names):
            continue
        for position, name in enumerate(names):
            components.setdefault(name, (match.group(1), position))

    atoms, shapes = [], {}
    for line in lines:
        if _QUALIFIER_RE.search(line):
            continue
        line = line.rstrip(".")
        parts = _OPERATOR_RE.split(line)
        operators = _OPERATOR_RE.findall(line)
        if not operators:
            continue
        groups = []
        for part in parts:
            group = [_term(item, components) for item in part.split(",")]
            if any(term is None for term in group):
                break
            if len(group) > 1 and any(term[0] == "num" for term in group):
                # A comma inside a bound is a name list, not a second comparison chain.
                break
            groups.append(group)
        else:
            for index, operator in enumerate(operators):
                for left in groups[index]:
                    for right in groups[index + 1]:
                        atoms.append((operator, left, right, line))
                        if (
                            operator in ("<", "<=")
                            and left[0] == right[0] == "leaf"
                            and left[1] == right[1]
                            and left[2][:-1] == right[2][:-1]
                            and isinstance(left[2][-1], int)
                            and isinstance(right[2][-1], int)
                            and left[2][-1] != right[2][-1]
                        ):
                            shapes.setdefault(left[1], set()).add(
                                (left[2][-1], right[2][-1])
                            )

    digit_pairs = [tuple(match.groups()) for match in _DIGITS_RE.finditer(text)]
    overlap_line = next(
        (line for line in lines if _OVERLAP_RE.search(line)), None
    )
    return ConstraintSet(atoms, digit_pairs, shapes, overlap_line)
