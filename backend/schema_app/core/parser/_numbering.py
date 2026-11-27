"""Episode / season / date numbering detection (SPEC §6.1 stage 4).

Patterns are tried most-specific first; the first acceptable hit wins. Each hit knows
whether it is *strict* (SxxEyy, 1x02, "Season 1 Episode 2", air dates) or a weaker
heuristic (bare anime numbers, leading numbers, 3-digit codes), which feeds parser
confidence and the kind vote.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from datetime import date

from ._rules import B0, B1, S

YEAR_LIKE = range(1920, 2041)  # numbers that look like years are never episode numbers
MAX_RANGE = 40  # longest S01E01-E40 style range we expand


@dataclass
class Numbering:
    kind: str
    start: int
    end: int
    season: int | None = None
    episodes: list[int] = field(default_factory=list)
    absolute: list[int] = field(default_factory=list)
    air_date: date | None = None
    special: bool = False
    special_marker: bool = False  # OVA / OAD / ONA / SP
    version: int | None = None
    strict: bool = True
    suspicious: bool = False


@dataclass(frozen=True)
class NumberingContext:
    season: int | None = None  # from a "Season N" folder
    show_folder: bool = False  # a parent folder looks like a show/movie title
    anime: bool = False  # leading [Group] or CRC present


def _overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    for s, e in spans:  # noqa: SIM110 - a plain loop is ~2x faster than any(); runs per candidate match
        if start < e and s < end:
            return True
    return False


def _expand(first: int, more: str) -> list[int]:
    """'S01E02' + '-03' -> [2, 3]; 'E02E03' -> [2, 3]; range for '-' delimiters."""
    eps = [first]
    for m in _EXPAND_PART.finditer(more):
        delim, num = m.group(1).strip(" .").lower(), int(m.group(2))
        prev = eps[-1]
        if delim.startswith(("-", "~")) and num > prev and num - prev <= MAX_RANGE:
            eps.extend(range(prev + 1, num + 1))
        elif num not in eps:
            eps.append(num)
    return eps


def _version(m: re.Match[str]) -> int | None:
    v = m.groupdict().get("v")
    return int(v) if v else None


# -- strict patterns -----------------------------------------------------------------------

_SXE = re.compile(
    rf"{B0}s(?P<s>\d{{1,4}}){S}?ep?(?P<e>\d{{1,4}})"
    rf"(?P<more>(?:(?:[ .]?e|-e?|&e?|\+e?)\d{{1,4}}(?![0-9]*[pi]{B1}))*)"
    rf"(?:v(?P<v>\d))?{B1}",
    re.IGNORECASE,
)
_WORDS = re.compile(
    rf"{B0}(?:season|saison|staffel|temporada|series){S}*(?P<s>\d{{1,4}}){S}*(?:[,&]{S}*)?"
    rf"(?:episode|episodio|[eé]pisode|folge|ep|e){S}*(?P<e>\d{{1,4}})"
    rf"(?:{S}*(?:-|to|&|and){S}*(?:(?:episode|ep|e){S}*)?(?P<e2>\d{{1,4}}))?{B1}",
    re.IGNORECASE,
)
_NXN = re.compile(
    rf"{B0}(?P<s>\d{{1,2}})x(?P<e>\d{{1,3}})"
    rf"(?P<more>(?:(?:[-&+]|[ .]?x)(?:\d{{1,2}}x)?\d{{1,3}})*)(?:v(?P<v>\d))?{B1}",
    re.IGNORECASE,
)
_DATE_YMD = re.compile(
    rf"{B0}(?P<y>(?:19|20)\d\d)(?P<sep>[ .\-])(?P<m>0[1-9]|1[0-2])(?P=sep)(?P<d>0[1-9]|[12]\d|3[01]){B1}"
)
_DATE_DMY = re.compile(
    rf"{B0}(?P<d>0[1-9]|[12]\d|3[01])(?P<sep>[.\-])(?P<m>0[1-9]|1[0-2])(?P=sep)(?P<y>(?:19|20)\d\d){B1}"
)
# compact yyyymmdd, years limited to 1930-2040 like the other date sanity checks
_DATE_COMPACT = re.compile(
    rf"{B0}(?P<y>19[3-9]\d|20[0-3]\d|2040)(?P<m>0[1-9]|1[0-2])(?P<d>0[1-9]|[12]\d|3[01]){B1}"
)
_MON = r"(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_DATE_NAMED = [
    re.compile(rf"{B0}(?P<d>\d{{1,2}})(?:st|nd|rd|th)?[ .\-]{_MON}[ .\-](?P<y>(?:19|20)\d\d){B1}", re.IGNORECASE),
    re.compile(rf"{B0}{_MON}[ .\-](?P<d>\d{{1,2}})(?:st|nd|rd|th)?,?[ .\-]+(?P<y>(?:19|20)\d\d){B1}", re.IGNORECASE),
    re.compile(rf"{B0}(?P<y>(?:19|20)\d\d)[ .\-]{_MON}[ .\-](?P<d>\d{{1,2}}){B1}", re.IGNORECASE),
]
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
# "1 of 6", "Part 2 of 6" (documentary mini-series)
_OF = re.compile(rf"{B0}(?:part{S}?)?(?P<e>\d{{1,2}}){S}?of{S}?(?P<n>\d{{1,2}}){B1}", re.IGNORECASE)
_SPECIAL = re.compile(
    rf"{B0}(?P<tag>ova|oad|ona)(?:{S}*(?P<e>\d{{1,3}}))?(?:v(?P<v>\d))?{B1}", re.IGNORECASE
)
_SP = re.compile(rf"{B0}SP{S}?(?P<e>\d{{1,3}})(?:v(?P<v>\d))?{B1}")
_ANIME_SN = re.compile(
    rf"{B0}(?:s|season{S}*)(?P<s>\d{{1,2}}) +- +(?P<e>\d{{1,4}})(?:v(?P<v>\d))?{B1}", re.IGNORECASE
)

# -- weak patterns -------------------------------------------------------------------------

_DASH = re.compile(
    r"(?<=\S) +- +(?P<e>\d{1,4})(?:v(?P<v>\d))?(?: *[-~] *(?P<e2>\d{1,4})(?:v\d)?)?(?= |$|[\[\(.])"
)
_EPMARK = re.compile(rf"{B0}(?:episode|ep|e)[ .]?(?P<e>\d{{1,4}})(?:v(?P<v>\d))?{B1}", re.IGNORECASE)
_HASH = re.compile(rf"#(?P<e>\d{{1,4}}){B1}")
_JP = re.compile(r"第(?P<e>\d{1,4})[話话集回]")
_LEADING = re.compile(r" *(?P<e>\d{1,4})(?:v(?P<v>\d))?(?P<sep> *[-.] *| +)(?=\S)")
_DIGITS = re.compile(rf"{B0}(?P<n>\d{{3,4}}){B1}")
_BARE = re.compile(r"(?<= )(?P<e>\d{1,4})(?:v(?P<v>\d))?(?= *(?:\[|\(|$))")
_BRACKET_NUM = re.compile(r"\[(?P<e>\d{1,4})(?:v(?P<v>\d))?\]")
_SEASON_ONLY = re.compile(
    rf"{B0}(?:s(?P<s>\d{{1,2}})|(?:season|saison|staffel|temporada){S}*(?P<s2>\d{{1,4}})){B1}",
    re.IGNORECASE,
)


_EXPAND_PART = re.compile(r"([ .]?[a-z]*|[-&+~][a-z]*)(\d{1,4})", re.IGNORECASE)
_NXN_SEASON = re.compile(r"\d{1,2}x", re.IGNORECASE)

# Every pattern ``find_numbering`` scans with ``_first_free``. The parser's prefilter tells it
# (as indices into this tuple) which of them can match a name at all.
PATTERNS: tuple[re.Pattern[str], ...] = (
    _SXE, _WORDS, _NXN, _DATE_YMD, *_DATE_NAMED, _DATE_COMPACT, _ANIME_SN, _SPECIAL, _SP,
    _DASH, _OF, _EPMARK, _HASH, _JP, _DIGITS, _BRACKET_NUM, _BARE, _DATE_DMY, _SEASON_ONLY,
)
_EVERY_PATTERN = frozenset(range(len(PATTERNS)))
(_I_SXE, _I_WORDS, _I_NXN, _I_ANIME_SN, _I_SPECIAL, _I_SP, _I_DASH, _I_OF, _I_DIGITS, _I_DATE_DMY,
 _I_SEASON_ONLY) = map(PATTERNS.index, (_SXE, _WORDS, _NXN, _ANIME_SN, _SPECIAL, _SP, _DASH, _OF, _DIGITS,
                                         _DATE_DMY, _SEASON_ONLY))
_DATES = tuple((PATTERNS.index(rx), rx) for rx in (_DATE_YMD, *_DATE_NAMED, _DATE_COMPACT))
_MARKS = tuple((PATTERNS.index(rx), rx, kind) for rx, kind in ((_EPMARK, "epmark"), (_HASH, "hash"), (_JP, "jp")))
_ANIME_BARE = tuple((PATTERNS.index(rx), rx) for rx in (_BRACKET_NUM, _BARE))


def _first_free(rx: re.Pattern[str], text: str, spans: list[tuple[int, int]], start: int = 0
                ) -> Iterable[re.Match[str]]:
    return (m for m in rx.finditer(text, start) if not _overlaps(m.start(), m.end(), spans))


def find_numbering(
    text: str, *, spans: list[tuple[int, int]], ctx: NumberingContext, title_start: int = 0,
    live: AbstractSet[int] | None = None,
) -> Numbering | None:
    """Return the most specific numbering found in ``text`` (or None).

    ``live``: indices into ``PATTERNS`` that may match ``text`` (parser prefilter; other
    numbers are ignored); None scans every pattern.
    """
    if live is None:
        live = _EVERY_PATTERN
    for m in _first_free(_SXE, text, spans, title_start) if _I_SXE in live else ():
        season = int(m.group("s"))
        eps = _expand(int(m.group("e")), m.group("more") or "")
        return Numbering("sxe", m.start(), m.end(), season=season, episodes=eps,
                         special=season == 0, version=_version(m))

    for m in _first_free(_WORDS, text, spans, title_start) if _I_WORDS in live else ():
        season, first = int(m.group("s")), int(m.group("e"))
        eps = [first]
        if m.group("e2"):
            last = int(m.group("e2"))
            eps = list(range(first, last + 1)) if 0 < last - first <= MAX_RANGE else [first, last]
        return Numbering("words", m.start(), m.end(), season=season, episodes=eps, special=season == 0)

    for m in _first_free(_NXN, text, spans, title_start) if _I_NXN in live else ():
        season = int(m.group("s"))
        eps = _expand(int(m.group("e")), _NXN_SEASON.sub("", m.group("more") or ""))
        return Numbering("nxn", m.start(), m.end(), season=season, episodes=eps,
                         special=season == 0, version=_version(m))

    for i, rx in _DATES:
        for m in _first_free(rx, text, spans, title_start) if i in live else ():
            gd = m.groupdict()
            month = _MONTHS[gd["mon"][:3].lower()] if gd.get("mon") else int(gd["m"])
            try:
                d = date(int(gd["y"]), month, int(gd["d"]))
            except ValueError:
                continue
            return Numbering("date", m.start(), m.end(), air_date=d)

    for m in _first_free(_ANIME_SN, text, spans, title_start) if _I_ANIME_SN in live else ():
        e = int(m.group("e"))
        if e in YEAR_LIKE:
            continue
        return Numbering("anime_sn", m.start(), m.end(), season=int(m.group("s")), episodes=[e],
                         version=_version(m))

    for m in _first_free(_SPECIAL, text, spans, title_start) if _I_SPECIAL in live else ():
        n = Numbering("special", m.start(), m.end(), special=True, special_marker=True,
                      version=_version(m), strict=False)
        if m.group("e"):
            n.season, n.episodes = 0, [int(m.group("e"))]
        return n
    for m in _first_free(_SP, text, spans, title_start) if _I_SP in live else ():
        return Numbering("special", m.start(), m.end(), season=0, episodes=[int(m.group("e"))],
                         special=True, special_marker=True, version=_version(m), strict=False)

    weak = _find_weak(text, spans=spans, ctx=ctx, title_start=title_start, live=live)
    if weak is not None:
        return weak

    for m in _first_free(_DATE_DMY, text, spans, title_start) if _I_DATE_DMY in live else ():
        try:
            d = date(int(m.group("y")), int(m.group("m")), int(m.group("d")))
        except ValueError:
            continue
        return Numbering("date", m.start(), m.end(), air_date=d)

    for m in _first_free(_SEASON_ONLY, text, spans, title_start) if _I_SEASON_ONLY in live else ():
        season = int(m.group("s") or m.group("s2"))
        if season > 100:
            continue
        return Numbering("season", m.start(), m.end(), season=season, strict=False)
    return None


def split_season_episode(num: int, season: int | None) -> int:
    """A 3-4 digit ``SEE`` / ``SSEE`` code whose leading digits are the known season -> the episode:
    301 in Season 3 -> 1, 1012 in Season 10 -> 12, 101 in Season 1 -> 1. Anything else is kept."""
    raw = str(num)
    if season is None or len(raw) < 3:
        return num
    lead = str(season)
    if raw.startswith(lead) and len(raw) - len(lead) == 2 and int(raw[len(lead):]) > 0:
        return int(raw[len(lead):])
    return num


def _weak_numbering(kind: str, m: re.Match[str], ctx: NumberingContext, first: int,
                    last: int | None = None) -> Numbering:
    nums = [first]
    if last is not None and 0 < last - first <= MAX_RANGE:
        nums = list(range(first, last + 1))
    n = Numbering(kind, m.start(), m.end(), version=_version(m), strict=False)
    if ctx.season is not None:
        if not ctx.anime:  # "The Venture Bros - 301 - ..." in "Season 03" is S03E01, not S03E301
            nums = [split_season_episode(e, ctx.season) for e in nums]
        n.season, n.episodes = ctx.season, nums
        n.special = ctx.season == 0
        n.suspicious = ctx.season > 30 or max(nums) > 50
    else:
        n.absolute = nums
        n.suspicious = max(nums) > 1000
    return n


def _find_weak(text: str, *, spans: list[tuple[int, int]], ctx: NumberingContext,
               title_start: int, live: AbstractSet[int] = _EVERY_PATTERN) -> Numbering | None:
    for m in _first_free(_DASH, text, spans, title_start) if _I_DASH in live else ():
        e = int(m.group("e"))
        if e in YEAR_LIKE:
            continue
        e2 = int(m.group("e2")) if m.group("e2") else None
        return _weak_numbering("dash", m, ctx, e, e2)

    for m in _first_free(_OF, text, spans, title_start) if _I_OF in live else ():
        e, total = int(m.group("e")), int(m.group("n"))
        if 0 < e <= total:
            return _weak_numbering("of", m, ctx, e)

    for i, rx, kind in _MARKS:
        for m in _first_free(rx, text, spans, title_start) if i in live else ():
            e = int(m.group("e"))
            if e in YEAR_LIKE:
                continue
            return _weak_numbering(kind, m, ctx, e)

    m = _LEADING.match(text, title_start)
    if m and not _overlaps(m.start("e"), m.end("e"), spans):
        e = int(m.group("e"))
        rest = text[m.end():]
        dashed = "-" in m.group("sep")
        if e not in YEAR_LIKE and rest.strip() and (ctx.season is not None or (ctx.show_folder and dashed)):
            if ctx.season and e >= 100 and e // 100 == ctx.season:
                e = e % 100
            if e < 1000:
                return _weak_numbering("leading", m, ctx, e)

    if ctx.season is not None:
        for m in _first_free(_DIGITS, text, spans, title_start) if _I_DIGITS in live else ():
            n = int(m.group("n"))
            if n in YEAR_LIKE or n // 100 != ctx.season or n % 100 == 0:
                continue
            num = Numbering("digits", m.start(), m.end(), season=ctx.season, episodes=[n % 100],
                            special=ctx.season == 0, strict=False)
            return num

    if ctx.anime:
        for i, rx in _ANIME_BARE:
            for m in _first_free(rx, text, spans, title_start) if i in live else ():
                e = int(m.group("e"))
                if e in YEAR_LIKE or m.start() <= title_start:
                    continue
                return _weak_numbering("bare", m, ctx, e)
    return None
