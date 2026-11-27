"""Exceptions raised by the format engine."""

from __future__ import annotations


class FormatError(Exception):
    """Base class for format problems. ``start``/``end`` are character offsets into the whole
    format string (used by the editor for squiggles)."""

    def __init__(self, message: str, start: int = 0, end: int = 0) -> None:
        super().__init__(message)
        self.message = message
        self.start = start
        self.end = max(end, start)

    def __str__(self) -> str:
        return self.message


class FormatSyntaxError(FormatError):
    """The format could not be parsed."""


class FormatEvalError(FormatError):
    """Evaluation failed for a reason other than a missing binding (unknown method, wrong arity,
    type error, regex guard, step budget ...)."""


class BudgetExceeded(FormatEvalError):
    """The step budget ran out. Never swallowed by ``any{}`` / ``allOf{}``."""


class BindingMissing(KeyError):
    """A binding (or property, or method result) is undefined. A ``{...}`` block that raises this
    renders as an empty string, which is how ``{' - ' + t}`` works as an optional segment."""

    def __init__(self, name: str, message: str | None = None) -> None:
        super().__init__(name)
        self.name = name
        self.message = message or f"Binding '{name}' is undefined"

    def __str__(self) -> str:
        return self.message


class MethodError(Exception):
    """Raised inside method implementations; the evaluator converts it to FormatEvalError with
    the offsets of the call."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
