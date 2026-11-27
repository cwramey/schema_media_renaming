"""Guarded regular expressions for format methods (``replaceAll``, ``match``, ``before`` …).

This module is the "regex timeout guard" of SPEC §8.3/§11. User-supplied patterns are compiled
with the third-party ``regex`` package (mrab-regex, in ``VERSION0`` = ``re``-compatible mode)
because, unlike ``re``, it can abort a match after a time limit. Python threads can't be
killed, so without that a slow pattern on a hostile file name would pin a worker forever.

* patterns longer than :data:`MAX_PATTERN` characters are refused;
* nested unbounded quantifiers such as ``(a+)+``, ``(a*)*`` or ``(.+)*`` (the classic
  catastrophic-backtracking shapes) are refused before compiling (a cheap first line);
* subjects longer than :data:`MAX_SUBJECT` characters are refused;
* every match/search/sub/split/finditer runs under a total budget of :data:`MATCH_TIMEOUT`
  seconds and raises :class:`MethodError` when it runs out. That covers the shapes the static
  check can't see, e.g. ``(a|aa)*c`` or ``\\d*\\d*\\d*x``.

:func:`compile_regex` returns a :class:`GuardedPattern`, whose methods apply both the subject limit
and the timeout, so no caller can run a user pattern unguarded. Fixed internal patterns elsewhere
stay on ``re``.

Java-isms common in FileBot formats are translated: ``\\p{Punct}``-style POSIX classes, Unicode
letter classes, ``(?<name>…)`` named groups and ``$1`` / ``${name}`` replacement references.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from functools import lru_cache
from typing import Any

import regex

from schema_app.core.format.errors import MethodError

MAX_PATTERN = 300
MAX_SUBJECT = 5000
MATCH_TIMEOUT = 0.25  # seconds, total per call (finditer/sub/split included)

_TIMEOUT_MESSAGE = (
    f"Regex /{{pattern}}/ took longer than {MATCH_TIMEOUT:g}s on this text and was stopped; "
    "simplify it (avoid overlapping alternatives or repeats like (a|aa)* or \\d*\\d*)"
)

_LETTERS = (
    "a-zA-ZªµºÀ-ÖØ-öø-ʯͰ-ϿЀ-ӿ"
    "぀-ヿ一-鿿가-힯"
)
_PUNCT_ASCII = r"!-/:-@\[-`{-~"
_CLASSES: dict[str, str] = {
    "Punct": _PUNCT_ASCII,
    "Alpha": "a-zA-Z",
    "Alnum": "a-zA-Z0-9",
    "Digit": "0-9",
    "Upper": "A-Z",
    "Lower": "a-z",
    "Space": r" \t\n\x0b\f\r",
    "Blank": r" \t",
    "XDigit": "0-9a-fA-F",
    "Cntrl": r"\x00-\x1f\x7f",
    "ASCII": r"\x00-\x7f",
    "Graph": "!-~",
    "Print": " -~",
    "L": _LETTERS,
    "IsL": _LETTERS,
    "IsLetter": _LETTERS,
    "IsAlphabetic": _LETTERS,
    "Lu": "A-ZÀ-ÖØ-Þ",
    "Ll": "a-zß-öø-ÿ",
    "IsLatin": "a-zA-ZÀ-ɏḀ-ỿ",
    "InLatin": "a-zA-ZÀ-ɏ",
    "N": "0-9",
    "Nd": "0-9",
    "IsDigit": "0-9",
    "P": _PUNCT_ASCII + "¡-¿‐-‧‰-⁞　-〿",
    "IsPunctuation": _PUNCT_ASCII + "¡-¿‐-‧‰-⁞　-〿",
}
_LETTER_NAMES = frozenset({"L", "IsL", "IsLetter", "IsAlphabetic"})


def _translate(pattern: str) -> str:
    out: list[str] = []
    i = 0
    n = len(pattern)
    in_class = False
    while i < n:
        ch = pattern[i]
        if ch == "\\" and i + 1 < n:
            nxt = pattern[i + 1]
            if nxt in "pP" and i + 2 < n and pattern[i + 2] == "{":
                close = pattern.find("}", i + 3)
                if close == -1:
                    raise MethodError(f"Invalid regex class in /{pattern}/")
                name = pattern[i + 3 : close]
                if name not in _CLASSES:
                    raise MethodError(f"Unsupported regex class \\{nxt}{{{name}}}")
                inner = _CLASSES[name]
                if in_class:
                    if nxt == "P":
                        raise MethodError(f"Negated class \\P{{{name}}} is not supported inside [...]")
                    out.append(inner)
                elif name in _LETTER_NAMES:
                    out.append(r"[\W\d_]" if nxt == "P" else r"[^\W\d_]")
                else:
                    out.append(f"[^{inner}]" if nxt == "P" else f"[{inner}]")
                i = close + 1
                continue
            if nxt == "k" and i + 2 < n and pattern[i + 2] == "<":
                close = pattern.find(">", i + 3)
                if close != -1:
                    out.append(f"(?P={pattern[i + 3 : close]})")
                    i = close + 1
                    continue
            out.append(pattern[i : i + 2])
            i += 2
            continue
        if in_class:
            if ch == "]":
                in_class = False
            out.append(ch)
            i += 1
            continue
        if ch == "[":
            in_class = True
            out.append(ch)
            i += 1
            # A leading "^" and/or "]" are part of the class, not its end.
            if i < n and pattern[i] == "^":
                out.append("^")
                i += 1
            if i < n and pattern[i] == "]":
                out.append("]")
                i += 1
            continue
        if ch == "(" and pattern.startswith("(?<", i) and i + 3 < n and pattern[i + 3] not in "=!":
            out.append("(?P<")
            i += 3
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _unbounded_quantifier_at(pattern: str, i: int) -> bool:
    if i >= len(pattern):
        return False
    ch = pattern[i]
    if ch in "*+":
        return True
    if ch == "{":
        m = re.match(r"\{(\d*),(\d*)\}", pattern[i:])
        if m and (m.group(2) == "" or int(m.group(2) or 0) > 10):
            return True
    return False


def _check_nested_quantifiers(pattern: str) -> None:
    stack: list[bool] = [False]
    i = 0
    n = len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "\\":
            i += 2
            if _unbounded_quantifier_at(pattern, i):
                stack[-1] = True
            continue
        if ch == "[":
            j = i + 1
            if j < n and pattern[j] == "^":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 2 if pattern[j] == "\\" else 1
            i = j + 1
            if _unbounded_quantifier_at(pattern, i):
                stack[-1] = True
            continue
        if ch == "(":
            stack.append(False)
            i += 1
            if i < n and pattern[i] == "?":
                i += 1  # group modifier, not a quantifier
            continue
        if ch == ")":
            inner = stack.pop() if len(stack) > 1 else False
            i += 1
            quantified = _unbounded_quantifier_at(pattern, i)
            if inner and quantified:
                raise MethodError("Regex rejected: nested repetition like (a+)+ can take forever")
            stack[-1] = stack[-1] or inner or quantified
            continue
        if _unbounded_quantifier_at(pattern, i):
            stack[-1] = True
        i += 1


def check_subject(text: str) -> str:
    if len(text) > MAX_SUBJECT:
        raise MethodError(f"Text is too long for a regex (max {MAX_SUBJECT} characters)")
    return text


class GuardedPattern:
    """A compiled user pattern whose every operation checks the subject length and runs under
    :data:`MATCH_TIMEOUT`. Mirrors the subset of the ``re.Pattern`` API the format methods use."""

    __slots__ = ("_compiled", "source")

    def __init__(self, compiled: Any, source: str) -> None:
        self._compiled = compiled
        self.source = source  # the pattern as the user wrote it (for messages)

    @property
    def groups(self) -> int:
        return self._compiled.groups

    @property
    def groupindex(self) -> dict[str, int]:
        return dict(self._compiled.groupindex)

    def _run(self, op: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return op(*args, timeout=MATCH_TIMEOUT, **kwargs)
        except TimeoutError as exc:
            raise MethodError(_TIMEOUT_MESSAGE.format(pattern=self.source)) from exc

    def search(self, text: str) -> Any:
        return self._run(self._compiled.search, check_subject(text))

    def fullmatch(self, text: str) -> Any:
        return self._run(self._compiled.fullmatch, check_subject(text))

    def finditer(self, text: str) -> Iterator[Any]:
        # The timeout covers the whole scan, but only while it is being consumed; collect it
        # here so the budget (and the MethodError) can't escape into the caller's loop.
        scanner = self._compiled.finditer(check_subject(text), timeout=MATCH_TIMEOUT)
        try:
            return iter(list(scanner))
        except TimeoutError as exc:
            raise MethodError(_TIMEOUT_MESSAGE.format(pattern=self.source)) from exc

    def split(self, text: str) -> list[str]:
        return self._run(self._compiled.split, check_subject(text))

    def sub(self, repl: str | Callable[[Any], str], text: str, count: int = 0) -> str:
        return self._run(self._compiled.sub, repl, check_subject(text), count=count)


@lru_cache(maxsize=512)
def compile_regex(pattern: str, flags: int = 0) -> GuardedPattern:
    """Translate, guard and compile ``pattern``. Raises MethodError when it is refused or invalid.
    ``flags`` are ``re``/``regex`` flag values (the common ones are numerically identical)."""
    if len(pattern) > MAX_PATTERN:
        raise MethodError(f"Regex is too long (max {MAX_PATTERN} characters)")
    translated = _translate(pattern)
    _check_nested_quantifiers(translated)
    try:
        compiled = regex.compile(translated, flags | regex.VERSION0)
    except regex.error as exc:
        raise MethodError(f"Invalid regex /{pattern}/: {exc.msg}") from exc
    return GuardedPattern(compiled, pattern)


def compile_or_literal(pattern: str, flags: int = 0) -> GuardedPattern:
    """Compile as a regex; if it isn't a valid regex, match it literally (FileBot formats often
    pass plain text like ``' ('`` to regex helpers). Guard violations still raise."""
    try:
        return compile_regex(pattern, flags)
    except MethodError as exc:
        if exc.message.startswith("Invalid regex"):
            return compile_regex(re.escape(pattern), flags)
        raise


def java_replacement(repl: str, pattern: GuardedPattern | re.Pattern[str]) -> Callable[[Any], str]:
    """Java ``Matcher.replaceAll`` replacement syntax: ``$1``, ``${name}``, ``\\$`` for a dollar.
    The returned callable takes an ``re`` or ``regex`` match object."""
    parts: list[str | int] = []
    buf: list[str] = []
    i = 0
    n = len(repl)
    groups = pattern.groups
    while i < n:
        ch = repl[i]
        if ch == "\\" and i + 1 < n:
            buf.append(repl[i + 1])
            i += 2
            continue
        if ch == "$" and i + 1 < n:
            if repl[i + 1] == "{":
                close = repl.find("}", i + 2)
                if close == -1:
                    raise MethodError("Unclosed ${name} in replacement")
                name = repl[i + 2 : close]
                if name not in pattern.groupindex:
                    raise MethodError(f"No group named '{name}' in the regex")
                if buf:
                    parts.append("".join(buf))
                    buf = []
                parts.append(pattern.groupindex[name])
                i = close + 1
                continue
            if repl[i + 1].isdigit():
                j = i + 1
                number = int(repl[j])
                j += 1
                while j < n and repl[j].isdigit() and number * 10 + int(repl[j]) <= groups:
                    number = number * 10 + int(repl[j])
                    j += 1
                if number > groups:
                    raise MethodError(f"No group {number} in the regex")
                if buf:
                    parts.append("".join(buf))
                    buf = []
                parts.append(number)
                i = j
                continue
        buf.append(ch)
        i += 1
    if buf:
        parts.append("".join(buf))

    def _replace(match: Any) -> str:
        out: list[str] = []
        for part in parts:
            if isinstance(part, int):
                out.append(match.group(part) or "")
            else:
                out.append(part)
        return "".join(out)

    return _replace
