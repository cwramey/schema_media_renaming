"""Metadata provider contract.

Signatures here are fixed by docs/SPEC.md §6;
implementations live in sibling modules (tmdb.py, tvdb.py, anidb.py, trakt.py,
tvmaze.py, fixtures.py) and are assembled by registry.py.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from schema_app.domain import (
    EpisodeInfo,
    EpisodeOrder,
    MediaKind,
    MovieInfo,
    ProviderCredentials,
    SeriesInfo,
)


class ProviderError(Exception):
    """Base error. The matcher catches these and falls back to the next provider."""


class ProviderNotConfigured(ProviderError):
    """Missing API key / client registration."""


class ProviderRateLimited(ProviderError):
    """The provider (or our own limiter) refused the request; retry later."""


class ProviderNotFound(ProviderError):
    """The requested id does not exist at this provider."""


class ProviderUnavailable(ProviderError):
    """Network failure, timeout, or 5xx after retries."""


class MetadataProvider(ABC):
    """One metadata source. Instances are long-lived and shared; methods must be concurrency-safe."""

    name: ClassVar[str]  # "tmdb"
    label: ClassVar[str]  # "The Movie Database"
    kinds: ClassVar[frozenset[MediaKind]]  # kinds this provider can identify
    requires_key: ClassVar[bool] = True
    rate_limit_label: ClassVar[str | None] = None  # human readable, e.g. "1 request / 2 s"

    def __init__(self, credentials: ProviderCredentials, *, language: str = "en") -> None:
        self.credentials = credentials
        self.language = language

    @abstractmethod
    def is_configured(self) -> bool:
        """True when the credentials this provider needs are present."""

    # Movies -------------------------------------------------------------
    async def search_movies(self, query: str, year: int | None = None) -> list[MovieInfo]:
        raise ProviderError(f"{self.name} does not support movies")

    async def get_movie(self, movie_id: str) -> MovieInfo:
        raise ProviderError(f"{self.name} does not support movies")

    # Series / anime -------------------------------------------------------
    async def search_series(
        self, query: str, year: int | None = None, *, anime: bool = False
    ) -> list[SeriesInfo]:
        raise ProviderError(f"{self.name} does not support series")

    async def get_series(self, series_id: str) -> SeriesInfo:
        raise ProviderError(f"{self.name} does not support series")

    async def get_episodes(
        self, series_id: str, order: EpisodeOrder = EpisodeOrder.AIRED
    ) -> list[EpisodeInfo]:
        """All episodes including specials (season 0). Must fill ``absolute`` whenever the
        provider knows it or it can be derived (cumulative count over regular seasons)."""
        raise ProviderError(f"{self.name} does not support series")

    async def healthcheck(self) -> None:  # noqa: B027 - optional hook, no-op by default
        """Cheap authenticated call; raise ProviderError on failure."""
        return None

    async def aclose(self) -> None:  # noqa: B027 - optional hook, no-op by default
        """Release HTTP clients."""
        return None
