"""AniDB provider + Anime-Lists mapping against respx-mocked HTTP.

Covers the titles-dump search, the anime XML API, Anime-Lists season/offset mapping
(TVDB and TMDb targets), the sliding-window limiter, the persisted ban latch, and the
matcher's absolute-number walk across Anime-Lists siblings.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import threading
import time

import httpx
import pytest
import respx

from schema_app.core.matcher import Matcher
from schema_app.core.parser import parse
from schema_app.domain import MediaKind, ProviderCredentials, ProviderSettings
from schema_app.providers.anidb import API_URL, BAN_KEY, TITLES_URL, AnidbBanned, AnidbProvider
from schema_app.providers.animelists import ANIME_LISTS_URL, AnimeLists, parse_anime_lists
from schema_app.providers.base import ProviderError, ProviderNotConfigured, ProviderRateLimited
from schema_app.providers.cache import ResponseCache
from schema_app.providers.registry import ProviderRegistry

from .conftest import payload

CREDS = ProviderCredentials(anidb_client="schemaclient", anidb_client_version=1)
ANIME = {aid: payload(f"anidb_{aid}.xml") for aid in ("61001", "61002", "61003")}


def anidb_handler(request: httpx.Request) -> httpx.Response:
    aid = request.url.params.get("aid")
    if aid not in ANIME:
        return httpx.Response(200, content=b"<error>No such anime</error>")
    # AniDB always gzips its answers
    return httpx.Response(200, content=gzip.compress(ANIME[aid]), headers={"Content-Encoding": "gzip"})


@pytest.fixture
def routes():
    with respx.mock(assert_all_called=False) as router:
        api = router.get(API_URL).mock(side_effect=anidb_handler)
        titles = router.get(TITLES_URL).mock(return_value=httpx.Response(200, content=gzip.compress(payload("anidb_titles.xml"))))
        lists = router.get(ANIME_LISTS_URL).mock(return_value=httpx.Response(200, content=payload("anime_list.xml")))
        yield {"api": api, "titles": titles, "lists": lists, "router": router}


@pytest.fixture
def cache():
    return ResponseCache(None)


@pytest.fixture
def anidb(cache, clock):
    lists = AnimeLists(cache=cache, sleep=clock.sleep)
    return AnidbProvider(CREDS, cache=cache, sleep=clock.sleep, clock=clock, animelists=lists)


# --------------------------------------------------------------------------- Anime-Lists


def test_anime_lists_mapping_rules() -> None:
    lists = AnimeLists()
    lists.load_from_bytes(payload("anime_list.xml"))
    assert lists.map_episode("61001", 1, 5) == (1, 5)
    assert lists.map_episode("61001", 0, 1) == (0, 3)  # explicit special pair
    assert lists.map_episode("61001", 0, 2) == (0, 2)  # unmapped special keeps its number
    assert lists.map_episode("61002", 1, 3) == (2, 3)
    assert lists.map_episode("61002", 1, 3, "tmdb") == (1, 15)  # TMDb fields: season 1, offset 12
    assert lists.map_episode("61001", 1, 3, "tmdb") is None  # no TMDb mapping for this entry
    assert lists.map_episode("61003", 1, 4) == (3, 4)  # start/end range
    assert lists.map_episode("61003", 1, 11) == (0, 4)  # explicit pair overrides the default season
    assert lists.map_episode("61010", 1, 7) == (None, 107)  # "a": TVDB absolute numbering
    assert [m.aid for m in lists.siblings("61002")] == ["61001", "61002", "61003"]
    assert lists.entry("61020").tvdb_id is None and lists.entry("61020").imdb_id == "tt6102000"
    assert lists.entry("61002").ids() == {"anidb": "61002", "tvdb": "99100", "tmdb": "4400"}


def test_anime_lists_tolerates_odd_attributes() -> None:
    parsed = parse_anime_lists(b'<anime-list><anime anidbid="1" tvdbid="hentai" defaulttvdbseason="x"/>'
                               b'<anime tvdbid="5"/></anime-list>')
    assert list(parsed) == ["1"] and parsed["1"].tvdb_id is None


async def test_anime_lists_download_is_cached_daily(routes, cache) -> None:
    a = AnimeLists(cache=cache)
    assert await a.ensure_loaded()
    assert await a.ensure_loaded()
    b = AnimeLists(cache=cache)
    assert await b.ensure_loaded()
    assert routes["lists"].call_count == 1


async def test_anime_lists_failure_is_soft(cache) -> None:
    with respx.mock() as router:
        router.get(ANIME_LISTS_URL).mock(return_value=httpx.Response(404))
        a = AnimeLists(cache=cache)
        assert await a.ensure_loaded() is False
        assert a.entry("61001") is None and a.last_error


# --------------------------------------------------------------------------- AniDB provider


async def test_not_configured_never_touches_network(no_network) -> None:
    p = AnidbProvider(ProviderCredentials(anidb_client="x"))
    assert not p.is_configured()
    with pytest.raises(ProviderNotConfigured):
        await p.search_series("Sora no Kaizoku")


async def test_search_uses_local_titles_index(anidb, routes) -> None:
    results = await anidb.search_series("Sky Pirates")
    assert results[0].ref.id == "61001" and results[0].title == "Sora no Kaizoku"
    assert "Sky Pirates" in results[0].aliases
    assert results[0].ids == {"anidb": "61001", "tvdb": "99100"}
    assert routes["api"].call_count == 0, "title search must not use the rate-limited API"
    await anidb.search_series("Sora no Kaizoku")
    assert routes["titles"].call_count == 1


async def test_get_series_parses_xml_and_sends_client(anidb, routes) -> None:
    s = await anidb.get_series("61001")
    params = routes["api"].calls.last.request.url.params
    assert (params["request"], params["client"], params["clientver"], params["protover"], params["aid"]) == (
        "anime", "schemaclient", "1", "1", "61001")
    assert s.title == "Sora no Kaizoku" and s.original_title == "空の海賊"
    assert s.year == 2017 and s.is_anime and s.rating == 7.81
    assert {"Sky Pirates", "Pirates of the Sky"} <= set(s.aliases)
    assert s.ids["tvdb"] == "99100"


async def test_episodes_are_mapped_through_anime_lists(anidb, routes) -> None:
    eps = await anidb.get_episodes("61001")
    regular = [(e.season, e.episode, e.absolute) for e in eps if e.season]
    assert regular[:3] == [(1, 1, 1), (1, 2, 2), (1, 3, 3)] and len(regular) == 12
    special = next(e for e in eps if e.season == 0)
    assert (special.episode, special.title) == (3, "Shore Leave")  # mapping-list ;1-3;
    assert all(e.title != "Opening" for e in eps)  # credits (type 3) are dropped
    second = await anidb.get_episodes("61002")
    assert (second[2].season, second[2].episode, second[2].absolute) == (2, 3, 3)
    assert second[2].ids["mapped"] == "tvdb"


async def test_tmdb_mapping_target(cache, clock, routes) -> None:
    p = AnidbProvider(CREDS, cache=cache, sleep=clock.sleep, clock=clock, mapping_target="tmdb",
                      animelists=AnimeLists(cache=cache))
    eps = await p.get_episodes("61002")
    assert (eps[2].season, eps[2].episode, eps[2].ids["mapped"]) == (1, 15, "tmdb")
    eps = await p.get_episodes("61001")  # no TMDb fields -> falls back to the TVDB mapping
    assert eps[0].ids["mapped"] == "tvdb"


async def test_franchise_episodes_have_cumulative_absolute(anidb, routes) -> None:
    eps = await anidb.franchise_episodes("61001")
    by_abs = {e.absolute: (e.series_ref.id, e.season, e.episode) for e in eps if e.absolute}
    assert by_abs[15] == ("61002", 2, 3)
    assert by_abs[26] == ("61003", 3, 1)
    assert by_abs[36] == ("61003", 0, 4)  # last cour's ep 11 is mapped to a TVDB special


async def test_limiter_spaces_and_windows_requests(anidb, routes, clock) -> None:
    for aid in ("61001", "61002", "61003", "61001x", "61002x", "61003x"):
        with contextlib.suppress(Exception):
            await anidb.get_series(aid)
    assert clock.sleeps == [2.5, 2.5, 2.5, 2.5, 50.0]


async def test_limiter_history_survives_restart_and_daily_cap(cache, clock, routes) -> None:
    p = AnidbProvider(CREDS, cache=cache, sleep=clock.sleep, clock=clock, animelists=AnimeLists(cache=cache))
    await p.get_series("61001")
    again = AnidbProvider(CREDS, cache=cache, sleep=clock.sleep, clock=clock, animelists=AnimeLists(cache=cache))
    assert again.limiter.history == p.limiter.history
    again.limiter.history = [clock() - 3600 + i for i in range(200)]  # 200 calls in the last 24 h
    with pytest.raises(ProviderRateLimited):
        await again.get_series("61002")


async def test_ban_latch_is_persisted_and_reported(cache, clock) -> None:
    with respx.mock() as router:
        api = router.get(API_URL).mock(return_value=httpx.Response(200, content=b"<error>Banned</error>"))
        router.get(ANIME_LISTS_URL).mock(return_value=httpx.Response(200, content=payload("anime_list.xml")))
        p = AnidbProvider(CREDS, cache=cache, sleep=clock.sleep, clock=clock, animelists=AnimeLists(cache=cache))
        with pytest.raises(AnidbBanned):
            await p.get_series("61001")
        assert "ban latch" in (p.status_error or "")
        with pytest.raises(AnidbBanned):
            await p.get_series("61002")
        assert api.call_count == 1, "no AniDB request while the latch is active"
    stored = json.loads(cache.get(BAN_KEY))
    assert stored["until"] > time.time() + 23 * 3600
    reg = ProviderRegistry(ProviderSettings(credentials=CREDS), cache=cache)
    status = next(s for s in reg.statuses() if s.name == "anidb")
    assert status.healthy is False and "ban latch" in (status.last_error or "")
    with pytest.raises(AnidbBanned):
        await reg.get("anidb").search_series("Sky Pirates")


@pytest.mark.parametrize("body", [b'<error code="555">BANNED</error>', b'<error code="500">banned</error>'])
async def test_ban_codes(body, cache, clock) -> None:
    with respx.mock() as router:
        router.get(API_URL).mock(return_value=httpx.Response(200, content=body))
        router.get(ANIME_LISTS_URL).mock(return_value=httpx.Response(200, content=payload("anime_list.xml")))
        p = AnidbProvider(CREDS, cache=cache, sleep=clock.sleep, clock=clock, animelists=AnimeLists(cache=cache))
        with pytest.raises(AnidbBanned):
            await p.get_episodes("61001")


async def test_client_error_means_not_configured_and_is_not_cached(cache, clock) -> None:
    with respx.mock() as router:
        api = router.get(API_URL).mock(side_effect=[
            httpx.Response(200, content=b'<error code="302">client version missing or invalid</error>'),
            httpx.Response(200, content=ANIME["61001"]),
        ])
        router.get(ANIME_LISTS_URL).mock(return_value=httpx.Response(200, content=payload("anime_list.xml")))
        p = AnidbProvider(CREDS, cache=cache, sleep=clock.sleep, clock=clock, animelists=AnimeLists(cache=cache))
        with pytest.raises(ProviderNotConfigured):
            await p.get_series("61001")
        assert (await p.get_series("61001")).title == "Sora no Kaizoku"
        assert api.call_count == 2


# --------------------------------------------------------------------------- plain-HTTP API hardening


@pytest.mark.parametrize("encoded", [False, True], ids=["raw-gzip-body", "content-encoding"])
async def test_gzip_bomb_from_the_api_is_refused_and_not_cached(cache, clock, encoded) -> None:
    bomb = gzip.compress(b"<anime>" + b" " * (64 * 1024 * 1024))
    headers = {"Content-Encoding": "gzip"} if encoded else {}
    with respx.mock() as router:
        api = router.get(API_URL).mock(side_effect=[
            httpx.Response(200, content=bomb, headers=headers),
            httpx.Response(200, content=gzip.compress(ANIME["61001"]), headers={"Content-Encoding": "gzip"}),
        ])
        router.get(ANIME_LISTS_URL).mock(return_value=httpx.Response(200, content=payload("anime_list.xml")))
        p = AnidbProvider(CREDS, cache=cache, sleep=clock.sleep, clock=clock, animelists=AnimeLists(cache=cache))
        with pytest.raises(ProviderError, match="larger than"):
            await p.get_series("61001")
        assert (await p.get_series("61001")).title == "Sora no Kaizoku"
        assert api.call_count == 2


async def test_answer_for_another_anime_is_refused_and_not_cached(cache, clock) -> None:
    with respx.mock() as router:
        api = router.get(API_URL).mock(side_effect=[
            httpx.Response(200, content=ANIME["61002"]),  # tampered / mixed up
            httpx.Response(200, content=ANIME["61001"]),
        ])
        router.get(ANIME_LISTS_URL).mock(return_value=httpx.Response(200, content=payload("anime_list.xml")))
        p = AnidbProvider(CREDS, cache=cache, sleep=clock.sleep, clock=clock, animelists=AnimeLists(cache=cache))
        with pytest.raises(ProviderError, match="asked for anime 61001, got 61002"):
            await p.get_series("61001")
        assert (await p.get_series("61001")).title == "Sora no Kaizoku"
        assert (await p.get_series("61001")).title == "Sora no Kaizoku"  # cached now
        assert api.call_count == 2


async def test_parsing_and_search_run_off_the_event_loop(anidb, routes, monkeypatch) -> None:
    from schema_app.providers import anidb as anidb_mod

    threads: dict[str, list[str]] = {}

    def spy(name: str, fn):  # type: ignore[no-untyped-def]
        def wrapper(*args, **kwargs):  # type: ignore[no-untyped-def]
            threads.setdefault(name, []).append(threading.current_thread().name)
            return fn(*args, **kwargs)

        return wrapper

    for name in ("parse_anime_xml", "build_title_index", "rank_titles"):
        monkeypatch.setattr(anidb_mod, name, spy(name, getattr(anidb_mod, name)))
    assert (await anidb.search_series("Sora no Kaizoku"))[0].ref.id == "61001"
    await anidb.get_series("61001")  # miss: one parse
    await anidb.get_series("61001")  # hit: one parse
    main = threading.main_thread().name
    assert len(threads["build_title_index"]) == 1 and len(threads["parse_anime_xml"]) == 2
    assert all(t != main for ts in threads.values() for t in ts), threads


# --------------------------------------------------------------------------- matcher integration


async def test_matcher_walks_anime_lists_siblings_for_absolute(routes, cache, clock) -> None:
    settings = ProviderSettings(credentials=CREDS, priority={MediaKind.ANIME: ["anidb"]})
    reg = ProviderRegistry(settings, cache=cache, sleep=clock.sleep)
    reg.providers["anidb"] = AnidbProvider(CREDS, cache=cache, sleep=clock.sleep, clock=clock,
                                           animelists=reg.animelists)
    m = Matcher(reg)
    r = await m.match(parse("[Fansub] Sora no Kaizoku - 15 [720p].mkv"))
    assert r.best is not None, r.warnings
    assert r.best.id == "anidb:anime:61001"
    assert [(e.series_ref.id, e.season, e.episode, e.absolute) for e in r.best.episodes] == [("61002", 2, 3, 15)]
    assert any("Anime-Lists" in reason for reason in r.best.reasons)
    assert r.confidence >= 0.85
    siblings = [c for c in r.candidates if c.series and c.series.ids.get("tvdb") == "99100"]
    assert len(siblings) == 1, "AniDB entries of one TVDB series are merged by shared ids"
