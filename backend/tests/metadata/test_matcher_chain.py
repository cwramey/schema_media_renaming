"""Fallback-chain behaviour of the matcher with fake providers (SPEC §6.2)."""

from __future__ import annotations

from datetime import date

from schema_app.core.matcher import Matcher
from schema_app.core.parser import parse
from schema_app.domain import (
    MediaKind,
    MovieInfo,
    ProviderRef,
    ProviderSettings,
    SeriesInfo,
    default_priority,
)
from schema_app.providers.base import (
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderUnavailable,
)
from schema_app.providers.registry import ProviderRegistry

from .conftest import ep, fake_registry, named


def show(provider: str, sid: str, title: str = "Night Ferry", year: int = 2015, **ids: str) -> SeriesInfo:
    return SeriesInfo(ref=ProviderRef(provider=provider, id=sid), title=title, year=year,
                      ids={provider: sid, **ids}, season_count=2)


def ferry_episodes(provider: str, sid: str, *, specials: bool = True) -> list:
    out = [ep(sid, s, e, f"Ep {s}x{e}", absolute=(s - 1) * 10 + e, provider=provider)
           for s in (1, 2) for e in range(1, 11)]
    if specials:
        out.append(ep(sid, 0, 1, "Pilot Presentation", provider=provider))
    return out


async def test_falls_through_errors_and_records_them() -> None:
    down = named("down", fail=ProviderUnavailable("down: timed out"))
    limited = named("limited", fail=ProviderRateLimited("limited: HTTP 429"))
    good = named("good", series=[show("good", "1")], episodes={"1": ferry_episodes("good", "1")})
    m = Matcher(fake_registry([down, limited, good]))
    r = await m.match(parse("Night.Ferry.S01E02.mkv"))
    assert r.best is not None and r.best.provider == "good"
    assert r.providers_tried == ["down", "limited", "good"]
    assert r.provider_errors == {"down": "down: timed out", "limited": "limited: HTTP 429"}
    assert m.registry._health["down"].healthy is False
    assert m.registry._health["down"].last_error == "down: timed out"


async def test_not_configured_is_skipped_and_recorded() -> None:
    nokey = named("nokey", fail=ProviderNotConfigured("nokey: API key missing"))
    good = named("good", series=[show("good", "1")], episodes={"1": ferry_episodes("good", "1")})
    r = await Matcher(fake_registry([nokey, good])).match(parse("Night.Ferry.S01E02.mkv"))
    assert r.best.provider == "good"
    assert r.provider_errors["nokey"].startswith("not configured")


async def test_crashing_provider_is_contained() -> None:
    broken = named("broken", fail=RuntimeError("boom"))
    good = named("good", series=[show("good", "1")], episodes={"1": ferry_episodes("good", "1")})
    r = await Matcher(fake_registry([broken, good])).match(parse("Night.Ferry.S01E02.mkv"))
    assert r.best.provider == "good"
    assert r.provider_errors["broken"] == "internal error: RuntimeError"


async def test_stops_at_first_confident_resolved_provider() -> None:
    first = named("first", series=[show("first", "1")], episodes={"1": ferry_episodes("first", "1")})
    second = named("second", series=[show("second", "9")], episodes={"9": ferry_episodes("second", "9")})
    r = await Matcher(fake_registry([first, second])).match(parse("Night.Ferry.S01E02.mkv"))
    assert r.best.provider == "first"
    assert r.providers_tried == ["first"]
    assert second.calls == []


async def test_continues_when_episode_missing_and_merges_by_shared_ids() -> None:
    # "first" knows the show but not season 3; "second" has it. Same TVDB id -> one candidate.
    first = named("first", series=[show("first", "1", tvdb="555")], episodes={"1": ferry_episodes("first", "1")})
    s2 = show("second", "9", tvdb="555")
    eps2 = ferry_episodes("second", "9") + [ep("9", 3, 1, "New Route", absolute=21, provider="second")]
    second = named("second", series=[s2], episodes={"9": eps2})
    r = await Matcher(fake_registry([first, second])).match(parse("Night.Ferry.S03E01.mkv"))
    assert r.best is not None
    assert r.best.episodes[0].title == "New Route"
    assert r.providers_tried[:2] == ["first", "second"]
    same_show = [c for c in r.candidates if (c.series and c.series.ids.get("tvdb") == "555")]
    assert len(same_show) == 1, "duplicates across providers must be merged by shared ids"


async def test_episode_level_fallback_via_shared_ids() -> None:
    # The chain stops at "first" only when resolved; here "first" lacks the special, "second"
    # is reachable only through an id lookup (different ids namespace).
    first = named("first", series=[show("first", "1", tvdb="777")],
                  episodes={"1": ferry_episodes("first", "1", specials=False)})
    second = named("second", series=[], episodes={"42": ferry_episodes("second", "42")},
                   lookup={("tvdb", "777"): "42"})
    r = await Matcher(fake_registry([first, second])).match(parse("Night.Ferry.S00E01.mkv"))
    assert r.best is not None and r.best.provider == "first"
    assert [(e.season, e.episode, e.title) for e in r.best.episodes] == [(0, 1, "Pilot Presentation")]
    assert any("episode from Second" in reason for reason in r.best.reasons)
    assert ("lookup", ("tvdb", "777")) in second.calls


async def test_unresolved_episode_keeps_candidate_but_no_best() -> None:
    only = named("only", series=[show("only", "1")], episodes={"1": ferry_episodes("only", "1")})
    r = await Matcher(fake_registry([only])).match(parse("Night.Ferry.S07E01.mkv"))
    assert r.best is None
    assert r.candidates and "S07E01 missing" in r.candidates[0].reasons
    assert "episode_not_found" in r.warnings


async def test_daily_show_by_air_date() -> None:
    sid = "5"
    eps = [ep(sid, 2023, n, f"Show {n}", provider="d", aired=date(2023, 5, n)) for n in range(1, 6)]
    d = named("d", series=[show("d", sid, title="Night Desk", year=2010)], episodes={sid: eps})
    r = await Matcher(fake_registry([d])).match(parse("Night.Desk.2023.05.03.mkv"))
    assert [(e.season, e.episode) for e in r.best.episodes] == [(2023, 3)]
    assert any(reason.startswith("aired 2023-05-03") for reason in r.best.reasons)


async def test_absolute_without_provider_absolute_is_cumulative() -> None:
    sid = "3"
    eps = [ep(sid, s, e, f"{s}-{e}", provider="c") for s in (1, 2) for e in range(1, 13)]
    c = named("c", series=[show("c", sid, title="Sky Pirates")], episodes={sid: eps})
    r = await Matcher(fake_registry([c])).match(parse("[Fansub] Sky Pirates - 14 [720p].mkv"))
    assert [(e.season, e.episode) for e in r.best.episodes] == [(2, 2)]


async def test_season_one_numbering_beyond_season_reads_as_absolute_for_anime_only() -> None:
    sid = "3"
    eps = [ep(sid, s, e, f"{s}-{e}", absolute=(s - 1) * 12 + e, provider="c") for s in (1, 2) for e in range(1, 13)]
    anime = show("c", sid, title="Sky Pirates").model_copy(update={"is_anime": True})
    c = named("c", series=[anime], episodes={sid: eps})
    r = await Matcher(fake_registry([c])).match(parse("Sky.Pirates.S01E15.mkv"))
    assert [(e.season, e.episode) for e in r.best.episodes] == [(2, 3)]
    # A regular show with an explicit season never jumps to another season on the number alone.
    c = named("c", series=[show("c", sid, title="Sky Pirates")], episodes={sid: eps})
    r = await Matcher(fake_registry([c])).match(parse("Sky.Pirates.S01E15.mkv"))
    assert r.best is None and "episode_not_found" in r.warnings


async def test_movies_ranked_by_year() -> None:
    movies = [
        MovieInfo(ref=ProviderRef(provider="mv", id="1"), title="Glass Tide", year=1999, ids={"mv": "1"}),
        MovieInfo(ref=ProviderRef(provider="mv", id="2"), title="Glass Tide", year=2020, ids={"mv": "2"}),
    ]
    mv = named("mv", movies=movies)
    r = await Matcher(fake_registry([mv], MediaKind.MOVIE)).match(parse("Glass.Tide.2020.1080p.mkv"))
    assert r.best.year == 2020 and r.confidence >= 0.85


async def test_hint_provider_restricts_chain() -> None:
    a = named("a", series=[show("a", "1")], episodes={"1": ferry_episodes("a", "1")})
    b = named("b", series=[show("b", "2")], episodes={"2": ferry_episodes("b", "2")})
    from schema_app.domain import MatchHint

    r = await Matcher(fake_registry([a, b])).match(parse("Night.Ferry.S01E02.mkv"), MatchHint(provider="b"))
    assert r.best.provider == "b" and a.calls == []
    r = await Matcher(fake_registry([a, b])).match(parse("Night.Ferry.S01E02.mkv"), MatchHint(provider="zzz"))
    assert r.best is None and r.provider_errors == {"zzz": "unknown provider"}


async def test_match_many_searches_each_group_once() -> None:
    a = named("a", series=[show("a", "1")], episodes={"1": ferry_episodes("a", "1")})
    names = [f"Night.Ferry.S01E0{n}.mkv" for n in range(1, 6)]
    results = await Matcher(fake_registry([a])).match_many([parse(n) for n in names])
    assert [r.best.episodes[0].episode for r in results] == [1, 2, 3, 4, 5]
    assert sum(1 for call in a.calls if call[0] == "search_series") == 1
    assert sum(1 for call in a.calls if call[0] == "get_episodes") == 1


async def test_manual_pick_fails_loudly_when_episodes_unavailable() -> None:
    import pytest

    from schema_app.domain import MatchHint

    from .conftest import FakeProvider

    class NoEpisodes(FakeProvider):
        async def get_episodes(self, series_id, order=None):  # type: ignore[no-untyped-def, override]
            raise ProviderUnavailable("p: HTTP 503")

    p = named("p", base=NoEpisodes, series=[show("p", "1")])
    m = Matcher(fake_registry([p]))
    ref = ProviderRef(provider="p", id="1")
    with pytest.raises(ProviderUnavailable):
        await m.resolve(parse("Night.Ferry.S01E02.mkv"), ref, MediaKind.SERIES)
    r = await m.match(parse("Night.Ferry.S01E02.mkv"), MatchHint(ref=ref, kind=MediaKind.SERIES))
    assert r.best is None and r.provider_errors == {"p": "p: HTTP 503"} and "manual_pick_failed" in r.warnings
    # without numbering the episode list is not needed, so the pick still works
    cand = await m.resolve(parse("Night Ferry.mkv"), ref, MediaKind.SERIES)
    assert cand.title == "Night Ferry" and "episodes unavailable (ProviderUnavailable)" in cand.reasons
    # the automatic chain keeps the candidate but records the failure on the provider status
    r = await m.match(parse("Night.Ferry.S01E02.mkv"))
    assert r.best is None and r.candidates and m.registry._health["p"].healthy is False


async def test_match_never_raises_on_empty_input() -> None:
    r = await Matcher(fake_registry([named("a")])).match(parse(""))
    assert r.best is None and "no_title" in r.warnings
    assert await Matcher(fake_registry([named("a")])).match_many([]) == []


def test_chain_follows_settings_priority_and_filters() -> None:
    reg = ProviderRegistry(ProviderSettings())
    expected = [n for n in default_priority()[MediaKind.SERIES] if n == "tvmaze"]  # only keyless one configured
    assert [p.name for p in reg.chain(MediaKind.SERIES)] == expected
    reg = ProviderRegistry(ProviderSettings(credentials={"tmdb_api_key": "k", "trakt_client_id": "t"}))
    for kind in (MediaKind.MOVIE, MediaKind.SERIES, MediaKind.ANIME):
        names = [p.name for p in reg.chain(kind)]
        assert names == [n for n in default_priority()[kind] if n in {"tmdb", "trakt", "tvmaze"}
                         and kind in reg.get(n).kinds]
    reg = ProviderRegistry(ProviderSettings(credentials={"tmdb_api_key": "k"}, disabled=["tmdb"]))
    assert "tmdb" not in [p.name for p in reg.chain(MediaKind.MOVIE)]
    reg = ProviderRegistry(ProviderSettings(credentials={"tmdb_api_key": "k"}), demo=True)
    assert [p.name for p in reg.chain(MediaKind.SERIES)] == ["fixtures", "tmdb"]  # keyless tvmaze stays offline
    assert [p.name for p in reg.chain(MediaKind.UNKNOWN)] == ["fixtures", "tmdb"]


async def test_reconfigure_rebuilds_providers_and_keeps_counters(tmp_path) -> None:
    reg = ProviderRegistry(ProviderSettings())
    m = Matcher(reg)
    assert not next(s for s in m.provider_statuses() if s.name == "tmdb").configured
    await m.reconfigure(ProviderSettings(credentials={"tmdb_api_key": "abc"}, language="de"))
    tmdb = next(s for s in m.provider_statuses() if s.name == "tmdb")
    assert tmdb.configured and reg.get("tmdb").language == "de"
    await m.aclose()
