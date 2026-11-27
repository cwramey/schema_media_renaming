"""Trakt provider (movies + series; carries tmdb/tvdb/imdb ids) - SPEC §6.2."""

from __future__ import annotations

from typing import Any, ClassVar

from schema_app.domain import EpisodeInfo, EpisodeOrder, MediaKind, MovieInfo, ProviderRef, SeriesInfo
from schema_app.providers.base import ProviderNotConfigured
from schema_app.providers.cache import TTL_DETAILS, TTL_EPISODES, TTL_SEARCH
from schema_app.providers.common import HttpProvider, fill_absolute, parse_date, year_of
from schema_app.providers.http import TokenBucket


def _ids(raw: dict[str, Any] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in ("trakt", "tvdb", "tmdb", "imdb", "slug"):
        val = (raw or {}).get(key)
        if val:
            out[key] = str(val)
    return out


class TraktProvider(HttpProvider):
    name: ClassVar[str] = "trakt"
    label: ClassVar[str] = "Trakt"
    kinds: ClassVar[frozenset[MediaKind]] = frozenset({MediaKind.MOVIE, MediaKind.SERIES})
    requires_key: ClassVar[bool] = True
    rate_limit_label: ClassVar[str | None] = "3 requests / s"
    BASE: ClassVar[str] = "https://api.trakt.tv"

    def __init__(self, credentials, **kwargs: Any) -> None:  # type: ignore[no-untyped-def]
        super().__init__(credentials, **kwargs)
        self.http = self.make_client(self.BASE, limiter=TokenBucket(3.0, 3.0, sleep=self.sleep))

    def is_configured(self) -> bool:
        return bool(self.credentials.trakt_client_id)

    def _headers(self) -> dict[str, str]:
        key = self.credentials.trakt_client_id
        if not key:
            raise ProviderNotConfigured("trakt: client id missing")
        return {"trakt-api-key": key, "trakt-api-version": "2", "Content-Type": "application/json"}

    async def _get(self, path: str, params: dict[str, Any] | None = None, *, ttl: float = TTL_SEARCH) -> Any:
        return await self.http.get_json(path, params=params, headers=self._headers(), ttl=ttl)

    def _movie_from(self, m: dict[str, Any]) -> MovieInfo:
        ids = _ids(m.get("ids"))
        return MovieInfo(
            ref=ProviderRef(provider=self.name, id=ids.get("trakt") or ids.get("slug", "")),
            title=m.get("title") or "",
            year=year_of(m.get("year")),
            release_date=parse_date(m.get("released")),
            overview=m.get("overview"),
            ids={k: v for k, v in ids.items() if k != "slug"},
            runtime_minutes=m.get("runtime"),
            genres=list(m.get("genres") or []),
            certification=m.get("certification"),
            rating=m.get("rating"),
            original_language=m.get("language"),
            popularity=m.get("votes"),
        )

    def _series_from(self, s: dict[str, Any]) -> SeriesInfo:
        ids = _ids(s.get("ids"))
        genres = list(s.get("genres") or [])
        return SeriesInfo(
            ref=ProviderRef(provider=self.name, id=ids.get("trakt") or ids.get("slug", "")),
            title=s.get("title") or "",
            year=year_of(s.get("year"), s.get("first_aired")),
            first_aired=parse_date(s.get("first_aired")),
            overview=s.get("overview"),
            ids={k: v for k, v in ids.items() if k != "slug"},
            popularity=s.get("votes"),
            status=s.get("status"),
            network=s.get("network"),
            genres=genres,
            is_anime="anime" in [g.lower() for g in genres],
            certification=s.get("certification"),
            rating=s.get("rating"),
            original_language=s.get("language"),
        )

    async def search_movies(self, query: str, year: int | None = None) -> list[MovieInfo]:
        params: dict[str, Any] = {"query": query}
        if year:
            params["years"] = year
        data = await self._get("search/movie", params)
        return [self._movie_from(hit["movie"]) for hit in data or [] if hit.get("movie")]

    async def get_movie(self, movie_id: str) -> MovieInfo:
        return self._movie_from(await self._get(f"movies/{movie_id}", {"extended": "full"}, ttl=TTL_DETAILS))

    async def search_series(self, query: str, year: int | None = None, *, anime: bool = False) -> list[SeriesInfo]:
        params: dict[str, Any] = {"query": query}
        if year:
            params["years"] = year
        data = await self._get("search/show", params)
        return [self._series_from(hit["show"]) for hit in data or [] if hit.get("show")]

    async def get_series(self, series_id: str) -> SeriesInfo:
        return self._series_from(await self._get(f"shows/{series_id}", {"extended": "full"}, ttl=TTL_DETAILS))

    async def get_episodes(self, series_id: str, order: EpisodeOrder = EpisodeOrder.AIRED) -> list[EpisodeInfo]:
        seasons = await self._get(f"shows/{series_id}/seasons", {"extended": "episodes,full"}, ttl=TTL_EPISODES)
        ref = ProviderRef(provider=self.name, id=str(series_id))
        out: list[EpisodeInfo] = []
        for season in seasons or []:
            for e in season.get("episodes") or []:
                ids = _ids(e.get("ids"))
                out.append(EpisodeInfo(
                    series_ref=ref,
                    season=e.get("season", season.get("number")),
                    episode=e.get("number"),
                    absolute=e.get("number_abs") or None,
                    title=e.get("title"),
                    air_date=parse_date(e.get("first_aired")),
                    ids={k: v for k, v in ids.items() if k != "slug"},
                ))
        return fill_absolute(out)

    async def healthcheck(self) -> None:
        await self.http.request("GET", "genres/movies", headers=self._headers())

    async def lookup_series_id(self, source: str, value: str) -> str | None:
        if source not in ("tvdb", "tmdb", "imdb") or not value:
            return None
        data = await self._get(f"search/{source}/{value}", {"type": "show"}, ttl=TTL_DETAILS)
        for hit in data or []:
            trakt_id = ((hit.get("show") or {}).get("ids") or {}).get("trakt")
            if trakt_id:
                return str(trakt_id)
        return None
