"""TVmaze (keyless) and Trakt providers against respx-mocked HTTP."""

from __future__ import annotations

import httpx
import pytest
import respx

from schema_app.domain import MediaKind, ProviderCredentials
from schema_app.providers.base import ProviderNotConfigured, ProviderRateLimited
from schema_app.providers.cache import ResponseCache
from schema_app.providers.trakt import TraktProvider
from schema_app.providers.tvmaze import TvmazeProvider

from .conftest import payload

DATA = payload("tvmaze_trakt.json")


# --------------------------------------------------------------------------- TVmaze


@pytest.fixture
def tvmaze(clock):
    return TvmazeProvider(ProviderCredentials(), cache=ResponseCache(None), sleep=clock.sleep)


@pytest.fixture
def maze_api():
    with respx.mock(base_url="https://api.tvmaze.com", assert_all_called=False) as router:
        yield router


def test_tvmaze_is_keyless(tvmaze) -> None:
    assert tvmaze.is_configured() and not tvmaze.requires_key
    assert MediaKind.SERIES in tvmaze.kinds


async def test_tvmaze_search_filters_year_like_other_providers(tvmaze, maze_api) -> None:
    route = maze_api.get("/search/shows").mock(return_value=httpx.Response(200, json=DATA["tvmaze_search"]))
    results = await tvmaze.search_series("Glass Harbour", 2014)
    assert route.calls.last.request.url.params["q"] == "Glass Harbour"
    assert [(s.title, s.year) for s in results] == [("Glass Harbour", 2014)]
    s = results[0]
    assert s.ids == {"tvmaze": "7001", "tvdb": "88001", "imdb": "tt8800001"}
    assert s.overview == "Lighthouse drama." and s.network == "Northern Channel" and s.original_language == "en"
    assert len(await tvmaze.search_series("Glass Harbour")) == 2


async def test_tvmaze_details_with_akas(tvmaze, maze_api) -> None:
    maze_api.get("/shows/7001").mock(return_value=httpx.Response(200, json=DATA["tvmaze_search"][0]["show"]))
    maze_api.get("/shows/7001/akas").mock(return_value=httpx.Response(200, json=DATA["tvmaze_akas"]))
    s = await tvmaze.get_series("7001")
    assert s.aliases == ["Glashafen"]


async def test_tvmaze_specials_move_to_season_zero(tvmaze, maze_api) -> None:
    route = maze_api.get("/shows/7001/episodes").mock(return_value=httpx.Response(200, json=DATA["tvmaze_episodes"]))
    eps = await tvmaze.get_episodes("7001")
    assert route.calls.last.request.url.params["specials"] == "1"
    assert [(e.season, e.episode, e.absolute, e.title) for e in eps] == [
        (1, 1, 1, "Low Water"), (1, 2, 2, "The Keeper"), (2, 1, 3, "High Water"),
        (0, 1, None, "Behind the Glass"), (0, 2, None, "Reunion"),
    ]


async def test_tvmaze_rate_limit_window(tvmaze, maze_api, clock) -> None:
    # 20 requests per 10 s: the 21st waits for the window to slide (fake clock, no real sleep)
    tvmaze.http.limiter._clock = clock
    maze_api.get("/shows/1").mock(return_value=httpx.Response(200, json={"id": 1}))
    for _ in range(21):
        await tvmaze.healthcheck()
    assert clock.sleeps and clock.sleeps[0] == pytest.approx(10.0)


async def test_tvmaze_lookup_and_429(tvmaze, maze_api, clock) -> None:
    maze_api.get("/lookup/shows").mock(return_value=httpx.Response(200, json={"id": 7001}))
    assert await tvmaze.lookup_series_id("tvdb", "88001") == "7001"
    maze_api.get("/search/shows").mock(return_value=httpx.Response(429, headers={"Retry-After": "600"}))
    with pytest.raises(ProviderRateLimited):
        await tvmaze.search_series("Glass Harbour")  # Retry-After beyond the cap fails fast


# --------------------------------------------------------------------------- Trakt


@pytest.fixture
def trakt(clock):
    return TraktProvider(ProviderCredentials(trakt_client_id="trakt-id"), cache=ResponseCache(None), sleep=clock.sleep)


@pytest.fixture
def trakt_api():
    with respx.mock(base_url="https://api.trakt.tv", assert_all_called=False) as router:
        yield router


async def test_trakt_not_configured() -> None:
    p = TraktProvider(ProviderCredentials())
    assert not p.is_configured()
    with pytest.raises(ProviderNotConfigured):
        await p.search_series("x")


async def test_trakt_series_flow(trakt, trakt_api) -> None:
    search = trakt_api.get("/search/show").mock(return_value=httpx.Response(200, json=DATA["trakt_search_show"]))
    trakt_api.get("/shows/3301").mock(return_value=httpx.Response(200, json=DATA["trakt_show"]))
    seasons = trakt_api.get("/shows/3301/seasons").mock(return_value=httpx.Response(200, json=DATA["trakt_seasons"]))
    found = await trakt.search_series("Glass Harbour", 2014)
    req = search.calls.last.request
    assert req.headers["trakt-api-key"] == "trakt-id" and req.headers["trakt-api-version"] == "2"
    assert req.url.params["years"] == "2014"
    assert found[0].ref.id == "3301"
    assert found[0].ids == {"trakt": "3301", "tvdb": "88001", "tmdb": "4100", "imdb": "tt8800001"}
    s = await trakt.get_series("3301")
    assert s.network == "Northern Channel" and s.certification == "TV-14" and s.first_aired.isoformat() == "2014-02-10"
    eps = await trakt.get_episodes("3301")
    assert seasons.calls.last.request.url.params["extended"] == "episodes,full"
    assert [(e.season, e.episode, e.absolute) for e in eps] == [(0, 1, None), (1, 1, 1), (1, 2, 2), (2, 1, 3)]
    assert eps[1].ids == {"trakt": "2", "tvdb": "1002"}


async def test_trakt_movie_flow(trakt, trakt_api) -> None:
    trakt_api.get("/search/movie").mock(return_value=httpx.Response(200, json=DATA["trakt_search_movie"]))
    trakt_api.get("/movies/501").mock(return_value=httpx.Response(200, json=DATA["trakt_movie"]))
    found = await trakt.search_movies("Copper Sky", 2011)
    assert (found[0].title, found[0].year, found[0].ref.id) == ("Copper Sky", 2011, "501")
    m = await trakt.get_movie("501")
    assert m.ids == {"trakt": "501", "imdb": "tt5100001", "tmdb": "51001"}
    assert m.certification == "PG-13" and m.runtime_minutes == 104


async def test_trakt_lookup_and_healthcheck(trakt, trakt_api) -> None:
    trakt_api.get("/search/tvdb/88001").mock(return_value=httpx.Response(200, json=DATA["trakt_search_show"]))
    assert await trakt.lookup_series_id("tvdb", "88001") == "3301"
    health = trakt_api.get("/genres/movies").mock(return_value=httpx.Response(200, json=[]))
    await trakt.healthcheck()
    assert health.calls.last.request.headers["trakt-api-key"] == "trakt-id"
