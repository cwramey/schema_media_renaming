"""Shared fixtures for the metadata tests: network guard, fake clock, fake providers."""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest
import respx

from schema_app.domain import (
    EpisodeInfo,
    EpisodeOrder,
    MediaKind,
    MovieInfo,
    ProviderCredentials,
    ProviderRef,
    ProviderSettings,
    SeriesInfo,
)
from schema_app.providers.base import MetadataProvider, ProviderError
from schema_app.providers.registry import ProviderRegistry

PAYLOADS = Path(__file__).parent / "payloads"


def payload(name: str) -> Any:
    path = PAYLOADS / name
    if path.suffix == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    return path.read_bytes()


class FakeClock:
    """Deterministic clock + sleep: sleeping advances the clock instead of waiting."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += max(0.0, seconds)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture(autouse=True)
def _block_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Belt and braces: no DNS lookups for non-local hosts in metadata tests.

    respx intercepts requests before name resolution, so mocked calls are unaffected; anything
    that slips past a mock fails here instead of reaching a real provider."""
    real = socket.getaddrinfo

    def guarded(host, *args, **kwargs):  # type: ignore[no-untyped-def]
        if host not in (None, "localhost", "127.0.0.1", "::1"):
            raise OSError(f"network access blocked in tests (host {host!r})")
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", guarded)


@pytest.fixture
def no_network() -> Iterator[respx.MockRouter]:
    """Any HTTP request fails the test (and is answered with a harmless 418 meanwhile)."""
    with respx.mock(assert_all_called=False) as router:
        guard = router.route().mock(return_value=httpx.Response(418))
        yield router
        assert guard.call_count == 0, f"unexpected network calls: {[str(c.request.url) for c in guard.calls]}"


# --------------------------------------------------------------------------- fake providers


def ep(series_id: str, season: int, episode: int, title: str, *, absolute: int | None = None,
       provider: str = "fake", aired: date | None = None) -> EpisodeInfo:
    return EpisodeInfo(series_ref=ProviderRef(provider=provider, id=series_id), season=season, episode=episode,
                       absolute=absolute, title=title, air_date=aired)


class FakeProvider(MetadataProvider):
    name: ClassVar[str] = "fake"
    label: ClassVar[str] = "Fake"
    kinds: ClassVar[frozenset[MediaKind]] = frozenset({MediaKind.MOVIE, MediaKind.SERIES, MediaKind.ANIME})
    requires_key: ClassVar[bool] = False

    def __init__(self, *, series: list[SeriesInfo] | None = None, movies: list[MovieInfo] | None = None,
                 episodes: dict[str, list[EpisodeInfo]] | None = None, fail: Exception | None = None,
                 configured: bool = True, lookup: dict[tuple[str, str], str] | None = None) -> None:
        super().__init__(ProviderCredentials())
        self.series = series or []
        self.movies = movies or []
        self.episodes = episodes or {}
        self.fail = fail
        self.configured = configured
        self.lookup = lookup or {}
        self.calls: list[tuple[str, Any]] = []

    def is_configured(self) -> bool:
        return self.configured

    def _maybe_fail(self) -> None:
        if self.fail is not None:
            raise self.fail

    async def search_movies(self, query: str, year: int | None = None) -> list[MovieInfo]:
        self.calls.append(("search_movies", query))
        self._maybe_fail()
        return [m for m in self.movies if query.lower().split()[0] in m.title.lower()]

    async def get_movie(self, movie_id: str) -> MovieInfo:
        self.calls.append(("get_movie", movie_id))
        self._maybe_fail()
        for m in self.movies:
            if m.ref.id == movie_id:
                return m
        raise ProviderError("missing")

    async def search_series(self, query: str, year: int | None = None, *, anime: bool = False) -> list[SeriesInfo]:
        self.calls.append(("search_series", query))
        self._maybe_fail()
        return [s for s in self.series if query.lower().split()[0] in s.title.lower()]

    async def get_series(self, series_id: str) -> SeriesInfo:
        self.calls.append(("get_series", series_id))
        self._maybe_fail()
        for s in self.series:
            if s.ref.id == series_id:
                return s
        raise ProviderError("missing")

    async def get_episodes(self, series_id: str, order: EpisodeOrder = EpisodeOrder.AIRED) -> list[EpisodeInfo]:
        self.calls.append(("get_episodes", series_id))
        self._maybe_fail()
        return list(self.episodes.get(series_id, []))

    async def lookup_series_id(self, source: str, value: str) -> str | None:
        self.calls.append(("lookup", (source, value)))
        return self.lookup.get((source, value))


def named(name: str, base: type[FakeProvider] = FakeProvider, **kwargs: Any) -> FakeProvider:
    cls = type(f"Fake_{name}", (base,), {"name": name, "label": name.title()})
    return cls(**kwargs)


def fake_registry(providers: list[FakeProvider], kind: MediaKind = MediaKind.SERIES) -> ProviderRegistry:
    names = [p.name for p in providers]
    settings = ProviderSettings(priority={MediaKind.MOVIE: names, MediaKind.SERIES: names, MediaKind.ANIME: names})
    reg = ProviderRegistry(settings)
    reg.providers = {p.name: p for p in providers}
    reg._health = {}
    return reg
