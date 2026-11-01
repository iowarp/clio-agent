"""P3: seed five realistic bugs into a copied package, one per file.

Each category has a finder (sites in a parsed module where the bug can be
introduced) and a transform (new source text plus the bug's line range in the
changed file). A transform must leave the module parseable; a site whose
change fails that check is skipped. The answer key records file, line range,
category word, enclosing function and the before/after text.
"""

from __future__ import annotations

import ast
import random
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CATEGORIES = (
    "off-by-one",
    "swallowed-exception",
    "wrong-default",
    "unclosed-file",
    "inverted-condition",
)


@dataclass(frozen=True)
class Site:
    """One place a bug can go, with the change already computed."""

    file: str
    category: str
    new_source: str
    line_start: int
    line_end: int
    function: str
    before: str
    after: str


_NEGATE: dict[type, type] = {
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
}
_OFF_BY_ONE: dict[type, type] = {ast.Lt: ast.LtE, ast.LtE: ast.Lt}


def _enclosing(tree: ast.Module) -> Callable[[int], str]:
    spans = [
        (node.lineno, node.end_lineno or node.lineno, node.name)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]

    def lookup(line: int) -> str:
        inside = [s for s in spans if s[0] <= line <= s[1]]
        return min(inside, key=lambda s: s[1] - s[0])[2] if inside else ""

    return lookup


def _replace_segment(lines: list[str], node: ast.AST, text: str) -> list[str] | None:
    """Replace one single-line node's exact columns with ``text``."""
    if node.lineno != node.end_lineno:
        return None
    row = node.lineno - 1
    line = lines[row]
    start, end = node.col_offset, node.end_col_offset
    encoded = line.encode("utf-8")
    new_line = (encoded[:start] + text.encode("utf-8") + encoded[end:]).decode("utf-8")
    return [*lines[:row], new_line, *lines[row + 1 :]]


def _segment(lines: list[str], node: ast.AST) -> str:
    encoded = lines[node.lineno - 1].encode("utf-8")
    return encoded[node.col_offset : node.end_col_offset].decode("utf-8")


_SYMBOL: dict[type, str] = {
    ast.Eq: "==",
    ast.NotEq: "!=",
    ast.Is: "is",
    ast.IsNot: "is not",
    ast.In: "in",
    ast.NotIn: "not in",
    ast.Lt: "<",
    ast.LtE: "<=",
}


def _compare_swap(lines: list[str], node: ast.Compare, table: dict[type, type]) -> list[str] | None:
    """Swap a single comparison operator in place, keeping the rest of the text as written."""
    if len(node.ops) != 1 or type(node.ops[0]) not in table:
        return None
    left, right = node.left, node.comparators[0]
    if not (node.lineno == node.end_lineno == left.end_lineno == right.lineno):
        return None
    encoded = lines[node.lineno - 1].encode("utf-8")
    between = encoded[left.end_col_offset : right.col_offset].decode("utf-8")
    old, new = _SYMBOL[type(node.ops[0])], _SYMBOL[table[type(node.ops[0])]]
    if between.strip() != old:
        return None  # parentheses or comments around the operator: leave this site
    text = encoded[node.col_offset : left.end_col_offset].decode("utf-8")
    text += between.replace(old, new, 1)
    text += encoded[right.col_offset : node.end_col_offset].decode("utf-8")
    return _replace_segment(lines, node, text)


def _inverted_condition(tree: ast.Module, lines: list[str]) -> Iterator[tuple[list[str], int, int]]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.While)) and isinstance(node.test, ast.Compare):
            changed = _compare_swap(lines, node.test, _NEGATE)
            if changed is not None:
                yield changed, node.lineno, node.lineno


def _off_by_one(tree: ast.Module, lines: list[str]) -> Iterator[tuple[list[str], int, int]]:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "range"
            and len(node.args) == 1
            and not node.keywords
        ):
            arg = node.args[0]
            text = _segment(lines, arg)
            wrapped = f"({text})" if isinstance(arg, ast.BinOp) else text
            changed = _replace_segment(lines, arg, f"{wrapped} - 1")
            if changed is not None:
                yield changed, node.lineno, node.lineno
        elif isinstance(node, ast.Slice) and node.upper is not None and node.lower is None:
            upper = node.upper
            if isinstance(upper, (ast.Name, ast.Call, ast.Attribute)):
                changed = _replace_segment(lines, upper, f"{_segment(lines, upper)} - 1")
                if changed is not None:
                    yield changed, upper.lineno, upper.lineno
        elif isinstance(node, ast.Compare) and "len(" in ast.unparse(node):
            changed = _compare_swap(lines, node, _OFF_BY_ONE)
            if changed is not None:
                yield changed, node.lineno, node.lineno


def _swallowed_exception(
    tree: ast.Module, lines: list[str]
) -> Iterator[tuple[list[str], int, int]]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or node.type is None or not node.body:
            continue
        last = node.body[-1]
        if not isinstance(last, ast.Raise) or last.exc is None or last.lineno - node.lineno > 5:
            continue
        indent = lines[last.lineno - 1][: last.col_offset]
        end = last.end_lineno or last.lineno
        changed = [*lines[: last.lineno - 1], f"{indent}pass", *lines[end:]]
        yield changed, node.lineno, last.lineno


def _wrong_default(tree: ast.Module, lines: list[str]) -> Iterator[tuple[list[str], int, int]]:
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for default in [*node.args.defaults, *(d for d in node.args.kw_defaults if d)]:
            if not isinstance(default, ast.Constant):
                continue
            value = default.value
            if isinstance(value, bool):
                text = repr(not value)
            elif isinstance(value, int) and value >= 2:
                text = "0"
            elif isinstance(value, float) and value > 0:
                text = "0.0"
            else:
                continue
            changed = _replace_segment(lines, default, text)
            if changed is not None:
                yield changed, default.lineno, default.lineno


def _is_open_call(node: ast.expr) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (isinstance(func, ast.Name) and func.id == "open") or (
        isinstance(func, ast.Attribute) and func.attr == "open"
    )


def _unclosed_file(tree: ast.Module, lines: list[str]) -> Iterator[tuple[list[str], int, int]]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.With) or len(node.items) != 1:
            continue
        item = node.items[0]
        target = item.optional_vars
        call = item.context_expr
        if not isinstance(target, ast.Name) or not _is_open_call(call):
            continue
        if call.lineno != node.lineno or call.end_lineno != node.lineno:
            continue
        end = node.end_lineno or node.lineno
        if node.body[0].lineno != node.lineno + 1:
            continue
        indent = lines[node.lineno - 1][: node.col_offset]
        inner = lines[node.body[0].lineno - 1][: node.body[0].col_offset]
        step = len(inner) - len(indent)
        body = lines[node.lineno : end]
        if step <= 0 or not all(not ln.strip() or ln.startswith(inner) for ln in body):
            continue
        header = f"{indent}{target.id} = {_segment(lines, call)}"
        dedented = [ln[step:] if ln.strip() else ln for ln in body]
        changed = [*lines[: node.lineno - 1], header, *dedented, *lines[end:]]
        yield changed, node.lineno, end


FINDERS: dict[str, Callable[[ast.Module, list[str]], Iterator[tuple[list[str], int, int]]]] = {
    "off-by-one": _off_by_one,
    "swallowed-exception": _swallowed_exception,
    "wrong-default": _wrong_default,
    "unclosed-file": _unclosed_file,
    "inverted-condition": _inverted_condition,
}


def find_sites(path: Path, rel: str, category: str) -> list[Site]:
    """Every valid site of ``category`` in one module."""
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    lines = source.split("\n")
    enclosing = _enclosing(tree)
    sites: list[Site] = []
    for changed, start, end in FINDERS[category](tree, lines):
        new_source = "\n".join(changed)
        try:
            ast.parse(new_source)
        except SyntaxError:
            continue
        if new_source == source:
            continue
        sites.append(
            Site(
                file=rel,
                category=category,
                new_source=new_source,
                line_start=start,
                line_end=end,
                function=enclosing(start),
                before="\n".join(lines[start - 1 : start - 1 + (end - start + 1)]),
                after="\n".join(changed[start - 1 : end]),
            )
        )
    return sites


def seed_bugs(root: Path, package: str, rng: random.Random) -> list[dict[str, Any]]:
    """Seed one bug per category in distinct files of ``root/package``; return the answer key.

    Recorded files are relative to ``root`` (the folder the agent works in).
    """
    files = sorted((root / package).rglob("*.py"))
    used: set[str] = set()
    seeded: list[dict[str, Any]] = []
    for number, category in enumerate(CATEGORIES, 1):
        pool: list[Site] = []
        for path in files:
            rel = path.relative_to(root).as_posix()
            if rel not in used:
                pool.extend(s for s in find_sites(path, rel, category) if s.function)
        if not pool:
            raise ValueError(f"no site for {category} in {root / package}")
        site = rng.choice(pool)
        used.add(site.file)
        (root / site.file).write_text(site.new_source, encoding="utf-8")
        seeded.append(
            {
                "id": f"B{number}",
                "category": category,
                "file": site.file,
                "line_start": site.line_start,
                "line_end": site.line_end,
                "function": site.function,
                "before": site.before,
                "after": site.after,
            }
        )
    return seeded
