"""Whitelisted methods (SPEC §8.4 + FileBot helper names) and the regex guard."""

from __future__ import annotations

import time
from datetime import date

import pytest
from format_helpers import render

from schema_app.core.format import FormatEvalError
from schema_app.core.format.methods import METHODS, UNSUPPORTED_FILEBOT_METHODS

CASES: list[tuple[str, dict, str]] = [
    # padding & numbers
    ("{e.pad(3)}", {"e": 7}, "007"),
    ("{s.pad(2)}", {"s": 12}, "12"),
    ("{'7'.pad(2)}", {}, "07"),
    ("{'Part 3'.pad(2)}", {}, "Part 03"),
    ("{'ab'.pad(4, '_')}", {}, "__ab"),
    ("{es.pad(2)}", {"es": [2, 3]}, "[02, 03]"),
    ("{'7'.padLeft(3, '0')}", {}, "007"),
    ("{'ab'.padRight(4, '_')}", {}, "ab__"),
    ("{rating.round()}", {"rating": 7.5}, "8"),
    ("{rating.round(1)}", {"rating": 7.46}, "7.5"),
    ("{'007'.toInteger() + 1}", {}, "8"),
    ("{y.toString() + '!'}", {"y": 2019}, "2019!"),
    ("{'Rocky 2'.roman()}", {}, "Rocky II"),
    ("{s.roman()}", {"s": 14}, "XIV"),
    # case
    ("{n.lower()}", {"n": "Starfall Station"}, "starfall station"),
    ("{n.upper()}", {"n": "Starfall Station"}, "STARFALL STATION"),
    ("{n.upper(/\\bs\\w*/)}", {"n": "star sun moon"}, "STAR SUN moon"),
    ("{n.toLowerCase()}", {"n": "AB"}, "ab"),
    ("{n.toUpperCase()}", {"n": "ab"}, "AB"),
    ("{n.upperInitial()}", {"n": "the man who wasn't there"}, "The Man Who Wasn't There"),
    ("{n.lowerTrail()}", {"n": "THE OFFICE"}, "The Office"),
    ("{n.capitalize()}", {"n": "starfall"}, "Starfall"),
    # separators
    ("{n.colon(' - ')}", {"n": "Avengers: Endgame"}, "Avengers - Endgame"),
    ("{n.colon('.')}", {"n": "Avengers: Endgame"}, "Avengers. Endgame"),
    ("{n.colon('x', ' - ')}", {"n": "16:9 Cut: Final"}, "16x9 Cut - Final"),
    ("{n.space('.')}", {"n": "Starfall  Station"}, "Starfall.Station"),
    ("{'a/b'.slash('-')}", {}, "a-b"),
    # replace & regex
    ("{n.replace(' ', '_')}", {"n": "Starfall Station"}, "Starfall_Station"),
    ("{t.replaceAll(/[!?.]+$/, '')}", {"t": "Why"}, "Why"),
    ("{t.replaceAll(/(\\w+) (\\w+)/, '$2 $1')}", {"t": "Dead Reckoning"}, "Reckoning Dead"),
    ("{t.replaceAll(/\\p{Punct}/)}", {"t": "a,b;c"}, "abc"),
    ("{n.replaceFirst(/^The /, '')}", {"n": "The Office"}, "Office"),
    ("{t.removeAll(/[()]/)}", {"t": "Dead Reckoning (1)"}, "Dead Reckoning 1"),
    ("{n.remove(', The', 'x')}", {"n": "Office, The x"}, "Office"),
    ("{t.before(' (')}", {"t": "Dead Reckoning (1)"}, "Dead Reckoning"),
    ("{t.after(/: /)}", {"t": "Either Or: Part One"}, "Part One"),
    ("{fn.match(/S\\d+E\\d+/)}", {"fn": "Show.S01E02.720p"}, "S01E02"),
    ("{fn.match(/S(\\d+)E\\d+/)}", {"fn": "Show.S01E02.720p"}, "01"),
    ("{fn.match(/(S)(\\d+)/, 2)}", {"fn": "Show.S01E02"}, "01"),
    ("{fn.matchAll(/\\d+/).join('-')}", {"fn": "a1b22c333"}, "1-22-333"),
    ("{vf.matches(/\\d+p/)}", {"vf": "1080p"}, "true"),
    ("{'a.b.c'.split(/\\./).size()}", {}, "3"),
    ("{'a b  c'.tokenize().join(',')}", {}, "a,b,c"),
    ("{'a-b_c'.tokenize('-_').join(',')}", {}, "a,b,c"),
    # transliteration & names
    ("{n.ascii()}", {"n": "Pokémon Straße"}, "Pokemon Strasse"),
    ("{n.ascii()}", {"n": "Über"}, "Ueber"),
    ("{t.asciiQuotes()}", {"t": "It’s “fine”"}, "It's 'fine'"),
    ("{n.acronym()}", {"n": "Starfall Station"}, "SS"),
    ("{n.initialName()}", {"n": "Sam Reyes"}, "S. Reyes"),
    ("{n.sortName()}", {"n": "The Office"}, "Office, The"),
    ("{n.sortName('$2')}", {"n": "The Office"}, "Office"),
    ("{n.sortName()}", {"n": "Starfall"}, "Starfall"),
    ("{n.sortInitial()}", {"n": "The Office"}, "O"),
    ("{n.sortInitial()}", {"n": "2001 A Space Odyssey"}, "0-9"),
    ("{n.sortInitial()}", {"n": "Élan"}, "E"),
    ("{t.truncate(8)}", {"t": "Dead Reckoning"}, "Dead Rec"),
    ("{t.truncate(10, /\\s/)}", {"t": "Dead Reckoning Again"}, "Dead"),
    ("{' x '.trim()}", {}, "x"),
    ("{n.replaceTrailingBrackets()}", {"n": "The Office (US)"}, "The Office"),
    ("{n.replaceTrailingBrackets(' US')}", {"n": "The Office (US)"}, "The Office US"),
    ("{n.removeBrackets()}", {"n": "Title (2019) [x] end"}, "Title end"),
    ("{t.replacePart()}", {"t": "Dead Reckoning (1)"}, "Dead Reckoning, Part 1"),
    ("{t.replacePart(' - Part $1')}", {"t": "Dead Reckoning Part 2"}, "Dead Reckoning - Part 2"),
    ("{'What?'.removeIllegalCharacters()}", {}, "What"),
    ("{'A? B*'.replaceIllegalCharacters()}", {}, "A？ B∗"),
    # strings & lists
    ("{n.take(4)}", {"n": "Starfall"}, "Star"),
    ("{n.drop(4)}", {"n": "Starfall"}, "fall"),
    ("{genres.take(1)}", {"genres": ["Drama", "Sci-Fi"]}, "[Drama]"),
    ("{n.substring(0, 4)}", {"n": "Starfall"}, "Star"),
    ("{genres.join(', ')}", {"genres": ["Drama", "Sci-Fi"]}, "Drama, Sci-Fi"),
    ("{[vf, vc].joining(' ', '[', ']')}", {"vf": "1080p", "vc": "x264"}, "[1080p x264]"),
    ("{genres.first()}", {"genres": ["Drama", "Sci-Fi"]}, "Drama"),
    ("{genres.last()}", {"genres": ["Drama", "Sci-Fi"]}, "Sci-Fi"),
    ("{n.first()}", {"n": "Star"}, "S"),
    ("{genres.sort().join(',')}", {"genres": ["b", "a", "C"]}, "a,b,C"),
    ("{es.sort().join(',')}", {"es": [10, 2, 3]}, "2,3,10"),
    ("{alias.unique().join(',')}", {"alias": ["a", "b", "a"]}, "a,b"),
    ("{genres.reverse().join(',')}", {"genres": ["a", "b"]}, "b,a"),
    ("{'abc'.reverse()}", {}, "cba"),
    ("{es.size()}", {"es": [2, 3]}, "2"),
    ("{n.length()}", {"n": "Starfall"}, "8"),
    ("{tags.isEmpty()}", {"tags": []}, "true"),
    ("{genres.contains('Drama')}", {"genres": ["Drama"]}, "true"),
    ("{n.contains('fall')}", {"n": "Starfall"}, "true"),
    ("{n.startsWith('Star')}", {"n": "Starfall"}, "true"),
    ("{n.endsWith('x')}", {"n": "Starfall"}, "false"),
    ("{vf.equalsIgnoreCase('1080P')}", {"vf": "1080p"}, "true"),
    ("{n.indexOf('fall')}", {"n": "Starfall"}, "4"),
    ("{es.max()}-{es.min()}-{es.sum()}", {"es": [2, 3]}, "3-2-5"),
    ("{n.plus('!')}", {"n": "Hi"}, "Hi!"),
    ("{n.minus('The ')}", {"n": "The Office"}, "Office"),
    ("{y.compareTo(2000)}", {"y": 2019}, "1"),
    ("{n.lowerCase()}", {"n": "AB"}, "ab"),
    ("{n.upperCase()}", {"n": "ab"}, "AB"),
    # format()
    ("{d.format('yyyy.MM.dd')}", {"d": date(2024, 3, 4)}, "2024.03.04"),
    ("{d.format('MMM d, yyyy')}", {"d": date(2024, 3, 4)}, "Mar 4, yyyy".replace("yyyy", "2024")),
    ("{d.format('EEEE dd MMMM yy')}", {"d": date(2024, 3, 4)}, "Monday 04 March 24"),
    ("{d.format(\"'Aired' yyyy\")}", {"d": date(2024, 3, 4)}, "Aired 2024"),
    ("{rating.format('0.00')}", {"rating": 7.5}, "7.50"),
    ("{bytes.format('#,##0')}", {"bytes": 1234567}, "1,234,567"),
    ("{e.format('000')}", {"e": 7}, "007"),
]


@pytest.mark.parametrize(("expr", "values", "expected"), CASES)
def test_method(expr: str, values: dict, expected: str) -> None:
    assert render(expr, strict=True, **values) == expected


def test_every_registered_method_is_covered_by_a_case() -> None:
    used = {name for name in METHODS if any(f".{name}(" in expr for expr, _, _ in CASES)}
    assert set(METHODS) - used == set()


@pytest.mark.parametrize(
    ("expr", "values", "fragment"),
    [
        ("{n.pad()}", {"n": "x"}, "takes 1–2 argument(s)"),
        ("{n.replace('a')}", {"n": "x"}, "takes 2 argument(s)"),
        ("{n.pad(0)}", {"n": 5}, "at least 1"),
        ("{genres.lower()}", {"genres": ["a"]}, "expects text"),
        ("{n.sort()}", {"n": "abc"}, "needs a List"),
        ("{n.substring(5, 2)}", {"n": "abc"}, "out of range"),
        ("{n.toInteger()}", {"n": "abc"}, "not a whole number"),
        ("{n.format('yyyy')}", {"n": "abc"}, "needs a date or a number"),
        ("{d.format('QQ')}", {"d": date(2024, 1, 1)}, "unsupported date pattern"),
        ("{n.transliterate('Any-Latin')}", {"n": "x"}, "Unsupported FileBot method"),
        ("{n.frobnicate()}", {"n": "x"}, "Unknown method"),
        ("{n.replaceAll(/(a+)+$/, '')}", {"n": "aaa"}, "nested repetition"),
        ("{n.replaceAll(/(.*)*/, '')}", {"n": "aaa"}, "nested repetition"),
        ("{n.replaceAll(/[/, '')}", {"n": "aaa"}, "Invalid regex"),
        ("{n.replaceAll(/\\p{javaLowerCase}/, '')}", {"n": "aaa"}, "Unsupported regex class"),
        ("{n.replaceAll(/(a)/, '$2')}", {"n": "aaa"}, "No group 2"),
    ],
)
def test_method_errors(expr: str, values: dict, fragment: str) -> None:
    with pytest.raises(FormatEvalError) as info:
        render(expr, strict=True, **values)
    assert fragment in info.value.message


def test_overlong_regex_is_rejected() -> None:
    with pytest.raises(FormatEvalError) as info:
        render("{n.replaceAll('" + "a" * 301 + "', '')}", strict=True, n="x")
    assert "too long" in info.value.message


def test_overlong_subject_is_rejected() -> None:
    with pytest.raises(FormatEvalError) as info:
        render("{n.removeAll(/a/)}", strict=True, n="b" * 6000)
    assert "too long" in info.value.message


@pytest.mark.parametrize(
    ("expr", "values"),
    [
        ("A{t.before('zzz')}B", {"t": "abc"}),
        ("A{t.after('zzz')}B", {"t": "abc"}),
        ("A{fn.match(/S\\d+/)}B", {"fn": "movie"}),
        ("A{fn.matchAll(/\\d/)}B", {"fn": "movie"}),
        ("A{[t].joining(' ')}B", {"t": ""}),
        ("A{genres.first()}B", {"genres": []}),
    ],
)
def test_not_found_helpers_make_the_block_empty(expr: str, values: dict) -> None:
    assert render(expr, strict=True, **values) == "AB"


def test_named_groups_and_java_named_syntax() -> None:
    assert render("{fn.replaceAll(/(?<s>S\\d+)(?<e>E\\d+)/, '${e}${s}')}", strict=True, fn="S01E02") == "E02S01"


def test_unsupported_method_list_does_not_overlap_supported() -> None:
    assert not (set(METHODS) & UNSUPPORTED_FILEBOT_METHODS)


def test_method_metadata_is_complete() -> None:
    for spec in METHODS.values():
        assert spec.signature.startswith(spec.name) or spec.name in spec.signature
        assert spec.description and spec.example


# --------------------------------------------------------------------------- regex timeout guard
# Shapes the static nested-quantifier check can't see. On ``re`` these took seconds (and grew
# exponentially); user patterns now run on the ``regex`` package under a per-call time budget.

_SLOW_SUBJECT = "a" * 40 + "bc"  # 'c' present, so the engine can't bail out early


@pytest.mark.parametrize(
    ("expr", "subject"),
    [
        ("{n.replaceAll(/(a|aa)*c/, 'X')}", "a" * 32),
        ("{n.replaceAll(/\\d*\\d*\\d*\\d*\\d*x/, 'X')}", "1" * 80),
    ],
)
def test_audit_backtracking_patterns_are_fast(expr: str, subject: str) -> None:
    started = time.perf_counter()
    assert render(expr, strict=True, n=subject) == subject
    assert time.perf_counter() - started < 1.0


@pytest.mark.parametrize(
    "expr",
    [
        "{n.replaceAll(/(a|aa)*c/, 'X')}",
        "{n.replaceFirst(/(a|aa)*c/, 'X')}",
        "{n.removeAll(/(a|aa)*c/)}",
        "{n.remove(/(a|aa)*c/)}",
        "{n.lower(/(a|aa)*c/)}",
        "{n.before(/(a|aa)*c/)}",
        "{n.after(/(a|aa)*c/)}",
        "{n.match(/(a|aa)*c/)}",
        "{n.matchAll(/(a|aa)*c/)}",
        "{n.matches(/(a|aa)*c/)}",
        "{n.split(/(a|aa)*c/)}",
        "{n.truncate(41, /(a|aa)*c/)}",
        "{m.match(/(?:\\d|\\d\\d)*\\D/)}",
    ],
)
def test_slow_regex_is_stopped_with_a_format_error(expr: str) -> None:
    started = time.perf_counter()
    with pytest.raises(FormatEvalError) as info:
        render(expr, strict=True, n=_SLOW_SUBJECT, m="1" * 40)
    assert time.perf_counter() - started < 1.0
    assert "took longer than" in info.value.message


def test_slow_regex_does_not_poison_later_calls() -> None:
    with pytest.raises(FormatEvalError):
        render("{n.match(/(a|aa)*c/)}", strict=True, n=_SLOW_SUBJECT)
    assert render("{n.match(/(a|aa)*c/)}", strict=True, n="aaac") == "a"


@pytest.mark.parametrize(
    ("expr", "values", "expected"),
    [
        ("{n.split(/(?=[A-Z])/)}", {"n": "aBcDe"}, "[a, Bc, De]"),
        ("{n.replaceAll(/(?<x>o)\\k<x>/, '0')}", {"n": "foo boo"}, "f0 b0"),
        ("{n.replaceAll(/(?i)the /, '')}", {"n": "THE Office"}, "Office"),
        ("{n.replaceAll(/\\p{L}+/, 'w')}", {"n": "Amélie 2001"}, "w 2001"),
        ("{n.replaceAll(/[\\p{Punct}]/, '')}", {"n": "a.b,c!"}, "abc"),
        ("{n.replaceAll(/(?<=\\d)x(?=\\d)/, '-')}", {"n": "16x9"}, "16-9"),
        ("{n.replaceAll(/\\s+$/, '')}", {"n": "a  "}, "a"),
        ("{n.matchAll(/\\d+/)}", {"n": "S01E02"}, "[01, 02]"),
        ("{n.matches(/S\\d{2}E\\d{2}/)}", {"n": "S01E02"}, "true"),
        ("{n.match(/E(\\d+)/)}", {"n": "S01E02"}, "02"),
        ("{n.before(' (')}", {"n": "Movie (2001)"}, "Movie"),
    ],
)
def test_regex_methods_behave_like_re(expr: str, values: dict, expected: str) -> None:
    assert render(expr, strict=True, **values) == expected
