"""Tree-walking interpreter for format expressions (SPEC §8.3).

No ``eval``/``exec``, no Python attribute access: identifiers resolve through the bindings
mapping, dotted access only reaches :class:`BindingObject` properties and a few whitelisted date
properties, and method calls go through the :mod:`methods` whitelist. A step budget bounds the
work per render.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

from schema_app.core.format.errors import BindingMissing, BudgetExceeded, FormatEvalError, MethodError
from schema_app.core.format.methods import METHODS, call_method
from schema_app.core.format.parser import (
    Binary,
    Call,
    Closures,
    Elvis,
    GStr,
    Ident,
    Index,
    ListLit,
    Lit,
    Member,
    Node,
    Ternary,
    Unary,
)
from schema_app.core.format.values import (
    DATE_PROPERTIES,
    MAX_VALUE_LENGTH,
    BindingObject,
    groovy_equals,
    is_empty,
    is_number,
    op_add,
    op_compare,
    op_mod,
    op_mul,
    op_neg,
    op_sub,
    to_text,
    truthy,
    type_name,
)

DEFAULT_BUDGET = 10_000


class Evaluator:
    """Evaluates AST nodes against a bindings mapping. One instance per render (it carries the
    step counter, which is shared by all blocks of that render)."""

    def __init__(self, bindings: Mapping[str, Any], *, budget: int = DEFAULT_BUDGET) -> None:
        self.bindings = bindings
        self.budget = budget
        self.steps = 0

    def eval(self, node: Node) -> Any:
        self.steps += 1
        if self.steps > self.budget:
            raise BudgetExceeded("Expression is too complex (step budget exceeded)", node.start, node.end)
        value = _DISPATCH[type(node)](self, node)
        if isinstance(value, str) and len(value) > MAX_VALUE_LENGTH:
            raise BudgetExceeded(f"Value is too long (max {MAX_VALUE_LENGTH} characters)", node.start, node.end)
        return value

    # ------------------------------------------------------------------ primaries
    def _lit(self, node: Lit) -> Any:
        return node.value

    def _ident(self, node: Ident) -> Any:
        try:
            value = self.bindings[node.name]
        except BindingMissing:
            raise
        except KeyError:
            raise BindingMissing(node.name) from None
        if value is None:
            raise BindingMissing(node.name)
        return value

    def _list(self, node: ListLit) -> list[Any]:
        items: list[Any] = []
        for item in node.items:
            try:
                items.append(self.eval(item))
            except BindingMissing:
                continue  # SPEC §8.3: lists drop missing entries
        return items

    def _gstr(self, node: GStr) -> str:
        out: list[str] = []
        for part in node.parts:
            out.append(part if isinstance(part, str) else to_text(self.eval(part)))
        return "".join(out)

    # ------------------------------------------------------------------ postfix
    def _member(self, node: Member) -> Any:
        obj = self.eval(node.obj)
        if obj is None:
            if node.safe:
                return None
            raise FormatEvalError(f"Cannot read '{node.name}' of null (use '?.')", node.start, node.end)
        if isinstance(obj, BindingObject):
            return obj.get_property(node.name)
        if isinstance(obj, date) and node.name in DATE_PROPERTIES:
            return {"year": obj.year, "month": obj.month, "day": obj.day}[node.name]
        hint = f"; did you mean .{node.name}()?" if node.name in METHODS else ""
        raise FormatEvalError(f"No property '{node.name}' on {type_name(obj)}{hint}", node.start, node.end)

    def _call(self, node: Call) -> Any:
        obj = self.eval(node.obj)
        if obj is None and node.safe:
            return None
        args = [self.eval(arg) for arg in node.args]
        try:
            return call_method(node.name, obj, args)
        except MethodError as exc:
            raise FormatEvalError(exc.message, node.name_start, node.end) from None

    def _index(self, node: Index) -> Any:
        obj = self.eval(node.obj)
        key = self.eval(node.index)
        if isinstance(obj, list | tuple | str):
            if not isinstance(key, int) or isinstance(key, bool):
                raise FormatEvalError(f"Index must be a whole number, got {type_name(key)}", node.index.start, node.index.end)
            try:
                return obj[key]
            except IndexError:
                raise BindingMissing("[]", f"Index {key} is out of range") from None
        if isinstance(obj, BindingObject) and isinstance(key, str):
            return obj.get_property(key)
        raise FormatEvalError(f"Cannot index {type_name(obj)}", node.start, node.end)

    # ------------------------------------------------------------------ operators
    def _unary(self, node: Unary) -> Any:
        value = self.eval(node.operand)
        if node.op == "!":
            return not truthy(value)
        if node.op == "+":
            if is_number(value):
                return value
            raise FormatEvalError(f"Cannot apply unary '+' to {type_name(value)}", node.start, node.end)
        try:
            return op_neg(value)
        except MethodError as exc:
            raise FormatEvalError(exc.message, node.start, node.end) from None

    def _binary(self, node: Binary) -> Any:
        op = node.op
        if op == "&&":
            return truthy(self.eval(node.left)) and truthy(self.eval(node.right))
        if op == "||":
            return truthy(self.eval(node.left)) or truthy(self.eval(node.right))
        left = self.eval(node.left)
        right = self.eval(node.right)
        if op == "==":
            return groovy_equals(left, right)
        if op == "!=":
            return not groovy_equals(left, right)
        try:
            if op in ("<", "<=", ">", ">="):
                cmp = op_compare(left, right)
                return {"<": cmp < 0, "<=": cmp <= 0, ">": cmp > 0, ">=": cmp >= 0}[op]
            return _ARITHMETIC[op](left, right)
        except MethodError as exc:
            raise FormatEvalError(exc.message, node.start, node.end) from None

    def _elvis(self, node: Elvis) -> Any:
        try:
            left = self.eval(node.left)
        except BindingMissing:
            return self.eval(node.right)
        return left if truthy(left) else self.eval(node.right)

    def _ternary(self, node: Ternary) -> Any:
        return self.eval(node.then) if truthy(self.eval(node.cond)) else self.eval(node.other)

    def _closures(self, node: Closures) -> Any:
        values: list[Any] = []
        for body in node.bodies:
            try:
                value = self.eval(body)
            except BudgetExceeded:
                raise
            except (BindingMissing, FormatEvalError):
                continue
            if is_empty(value):
                continue
            if node.kind == "any":
                return value
            values.append(value)
        if node.kind == "any":
            raise BindingMissing("any", "any{…}: no alternative produced a value")
        return values


_ARITHMETIC: dict[str, Callable[[Any, Any], Any]] = {"+": op_add, "-": op_sub, "*": op_mul, "%": op_mod}

_DISPATCH: dict[type, Callable[[Evaluator, Any], Any]] = {
    Lit: Evaluator._lit,
    Ident: Evaluator._ident,
    ListLit: Evaluator._list,
    GStr: Evaluator._gstr,
    Member: Evaluator._member,
    Call: Evaluator._call,
    Index: Evaluator._index,
    Unary: Evaluator._unary,
    Binary: Evaluator._binary,
    Elvis: Evaluator._elvis,
    Ternary: Evaluator._ternary,
    Closures: Evaluator._closures,
}
