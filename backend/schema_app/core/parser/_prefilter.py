"""Literal prefilter: skip parser regexes that cannot match a name (pure speed-up).

The parser runs ~230 tag / numbering regexes over every name, yet a typical name can only
match a handful of them: a name without "dts" in it cannot hold a DTS tag. At import, each
regex is analysed with the stdlib regex parser into *needles*, literal strings of which
every possible match must contain at least one ("dts" for the DTS rules, "1080p" ...
"4320i" for the resolution rule). One scan over a name finds which needles occur in it, and
only regexes with a needle present (or without derivable needles) are run at all.

The filter never changes a result, it only avoids work:
  * needles are derived conservatively: an unknown construct contributes nothing, and a
    regex without needles (or whose analysis fails) is always run;
  * case-insensitive regexes are checked against the lower-cased name. Four non-ASCII
    characters also match ASCII letters case-insensitively (dotted / dotless i, long s,
    Kelvin sign); a name containing any of them skips the filter and runs every regex;
  * pure-ASCII names (nearly all of them) get a sharper needle set: there ``\\d`` can only
    be 0-9, so ``s\\d`` yields the needles "s0" ... "s9" rather than just "s", and a regex
    that needs a non-ASCII character can never match;
  * needles never contain a space, so a needle found in a copy of the name with parts
    replaced by spaces (the parser masks consumed regions that way) was already in the
    name: one ``live`` set per name covers every masked working copy of it.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from itertools import product
from typing import NamedTuple

try:  # stdlib internals (3.11+); without them every regex simply always runs
    from re import _constants as _c
    from re import _parser as _p
except ImportError:  # pragma: no cover
    _c = _p = None  # type: ignore[assignment]

__all__ = ["Prefilter", "needles_of", "CASE_SPECIALS"]

# Non-ASCII characters that match an ASCII letter under re.IGNORECASE (exhaustively checked
# by tests/metadata/test_parser_prefilter.py over every code point).
CASE_SPECIALS = frozenset("İıſK")
_SPECIALS_RX = re.compile("[İıſK]")

_LIMIT = 256  # largest exact string set tracked while expanding a pattern
_SMALL_CLASS = 16  # character classes up to this size are expanded into literals
_ASCII = [chr(i) for i in range(128)]


class _Info(NamedTuple):
    """What a pattern node can match. ``exact``: every string it can match (finite), else None.
    ``needles``: every match contains one of these. ``prefix`` / ``suffix``: every match starts /
    ends with one of these. An empty set means the node can never match."""

    exact: frozenset[str] | None = None
    needles: frozenset[str] | None = None
    prefix: frozenset[str] | None = None
    suffix: frozenset[str] | None = None


_UNKNOWN = _Info()
_EMPTY = _Info(frozenset({""}), None, frozenset({""}), frozenset({""}))
_NEVER = _Info(frozenset(), frozenset(), frozenset(), frozenset())


def _exact(strings: frozenset[str]) -> _Info:
    return _Info(strings, strings if "" not in strings else None, strings, strings)


def _cross(a: frozenset[str] | None, b: frozenset[str] | None) -> frozenset[str] | None:
    if a is None or b is None or len(a) * len(b) > _LIMIT:
        return None
    return frozenset(x + y for x, y in product(a, b))


def _useful(strings: frozenset[str] | None) -> bool:
    return bool(strings) and "" not in strings  # type: ignore[operator]


def _as_needles(info: _Info) -> frozenset[str] | None:
    if info.needles is not None:
        return info.needles
    if info.exact is not None and "" not in info.exact:
        return info.exact
    return None


def _without_spaces(needle: str) -> str:
    """Longest space-free piece of ``needle`` ("blu ray" -> "blu", "" if all spaces)."""
    return max(needle.split(" "), key=len)


# Rough per-character frequencies in media file names (letters case-folded), only used to
# break ties between equally long needle sets (a speed heuristic: any choice is correct).
_CHAR_FREQ = dict(zip("esaoitrdnmlpvhcbufgwxkyzjq0123456789 .-[]()", (
    .071, .056, .049, .046, .041, .038, .036, .036, .034, .033, .026, .026, .023, .022, .019, .018,
    .015, .013, .012, .011, .011, .010, .006, .002, .001, .001, .032, .028, .024, .008, .011, .008,
    .007, .007, .011, .009, .083, .052, .017, .011, .011, .006, .006,
), strict=True))


def _score(needles: frozenset[str]) -> tuple[float, int, float]:
    """Selectivity of a needle set once spaces are dropped (higher is better): longest shortest
    needle, then fewest needles, then rarest characters."""
    if not needles:
        return float("inf"), 0, 0.0  # can never match: the best possible set
    pieces = {_without_spaces(n) for n in needles}
    freq = 0.0
    for piece in pieces:
        p = 1.0
        for ch in piece:
            p *= _CHAR_FREQ.get(ch.lower(), 0.005)
        freq += p
    return min(map(len, pieces)), -len(pieces), -freq


def _best(candidates: list[frozenset[str] | None]) -> frozenset[str] | None:
    best = None
    for cand in candidates:
        if cand is None or "" in cand:
            continue
        if best is None or _score(cand) > _score(best):
            best = cand
    return best


class _Analyzer:
    def __init__(self, flags: int, ascii_text: bool) -> None:
        self.ci = bool(flags & re.IGNORECASE)
        self.ascii_text = ascii_text
        self.categories: dict[object, str] = {}
        if ascii_text:
            ascii_flag = flags & re.ASCII
            for name, cls in (("DIGIT", r"\d"), ("NOT_DIGIT", r"\D"), ("SPACE", r"\s"),
                              ("NOT_SPACE", r"\S"), ("WORD", r"\w"), ("NOT_WORD", r"\W")):
                rx = re.compile(cls, ascii_flag)
                self.categories[getattr(_c, "CATEGORY_" + name)] = "".join(ch for ch in _ASCII if rx.match(ch))

    def char(self, code: int) -> str | None:
        """The literal as it appears in the (lower-cased for ci) text; "" = can never match there."""
        ch = chr(code)
        if ch.isascii():
            return ch.lower() if self.ci else ch
        if self.ci:  # a non-ASCII literal may case-fold to other characters, ASCII ones included
            return None
        return "" if self.ascii_text else ch

    def seq(self, items) -> _Info:
        run: frozenset[str] = frozenset({""})
        candidates: list[frozenset[str] | None] = []
        prefix: frozenset[str] | None = None
        broken = False
        for item in items:
            info = self.node(item)
            if info.exact is not None:
                if not info.exact:
                    return _NEVER
                joined = _cross(run, info.exact)
                if joined is not None:
                    run = joined
                    continue
                if not broken:
                    prefix = run
                broken = True
                candidates.append(run)
                run = info.exact
                continue
            head = _cross(run, info.prefix) if _useful(info.prefix) else None
            if not broken:
                prefix = head if head is not None else run
            broken = True
            candidates += [head if head is not None else run, info.needles]
            run = info.suffix if _useful(info.suffix) else frozenset({""})
        if not broken:
            return _exact(run)
        candidates.append(run)
        return _Info(None, _best(candidates), prefix if _useful(prefix) else None, run if _useful(run) else None)

    def node(self, item) -> _Info:
        op, av = item
        if op is _c.LITERAL:
            ch = self.char(av)
            if ch is None:
                return _UNKNOWN
            return _NEVER if ch == "" else _exact(frozenset({ch}))
        if op is _c.IN:
            return self.char_class(av)
        if op is _c.BRANCH:
            return self.branch([self.seq(alt) for alt in av[1]])
        if op is _c.SUBPATTERN:
            _group, add_flags, del_flags, sub = av
            return _UNKNOWN if (add_flags or del_flags) else self.seq(sub)
        if op in (_c.MAX_REPEAT, _c.MIN_REPEAT, getattr(_c, "POSSESSIVE_REPEAT", _c.MAX_REPEAT)):
            return self.repeat(*av)
        if op in (_c.ASSERT, _c.ASSERT_NOT, _c.AT):  # zero width
            return _EMPTY
        return _UNKNOWN  # ANY, NOT_LITERAL, GROUPREF, atomic groups, ...

    def char_class(self, items) -> _Info:
        chars: set[str] = set()
        for op, av in items:
            if op is _c.LITERAL:
                found = [self.char(av)]
            elif op is _c.RANGE and av[1] - av[0] < 4 * _SMALL_CLASS:
                found = [self.char(code) for code in range(av[0], av[1] + 1)]
            elif op is _c.CATEGORY and av in self.categories:
                found = list(self.categories[av])
                if self.ci:
                    found = [ch.lower() for ch in found]
            else:  # NEGATE, unknown categories, big ranges
                return _UNKNOWN
            if None in found:
                return _UNKNOWN
            chars.update(ch for ch in found if ch)
        if len(chars) > _SMALL_CLASS:
            return _UNKNOWN
        return _exact(frozenset(chars)) if chars else _NEVER

    def branch(self, alts: list[_Info]) -> _Info:
        alts = [a for a in alts if not (a.exact is not None and not a.exact)]  # drop never-matching
        if not alts:
            return _NEVER
        exacts = [a.exact for a in alts]
        if all(e is not None for e in exacts) and sum(map(len, exacts)) <= _LIMIT:  # type: ignore[arg-type]
            return _exact(frozenset().union(*exacts))  # type: ignore[arg-type]
        needles: set[str] = set()
        for alt in alts:
            alt_needles = _as_needles(alt)
            if alt_needles is None:  # this alternative can match without any known literal
                needles = None  # type: ignore[assignment]
                break
            needles |= alt_needles
        prefixes = [a.prefix for a in alts]
        suffixes = [a.suffix for a in alts]
        return _Info(
            None,
            frozenset(needles) if needles is not None else None,
            frozenset().union(*prefixes) if all(_useful(p) for p in prefixes) else None,  # type: ignore[arg-type]
            frozenset().union(*suffixes) if all(_useful(s) for s in suffixes) else None,  # type: ignore[arg-type]
        )

    def repeat(self, lo: int, hi: int, sub) -> _Info:
        inner = self.seq(sub)
        if lo == 0:
            if hi == 1 and inner.exact is not None:
                return _exact(inner.exact | {""})
            return _UNKNOWN
        if inner.exact is not None and not inner.exact:
            return _NEVER
        if inner.exact is not None:
            core = frozenset({""})
            for _ in range(lo):
                core = _cross(core, inner.exact)  # type: ignore[assignment]
                if core is None:
                    break
            if core is not None and lo == hi:
                return _exact(core)
            if core is not None:  # lo exact copies, then up to hi - lo more
                return _Info(None, core if "" not in core else None, core, core)
            return _Info(None, _as_needles(inner), inner.exact, inner.exact)
        return _Info(None, _as_needles(inner), inner.prefix, inner.suffix)


def needles_of(regex: re.Pattern[str], *, ascii_text: bool = False) -> frozenset[str] | None:
    """Needles for ``regex`` (lower-cased when it is case-insensitive). None when unknown (the
    regex must always run); an empty set when it can never match (only with ``ascii_text``).

    Every string ``regex`` can match in a text contains at least one needle. ``ascii_text``
    derives them for pure-ASCII texts only.
    """
    if _p is None:
        return None
    try:
        info = _Analyzer(regex.flags, ascii_text).seq(_p.parse(regex.pattern, regex.flags))
        needles = _as_needles(info)
    except Exception:  # unexpected regex internals: no filter, the regex always runs
        return None
    if needles is None:
        return None
    pieces = frozenset(map(_without_spaces, needles))
    return None if "" in pieces else pieces


def _trie_pattern(words: Sequence[str]) -> str:
    """Regex alternation over ``words`` as a trie, preferring the longest word at a position."""
    trie: dict = {}
    for word in words:
        node = trie
        for ch in word:
            node = node.setdefault(ch, {})
        node[""] = {}

    def emit(node: dict) -> str:
        alts = [re.escape(ch) + emit(child) for ch, child in sorted(node.items()) if ch]
        if not alts:
            return ""
        body = alts[0] if len(alts) == 1 else "(?:" + "|".join(alts) + ")"
        return f"(?:{body})?" if "" in node else body

    return emit(trie)


class _Mode:
    """Needle index for one kind of text (pure ASCII or anything).

    One zero-width trie scan over the lower-cased text reports, at every position, the longest
    needle starting there; every shorter needle starting at that position is a prefix of it, so
    a found needle also unlocks the regexes of each of its prefixes that is a needle. Needles of
    case-sensitive regexes take part lower-cased and are then confirmed in the original text.
    """

    def __init__(self, regexes: Sequence[re.Pattern[str]], ascii_text: bool) -> None:
        always: set[int] = set()
        plain: dict[str, set[int]] = {}  # lower-cased needle -> regexes it unlocks outright
        exact: dict[str, dict[str, set[int]]] = {}  # lower-cased -> {needle as written: regexes}
        self.needles: list[frozenset[str] | None] = []
        for i, rx in enumerate(regexes):
            needles = needles_of(rx, ascii_text=ascii_text)
            self.needles.append(needles)
            if needles is None:
                always.add(i)
                continue
            ci = bool(rx.flags & re.IGNORECASE)
            for needle in needles:  # none at all: can never match this kind of text
                low = needle.lower()
                if ci or needle == low:  # lower-casing leaves this needle as it is
                    plain.setdefault(low, set()).add(i)
                else:
                    exact.setdefault(low, {}).setdefault(needle, set()).add(i)
        self.always = frozenset(always)
        words = sorted(set(plain) | set(exact))
        self.unlock: dict[str, frozenset[int]] = {}
        self.confirm: dict[str, tuple[tuple[str, frozenset[int]], ...]] = {}
        for word in words:
            ids: set[int] = set()
            checks: dict[str, set[int]] = {}
            for k in range(1, len(word) + 1):
                ids |= plain.get(word[:k], set())
                for written, regex_ids in exact.get(word[:k], {}).items():
                    checks.setdefault(written, set()).update(regex_ids)
            self.unlock[word] = frozenset(ids)
            if checks:
                self.confirm[word] = tuple((w, frozenset(r)) for w, r in sorted(checks.items()))
        self.scanner = re.compile(f"(?=({_trie_pattern(words)}))") if words else None

    def live(self, text: str) -> set[int]:
        found = set(self.always)
        if self.scanner is None:
            return found
        unlock, confirm = self.unlock, self.confirm
        for needle in set(self.scanner.findall(text.lower())):
            found |= unlock[needle]
            if needle in confirm:
                for written, ids in confirm[needle]:
                    if written in text:
                        found |= ids
        return found


class Prefilter:
    """Which of ``regexes`` can possibly match a given text (as indices into ``regexes``)."""

    def __init__(self, regexes: Sequence[re.Pattern[str]]) -> None:
        self.regexes = tuple(regexes)
        self.everything = frozenset(range(len(self.regexes)))
        self.ascii = _Mode(self.regexes, ascii_text=True)
        self.unicode = _Mode(self.regexes, ascii_text=False)

    def live(self, text: str) -> AbstractSet[int]:
        """Indices of the regexes that may match somewhere in ``text`` (from any position).

        Also valid for any copy of ``text`` with parts replaced by spaces (needles hold no spaces).
        """
        if text.isascii():
            return self.ascii.live(text)
        if _SPECIALS_RX.search(text):
            return self.everything
        return self.unicode.live(text)
