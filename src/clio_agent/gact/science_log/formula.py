"""A small, safe arithmetic formula language: ``<name> = <expression>``.

The same language the Science log tab reads (gact-tui ``formula-expression.ts``): numbers,
field names, ``+ - * /``, powers (``^`` or ``**``, right-associative), parentheses, the
constant ``pi``, the one-argument functions ``sqrt exp log abs`` (``log`` is natural) and
``min max`` (two or more arguments). Anything else is refused with a typed error. The text
is tokenized and parsed here, never handed to ``eval``.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

#: Longest formula text accepted. Unit: characters.
FORMULA_MAX_LENGTH = 2_000
#: Deepest nesting accepted, so a hostile text cannot exhaust the stack.
FORMULA_MAX_DEPTH = 64

_UNARY = frozenset({"sqrt", "exp", "log", "abs"})
_VARIADIC = frozenset({"min", "max"})
_NUMBER = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class FormulaError(ValueError):
    """Why a text is not a formula: ``code`` is stable, ``at`` the character offset."""

    def __init__(self, code: str, message: str, at: int) -> None:
        super().__init__(message)
        self.code = code
        self.at = at


@dataclass(frozen=True)
class Formula:
    """A parsed formula: the measured field it predicts and the expression over names."""

    target: str
    expression: tuple[Any, ...]
    names: tuple[str, ...] = field(default=())


def _tokens(text: str) -> list[tuple[str, Any, int]]:
    tokens: list[tuple[str, Any, int]] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char in " \t\r\n":
            index += 1
            continue
        if char.isdigit() or char == ".":
            match = _NUMBER.match(text, index)
            if match is None:
                raise FormulaError("invalid_number", f'"{char}" does not start a number.', index)
            end = match.end()
            if end < len(text) and (text[end].isalnum() or text[end] in "_."):
                raise FormulaError(
                    "invalid_number", f'"{text[index : end + 1]}" is not a number.', index
                )
            tokens.append(("number", float(match.group()), index))
            index = end
            continue
        match = _NAME.match(text, index)
        if match is not None:
            tokens.append(("name", match.group(), index))
            index = match.end()
            continue
        if text.startswith("**", index):
            tokens.append(("symbol", "^", index))
            index += 2
            continue
        if char in "+-*/^(),=":
            tokens.append(("symbol", char, index))
            index += 1
            continue
        raise FormulaError(
            "unexpected_character", f'"{char}" is not part of the formula language.', index
        )
    tokens.append(("end", None, len(text)))
    return tokens


class _Parser:
    def __init__(self, tokens: list[tuple[str, Any, int]]) -> None:
        self.tokens = tokens
        self.position = 0
        self.depth = 0
        self.names: list[str] = []

    def peek(self) -> tuple[str, Any, int]:
        return self.tokens[self.position]

    def next(self) -> tuple[str, Any, int]:
        token = self.tokens[self.position]
        if token[0] != "end":
            self.position += 1
        return token

    def is_symbol(self, value: str) -> bool:
        kind, token, _ = self.peek()
        return kind == "symbol" and token == value

    def enter(self, at: int) -> None:
        self.depth += 1
        if self.depth > FORMULA_MAX_DEPTH:
            raise FormulaError(
                "too_deep", f"The formula nests deeper than {FORMULA_MAX_DEPTH} levels.", at
            )

    @staticmethod
    def describe(token: tuple[str, Any, int]) -> str:
        return "the end" if token[0] == "end" else f'"{token[1]}"'

    def expect(self, value: str) -> None:
        token = self.next()
        if token[0] == "symbol" and token[1] == value:
            return
        code = "unexpected_end" if token[0] == "end" else "unexpected_token"
        raise FormulaError(code, f'Expected "{value}" but found {self.describe(token)}.', token[2])

    def equation(self) -> Formula:
        kind, target, _ = self.next()
        if kind != "name" or target == "pi" or target in _UNARY | _VARIADIC:
            raise FormulaError(
                "not_an_equation", 'A formula starts with the measured field\'s name, then "=".', 0
            )
        if not self.is_symbol("="):
            raise FormulaError(
                "not_an_equation", 'A formula is "<measured field> = <expression>".', self.peek()[2]
            )
        self.next()
        expression = self.sum()
        end = self.peek()
        if end[0] != "end":
            raise FormulaError(
                "unexpected_token", f"Unexpected {self.describe(end)} after the expression.", end[2]
            )
        return Formula(target, expression, tuple(self.names))

    def sum(self) -> tuple[Any, ...]:
        left = self.product()
        while self.is_symbol("+") or self.is_symbol("-"):
            operator = self.next()[1]
            left = ("binary", operator, left, self.product())
        return left

    def product(self) -> tuple[Any, ...]:
        left = self.unary()
        while self.is_symbol("*") or self.is_symbol("/"):
            operator = self.next()[1]
            left = ("binary", operator, left, self.unary())
        return left

    def unary(self) -> tuple[Any, ...]:
        """A sign binds looser than a power: ``-x^2`` is ``-(x^2)``."""

        if self.is_symbol("-") or self.is_symbol("+"):
            _, sign, at = self.next()
            self.enter(at)
            argument = self.unary()
            self.depth -= 1
            return ("negate", argument) if sign == "-" else argument
        return self.power()

    def power(self) -> tuple[Any, ...]:
        base = self.primary()
        if not self.is_symbol("^"):
            return base
        at = self.next()[2]
        self.enter(at)
        exponent = self.unary()  # right-associative; the exponent may carry a sign
        self.depth -= 1
        return ("binary", "^", base, exponent)

    def primary(self) -> tuple[Any, ...]:
        token = self.next()
        kind, value, at = token
        if kind == "number":
            return ("number", value)
        if kind == "symbol" and value == "(":
            self.enter(at)
            inner = self.sum()
            self.expect(")")
            self.depth -= 1
            return inner
        if kind == "name":
            if self.is_symbol("("):
                return self.call(value, at)
            if value == "pi":
                return ("pi",)
            if value in _UNARY | _VARIADIC:
                raise FormulaError(
                    "reserved_name", f'"{value}" is a function; call it with parentheses.', at
                )
            if value not in self.names:
                self.names.append(value)
            return ("name", value)
        code = "unexpected_end" if kind == "end" else "unexpected_token"
        raise FormulaError(
            code, f'Expected a number, a name or "(" but found {self.describe(token)}.', at
        )

    def call(self, name: str, at: int) -> tuple[Any, ...]:
        if name not in _UNARY | _VARIADIC:
            raise FormulaError("unknown_function", f'"{name}" is not a known function.', at)
        self.next()
        self.enter(at)
        args: list[tuple[Any, ...]] = []
        if not self.is_symbol(")"):
            args.append(self.sum())
            while self.is_symbol(","):
                self.next()
                args.append(self.sum())
        self.expect(")")
        self.depth -= 1
        unary = name in _UNARY
        if (len(args) != 1) if unary else (len(args) < 2):
            wanted = "one argument" if unary else "two or more arguments"
            raise FormulaError("wrong_arity", f"{name} takes {wanted}, not {len(args)}.", at)
        return ("call", name, tuple(args))


def parse_formula(text: str) -> Formula:
    """Parse ``<name> = <expression>``; :class:`FormulaError` says exactly why not."""

    source = str(text or "").strip()
    if not source:
        raise FormulaError("empty", "The formula is empty.", 0)
    if len(text) > FORMULA_MAX_LENGTH:
        raise FormulaError(
            "too_long",
            f"The formula is longer than {FORMULA_MAX_LENGTH} characters.",
            FORMULA_MAX_LENGTH,
        )
    return _Parser(_tokens(source)).equation()


def _evaluate(node: tuple[Any, ...], values: dict[str, float]) -> float:
    kind = node[0]
    if kind == "number":
        return float(node[1])
    if kind == "pi":
        return math.pi
    if kind == "name":
        return float(values.get(node[1], math.nan))
    if kind == "negate":
        return -_evaluate(node[1], values)
    if kind == "call":
        args = [_evaluate(arg, values) for arg in node[2]]
        name = node[1]
        try:
            if name == "sqrt":
                return math.sqrt(args[0])
            if name == "exp":
                return math.exp(args[0])
            if name == "log":
                return math.log(args[0])
            if name == "abs":
                return abs(args[0])
            return min(args) if name == "min" else max(args)
        except (ValueError, OverflowError):
            return math.nan
    left, right = _evaluate(node[2], values), _evaluate(node[3], values)
    operator = node[1]
    try:
        if operator == "+":
            return left + right
        if operator == "-":
            return left - right
        if operator == "*":
            return left * right
        if operator == "/":
            return left / right if right else math.nan
        result = left**right
        return result if isinstance(result, float) else math.nan
    except (OverflowError, ZeroDivisionError):
        return math.nan


def evaluate_formula(formula: Formula, values: dict[str, float]) -> float | None:
    """The expression's value, or ``None`` when a name has no value or it is not finite."""

    value = _evaluate(formula.expression, values)
    return value if math.isfinite(value) else None


__all__ = [
    "FORMULA_MAX_DEPTH",
    "FORMULA_MAX_LENGTH",
    "Formula",
    "FormulaError",
    "evaluate_formula",
    "parse_formula",
]
