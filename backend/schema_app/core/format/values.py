"""Value model shared by the evaluator, methods and bindings.

Expression values are plain Python values (``str``, ``int``, ``float``, ``bool``, ``None``,
``list``, ``datetime.date``) plus :class:`BindingObject` instances for structured bindings such as
``episode``, ``info``, ``f`` or ``plex``. Nothing else ever reaches the evaluator, which is why no
attribute access on arbitrary Python objects is needed (or possible).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from schema_app.core.format.errors import BindingMissing, MethodError

# Longest string any expression step may produce. File names are <= 255 bytes; this only leaves room
# for long intermediates (an overview, a joined list) while stopping formats that grow strings until
# the container runs out of memory.
MAX_VALUE_LENGTH = 65_536


class BindingObject:
    """Structured binding value with whitelisted dotted properties (``episode.title``)."""

    type_label = "Object"
    properties: frozenset[str] = frozenset()

    def get_property(self, name: str) -> Any:
        if name not in self.properties:
            raise BindingMissing(name, f"No such property '{name}' on {self.type_label}")
        value = self._property(name)
        if value is None:
            raise BindingMissing(name, f"Property '{name}' is undefined")
        return value

    def _property(self, name: str) -> Any:  # pragma: no cover - overridden
        return None

    def __str__(self) -> str:  # pragma: no cover - overridden
        return self.type_label

    def __repr__(self) -> str:
        return f"<{self.type_label} {self!s}>"


DATE_PROPERTIES = frozenset({"year", "month", "day"})


def type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "Boolean"
    if isinstance(value, int):
        return "Integer"
    if isinstance(value, float):
        return "Decimal"
    if isinstance(value, str):
        return "String"
    if isinstance(value, list | tuple):
        return "List"
    if isinstance(value, date):
        return "Date"
    if isinstance(value, BindingObject):
        return value.type_label
    return type(value).__name__


def format_number(value: float) -> str:
    if value != value or value in (float("inf"), float("-inf")):
        return str(value)
    text = repr(float(value))
    if "e" in text or "E" in text:
        text = f"{value:.10f}".rstrip("0")
        if text.endswith("."):
            text += "0"
    return text


def to_text(value: Any) -> str:
    """Groovy-like string form used for concatenation and block output."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return format_number(value)
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, list | tuple):
        items = [to_text(item) for item in value if item is not None]
        return "[" + ", ".join(items) + "]" if items else ""
    return str(value)


def truthy(value: Any) -> bool:
    """Groovy truth: null, "", 0, false and empty collections are false."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    if isinstance(value, str | list | tuple):
        return len(value) > 0
    return True


def is_empty(value: Any) -> bool:
    """"Empty" for any{}/allOf{}/joining: null, "" (or whitespace) and empty lists."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, list | tuple):
        return len(value) == 0
    return False


def is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def groovy_equals(left: Any, right: Any) -> bool:
    if is_number(left) and is_number(right):
        return left == right
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, list | tuple) and isinstance(right, list | tuple):
        return len(left) == len(right) and all(groovy_equals(a, b) for a, b in zip(left, right, strict=True))
    if isinstance(left, BindingObject) or isinstance(right, BindingObject):
        if left is None or right is None:
            return False
        return str(left) == str(right)
    if isinstance(left, date) and isinstance(right, date):
        return left == right
    if type(left) is not type(right) and not (isinstance(left, str) and isinstance(right, str)):
        return False
    return left == right


# --------------------------------------------------------------------------- operators
# Shared by the evaluator (+ - * % < ...) and the plus()/minus() methods. They raise MethodError;
# the evaluator re-raises it as FormatEvalError with the operator's offsets.


def _concat_text(value: Any) -> str:
    # Groovy would print "null"; an empty string is the friendlier choice for file names.
    return to_text(value)


def op_add(left: Any, right: Any) -> Any:
    if isinstance(left, list | tuple):
        return list(left) + (list(right) if isinstance(right, list | tuple) else [right])
    if isinstance(left, str) or isinstance(right, str):
        return _concat_text(left) + _concat_text(right)
    if is_number(left) and is_number(right):
        return left + right
    if isinstance(left, date) and isinstance(right, int) and not isinstance(right, bool):
        return left + timedelta(days=right)
    if isinstance(left, BindingObject) or isinstance(right, BindingObject):
        return _concat_text(left) + _concat_text(right)
    raise MethodError(f"Cannot add {type_name(left)} and {type_name(right)}")


def op_sub(left: Any, right: Any) -> Any:
    if is_number(left) and is_number(right):
        return left - right
    if isinstance(left, str) and right is not None and not isinstance(right, list | tuple):
        needle = to_text(right)
        return left.replace(needle, "", 1) if needle else left
    if isinstance(left, list | tuple):
        drop = list(right) if isinstance(right, list | tuple) else [right]
        return [item for item in left if not any(groovy_equals(item, d) for d in drop)]
    if isinstance(left, date) and isinstance(right, date):
        return (left - right).days
    if isinstance(left, date) and isinstance(right, int) and not isinstance(right, bool):
        return left - timedelta(days=right)
    raise MethodError(f"Cannot subtract {type_name(right)} from {type_name(left)}")


def op_mul(left: Any, right: Any) -> Any:
    if is_number(left) and is_number(right):
        return left * right
    if isinstance(left, str | list) and isinstance(right, int) and not isinstance(right, bool):
        if right < 0 or right > 1000:
            raise MethodError("Repeat count must be between 0 and 1000")
        if len(left) * right > MAX_VALUE_LENGTH:  # check before building it: ('x'*1000)*1000*1000 is 1 GB
            raise MethodError(f"Result would be too long (max {MAX_VALUE_LENGTH} characters)")
        return left * right
    raise MethodError(f"Cannot multiply {type_name(left)} by {type_name(right)}")


def op_mod(left: Any, right: Any) -> Any:
    if is_number(left) and is_number(right):
        if right == 0:
            raise MethodError("Modulo by zero")
        return left % right
    raise MethodError(f"Cannot compute {type_name(left)} % {type_name(right)}")


def op_compare(left: Any, right: Any) -> int:
    comparable = (
        (is_number(left) and is_number(right))
        or (isinstance(left, str) and isinstance(right, str))
        or (isinstance(left, bool) and isinstance(right, bool))
        or (isinstance(left, date) and isinstance(right, date))
    )
    if not comparable:
        raise MethodError(f"Cannot compare {type_name(left)} with {type_name(right)}")
    return (left > right) - (left < right)


def op_neg(value: Any) -> Any:
    if is_number(value):
        return -value
    raise MethodError(f"Cannot negate {type_name(value)}")
