"""The parser's literal prefilter may only skip regexes that cannot match (core/parser/_prefilter).

If it ever skipped a regex that does match, parse results would silently change; these tests
attack that property directly, on top of the end-to-end golden snapshot.
"""

from __future__ import annotations

import json
import random
import re
import sys
from pathlib import Path

import pytest

from schema_app.core.parser import _core
from schema_app.core.parser._prefilter import CASE_SPECIALS, Prefilter, needles_of

GOLDEN = Path(__file__).with_name("parser_golden.jsonl")
FILTER = _core._FILTER


def test_case_specials_are_exactly_the_non_ascii_case_folds_of_ascii() -> None:
    """Every non-ASCII code point that matches some ASCII character under IGNORECASE."""
    ascii_any = re.compile(r"[\x00-\x7f]", re.IGNORECASE)
    found = {chr(c) for c in range(128, sys.maxunicode + 1) if ascii_any.match(chr(c))}
    assert found == CASE_SPECIALS


def test_needle_derivation_examples() -> None:
    ci = re.IGNORECASE
    assert needles_of(re.compile(r"(?<![0-9A-Za-z])(?:dts|truehd)(?![0-9A-Za-z])", ci)) == {"dts", "truehd"}
    # every match of "blu ray" contains "blu"; needles never hold a space
    assert needles_of(re.compile(r"blu[ .]?ray", ci)) == {"blu", "blu.ray", "bluray"}
    assert needles_of(re.compile(r"(?:REAL )?PROPER")) == {"PROPER"}  # "REAL PROPER" holds "PROPER"
    # \d may be any Unicode digit, so only ASCII texts get digit needles
    assert needles_of(re.compile(r"s\d{1,2}", ci)) == {"s"}
    assert needles_of(re.compile(r"s\d{1,2}", ci), ascii_text=True) == {f"s{d}" for d in range(10)}
    # a regex that needs a non-ASCII character can never match an ASCII text
    assert needles_of(re.compile(r"第\d+話"), ascii_text=True) == frozenset()
    assert needles_of(re.compile(r"第\d+話")) == {"第"}
    # a case-insensitive non-ASCII literal may fold onto ASCII ("K" matches "k")
    assert needles_of(re.compile("K", ci), ascii_text=True) is None
    # nothing usable: always run
    assert needles_of(re.compile(r"\d+")) is None
    assert needles_of(re.compile(r"(?:x|y)?z?")) is None


def test_most_parser_regexes_get_needles() -> None:
    """Guards the speed-up itself: a stdlib change that breaks derivation must not go unnoticed."""
    assert len(FILTER.regexes) > 200
    assert len(FILTER.ascii.always) <= 3
    assert len(FILTER.unicode.always) <= 8


def _golden_texts() -> list[str]:
    texts = []
    for line in GOLDEN.read_text(encoding="utf-8").splitlines():
        path = json.loads(line)["path"]
        texts += [part.replace("_", " ") for part in re.split(r"[\\/]+", path) if part]
    return texts


def _fuzz_texts(seed: int, count: int) -> list[str]:
    rng = random.Random(seed)
    base = _golden_texts()
    alphabet = " ._-[](){}#+&'~,0123456789xXsSeEpPdDtThHvV" "İıſKé第話٣"
    out = []
    for _ in range(count):
        text = rng.choice(base)
        chars = list(text)
        for _ in range(rng.randint(0, 6)):
            i = rng.randrange(len(chars) + 1)
            roll = rng.random()
            if roll < 0.4:
                chars.insert(i, rng.choice(alphabet))
            elif roll < 0.6 and chars:
                del chars[min(i, len(chars) - 1)]
            elif chars:
                j = min(i, len(chars) - 1)
                chars[j] = chars[j].swapcase()
        out.append("".join(chars))
    out += ["".join(rng.choice(alphabet + "abcdefghijklmnopqrstuvwxyz") for _ in range(rng.randint(0, 30)))
            for _ in range(count // 4)]
    return out


def _masked(text: str, rng: random.Random) -> str:
    """The parser only ever edits a name by replacing spans with spaces."""
    if not text:
        return text
    start = rng.randrange(len(text))
    end = min(len(text), start + rng.randint(1, 12))
    return text[:start] + " " * (end - start) + text[end:]


@pytest.mark.parametrize("seed", [1, 2])
def test_prefilter_never_skips_a_regex_that_matches(seed: int) -> None:
    rng = random.Random(seed)
    texts = (_golden_texts()[seed - 1::2] if seed <= 2 else []) + _fuzz_texts(seed, 1200)
    misses = []
    for text in texts:
        live = FILTER.live(text)
        for variant in (text, _masked(text, rng)):
            for i, rx in enumerate(FILTER.regexes):
                if i not in live and rx.search(variant):
                    misses.append((text, variant, rx.pattern))
    assert not misses, misses[:5]


def test_prefilter_handles_case_sensitive_needles_found_lower_cased() -> None:
    rx_cs = re.compile(r"(?<![0-9A-Za-z])ENG(?![0-9A-Za-z])")
    rx_ci = re.compile(r"(?<![0-9A-Za-z])eng(?![0-9A-Za-z])", re.IGNORECASE)
    pf = Prefilter([rx_cs, rx_ci])
    assert pf.live("Show.ENG.mkv") == {0, 1}
    assert pf.live("Show.eng.mkv") == {1}
    assert pf.live("Show.Eng.mkv") == {1}
    assert pf.live("Show.mkv") == set()
    assert pf.live("Show.İng.mkv") == {0, 1}  # case special present: everything runs
