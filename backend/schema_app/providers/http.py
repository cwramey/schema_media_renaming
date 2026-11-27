"""Shared HTTP plumbing for metadata providers (SPEC §6.2).

* one ``httpx.AsyncClient`` per provider (connect 5 s / read 20 s, ``User-Agent: Schema/<v>``);
* per-host rate limiting: a token bucket (``TokenBucket``) or FloodLimit-style sliding
  windows (``WindowLimiter``, used for AniDB's 5/60 s + 200/24 h rule);
* retries with jittered exponential backoff on 429 / 5xx / timeouts, honouring
  ``Retry-After``;
* response caching through ``ResponseCache`` with request / cache-hit counters that feed
  ``ProviderStatus``;
* bodies are streamed and capped (``max_bytes``, counted after gzip/deflate decoding, which is
  done here with a bounded ``zlib`` decompressor so a compression bomb stops at the cap);
* redirects are followed by hand: same origin keeps the request as it was, another origin gets
  only ``User-Agent``/``Accept-Encoding`` (provider keys such as ``trakt-api-key`` or a bearer
  token stay behind; httpx itself would only drop ``Authorization``), never a request body, and
  never a downgrade from https to http;
* large bodies are parsed in a worker thread (``get_json`` / ``get_parsed``), once per fetch.

Error messages never include query strings, so API keys passed as parameters cannot
leak into logs or ``provider_errors``.
"""

from __future__ import annotations

import asyncio
import email.utils
import json
import logging
import random
import time
import zlib
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

import httpx

from schema_app import __version__
from schema_app.providers.base import (
    ProviderError,
    ProviderNotFound,
    ProviderRateLimited,
    ProviderUnavailable,
)
from schema_app.providers.cache import ResponseCache

log = logging.getLogger("schema.providers.http")

USER_AGENT = f"Schema/{__version__} (+https://github.com/schema-media/schema)"
DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
MIB = 1024 * 1024
DEFAULT_MAX_BYTES = 16 * MIB  # per response, decoded; metadata JSON is far below this
MAX_REDIRECTS = 5
PARSE_IN_THREAD_BYTES = 64 * 1024  # smaller bodies parse faster than a thread hop
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_SAFE_HEADERS = frozenset({"user-agent", "accept-encoding", "accept"})  # sent to any host
_HOP_HEADERS = frozenset({"host", "content-length"})

Sleep = Callable[[float], Awaitable[None]]


class ProviderAuthError(ProviderError):
    """401/403: the credentials were rejected (or a bearer token expired)."""


class ProviderResponseTooLarge(ProviderError):
    """The response (after decoding) is larger than the request's byte cap."""


def bounded_gunzip(data: bytes, max_bytes: int, *, what: str = "response") -> bytes:
    """Gunzip ``data`` (a single gzip member) to at most ``max_bytes`` bytes, or raise
    ``ProviderResponseTooLarge`` / ``ProviderError`` (corrupt data)."""
    body = _BoundedBody("gzip", max_bytes, what)
    body.feed(data)
    return body.finish()


class _BoundedBody:
    """Collects a body, undoing gzip / deflate with ``decompress(data, max_length)`` so that the
    decoded size never exceeds ``limit`` (a few KB of gzip can expand to gigabytes)."""

    def __init__(self, encoding: str, limit: int, where: str) -> None:
        self.limit = limit
        self.raw_limit = limit + limit // 64 + 4096  # wire bytes: room for gzip framing on small bodies
        self.where = where
        self.parts: list[bytes] = []
        self.size = 0
        self.raw = 0
        codings = [c.strip().lower() for c in (encoding or "").split(",") if c.strip().lower() not in ("", "identity")]
        self.coding = codings[0] if len(codings) == 1 else ("" if not codings else "unsupported")
        self._deflate_probe = self.coding == "deflate"
        self._z: Any = None
        if self.coding in ("gzip", "x-gzip"):
            self._z = zlib.decompressobj(zlib.MAX_WBITS | 16)
        elif self.coding == "deflate":
            self._z = zlib.decompressobj()
        elif self.coding:
            raise ProviderError(f"{where}: unsupported content encoding {encoding!r}")

    def _too_large(self) -> ProviderResponseTooLarge:
        return ProviderResponseTooLarge(f"{self.where}: response larger than {self.limit // 1024} KiB")

    def _add(self, data: bytes) -> None:
        self.size += len(data)
        if self.size > self.limit:
            raise self._too_large()
        if data:
            self.parts.append(data)

    def feed(self, chunk: bytes) -> None:
        self.raw += len(chunk)
        if self.raw > self.raw_limit:  # also ends a compressed stream that never yields output
            raise self._too_large()
        if self._z is None:
            self._add(chunk)
            return
        try:
            out = self._z.decompress(chunk, self.limit + 1 - self.size)
        except zlib.error:
            if not self._deflate_probe:
                raise ProviderError(f"{self.where}: corrupt compressed response") from None
            # "deflate" servers sometimes send a raw stream without the zlib header (httpx does the same).
            self._deflate_probe = False
            self._z = zlib.decompressobj(-zlib.MAX_WBITS)
            try:
                out = self._z.decompress(chunk, self.limit + 1 - self.size)
            except zlib.error:
                raise ProviderError(f"{self.where}: corrupt compressed response") from None
        self._deflate_probe = False
        self._add(out)
        if self._z.unconsumed_tail:  # output stopped at the cap with input left over
            raise self._too_large()

    def finish(self) -> bytes:
        if self._z is not None:
            self._add(self._z.flush())
        return b"".join(self.parts)


def _plain_headers(headers: httpx.Headers) -> list[tuple[str, str]]:
    """Response headers minus the ones that describe the (already decoded) wire body."""
    drop = {"content-encoding", "content-length", "transfer-encoding"}
    return [(k, v) for k, v in headers.multi_items() if k.lower() not in drop]


class Limiter(Protocol):
    async def acquire(self) -> None: ...

    def describe(self) -> str: ...


class TokenBucket:
    """Classic token bucket: ``rate`` tokens per second, bursts up to ``capacity``."""

    def __init__(
        self,
        rate: float,
        capacity: float = 1.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Sleep | None = None,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate
        self.capacity = max(1.0, capacity)
        self._tokens = self.capacity
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._lock: asyncio.Lock | None = None

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
        self._updated = now

    async def acquire(self) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        sleep = self._sleep or asyncio.sleep
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await sleep((1.0 - self._tokens) / self.rate)

    def describe(self) -> str:
        if self.rate >= 1:
            return f"{self.rate:g} requests / s"
        return f"1 request / {1 / self.rate:g} s"


class WindowLimiter:
    """At most N requests per sliding window, for every (N, window) pair (FileBot ``FloodLimit``).

    ``max_wait`` bounds how long ``acquire`` may block; a longer wait (e.g. the daily
    quota is used up) raises ``ProviderRateLimited`` instead of stalling a job for hours.
    ``on_acquire`` lets the owner persist the request history across restarts.
    """

    def __init__(
        self,
        windows: list[tuple[int, float]],
        *,
        min_interval: float = 0.0,
        max_wait: float = 90.0,
        history: list[float] | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Sleep | None = None,
        on_acquire: Callable[[list[float]], None] | None = None,
    ) -> None:
        self.windows = sorted(windows, key=lambda w: w[1])
        self.min_interval = min_interval
        self.max_wait = max_wait
        self.history: list[float] = sorted(history or [])
        self._clock = clock
        self._sleep = sleep
        self._on_acquire = on_acquire
        self._lock: asyncio.Lock | None = None

    def _wait_needed(self, now: float) -> float:
        horizon = max(w for _n, w in self.windows) if self.windows else 0.0
        self.history = [t for t in self.history if t > now - horizon]
        wait = 0.0
        if self.min_interval and self.history:
            wait = max(wait, self.history[-1] + self.min_interval - now)
        for limit, window in self.windows:
            recent = [t for t in self.history if t > now - window]
            if len(recent) >= limit:
                wait = max(wait, recent[-limit] + window - now)
        return wait

    async def acquire(self) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        sleep = self._sleep or asyncio.sleep
        async with self._lock:
            while True:
                now = self._clock()
                wait = self._wait_needed(now)
                if wait <= 0:
                    self.history.append(now)
                    if self._on_acquire is not None:
                        self._on_acquire(list(self.history))
                    return
                if wait > self.max_wait:
                    raise ProviderRateLimited(
                        f"local rate limit reached; next request allowed in {int(wait // 60)} min"
                    )
                await sleep(wait)

    def describe(self) -> str:
        parts = []
        for limit, window in self.windows:
            if window >= 86400:
                parts.append(f"{limit} / {window / 3600:g} h")
            else:
                parts.append(f"{limit} / {window:g} s")
        return " and ".join(parts)


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    raw = raw.strip()
    if raw.isdigit():
        return float(raw)
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _path_only(url: str) -> str:
    parts = urlsplit(url)
    return parts.path or url.split("?", 1)[0]


async def _run_parse[T](parse: Callable[[bytes], T], body: bytes, thread: bool | None) -> T:
    if thread or (thread is None and len(body) >= PARSE_IN_THREAD_BYTES):
        return await asyncio.to_thread(parse, body)
    return parse(body)


class HttpClient:
    """Provider-scoped HTTP client: limiter + retries + cache + counters."""

    def __init__(
        self,
        provider: str,
        *,
        base_url: str = "",
        limiter: Limiter | None = None,
        host_limiters: Mapping[str, Limiter] | None = None,
        cache: ResponseCache | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        backoff_max: float = 30.0,
        retry_after_cap: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self.limiter = limiter
        self.host_limiters = dict(host_limiters or {})
        self.cache = cache
        self.headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}
        self.headers.update(headers or {})
        self.max_bytes = max_bytes
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.retry_after_cap = retry_after_cap
        self.transport = transport
        self._sleep = sleep
        self._client: httpx.AsyncClient | None = None
        self.requests_made = 0
        self.cache_hits = 0
        self.last_request_at: float | None = None

    # ------------------------------------------------------------------ plumbing

    def _client_for_use(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers={k: v for k, v in self.headers.items() if k.lower() in _SAFE_HEADERS},
                timeout=self.timeout,
                transport=self.transport,
                follow_redirects=False,  # see _follow: provider headers must not reach another host
            )
        return self._client

    def _own_headers(self, extra: Mapping[str, str] | None) -> dict[str, str]:
        """Client-level custom headers plus the call's own (both stay with the provider's origin)."""
        own = {k: v for k, v in self.headers.items() if k.lower() not in _SAFE_HEADERS}
        own.update(extra or {})
        return own

    async def _exchange(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None,
        headers: Mapping[str, str] | None,
        json_body: Any,
        limit: int,
        where: str,
    ) -> httpx.Response:
        """One request, its redirects (see ``_follow``) and its capped body. The returned response
        holds the decoded body; it is empty unless the status is 2xx."""
        request = client.build_request(method, url, params=params, headers=self._own_headers(headers), json=json_body)
        for _hop in range(MAX_REDIRECTS + 1):
            resp = await client.send(request, stream=True)
            try:
                location = resp.headers.get("location")
                if resp.status_code in _REDIRECTS and location:
                    request = self._follow(client, request, resp.status_code, location, where)
                    continue
                body = await self._read_body(resp, limit, where) if resp.is_success else b""
            finally:
                await resp.aclose()
            return httpx.Response(resp.status_code, headers=_plain_headers(resp.headers), content=body, request=request)
        raise ProviderError(f"{where}: too many redirects")

    def _follow(self, client: httpx.AsyncClient, request: httpx.Request, status: int, location: str,
                where: str) -> httpx.Request:  # fmt: skip
        try:
            target = request.url.join(location)
        except (httpx.InvalidURL, ValueError):
            raise ProviderError(f"{where}: invalid redirect") from None
        if target.scheme not in ("http", "https") or not target.host:
            raise ProviderError(f"{where}: redirect to an unsupported URL")
        if request.url.scheme == "https" and target.scheme != "https":
            raise ProviderError(f"{where}: refusing a redirect from https to http")
        method, content = request.method, request.content
        if (status == 303 and method != "HEAD") or (status in (301, 302) and method == "POST"):
            method, content = "GET", b""
        origin = (request.url.scheme, request.url.host, request.url.port)
        if (target.scheme, target.host, target.port) == origin:
            drop = _HOP_HEADERS | ({"content-type"} if not content else set())
            headers = {k: v for k, v in request.headers.items() if k.lower() not in drop}
        else:
            if content:
                raise ProviderError(f"{where}: refusing to send the request body to another host")
            headers = {}  # client defaults only: User-Agent, Accept-Encoding
            log.info("%s: redirected to %s; provider headers not forwarded", where, target.host)
        return client.build_request(method, target, headers=headers, content=content or None)

    @staticmethod
    async def _read_body(resp: httpx.Response, limit: int, where: str) -> bytes:
        declared = resp.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            raise ProviderResponseTooLarge(f"{where}: response larger than {limit // 1024} KiB")
        body = _BoundedBody(resp.headers.get("content-encoding", ""), limit, where)
        async for chunk in resp.aiter_raw():
            body.feed(chunk)
        return body.finish()

    def url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    def cache_key(self, method: str, url: str, params: Mapping[str, Any] | None, extra: str = "") -> str:
        query = urlencode(sorted((k, str(v)) for k, v in (params or {}).items() if v is not None))
        return f"{self.provider}|{method}|{url}?{query}|{extra}"

    def _limiter_for(self, url: str) -> Limiter | None:
        host = urlsplit(url).hostname or ""
        return self.host_limiters.get(host, self.limiter)

    def _backoff(self, attempt: int) -> float:
        delay = min(self.backoff_max, self.backoff_base * (2**attempt))
        return delay * (0.5 + random.random() / 2)

    async def _pause(self, delay: float) -> None:
        await (self._sleep or asyncio.sleep)(delay)

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        auth_params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        json_body: Any = None,
        max_bytes: int | None = None,
    ) -> httpx.Response:
        """Send one request with limiting and retries. Returns a 2xx/3xx response, its body
        already read (at most ``max_bytes`` decoded bytes), or raises a ``ProviderError`` subclass."""
        url = self.url(path)
        merged = {k: v for k, v in {**(params or {}), **(auth_params or {})}.items() if v is not None}
        where = f"{self.provider}: {method} {_path_only(url)}"
        limit = max_bytes or self.max_bytes
        attempt = 0
        while True:
            limiter = self._limiter_for(url)
            if limiter is not None:
                await limiter.acquire()
            client = self._client_for_use()
            try:
                self.requests_made += 1
                self.last_request_at = time.time()
                resp = await self._exchange(client, method, url, params=merged or None, headers=headers,
                                            json_body=json_body, limit=limit, where=where)  # fmt: skip
            except httpx.TimeoutException:
                if attempt < self.max_retries:
                    await self._pause(self._backoff(attempt))
                    attempt += 1
                    continue
                raise ProviderUnavailable(f"{where} timed out") from None
            except httpx.TransportError as exc:
                if attempt < self.max_retries:
                    await self._pause(self._backoff(attempt))
                    attempt += 1
                    continue
                raise ProviderUnavailable(f"{where} failed: {type(exc).__name__}") from None

            status = resp.status_code
            if status == 429 or status >= 500:
                if attempt < self.max_retries:
                    delay = _retry_after(resp)
                    if delay is None:
                        delay = self._backoff(attempt)
                    elif delay > self.retry_after_cap:
                        raise ProviderRateLimited(f"{where}: rate limited, retry after {int(delay)} s")
                    log.info("%s -> HTTP %s, retrying in %.1fs", where, status, delay)
                    await self._pause(delay)
                    attempt += 1
                    continue
                if status == 429:
                    raise ProviderRateLimited(f"{where}: HTTP 429 (rate limited)")
                raise ProviderUnavailable(f"{where}: HTTP {status}")
            if status in (401, 403):
                raise ProviderAuthError(f"{where}: HTTP {status} (credentials rejected)")
            if status == 404:
                raise ProviderNotFound(f"{where}: not found")
            if status >= 400:
                raise ProviderError(f"{where}: HTTP {status}")
            return resp

    # ------------------------------------------------------------------ cached helpers

    async def get_bytes(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        auth_params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        ttl: float | None = None,
        cache_extra: str = "",
        validate: Callable[[bytes], None] | None = None,
        max_bytes: int | None = None,
    ) -> bytes:
        """Raw body (cached when ``ttl``). ``validate`` runs on a fresh body before it is cached;
        it should be cheap - use ``get_parsed`` when the body has to be parsed anyway."""
        url = self.url(path)
        key = self.cache_key("GET", url, params, cache_extra) if (ttl and self.cache is not None) else None
        if key is not None:
            cached = await self.cache.aget(key)  # type: ignore[union-attr]
            if cached is not None:
                self.cache_hits += 1
                return cached
        resp = await self.request("GET", url, params=params, auth_params=auth_params, headers=headers,
                                  max_bytes=max_bytes)  # fmt: skip
        body = resp.content
        if validate is not None:
            validate(body)
        if key is not None:
            await self.cache.aset(key, body, ttl)  # type: ignore[union-attr, arg-type]
        return body

    async def get_parsed[T](
        self,
        path: str,
        parse: Callable[[bytes], T],
        *,
        params: Mapping[str, Any] | None = None,
        auth_params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        ttl: float | None = None,
        cache_extra: str = "",
        max_bytes: int | None = None,
        thread: bool | None = None,
    ) -> T:
        """GET, then ``parse`` the body exactly once - in a worker thread when ``thread`` is true
        (default: bodies of 64 KiB and up). ``parse`` doubles as validation: a body it rejects
        (with a ``ProviderError``) is never cached, and a cached body it rejects is dropped and
        fetched again."""
        url = self.url(path)
        key = self.cache_key("GET", url, params, cache_extra) if (ttl and self.cache is not None) else None
        if key is not None:
            cached = await self.cache.aget(key)  # type: ignore[union-attr]
            if cached is not None:
                try:
                    value = await _run_parse(parse, cached, thread)
                except ProviderError as exc:
                    log.warning("%s: dropping an unreadable cache entry (%s)", self.provider, exc)
                    await self.cache.adelete(key)  # type: ignore[union-attr]
                else:
                    self.cache_hits += 1
                    return value
        resp = await self.request("GET", url, params=params, auth_params=auth_params, headers=headers,
                                  max_bytes=max_bytes)  # fmt: skip
        body = resp.content
        value = await _run_parse(parse, body, thread)
        if key is not None:
            await self.cache.aset(key, body, ttl)  # type: ignore[union-attr, arg-type]
        return value

    async def get_json(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        auth_params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        ttl: float | None = None,
        cache_extra: str = "",
        max_bytes: int | None = None,
    ) -> Any:
        where = f"{self.provider}: invalid JSON from {_path_only(self.url(path))}"

        def parse(body: bytes) -> Any:
            try:
                return json.loads(body)
            except ValueError as exc:
                raise ProviderError(where) from exc

        return await self.get_parsed(path, parse, params=params, auth_params=auth_params, headers=headers,
                                     ttl=ttl, cache_extra=cache_extra, max_bytes=max_bytes)  # fmt: skip

    async def post_json(self, path: str, body: Any, *, headers: Mapping[str, str] | None = None) -> Any:
        resp = await self.request("POST", path, json_body=body, headers=headers)
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError(f"{self.provider}: invalid JSON from {_path_only(self.url(path))}") from exc

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
