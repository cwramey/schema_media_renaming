"""TheTVDB v4 provider against respx-mocked HTTP: login, bearer refresh, search, details, episodes."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from schema_app.domain import EpisodeOrder, ProviderCredentials
from schema_app.providers.base import ProviderNotConfigured
from schema_app.providers.cache import ResponseCache
from schema_app.providers.http import ProviderAuthError
from schema_app.providers.tvdb import TvdbProvider

from .conftest import payload

BASE = "https://api4.thetvdb.com/v4"
DATA = payload("tvdb.json")


@pytest.fixture
def cache():
    return ResponseCache(None)


@pytest.fixture
def tvdb(cache, clock):
    return TvdbProvider(ProviderCredentials(tvdb_api_key="tvdb-key", tvdb_pin="1234"), cache=cache, sleep=clock.sleep)


@pytest.fixture
def api():
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        yield router


async def test_not_configured() -> None:
    p = TvdbProvider(ProviderCredentials())
    assert not p.is_configured()
    with pytest.raises(ProviderNotConfigured):
        await p.search_series("x")


async def test_login_then_search(tvdb, api) -> None:
    login = api.post("/login").mock(return_value=httpx.Response(200, json=DATA["login"]))
    search = api.get("/search").mock(return_value=httpx.Response(200, json=DATA["search"]))
    results = await tvdb.search_series("Glass Harbour", 2014)
    assert json.loads(login.calls.last.request.content) == {"apikey": "tvdb-key", "pin": "1234"}
    req = search.calls.last.request
    assert req.headers["Authorization"] == "Bearer tok-1"
    assert req.url.params["type"] == "series" and req.url.params["year"] == "2014"
    first, second = results
    assert first.title == "Glass Harbour" and first.year == 2014  # "(2014)" suffix stripped
    assert first.ids == {"tvdb": "88001", "imdb": "tt8800001", "tmdb": "4100"}
    assert "Glashafen" in first.aliases
    assert second.title == "Spider Thread" and second.is_anime  # English translation preferred


async def test_token_is_reused_and_cached(tvdb, api, cache, clock) -> None:
    login = api.post("/login").mock(return_value=httpx.Response(200, json=DATA["login"]))
    api.get("/search").mock(return_value=httpx.Response(200, json=DATA["search"]))
    await tvdb.search_series("A")
    await tvdb.search_series("B")
    assert login.call_count == 1
    again = TvdbProvider(ProviderCredentials(tvdb_api_key="tvdb-key", tvdb_pin="1234"), cache=cache, sleep=clock.sleep)
    await again.search_series("C")
    assert login.call_count == 1, "a restarted provider reuses the cached 25-day token"


async def test_expired_token_triggers_one_relogin(tvdb, api) -> None:
    login = api.post("/login").mock(side_effect=[httpx.Response(200, json=DATA["login"]),
                                                 httpx.Response(200, json=DATA["login2"])])
    search = api.get("/search").mock(side_effect=[httpx.Response(401), httpx.Response(200, json=DATA["search"])])
    results = await tvdb.search_series("Glass Harbour")
    assert results and login.call_count == 2
    assert search.calls[-1].request.headers["Authorization"] == "Bearer tok-2"


async def test_rejected_login(tvdb, api) -> None:
    api.post("/login").mock(return_value=httpx.Response(401))
    with pytest.raises(ProviderAuthError) as err:
        await tvdb.search_series("x")
    assert "PIN" in str(err.value)
    assert "tvdb-key" not in str(err.value)


async def test_series_extended(tvdb, api) -> None:
    api.post("/login").mock(return_value=httpx.Response(200, json=DATA["login"]))
    route = api.get("/series/88001/extended").mock(return_value=httpx.Response(200, json=DATA["extended"]))
    s = await tvdb.get_series("88001")
    assert route.calls.last.request.url.params["meta"] == "translations"
    assert s.title == "Glass Harbour" and s.year == 2014
    assert s.ids == {"tvdb": "88001", "imdb": "tt8800001", "tmdb": "4100", "tvmaze": "7001"}
    assert s.season_count == 2  # official seasons > 0 only
    assert s.network == "Northern Channel" and s.status == "Ended" and not s.is_anime


async def test_episodes_paginate_with_native_absolute(tvdb, api) -> None:
    api.post("/login").mock(return_value=httpx.Response(200, json=DATA["login"]))
    route = api.get("/series/88001/episodes/default/eng").mock(side_effect=[
        httpx.Response(200, json=DATA["episodes_page0"]), httpx.Response(200, json=DATA["episodes_page1"]),
    ])
    eps = await tvdb.get_episodes("88001")
    assert [c.request.url.params["page"] for c in route.calls] == ["0", "1"]
    assert [(e.season, e.episode, e.absolute) for e in eps if e.season] == [
        (1, 1, 1), (1, 2, 2), (1, 3, 3), (2, 1, 4), (2, 2, 5)]
    assert next(e for e in eps if e.season == 0).title == "Behind the Glass"


async def test_episodes_fall_back_to_untranslated_and_dvd_type(tvdb, api) -> None:
    api.post("/login").mock(return_value=httpx.Response(200, json=DATA["login"]))
    api.get("/series/88001/episodes/dvd/eng").mock(return_value=httpx.Response(404))
    plain = api.get("/series/88001/episodes/dvd").mock(return_value=httpx.Response(200, json=DATA["episodes_page1"]))
    eps = await tvdb.get_episodes("88001", EpisodeOrder.DVD)
    assert plain.call_count == 1 and len(eps) == 3


async def test_healthcheck_forces_login(tvdb, api) -> None:
    login = api.post("/login").mock(return_value=httpx.Response(200, json=DATA["login"]))
    await tvdb.healthcheck()
    await tvdb.healthcheck()
    assert login.call_count == 2
