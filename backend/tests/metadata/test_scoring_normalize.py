"""Normalization and scoring rules (SPEC §6.1 normalize, §6.3 scoring table)."""

from __future__ import annotations

import pytest

from schema_app.core import scoring
from schema_app.core.normalize import normalize_title, roman_to_int, strip_trailing_year, title_key
from schema_app.core.scoring import (
    EpisodeCheck,
    best_title_similarity,
    confidence_from,
    popularity_bonus,
    score_candidate,
    title_similarity,
)
from schema_app.domain import MediaKind

M, S, A, U = MediaKind.MOVIE, MediaKind.SERIES, MediaKind.ANIME, MediaKind.UNKNOWN


@pytest.mark.parametrize(("raw", "expected"), [
    ("The Quiet Meridian", "quiet meridian"),
    ("THE.QUIET.MERIDIAN", "quiet meridian"),
    ("Law & Order", "law and order"),
    ("Rocky II", "rocky 2"),
    ("Star Wars Episode IV", "star wars episode 4"),
    ("Rocky V", "rocky 5"),
    ("V for Vendetta", "v for vendetta"),
    ("I, Robot", "i robot"),
    ("Amélie", "amelie"),
    ("Pokémon: The Movie", "pokemon the movie"),
    ("S.W.A.T.", "swat"),
    ("Marvel's Agents of S.H.I.E.L.D.", "marvels agents of shield"),
    ("Harbor Lights (2021)", "harbor lights"),
    ("A Quiet Place", "quiet place"),
    ("The", "the"),
    ("風の翼", normalize_title("風の翼")),
    ("  Spaced   Out  ", "spaced out"),
    ("", ""),
    (None, ""),
])
def test_normalize_title(raw, expected) -> None:
    assert normalize_title(raw) == expected


def test_normalize_helpers() -> None:
    assert roman_to_int("xiv") == 14 and roman_to_int("iiii") is None and roman_to_int("mix") is None
    assert strip_trailing_year("Harbor Lights (2021)") == "Harbor Lights"
    assert title_key("Star-Fall") == title_key("Starfall") == "starfall"
    assert normalize_title("The Office", strip_articles=False) == "the office"


def test_title_similarity_penalises_extra_words() -> None:
    assert title_similarity("Starfall Station", "Starfall Station") == 1.0
    assert title_similarity("Rocky 2", "Rocky II") == 1.0
    assert title_similarity("starfall station", "Star Fall Station") == pytest.approx(0.98)
    subset = title_similarity("Starfall Station", "Starfall")
    assert 0.75 < subset < 0.9, "token_set alone would call this a perfect match"
    assert title_similarity("Neon Requiem", "Harbor Lights") < 0.4
    assert best_title_similarity(["nope", "Wings of the Wind"], ["Kaze no Tsubasa", "Wings of the Wind"]) == 1.0
    assert best_title_similarity([None, ""], ["x"]) == 0.0


def _score(**kw):
    base = dict(file_kind=S, cand_kind=S, title_sim=1.0, file_year=None, cand_year=None)
    base.update(kw)
    return score_candidate(**base)


def test_exact_title_and_episode_clear_the_bar() -> None:
    r = _score(episode=EpisodeCheck(True, "S01E01"))
    assert r.score == pytest.approx(scoring.TITLE_WEIGHT + scoring.EPISODE_FOUND + scoring.KIND_AGREE)
    assert r.score >= scoring.STOP_SCORE
    assert r.reasons == ["title 100%", "S01E01 exists"]


def test_year_signals() -> None:
    exact = _score(file_kind=M, cand_kind=M, file_year=2019, cand_year=2019)
    near = _score(file_kind=M, cand_kind=M, file_year=2019, cand_year=2020)
    far = _score(file_kind=M, cand_kind=M, file_year=2019, cand_year=2022)
    unknown = _score(file_kind=M, cand_kind=M, file_year=None, cand_year=2022)
    assert exact.score - unknown.score == pytest.approx(scoring.YEAR_EXACT)
    assert near.score - unknown.score == pytest.approx(scoring.YEAR_NEAR)
    assert unknown.score - far.score == pytest.approx(-scoring.YEAR_MISMATCH)
    assert "year exact" in exact.reasons and "year ±1" in near.reasons and "year 2022 ≠ 2019" in far.reasons
    assert exact.score >= 0.85 > unknown.score


def test_episode_missing_and_season_plausibility() -> None:
    missing = _score(episode=EpisodeCheck(False, "S02E05"), file_season=2, season_count=1)
    assert "S02E05 missing" in missing.reasons and "season 2 > 1 season" in missing.reasons
    assert missing.score == pytest.approx(
        scoring.TITLE_WEIGHT + scoring.EPISODE_MISSING + scoring.KIND_AGREE + scoring.SEASON_IMPLAUSIBLE)
    ok = _score(episode=EpisodeCheck(True, "S02E05"), file_season=2, season_count=2)
    assert not any(r.startswith("season") for r in ok.reasons)


def test_kind_agreement() -> None:
    anime = _score(file_kind=A, cand_kind=A, is_anime=True, episode=EpisodeCheck(True, "abs 5"))
    plain = _score(file_kind=A, cand_kind=S, is_anime=False, episode=EpisodeCheck(True, "abs 5"))
    assert anime.score - plain.score == pytest.approx(scoring.ANIME_AGREE)
    assert "anime" in anime.reasons
    mismatch = _score(file_kind=M, cand_kind=S)
    assert "movie/series mismatch" in mismatch.reasons
    assert mismatch.score == pytest.approx(scoring.TITLE_WEIGHT + scoring.KIND_MISMATCH)
    unknown_resolved = _score(file_kind=U, cand_kind=S, episode=EpisodeCheck(True, "x"))
    unknown_movie = _score(file_kind=U, cand_kind=M)
    assert unknown_resolved.score == pytest.approx(scoring.TITLE_WEIGHT + scoring.EPISODE_FOUND + scoring.KIND_AGREE)
    assert unknown_movie.score == pytest.approx(scoring.TITLE_WEIGHT)


def test_popularity_is_a_small_log_scaled_tiebreak() -> None:
    assert popularity_bonus(None) == 0 and popularity_bonus(0) == 0
    assert popularity_bonus(10) < popularity_bonus(100) < popularity_bonus(1000) <= scoring.POPULARITY_MAX
    assert popularity_bonus(10**9) == pytest.approx(scoring.POPULARITY_MAX)


def test_score_is_clamped() -> None:
    assert _score(file_year=2000, cand_year=2000, episode=EpisodeCheck(True, "x"), popularity=1e6).score == 1.0
    assert _score(title_sim=0.0, file_year=1990, cand_year=2020, episode=EpisodeCheck(False, "x")).score == 0.0


def test_ambiguity_penalty() -> None:
    assert confidence_from([]) == (0.0, False)
    assert confidence_from([0.9]) == (0.9, False)
    assert confidence_from([0.9, 0.7]) == (0.9, False)
    conf, ambiguous = confidence_from([0.9, 0.87])
    assert ambiguous and conf == pytest.approx(0.9 - scoring.AMBIGUITY_PENALTY)


def test_named_constants_match_findings() -> None:
    assert scoring.CANDIDATE_CUTOFF == 0.72  # FileBot's default acceptance threshold
    assert scoring.MAX_RESULTS_PER_QUERY == 5
    assert scoring.STOP_SCORE == 0.85
