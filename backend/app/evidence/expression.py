"""A tiny, safe arithmetic language for CALCULATION evidence.

A calculation is written as a formula over named inputs, for example

    (revenue_now / revenue_before) - 1          # growth, a ratio
    net_profit / equity                          # return on equity, a ratio
    market_cap / net_profit                      # a multiple

The engine, not the caller, evaluates it with exact `Decimal` arithmetic. This matters for the
project's core rule: no number reaches a report unless deterministic code produced it from stored
evidence. Nothing here uses `eval`; the grammar is numbers, names, + - * / ^ and parentheses.

Units are tracked, because mixing them silently is a classic source of wrong numbers:

* `+` and `-` need identical units (a ratio may also take a plain number: `growth - 1`);
* `rupees / rupees` is a `ratio`; `rupees / shares` is `inr_per_share`; `inr_per_share * shares`
  is `inr`; a plain number scales a value; `ratio * 100` is `percent` and `percent / 100` a ratio;
* anything else (for example rupees times rupees) is refused instead of guessed.

Division by zero and fractional powers of negative numbers raise `ExpressionError`: a calculation
never returns a value it cannot stand behind (missing is not zero).
"""

import re
from dataclasses import dataclass
from decimal import Decimal, DivisionByZero, InvalidOperation

SCALAR = "scalar"
MAX_LENGTH = 400
MAX_DEPTH = 40
RESULT_PLACES = Decimal("0.000001")

# A computed ratio may carry one of these display names when the caller declares it.
RELABEL = {"ratio": {"x", "days"}}


class ExpressionError(ValueError):
    """The formula is malformed, uses an unknown name, mixes units or cannot be evaluated."""


@dataclass(frozen=True)
class Quantity:
    value: Decimal
    unit: str


_TOKEN = re.compile(
    r"\s*(?:(?P<num>\d+(?:\.\d+)?)|(?P<name>[A-Za-z_][A-Za-z0-9_]*)|(?P<op>[-+*/^()]))"
)


def tokenize(text: str) -> list[tuple[str, str]]:
    if not text or not text.strip():
        raise ExpressionError("empty expression")
    if len(text) > MAX_LENGTH:
        raise ExpressionError(f"expression longer than {MAX_LENGTH} characters")
    tokens: list[tuple[str, str]] = []
    pos = 0
    text = text.rstrip()
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        if match is None or match.end() == pos:
            raise ExpressionError(f"unexpected character {text[pos:].strip()[:1]!r}")
        pos = match.end()
        if match.group("num") is not None:
            tokens.append(("num", match.group("num")))
        elif match.group("name") is not None:
            tokens.append(("name", match.group("name")))
        else:
            tokens.append(("op", match.group("op")))
    return tokens


def names_in(text: str) -> list[str]:
    """Input names a formula uses, in order of first appearance."""
    seen: list[str] = []
    for kind, token in tokenize(text):
        if kind == "name" and token not in seen:
            seen.append(token)
    return seen


# ---- unit algebra --------------------------------------------------------------------------------


def _add_units(a: str, b: str, op: str) -> str:
    if a == b:
        return a
    if {a, b} == {"ratio", SCALAR}:
        return "ratio"
    raise ExpressionError(f"cannot {'add' if op == '+' else 'subtract'} {a} and {b}")


def _mul_units(a: str, b: str) -> str:
    if a == SCALAR:
        return b
    if b == SCALAR:
        return a
    if {a, b} == {"inr_per_share", "shares"}:
        return "inr"
    if a == "ratio":
        return b
    if b == "ratio":
        return a
    raise ExpressionError(f"cannot multiply {a} by {b}")


def _div_units(a: str, b: str) -> str:
    if b == SCALAR:
        return a
    if a == b:
        return "ratio"
    if (a, b) == ("inr", "shares"):
        return "inr_per_share"
    if (a, b) == ("inr", "inr_per_share"):
        return "shares"
    if b == "ratio":
        return a
    raise ExpressionError(f"cannot divide {a} by {b}")


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]], variables: dict[str, Quantity]) -> None:
        self.tokens, self.vars, self.i, self.depth = tokens, variables, 0, 0
        self.literals: list[Decimal] = []

    def _peek(self) -> tuple[str, str] | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def _take(self) -> tuple[str, str]:
        token = self._peek()
        if token is None:
            raise ExpressionError("expression ends unexpectedly")
        self.i += 1
        return token

    def parse(self) -> Quantity:
        result = self._expr()
        if self._peek() is not None:
            raise ExpressionError(f"unexpected {self._peek()[1]!r}")
        return result

    def _expr(self) -> Quantity:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise ExpressionError("expression nested too deeply")
        left = self._term()
        while (tok := self._peek()) and tok[0] == "op" and tok[1] in "+-":
            self._take()
            right = self._term()
            unit = _add_units(left.unit, right.unit, tok[1])
            left = Quantity(
                left.value + right.value if tok[1] == "+" else left.value - right.value, unit
            )
        self.depth -= 1
        return left

    def _term(self) -> Quantity:
        left = self._unary()
        while (tok := self._peek()) and tok[0] == "op" and tok[1] in "*/":
            self._take()
            right = self._unary()
            if tok[1] == "*":
                unit = _mul_units(left.unit, right.unit)
                # ratio x 100 is how a percentage is written
                if {left.unit, right.unit} == {"ratio", SCALAR}:
                    plain = left if left.unit == SCALAR else right
                    if plain.value == 100:
                        unit = "percent"
                left = Quantity(left.value * right.value, unit)
            else:
                if right.value == 0:
                    raise ExpressionError("division by zero")
                unit = _div_units(left.unit, right.unit)
                if left.unit == "percent" and right.unit == SCALAR and right.value == 100:
                    unit = "ratio"
                left = Quantity(left.value / right.value, unit)
        return left

    def _unary(self) -> Quantity:
        tok = self._peek()
        if tok and tok == ("op", "-"):
            self._take()
            inner = self._unary()
            return Quantity(-inner.value, inner.unit)
        return self._power()

    def _power(self) -> Quantity:
        base = self._atom()
        tok = self._peek()
        if tok and tok == ("op", "^"):
            self._take()
            exponent = self._unary()
            if exponent.unit != SCALAR:
                raise ExpressionError("an exponent must be a plain number")
            if base.unit not in (SCALAR, "ratio"):
                raise ExpressionError(f"cannot raise {base.unit} to a power")
            try:
                if base.value < 0 and exponent.value != exponent.value.to_integral_value():
                    raise ExpressionError("fractional power of a negative number")
                if base.value == 0 and exponent.value < 0:
                    raise ExpressionError("division by zero")
                value = base.value**exponent.value
            except (InvalidOperation, DivisionByZero) as exc:
                raise ExpressionError("power cannot be evaluated") from exc
            return Quantity(value, base.unit)
        return base

    def _atom(self) -> Quantity:
        kind, token = self._take()
        if kind == "num":
            value = Decimal(token)
            self.literals.append(value)
            return Quantity(value, SCALAR)
        if kind == "name":
            if token not in self.vars:
                raise ExpressionError(f"unknown input {token!r}")
            return self.vars[token]
        if token == "(":
            inner = self._expr()
            closing = self._take()
            if closing != ("op", ")"):
                raise ExpressionError("missing closing parenthesis")
            return inner
        raise ExpressionError(f"unexpected {token!r}")


def evaluate(expression: str, variables: dict[str, Quantity]) -> Quantity:
    """Evaluate `expression` over `variables`; raises ExpressionError on any problem."""
    parser = _Parser(tokenize(expression), variables)
    try:
        result = parser.parse()
    except (InvalidOperation, DivisionByZero, OverflowError) as exc:
        raise ExpressionError("arithmetic error") from exc
    unused = [name for name in variables if name not in names_in(expression)]
    if unused:
        raise ExpressionError(f"inputs declared but not used: {', '.join(sorted(unused))}")
    return Quantity(result.value.quantize(RESULT_PLACES), result.unit)


def apply_result_unit(computed: str, declared: str | None) -> str:
    """The unit the caller declared must be the computed one, or an allowed display name of it."""
    if declared is None or declared == computed:
        return computed
    if declared in RELABEL.get(computed, set()):
        return declared
    raise ExpressionError(f"the formula yields {computed}, not {declared}")


__all__ = [
    "ExpressionError",
    "Quantity",
    "SCALAR",
    "apply_result_unit",
    "evaluate",
    "names_in",
    "tokenize",
]
