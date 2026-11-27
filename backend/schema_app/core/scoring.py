"""Candidate scoring (SPEC §6.3) with human-readable reasons.

A score is a sum of signed signals clamped to 0..1. The constants below are the
tuning knobs; they were chosen so that, with the SPEC weights for year / episode /
season plausibility kept as written, an exact title plus one confirming signal
(the year for movies, an existing episode for series) clears the 0.85 auto-approve
bar, while a wrong year or a missing episode cannot.

Title similarity deviates from a plain ``token_set_ratio`` on purpose: token-set
treats a subset as a perfect match, so "Starfall" (a decoy) would score 100% against
"Starfall Station". We blend set and sort ratios so extra words cost something.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache

from rapidfuzz import fuzz

from schema_app.core.normalize import normalize_title
from schema_app.domain import MediaKind

# --------------------------------------------------------------------------- constants

CANDIDATE_CUTOFF = 0.72  # search results below this title similarity are dropped (FileBot default)
MAX_RESULTS_PER_QUERY = 5  # keep at most this many search hits per query for episode matching
TITLE_WEIGHT = 0.70
YEAR_EXACT = 0.20
YEAR_NEAR = 0.10
YEAR_MISMATCH = -0.25
EPISODE_FOUND = 0.15
EPISODE_MISSING = -0.30
KIND_AGREE = 0.05
ANIME_AGREE = 0.05
KIND_MISMATCH = -0.20
POPULARITY_MAX = 0.03
SEASON_IMPLAUSIBLE = -0.20
AMBIGUITY_WINDOW = 0.05
AMBIGUITY_PENALTY = 0.10
STOP_SCORE = 0.85  # a provider whose best candidate reaches this (and resolved) ends the chain
SPECIAL_TITLE_THRESHOLD = 0.80  # fuzzy episode-title match for un-numbered specials
EPISODE_TITLE_AGREE = 0.60  # filename episode title vs the provider episode the number points at
EPISODE_TITLE_STRONG = 0.80  # whole-series title lookup: accept the best episode at or above this ...
EPISODE_TITLE_MARGIN = 0.20  # ... when it beats the runner-up by this much
EPISODE_TITLE_MISMATCH = -0.15  # the number resolved, but to an episode with a different title

EPISODIC = (MediaKind.SERIES, MediaKind.ANIME)


# --------------------------------------------------------------------------- similarity


@lru_cache(maxsize=65536)
def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a.replace(" ", "") == b.replace(" ", ""):
        return 0.98
    set_r = fuzz.token_set_ratio(a, b) / 100.0
    sort_r = fuzz.token_sort_ratio(a, b) / 100.0
    plain = fuzz.ratio(a, b) / 100.0
    return max(plain, sort_r, 0.5 * set_r + 0.5 * sort_r)


def title_similarity(a: str | None, b: str | None) -> float:
    """0..1 similarity of two titles after normalization (articles, roman numerals, punctuation)."""
    return _similarity(normalize_title(a), normalize_title(b))


def best_title_similarity(queries: Iterable[str | None], names: Iterable[str | None]) -> float:
    names = [n for n in names if n]
    best = 0.0
    for q in queries:
        if not q:
            continue
        for n in names:
            s = title_similarity(q, n)
            if s > best:
                best = s
                if best >= 1.0:
                    return 1.0
    return best


_EP_STOP = frozenset({"the", "a", "an", "of", "and", "part"})
_ROMAN_PART = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6}
_PART_SUFFIX = re.compile(
    r"(?:[\s,:;\-]+(?:part|pt)\.?\s*(?P<n>\d{1,2}|i{1,3}|iv|vi?)|\s*\((?:part\s*)?(?P<p>\d{1,2})\))\s*$",
    re.IGNORECASE,
)


def split_part(title: str | None) -> tuple[str, int | None]:
    """'Just Say Yes Part 2' -> ('Just Say Yes', 2); 'The Family That Slays Together (1)' -> (..., 1)."""
    text = (title or "").strip()
    m = _PART_SUFFIX.search(text)
    if not m or not text[: m.start()].strip():
        return text, None
    raw = (m.group("n") or m.group("p")).lower()
    return text[: m.start()].strip(), int(raw) if raw.isdigit() else _ROMAN_PART.get(raw)


def _episode_words(text: str) -> list[str]:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"['’`]", "", text)  # "He's" -> "hes"
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return [w for w in text.split() if w not in _EP_STOP]


_PLACEHOLDER_TITLE = re.compile(r"^(?:(?:episode|ep|chapter|folge)\.?\s*#?\s*)?\d+$|^(?:tba|tbd|untitled|unknown)$",
                                re.IGNORECASE)


def checkable_episode_title(title: str | None) -> bool:
    """False for titles that can't confirm anything: empty, "Episode 5", "TBA"."""
    base = split_part(title)[0]
    return bool(_episode_words(base)) and not _PLACEHOLDER_TITLE.match(base.strip())


def episode_title_similarity(a: str | None, b: str | None) -> float:
    """Word overlap of two episode titles (0..1), ignoring case, punctuation, articles and a trailing
    "Part N". Two different part numbers ("Part 1" vs "Part 2") never match."""
    (base_a, part_a), (base_b, part_b) = split_part(a), split_part(b)
    if part_a is not None and part_b is not None and part_a != part_b:
        return 0.0
    words_a, words_b = _episode_words(base_a), set(_episode_words(base_b))
    if not words_a or not words_b:
        return 0.0
    return sum(w in words_b for w in words_a) / max(len(words_a), len(words_b))


# --------------------------------------------------------------------------- scoring


@dataclass
class EpisodeCheck:
    """Outcome of episode resolution for one candidate (None = not applicable)."""

    found: bool | None
    label: str = ""  # "S02E05", "abs 15 -> S02E03", "2024-03-04 -> S2024E04"
    note: str | None = None
    title_mismatch: bool = False  # found, but the filename's episode title names another episode


@dataclass
class ScoreResult:
    score: float
    reasons: list[str] = field(default_factory=list)


def popularity_bonus(popularity: float | None) -> float:
    if not popularity or popularity <= 0:
        return 0.0
    return POPULARITY_MAX * min(1.0, math.log10(1.0 + popularity) / 3.0)


def score_candidate(
    *,
    file_kind: MediaKind,
    cand_kind: MediaKind,
    title_sim: float,
    file_year: int | None,
    cand_year: int | None,
    popularity: float | None = None,
    is_anime: bool = False,
    file_season: int | None = None,
    season_count: int | None = None,
    episode: EpisodeCheck | None = None,
) -> ScoreResult:
    reasons = [f"title {round(title_sim * 100)}%"]
    score = TITLE_WEIGHT * title_sim

    if file_year is not None and cand_year is not None:
        diff = abs(file_year - cand_year)
        if diff == 0:
            score += YEAR_EXACT
            reasons.append("year exact")
        elif diff == 1:
            score += YEAR_NEAR
            reasons.append("year ±1")
        else:
            score += YEAR_MISMATCH
            reasons.append(f"year {cand_year} ≠ {file_year}")

    if episode is not None and episode.found is not None:
        if episode.found:
            score += EPISODE_FOUND
            reasons.append(f"{episode.label} exists" if episode.label else "episode exists")
            if episode.title_mismatch:
                score += EPISODE_TITLE_MISMATCH
        else:
            score += EPISODE_MISSING
            reasons.append(f"{episode.label} missing" if episode.label else "episode missing")
        if episode.note:
            reasons.append(episode.note)

    cand_episodic = cand_kind in EPISODIC
    if file_kind == MediaKind.MOVIE and cand_kind == MediaKind.MOVIE:
        score += KIND_AGREE
    elif file_kind in EPISODIC and cand_episodic:
        score += KIND_AGREE
        if file_kind == MediaKind.ANIME and is_anime:
            score += ANIME_AGREE
            reasons.append("anime")
    elif file_kind == MediaKind.UNKNOWN:
        if cand_episodic and episode is not None and episode.found:
            score += KIND_AGREE
    elif (file_kind == MediaKind.MOVIE) != (cand_kind == MediaKind.MOVIE):
        score += KIND_MISMATCH
        reasons.append("movie/series mismatch")

    if cand_episodic and file_season and season_count is not None and file_season > season_count:
        score += SEASON_IMPLAUSIBLE
        reasons.append(f"season {file_season} > {season_count} season{'s' if season_count != 1 else ''}")

    score += popularity_bonus(popularity)
    return ScoreResult(round(max(0.0, min(1.0, score)), 4), reasons)


def confidence_from(scores: list[float]) -> tuple[float, bool]:
    """Best score, minus the ambiguity penalty when the runner-up is too close.

    Returns (confidence, ambiguous)."""
    if not scores:
        return 0.0, False
    ordered = sorted(scores, reverse=True)
    best = ordered[0]
    if len(ordered) > 1 and best - ordered[1] < AMBIGUITY_WINDOW:
        return round(max(0.0, best - AMBIGUITY_PENALTY), 4), True
    return round(best, 4), False
