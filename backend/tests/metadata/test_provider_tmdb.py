"""TMDb provider against respx-mocked HTTP (no real network)."""

from __future__ import annotations

import httpx
import pytest
import respx

from schema_app.domain import EpisodeOrder, ProviderCredentials
from schema_app.providers.base import (
    ProviderNotConfigured,
    ProviderNotFound,
    ProviderRateLimited,
    ProviderUnavailable,
)
from schema_app.providers.cache import ResponseCache
from schema_app.providers.http import ProviderAuthError
from schema_app.providers.tmdb import TmdbProvider

from .conftest import payload

BASE = "https://api.themoviedb.org/3"


@pytest.fixture
def tmdb(clock):
    p = TmdbProvider(ProviderCredentials(tmdb_api_key="v3secretkey"), cache=ResponseCache(None), sleep=clock.sleep)
    yield p


@pytest.fixture
def api():
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        yield router


def _mock_show(api) -> None:
    seasons = payload("tmdb_seasons_4100.json")
    groups = payload("tmdb_episode_groups_4100.json")
    api.get("/tv/4100").mock(return_value=httpx.Response(200, json=payload("tmdb_tv_4100.json")))
    for n in ("0", "1", "2"):
        api.get(f"/tv/4100/season/{n}").mock(return_value=httpx.Response(200, json=seasons[n]))
    api.get("/tv/4100/episode_groups").mock(return_value=httpx.Response(200, json=groups["groups_list"]))
    api.get("/tv/episode_group/grp-dvd").mock(return_value=httpx.Response(200, json=groups["grp-dvd"]))
    api.get("/tv/episode_group/grp-abs").mock(return_value=httpx.Response(200, json=groups["grp-abs"]))


async def test_not_configured() -> None:
    p = TmdbProvider(ProviderCredentials())
    assert not p.is_configured()
    with pytest.raises(ProviderNotConfigured):
        await p.search_movies("x")


async def test_search_series_maps_fields_and_sends_key(tmdb, api) -> None:
    route = api.get("/search/tv").mock(return_value=httpx.Response(200, json=payload("tmdb_search_tv.json")))
    results = await tmdb.search_series("Glass Harbour", 2014)
    assert [r.title for r in results] == ["Glass Harbour", "Glass Harbour Nights", "Kumo no Ito"]
    first = results[0]
    assert first.ref.provider == "tmdb" and first.ref.id == "4100"
    assert first.year == 2014 and first.ids == {"tmdb": "4100"}
    assert results[2].is_anime and not first.is_anime
    sent = route.calls.last.request.url.params
    assert sent["api_key"] == "v3secretkey"
    assert sent["query"] == "Glass Harbour" and sent["first_air_date_year"] == "2014"
    assert sent["language"] == "en"
    assert route.calls.last.request.headers["User-Agent"].startswith("Schema/")


async def test_bearer_token_auth(api, clock) -> None:
    p = TmdbProvider(ProviderCredentials(tmdb_api_key="eyJhbGciOi.fake.token"), cache=ResponseCache(None))
    route = api.get("/search/movie").mock(return_value=httpx.Response(200, json={"results": []}))
    await p.search_movies("Copper Sky")
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer eyJhbGciOi.fake.token"
    assert "api_key" not in req.url.params


async def test_series_details(tmdb, api) -> None:
    _mock_show(api)
    s = await tmdb.get_series("4100")
    assert s.title == "Glass Harbour" and s.year == 2014
    assert s.ids == {"tmdb": "4100", "tvdb": "88001", "imdb": "tt8800001"}
    assert s.aliases == ["Glashafen"]
    assert s.network == "Northern Channel" and s.season_count == 2 and s.status == "Ended"


async def test_episodes_aired_order_with_cumulative_absolute(tmdb, api) -> None:
    _mock_show(api)
    eps = await tmdb.get_episodes("4100")
    regular = [(e.season, e.episode, e.absolute, e.title) for e in eps if e.season]
    assert regular == [(1, 1, 1, "Low Water"), (1, 2, 2, "The Keeper"), (1, 3, 3, "Squall"),
                       (2, 1, 4, "High Water"), (2, 2, 5, "Last Lamp")]
    special = next(e for e in eps if e.season == 0)
    assert special.special and special.absolute is None and special.title == "Behind the Glass"


async def test_episodes_dvd_order_uses_episode_group(tmdb, api) -> None:
    _mock_show(api)
    eps = await tmdb.get_episodes("4100", EpisodeOrder.DVD)
    assert [(e.season, e.episode, e.title) for e in eps][:3] == [(1, 1, "The Keeper"), (1, 2, "Low Water"), (1, 3, "Squall")]


async def test_episodes_absolute_order_uses_group(tmdb, api) -> None:
    _mock_show(api)
    eps = await tmdb.get_episodes("4100", EpisodeOrder.ABSOLUTE)
    by_title = {e.title: e.absolute for e in eps}
    assert by_title["Squall"] == 2 and by_title["The Keeper"] == 3


async def test_absolute_order_falls_back_to_airdate(tmdb, api) -> None:
    _mock_show(api)
    api.get("/tv/4100/episode_groups").mock(return_value=httpx.Response(200, json={"results": []}))
    eps = await tmdb.get_episodes("4100", EpisodeOrder.ABSOLUTE)
    assert [e.absolute for e in eps if e.season] == [1, 2, 3, 4, 5]


async def test_movies_search_and_details(tmdb, api) -> None:
    data = payload("tmdb_movies.json")
    api.get("/search/movie").mock(return_value=httpx.Response(200, json=data["search"]))
    route = api.get("/movie/51001").mock(return_value=httpx.Response(200, json=data["details"]))
    found = await tmdb.search_movies("Copper Sky")
    assert [(m.title, m.year) for m in found] == [("Copper Sky", 2011), ("Copper Sky", 1962)]
    m = await tmdb.get_movie("51001")
    assert m.ids == {"tmdb": "51001", "imdb": "tt5100001"}
    assert m.certification == "PG-13" and m.collection == "Copper Sky Collection"
    assert m.aliases == ["Ciel de Cuivre"] and m.runtime_minutes == 104 and m.genres == ["Drama"]
    assert m.poster_url is None
    assert route.calls.last.request.url.params["append_to_response"] == "external_ids,alternative_titles,release_dates"


async def test_responses_are_cached(tmdb, api) -> None:
    route = api.get("/search/tv").mock(return_value=httpx.Response(200, json=payload("tmdb_search_tv.json")))
    await tmdb.search_series("Glass Harbour")
    await tmdb.search_series("Glass Harbour")
    assert route.call_count == 1
    assert tmdb.requests_made == 1 and tmdb.cache_hits == 1


async def test_429_backoff_honours_retry_after(tmdb, api, clock) -> None:
    route = api.get("/search/tv").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "3"}),
        httpx.Response(200, json=payload("tmdb_search_tv.json")),
    ])
    results = await tmdb.search_series("Glass Harbour")
    assert results and route.call_count == 2
    assert clock.sleeps == [3.0]


async def test_429_exhausted_raises_rate_limited(tmdb, api, clock) -> None:
    api.get("/search/tv").mock(return_value=httpx.Response(429))
    with pytest.raises(ProviderRateLimited):
        await tmdb.search_series("Glass Harbour")
    assert len(clock.sleeps) == 3  # max_retries


async def test_5xx_and_timeouts_retry_then_unavailable(tmdb, api, clock) -> None:
    api.get("/search/tv").mock(side_effect=[httpx.ConnectTimeout("slow"), httpx.Response(503), httpx.Response(502),
                                            httpx.Response(500)])
    with pytest.raises(ProviderUnavailable) as err:
        await tmdb.search_series("Glass Harbour")
    assert "v3secretkey" not in str(err.value)
    assert len(clock.sleeps) == 3


async def test_401_and_404(tmdb, api) -> None:
    api.get("/search/tv").mock(return_value=httpx.Response(401))
    with pytest.raises(ProviderAuthError) as err:
        await tmdb.search_series("x")
    assert "v3secretkey" not in str(err.value)
    api.get("/tv/999").mock(return_value=httpx.Response(404))
    with pytest.raises(ProviderNotFound):
        await tmdb.get_series("999")


async def test_healthcheck_and_lookup(tmdb, api) -> None:
    api.get("/configuration").mock(return_value=httpx.Response(200, json={"images": {}}))
    await tmdb.healthcheck()
    route = api.get("/find/88001").mock(return_value=httpx.Response(200, json={"tv_results": [{"id": 4100}]}))
    assert await tmdb.lookup_series_id("tvdb", "88001") == "4100"
    assert route.calls.last.request.url.params["external_source"] == "tvdb_id"
    assert await tmdb.lookup_series_id("anidb", "1") is None
