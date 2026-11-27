"""Shared string normalization for matching (SPEC §6.1).

Everything here is *comparison only*: the display title a provider returns is never
rewritten with these helpers. The goal is that "The Quiet Meridian", "quiet.meridian"
and "THE QUIET MERIDIAN (2019)" all collapse to the same key, and that "Rocky II" and
"Rocky 2" compare equal.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

from unidecode import unidecode

__all__ = [
    "LEADING_ARTICLES",
    "collapse_spaces",
    "normalize_title",
    "roman_to_int",
    "strip_trailing_year",
    "title_key",
]

LEADING_ARTICLES: tuple[str, ...] = ("the", "a", "an")

# Roman numerals we translate (1..39). Larger values are rare in titles and the risk
# of mangling ordinary words ("mix", "dim", "civil") grows quickly beyond this range.
_ROMAN_VALUES = {"i": 1, "v": 5, "x": 10, "l": 50}


def roman_to_int(token: str) -> int | None:
    """Return the value of a well-formed roman numeral between 1 and 39, else None."""
    t = token.lower()
    if not t or len(t) > 6 or any(ch not in "ivx" for ch in t):
        return None
    total = 0
    prev = 0
    for ch in reversed(t):
        val = _ROMAN_VALUES[ch]
        if val < prev:
            total -= val
        else:
            total += val
            prev = val
    if not 1 <= total <= 39:
        return None
    # Reject non-canonical spellings such as "iiii" or "vv" by re-encoding.
    return total if _int_to_roman(total) == t else None


def _int_to_roman(n: int) -> str:
    out = []
    for val, sym in ((10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):
        while n >= val:
            out.append(sym)
            n -= val
    return "".join(out)


_TRAILING_YEAR = re.compile(r"\s*[\(\[]\s*(?:19|20)\d{2}\s*[\)\]]\s*$")
_APOSTROPHES = re.compile(r"['‘’ʼ`´]")
_NON_WORD = re.compile(r"[^0-9a-z]+")
_SPACES = re.compile(r"\s+")


def strip_trailing_year(title: str) -> str:
    """'Harbor Lights (2021)' -> 'Harbor Lights' (TVDB-style disambiguation suffix)."""
    return _TRAILING_YEAR.sub("", title).strip()


def collapse_spaces(text: str) -> str:
    return _SPACES.sub(" ", text).strip()


def _join_initials(words: list[str]) -> list[str]:
    """['s', 'w', 'a', 't'] -> ['swat']: dotted acronyms compare equal to their joined form."""
    out: list[str] = []
    run: list[str] = []
    for w in words:
        if len(w) == 1 and w.isalpha():
            run.append(w)
            continue
        if len(run) >= 2:
            out.append("".join(run))
        else:
            out.extend(run)
        run = []
        out.append(w)
    if len(run) >= 2:
        out.append("".join(run))
    else:
        out.extend(run)
    return out


@lru_cache(maxsize=8192)
def normalize_title(title: str | None, *, strip_articles: bool = True) -> str:
    """Comparison key for a title.

    lowercase · Unidecode transliteration · ``&`` -> ``and`` · apostrophes dropped ·
    other punctuation -> space · dotted initials joined · roman numerals -> arabic ·
    leading English article removed (only when something remains) · whitespace collapsed.
    """
    if not title:
        return ""
    text = unicodedata.normalize("NFKC", title)
    text = strip_trailing_year(text)
    text = unidecode(text).lower()
    text = text.replace("&", " and ").replace("+", " plus ")
    text = _APOSTROPHES.sub("", text)
    text = _NON_WORD.sub(" ", text)
    words = text.split()
    if not words:
        return ""
    words = _join_initials(words)
    converted: list[str] = []
    for w in words:
        val = roman_to_int(w)
        # A lone "i"/"v"/"x" is only treated as a numeral after another word
        # ("Rocky V", "Episode I") so "I, Robot" and "V for Vendetta" keep their letter.
        if val is not None and (len(w) > 1 or converted):
            converted.append(str(val))
        else:
            converted.append(w)
    if strip_articles and len(converted) > 1 and converted[0] in LEADING_ARTICLES:
        converted = converted[1:]
    return " ".join(converted)


def title_key(title: str | None) -> str:
    """Grouping key: normalized title with all spaces removed (``"Star-Fall"`` == ``"Starfall"``)."""
    return normalize_title(title).replace(" ", "")
