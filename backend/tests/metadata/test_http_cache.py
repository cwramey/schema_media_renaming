"""HTTP plumbing (limiters, retries, cache keys) and the sqlite/LRU response cache."""

from __future__ import annotations

import asyncio
import gzip
import json
import threading
import zlib
from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from schema_app.providers.base import ProviderError, ProviderRateLimited, ProviderUnavailable
from schema_app.providers.cache import CACHE_FILENAME, ResponseCache
from schema_app.providers.fixtures import FixturesProvider
from schema_app.providers.http import (
    HttpClient,
    ProviderResponseTooLarge,
    TokenBucket,
    WindowLimiter,
    _retry_after,
    bounded_gunzip,
)


class _AsyncStream(httpx.AsyncByteStream):
    def __init__(self, chunks: AsyncIterator[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._chunks:
            yield chunk

# --------------------------------------------------------------------------- cache


class Tick:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_cache_ttl_and_persistence(tmp_path) -> None:
    tick = Tick()
    path = tmp_path / CACHE_FILENAME
    c = ResponseCache(path, clock=tick)
    c.set("k", b"value", ttl=60)
    assert c.get("k") == b"value"
    tick.now += 61
    assert c.get("k") is None
    c.set("k2", b"other", ttl=3600)
    c.close()
    reopened = ResponseCache(path, clock=tick)
    assert reopened.get("k2") == b"other"  # survives a restart (disk layer)
    assert reopened.hits == 1
    tick.now += 7200
    assert reopened.purge_expired() == 2  # the expired "k" row and now "k2"
    reopened.close()


def test_cache_lru_bounds_and_prefix_delete(tmp_path) -> None:
    c = ResponseCache(tmp_path / CACHE_FILENAME, memory_items=2)
    for i in range(4):
        c.set(f"tmdb|{i}", str(i).encode(), ttl=60)
    assert len(c._mem) == 2
    assert c.get("tmdb|0") == b"0"  # evicted from memory, still on disk
    c.set("tvdb|x", b"x", ttl=60)
    assert c.delete_prefix("tmdb|") == 4
    assert c.get("tmdb|1") is None and c.get("tvdb|x") == b"x"
    c.delete("tvdb|x")
    assert c.get("tvdb|x") is None


async def test_cache_async_wrappers(tmp_path) -> None:
    c = ResponseCache(tmp_path / CACHE_FILENAME)
    await c.aset("a", b"1", 60)
    assert await c.aget("a") == b"1"
    await c.adelete("a")
    assert await c.aget("a") is None
    mem = ResponseCache(None)
    await mem.aset("b", b"2", 60)
    assert await mem.aget("b") == b"2"


def test_unusable_cache_path_degrades_to_memory(tmp_path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    c = ResponseCache(blocker / "sub" / CACHE_FILENAME)  # parent is a file -> cannot create
    c.set("k", b"v", 60)
    assert c.get("k") == b"v"


# --------------------------------------------------------------------------- limiters


async def test_token_bucket_waits_for_tokens() -> None:
    tick = Tick()
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)
        tick.now += s

    bucket = TokenBucket(2.0, 2.0, clock=tick, sleep=sleep)
    for _ in range(4):
        await bucket.acquire()
    assert slept == [pytest.approx(0.5), pytest.approx(0.5)]
    assert bucket.describe() == "2 requests / s"
    assert TokenBucket(0.4).describe() == "1 request / 2.5 s"
    with pytest.raises(ValueError):
        TokenBucket(0)


async def test_window_limiter_multiple_windows_and_persistence() -> None:
    tick = Tick()
    slept: list[float] = []
    saved: list[list[float]] = []

    async def sleep(s: float) -> None:
        slept.append(s)
        tick.now += s

    lim = WindowLimiter([(3, 10.0), (4, 100.0)], clock=tick, sleep=sleep, on_acquire=saved.append, max_wait=200)
    for _ in range(5):
        await lim.acquire()
    assert slept == [pytest.approx(10.0), pytest.approx(90.0)]
    assert saved[-1] == lim.history
    assert lim.describe() == "3 / 10 s and 4 / 100 s"
    strict = WindowLimiter([(1, 3600.0)], clock=tick, sleep=sleep, max_wait=5, history=[tick.now])
    with pytest.raises(ProviderRateLimited):
        await strict.acquire()


# --------------------------------------------------------------------------- client


async def test_cache_key_excludes_auth_and_includes_language() -> None:
    client = HttpClient("tmdb", base_url="https://api.example/3", cache=ResponseCache(None))
    with respx.mock() as router:
        route = router.get("https://api.example/3/search").mock(return_value=httpx.Response(200, json={"ok": 1}))
        await client.get_json("search", params={"q": "a", "language": "en"}, auth_params={"api_key": "S1"}, ttl=60)
        await client.get_json("search", params={"q": "a", "language": "en"}, auth_params={"api_key": "S2"}, ttl=60)
        await client.get_json("search", params={"q": "a", "language": "de"}, auth_params={"api_key": "S1"}, ttl=60)
        assert route.call_count == 2
    key = client.cache_key("GET", "https://api.example/3/search", {"q": "a", "language": "en"})
    assert "S1" not in key and "language=en" in key
    assert client.requests_made == 2 and client.cache_hits == 1
    await client.aclose()


async def test_invalid_json_is_not_cached() -> None:
    client = HttpClient("x", cache=ResponseCache(None))
    with respx.mock() as router:
        route = router.get("https://h.example/a").mock(side_effect=[
            httpx.Response(200, content=b"<html>"), httpx.Response(200, json=[1]),
        ])
        with pytest.raises(ProviderError):
            await client.get_json("https://h.example/a", ttl=60)
        assert await client.get_json("https://h.example/a", ttl=60) == [1]
        assert route.call_count == 2


async def test_json_is_parsed_once_per_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    from schema_app.providers import http as http_mod

    calls: list[int] = []
    real_loads = json.loads
    monkeypatch.setattr(http_mod.json, "loads", lambda body: calls.append(1) or real_loads(body))
    client = HttpClient("x", cache=ResponseCache(None))
    with respx.mock() as router:
        router.get("https://h.example/a").mock(return_value=httpx.Response(200, json={"a": 1}))
        assert await client.get_json("https://h.example/a", ttl=60) == {"a": 1}  # miss
        assert await client.get_json("https://h.example/a", ttl=60) == {"a": 1}  # hit
    assert len(calls) == 2


async def test_large_json_is_parsed_off_the_event_loop() -> None:
    seen: list[str] = []

    def parse(body: bytes) -> int:
        seen.append(threading.current_thread().name)
        return len(body)

    client = HttpClient("x")
    big = b"[" + b"1," * 40_000 + b"1]"
    with respx.mock() as router:
        router.get("https://h.example/big").mock(return_value=httpx.Response(200, content=big))
        router.get("https://h.example/small").mock(return_value=httpx.Response(200, content=b"[1]"))
        assert await client.get_parsed("https://h.example/big", parse) == len(big)
        assert await client.get_parsed("https://h.example/small", parse) == 3
        assert await client.get_parsed("https://h.example/small", parse, thread=True) == 3
    main = threading.main_thread().name
    assert seen[0] != main and seen[1] == main and seen[2] != main


async def test_corrupt_cache_entry_is_refetched() -> None:
    cache = ResponseCache(None)
    client = HttpClient("x", cache=cache)
    await cache.aset(client.cache_key("GET", "https://h.example/a", None), b"{broken", 60)
    with respx.mock() as router:
        route = router.get("https://h.example/a").mock(return_value=httpx.Response(200, json=[2]))
        assert await client.get_json("https://h.example/a", ttl=60) == [2]
    assert route.call_count == 1 and client.cache_hits == 0


# --------------------------------------------------------------------------- size caps


async def test_body_over_the_cap_is_refused_and_not_cached() -> None:
    client = HttpClient("x", cache=ResponseCache(None), max_bytes=1000)
    with respx.mock() as router:
        router.get("https://h.example/big").mock(return_value=httpx.Response(200, content=b"x" * 1001))
        with pytest.raises(ProviderResponseTooLarge):
            await client.get_bytes("https://h.example/big", ttl=60)
        router.get("https://h.example/ok").mock(return_value=httpx.Response(200, content=b"x" * 1000))
        assert len(await client.get_bytes("https://h.example/ok")) == 1000
        # A per-call cap overrides the client's.
        with pytest.raises(ProviderResponseTooLarge):
            await client.get_bytes("https://h.example/ok", max_bytes=999)
    assert client.cache is not None and await client.cache.aget(client.cache_key("GET", "https://h.example/big", None)) is None


async def test_declared_length_over_the_cap_is_refused_before_reading() -> None:
    client = HttpClient("x", max_bytes=1000)

    async def endless():  # would never finish if the client read it
        while True:
            yield b"x" * 100

    with respx.mock() as router:
        router.get("https://h.example/a").mock(
            return_value=httpx.Response(200, headers={"Content-Length": "5000000"}, stream=_AsyncStream(endless()))
        )
        with pytest.raises(ProviderResponseTooLarge):
            await client.get_bytes("https://h.example/a")


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "deflate-raw"])
async def test_compressed_bodies_are_decoded_within_the_cap(encoding: str) -> None:
    plain = b'{"ok": true}' + b" " * 5000
    if encoding == "gzip":
        wire = gzip.compress(plain)
    elif encoding == "deflate":
        wire = zlib.compress(plain)
    else:  # raw deflate without the zlib header, as some servers send it
        packer = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        wire = packer.compress(plain) + packer.flush()
    header = {"Content-Encoding": encoding.removesuffix("-raw")}
    client = HttpClient("x", max_bytes=len(plain))
    with respx.mock() as router:
        router.get("https://h.example/a").mock(return_value=httpx.Response(200, content=wire, headers=header))
        assert await client.get_json("https://h.example/a") == {"ok": True}
        router.get("https://h.example/b").mock(return_value=httpx.Response(200, content=wire, headers=header))
        with pytest.raises(ProviderResponseTooLarge):
            await client.get_bytes("https://h.example/b", max_bytes=len(plain) - 1)


async def test_gzip_bomb_stops_at_the_cap() -> None:
    bomb = gzip.compress(b"\0" * (64 * 1024 * 1024))  # 64 MiB of zeros in ~64 KB
    assert len(bomb) < 100_000
    client = HttpClient("x", max_bytes=1024 * 1024)
    with respx.mock() as router:
        router.get("https://h.example/a").mock(
            return_value=httpx.Response(200, content=bomb, headers={"Content-Encoding": "gzip"})
        )
        with pytest.raises(ProviderResponseTooLarge):
            await client.get_bytes("https://h.example/a")
    with pytest.raises(ProviderResponseTooLarge):
        bounded_gunzip(bomb, 1024 * 1024)
    assert bounded_gunzip(gzip.compress(b"abc"), 3) == b"abc"
    with pytest.raises(ProviderError):
        bounded_gunzip(b"\x1f\x8bnot gzip at all", 100)


async def test_unknown_content_encoding_is_an_error() -> None:
    client = HttpClient("x")
    with respx.mock() as router:
        router.get("https://h.example/a").mock(
            return_value=httpx.Response(200, content=b"zz", headers={"Content-Encoding": "br"})
        )
        with pytest.raises(ProviderError, match="content encoding"):
            await client.get_bytes("https://h.example/a")


# --------------------------------------------------------------------------- redirects


async def test_same_origin_redirect_keeps_the_provider_headers() -> None:
    client = HttpClient("trakt", base_url="https://api.example", headers={"x-client": "c1"})
    with respx.mock() as router:
        router.get("https://api.example/old").mock(return_value=httpx.Response(301, headers={"Location": "/new"}))
        new = router.get("https://api.example/new").mock(return_value=httpx.Response(200, json={"ok": 1}))
        assert await client.get_json("old", headers={"trakt-api-key": "SECRET"}) == {"ok": 1}
    sent = new.calls.last.request.headers
    assert sent["trakt-api-key"] == "SECRET" and sent["x-client"] == "c1"


async def test_cross_origin_redirect_drops_provider_headers() -> None:
    client = HttpClient("trakt", base_url="https://api.example", headers={"x-client": "c1"})
    with respx.mock() as router:
        router.get("https://api.example/a").mock(
            return_value=httpx.Response(302, headers={"Location": "https://cdn.example/a?x=1"})
        )
        cdn = router.get("https://cdn.example/a").mock(return_value=httpx.Response(200, json=[1]))
        assert await client.get_json(
            "a", headers={"trakt-api-key": "SECRET", "Authorization": "Bearer T"}, auth_params={"api_key": "K"}
        ) == [1]
    request = cdn.calls.last.request
    lowered = {k.lower() for k in request.headers}
    assert not lowered & {"trakt-api-key", "authorization", "x-client"}
    assert request.headers["User-Agent"].startswith("Schema/") and "api_key" not in str(request.url)


async def test_redirect_refusals() -> None:
    client = HttpClient("x", max_retries=0)
    with respx.mock(assert_all_called=False) as router:
        router.get("https://h.example/down").mock(
            return_value=httpx.Response(302, headers={"Location": "http://h.example/plain"})
        )
        with pytest.raises(ProviderError, match="https to http"):
            await client.request("GET", "https://h.example/down")
        router.post("https://h.example/login").mock(
            return_value=httpx.Response(307, headers={"Location": "https://evil.example/login"})
        )
        evil = router.post("https://evil.example/login").mock(return_value=httpx.Response(200, json={}))
        with pytest.raises(ProviderError, match="request body"):
            await client.post_json("https://h.example/login", {"apikey": "SECRET"})
        assert evil.call_count == 0
        router.get("https://h.example/loop").mock(return_value=httpx.Response(302, headers={"Location": "/loop"}))
        with pytest.raises(ProviderError, match="too many redirects"):
            await client.request("GET", "https://h.example/loop")


async def test_same_origin_post_redirects() -> None:
    client = HttpClient("tvdb")
    with respx.mock() as router:
        router.post("https://h.example/login").mock(return_value=httpx.Response(308, headers={"Location": "/v4/login"}))
        moved = router.post("https://h.example/v4/login").mock(return_value=httpx.Response(200, json={"t": 1}))
        assert await client.post_json("https://h.example/login", {"apikey": "K"}) == {"t": 1}
        assert json.loads(moved.calls.last.request.content) == {"apikey": "K"}
        router.post("https://h.example/form").mock(return_value=httpx.Response(303, headers={"Location": "/done"}))
        done = router.get("https://h.example/done").mock(return_value=httpx.Response(200, json={"ok": 1}))
        assert await client.post_json("https://h.example/form", {"x": 1}) == {"ok": 1}
        assert done.calls.last.request.content == b""


async def test_transport_errors_retry_then_fail() -> None:
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)

    client = HttpClient("x", max_retries=2, sleep=sleep)
    with respx.mock() as router:
        router.get("https://h.example/a").mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(ProviderUnavailable) as err:
            await client.request("GET", "https://h.example/a", params={"apikey": "SECRET"})
    assert len(slept) == 2 and all(0.5 <= s <= 2.0 for s in slept)
    assert "SECRET" not in str(err.value) and "/a" in str(err.value)


def test_retry_after_parsing() -> None:
    assert _retry_after(httpx.Response(429, headers={"Retry-After": "7"})) == 7.0
    assert _retry_after(httpx.Response(429)) is None
    assert _retry_after(httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})) == 0.0
    assert _retry_after(httpx.Response(429, headers={"Retry-After": "soon"})) is None


async def test_concurrent_requests_share_the_limiter() -> None:
    tick = Tick()

    async def sleep(s: float) -> None:
        tick.now += s
        await asyncio.sleep(0)

    client = HttpClient("x", limiter=TokenBucket(1.0, 1.0, clock=tick, sleep=sleep))
    with respx.mock() as router:
        router.get("https://h.example/a").mock(return_value=httpx.Response(200, json={}))
        await asyncio.gather(*(client.request("GET", "https://h.example/a") for _ in range(3)))
    assert tick.now == pytest.approx(1002.0)


# --------------------------------------------------------------------------- fixtures provider


async def test_fixtures_provider_behaves_like_a_search_engine(no_network) -> None:
    p = FixturesProvider()
    assert p.is_configured() and not p.requires_key
    assert [m.year for m in await p.search_movies("Iron Orchard")] == [2023, 1987]  # popularity order
    assert [m.year for m in await p.search_movies("Iron Orchard", 1986)] == [1987]  # ±1 year filter
    assert await p.search_movies("Completely Unknown Title") == []
    s = (await p.search_series("Wings of the Wind"))[0]
    assert s.title == "Kaze no Tsubasa" and s.is_anime and s.season_count == 3
    assert s.ids["anidb"] == "90001" and s.ids["fixtures"] == "900101"
    eps = await p.get_episodes("900101")
    assert len([e for e in eps if e.season]) == 37 and [e.title for e in eps if e.season == 0] == ["Wind Beneath the Ruins"]
    assert await p.lookup_series_id("tvdb", "900001") == "900001"
    from schema_app.providers.base import ProviderNotFound

    with pytest.raises(ProviderNotFound):
        await p.get_series("nope")
