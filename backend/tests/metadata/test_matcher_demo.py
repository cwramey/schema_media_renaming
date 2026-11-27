"""Every demo_files.txt expectation must hold with the offline fixtures provider (SPEC §6.5, §12)."""

from __future__ import annotations

from pathlib import Path

import pytest

from schema_app.core.matcher import create_matcher
from schema_app.core.parser import parse
from schema_app.domain import MatchHint, MediaKind, ProviderRef, ProviderSettings

DEMO = Path(__file__).resolve().parents[2] / "schema_app" / "demo" / "demo_files.txt"
M, S, A = MediaKind.MOVIE, MediaKind.SERIES, MediaKind.ANIME

# rel path -> (kind, title, year, [(season, episode, absolute|None)], special, review_case)
EXPECT: dict[str, tuple] = {
    "Starfall.Station.S01E01.1080p.WEB-DL.DDP5.1.H.264-NTb.mkv": (S, "Starfall Station", 2016, [(1, 1, 1)], False),
    "Starfall.Station.S01E01.1080p.WEB-DL.DDP5.1.H.264-NTb.en.srt": (S, "Starfall Station", 2016, [(1, 1, 1)], False),
    "Starfall.Station.S01E02E03.720p.HDTV.x264-KILLERS.mkv": (S, "Starfall Station", 2016, [(1, 2, 2), (1, 3, 3)], False),
    "starfall_station_1x04_hdtv.avi": (S, "Starfall Station", 2016, [(1, 4, 4)], False),
    "Starfall Station - S00E02 - Christmas Special.mkv": (S, "Starfall Station", 2016, [(0, 2, None)], True),
    "Starfall Station - The Making of Starfall.mkv": (S, "Starfall Station", 2016, [(0, 1, None)], True),
    "Starfall.Station.S02E10.PROPER.1080p.BluRay.x265.10bit-GROUP.mkv": (S, "Starfall Station", 2016, [(2, 10, 20)], False),
    "Starfall.Station.S03E01.Sample.mkv": (S, "Starfall Station", 2016, [(3, 1, 21)], False),
    "Starfall Station/Season 3/03 - Ghosts in the Array.mkv": (S, "Starfall Station", 2016, [(3, 3, 23)], False),
    "Harbor.Lights.2021.S01E03.2160p.AMZN.WEB-DL.DV.HDR.H.265-FLUX.mkv": (S, "Harbor Lights", 2021, [(1, 3, 3)], False),
    "Harbor Lights S02E05.mkv": (S, "Harbor Lights", 2008, [(2, 5, 13)], False),
    "The.Late.Hour.with.Sam.Reyes.2024.03.04.720p.WEB.h264-SPAMnEGGS.mkv":
        (S, "The Late Hour with Sam Reyes", 2019, [(2024, 4, 4)], False),
    "[SubsPlease] Kaze no Tsubasa - 15 (1080p) [1A2B3C4D].mkv": (A, "Kaze no Tsubasa", 2018, [(2, 3, 15)], False),
    "[Erai-raws] Wings of the Wind - 26 [1080p][Multiple Subtitle].mkv": (A, "Kaze no Tsubasa", 2018, [(3, 1, 26)], False),
    "[Judas] Tensei Robo Gaiden - S01E07 [1080p][HEVC x265 10bit].mkv": (A, "Tensei Robo Gaiden", 2020, [(1, 7, 7)], False),
    "[Hatsuyuki] Kaze no Tsubasa - 07v2 [720p][DEADBEEF].mkv": (A, "Kaze no Tsubasa", 2018, [(1, 7, 7)], False),
    "Kaze no Tsubasa - OVA [BD 1080p].mkv": (A, "Kaze no Tsubasa", 2018, [(0, 1, None)], True),
    "The.Quiet.Meridian.2019.Directors.Cut.1080p.BluRay.x264-SPARKS.mkv": (M, "The Quiet Meridian", 2019, [], False),
    "Iron.Orchard.1987.REMASTERED.720p.BluRay.x264-AMIABLE.mkv": (M, "Iron Orchard", 1987, [], False),
    "Iron Orchard 2023 2160p WEB-DL DDP5.1 Atmos HDR10 HEVC-CMRG.mkv": (M, "Iron Orchard", 2023, [], False),
    "Paper.Moons.2004.CD1.DVDRip.XviD-FiCO.avi": (M, "Paper Moons", 2004, [], False),
    "Paper.Moons.2004.CD2.DVDRip.XviD-FiCO.avi": (M, "Paper Moons", 2004, [], False),
    "Neon.Requiem.2021.1080p.BluRay.x264-GROUP.mkv": (M, "Neon Requiem", 2021, [], False),
    "Neon.Requiem.2021.2160p.UHD.BluRay.REMUX.HDR.HEVC.TrueHD.7.1.Atmos-FGT.mkv": (M, "Neon Requiem", 2021, [], False),
    "Neon Requiem (2021)/Featurettes/Neon.Requiem.2021.Behind.The.Scenes.1080p.mkv": (M, "Neon Requiem", 2021, [], False),
}
REVIEW = {"completely.unknown.title.2011.1080p.mkv", "notes.txt"}


def demo_rows() -> list[str]:
    rows = []
    for line in DEMO.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            rows.append(line.split("|", 1)[0].strip().replace("{crc32}", "1A2B3C4D"))
    return rows


@pytest.fixture
async def demo_matcher(tmp_path):
    matcher = create_matcher(ProviderSettings(), cache_dir=tmp_path, demo=True)
    yield matcher
    await matcher.aclose()


def _assert_expected(rel: str, result) -> None:
    if rel in REVIEW:
        assert result.best is None, rel
        assert result.confidence < 0.85
        assert "no_match" in result.warnings
        return
    kind, title, year, episodes, special = EXPECT[rel]
    best = result.best
    assert best is not None, f"{rel}: no best ({result.warnings})"
    assert best.provider == "fixtures"
    assert best.kind == kind, rel
    assert best.title == title, rel
    assert best.year == year, rel
    assert [(e.season, e.episode, e.absolute) for e in best.episodes] == episodes, rel
    assert all(e.special == special for e in best.episodes)
    assert result.confidence >= 0.85, f"{rel}: {result.confidence} {best.reasons}"
    assert best.reasons and best.reasons[0].startswith("title ")
    assert best.id == f"fixtures:{kind.value}:{(best.series or best.movie).ref.id}"


def test_every_demo_line_has_an_expectation() -> None:
    assert set(demo_rows()) == set(EXPECT) | REVIEW


async def test_demo_batch_resolves_as_described(demo_matcher, no_network) -> None:
    rows = demo_rows()
    parsed = [parse("/config/demo/incoming/" + r) for r in rows]
    results = await demo_matcher.match_many(parsed)
    assert len(results) == len(rows)
    for rel, result in zip(rows, results, strict=True):
        _assert_expected(rel, result)
        assert result.providers_tried == ["fixtures"], f"{rel}: chain should stop at fixtures"
        assert result.provider_errors == {}


@pytest.mark.parametrize("rel", sorted(EXPECT) + sorted(REVIEW))
async def test_demo_single_match(demo_matcher, no_network, rel: str) -> None:
    result = await demo_matcher.match(parse("/config/demo/incoming/" + rel))
    _assert_expected(rel, result)


async def test_reasons_are_human_readable(demo_matcher, no_network) -> None:
    r = await demo_matcher.match(parse("Harbor.Lights.2021.S01E03.2160p.AMZN.WEB-DL.mkv"))
    assert r.best is not None
    assert r.best.reasons[:3] == ["title 100%", "year exact", "S01E03 exists"]


async def test_season_plausibility_rejects_2021_series(demo_matcher, no_network) -> None:
    r = await demo_matcher.match(parse("Harbor Lights S02E05.mkv"))
    loser = next(c for c in r.candidates if c.year == 2021)
    assert loser.score < 0.5
    assert "S02E05 missing" in loser.reasons
    assert any(reason.startswith("season 2 > 1 season") for reason in loser.reasons)


async def test_decoy_starfall_is_a_distant_runner_up(demo_matcher, no_network) -> None:
    r = await demo_matcher.match(parse("Starfall.Station.S01E01.mkv"))
    titles = [(c.title, c.year) for c in r.candidates]
    assert titles[0] == ("Starfall Station", 2016)
    assert ("Starfall", 1998) in titles
    assert r.candidates[0].score - r.candidates[1].score >= 0.05
    assert "ambiguous_match" not in r.warnings


async def test_ambiguity_penalty_without_year(demo_matcher, no_network) -> None:
    # S01E03 exists in both Harbor Lights series and no year is given: a coin flip.
    r = await demo_matcher.match(parse("Harbor Lights S01E03.mkv"))
    assert "ambiguous_match" in r.warnings
    assert r.confidence == pytest.approx(r.candidates[0].score - 0.10, abs=1e-4)
    assert r.confidence < 0.85


async def test_year_off_by_one_still_matches(demo_matcher, no_network) -> None:
    r = await demo_matcher.match(parse("Iron.Orchard.1988.720p.BluRay.mkv"))
    assert r.best is not None and r.best.year == 1987
    assert "year ±1" in r.best.reasons


async def test_movie_without_year_needs_review(demo_matcher, no_network) -> None:
    r = await demo_matcher.match(parse("Paper Moons.avi"))
    assert r.best is not None and r.best.title == "Paper Moons"
    assert r.confidence < 0.85


async def test_unknown_kind_tries_movie_then_series(demo_matcher, no_network) -> None:
    parsed = parse("Neon Requiem - Making Of.mkv")
    assert parsed.kind == MediaKind.UNKNOWN
    r = await demo_matcher.match(parsed)
    assert r.best is not None and r.best.kind == MediaKind.MOVIE


async def test_hint_overrides_numbering_and_kind(demo_matcher, no_network) -> None:
    parsed = parse("Starfall Station extras.mkv")
    r = await demo_matcher.match(parsed, MatchHint(kind=MediaKind.SERIES, season=2, episodes=[4]))
    assert r.best is not None
    assert [(e.season, e.episode) for e in r.best.episodes] == [(2, 4)]
    assert r.best.episodes[0].title == "Coldwater"


async def test_hint_absolute_order(demo_matcher, no_network) -> None:
    parsed = parse("Kaze no Tsubasa - 03.mkv")
    r = await demo_matcher.match(parsed, MatchHint(absolute=[30]))
    assert [(e.season, e.episode) for e in r.best.episodes] == [(3, 5)]


async def test_hint_query_replaces_title(demo_matcher, no_network) -> None:
    parsed = parse("garbled-name-s01e02.mkv")
    r = await demo_matcher.match(parsed, MatchHint(query="Starfall Station"))
    assert r.best is not None and r.best.title == "Starfall Station"
    assert [(e.season, e.episode) for e in r.best.episodes] == [(1, 2)]


async def test_hint_ref_resolves_directly(demo_matcher, no_network) -> None:
    parsed = parse("Harbor Lights S01E02.mkv")
    ref = ProviderRef(provider="fixtures", id="900004")
    r = await demo_matcher.match(parsed, MatchHint(ref=ref, kind=MediaKind.SERIES))
    assert r.best is not None and r.best.year == 2021
    assert r.confidence == 1.0
    assert "manual pick" in r.best.reasons
    assert r.providers_tried == ["fixtures"]


async def test_resolve_manual_pick_converts_absolute(demo_matcher, no_network) -> None:
    parsed = parse("[Group] Something Else - 14 [720p].mkv")
    cand = await demo_matcher.resolve(parsed, ProviderRef(provider="fixtures", id="900101"), MediaKind.ANIME)
    assert cand.id == "fixtures:anime:900101"
    assert [(e.season, e.episode, e.absolute) for e in cand.episodes] == [(2, 2, 14)]
    assert "manual pick" in cand.reasons


async def test_resolve_manual_pick_movie(demo_matcher, no_network) -> None:
    cand = await demo_matcher.resolve(parse("x.mkv"), ProviderRef(provider="fixtures", id="700005"), MediaKind.MOVIE)
    assert cand.title == "Paper Moons" and cand.movie is not None


async def test_resolve_unknown_provider_raises(demo_matcher) -> None:
    from schema_app.providers.base import ProviderError

    with pytest.raises(ProviderError):
        await demo_matcher.resolve(parse("x.mkv"), ProviderRef(provider="nope", id="1"), MediaKind.MOVIE)


async def test_free_text_search(demo_matcher, no_network) -> None:
    movies = await demo_matcher.search("Iron Orchard", MediaKind.MOVIE)
    assert {(c.title, c.year) for c in movies} == {("Iron Orchard", 1987), ("Iron Orchard", 2023)}
    assert all(not c.episodes for c in movies)
    dated = await demo_matcher.search("Iron Orchard", MediaKind.MOVIE, year=2023)
    assert dated[0].year == 2023
    mixed = await demo_matcher.search("Harbor Lights", MediaKind.UNKNOWN)
    assert {c.kind for c in mixed} == {MediaKind.SERIES}
    anime = await demo_matcher.search("Wings of the Wind", MediaKind.ANIME, provider="fixtures")
    assert anime[0].id == "fixtures:anime:900101"


async def test_batch_one_to_one_assignment(demo_matcher, no_network) -> None:
    names = [
        "Starfall.Station.S01E05.720p.mkv",  # exact claim on S01E05
        "Starfall.Station.1x05.1080p.mkv",  # same numbering: a second version, not a conflict
        "Starfall Station - Hull Breach.mkv",  # title claim on S01E05 loses to the exact ones
        "Starfall Station - The Making of Starfall.mkv",  # title claim on S00E01 ...
        "Starfall Station - Making of Starfall Station.mkv",  # ... contested by a different title claim
    ]
    results = await demo_matcher.match_many([parse(n) for n in names])
    assert [(e.season, e.episode) for e in results[0].best.episodes] == [(1, 5)]
    assert [(e.season, e.episode) for e in results[1].best.episodes] == [(1, 5)]
    for r in results[2:]:
        assert r.best is None
        assert "episode_conflict" in r.warnings
        assert r.confidence < 0.85


async def test_provider_statuses_and_test(demo_matcher) -> None:
    statuses = demo_matcher.provider_statuses()
    assert statuses[0].name == "fixtures" and statuses[0].configured
    names = {s.name for s in statuses}
    assert {"tmdb", "tvdb", "anidb", "trakt", "tvmaze"} <= names
    tmdb = next(s for s in statuses if s.name == "tmdb")
    assert tmdb.requires_key and not tmdb.configured
    status = await demo_matcher.test_provider("fixtures")
    assert status.healthy is True
    status = await demo_matcher.test_provider("tmdb")
    assert status.healthy is False and "credentials" in (status.last_error or "")
    with pytest.raises(KeyError):
        await demo_matcher.test_provider("nope")
