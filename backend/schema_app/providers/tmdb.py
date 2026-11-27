"""The Movie Database (TMDb) v3 provider: movies, TV series and anime (SPEC §6.2).

Auth: a v3 API key (``api_key`` query parameter) or a v4 read-access token (JWT starting
with ``eyJ``) sent as a Bearer header. Absolute numbers are cumulative over regular
seasons; for absolute / DVD order the episode groups (types 2 / 3) are used when present,
and absolute order falls back to air-date order over regular seasons.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, ClassVar

from schema_app.domain import (
    EpisodeInfo,
    EpisodeOrder,
    MediaKind,
    MovieInfo,
    ProviderRef,
    SeriesInfo,
)
from schema_app.providers.base import ProviderError, ProviderNotConfigured
from schema_app.providers.cache import TTL_DETAILS, TTL_EPISODES, TTL_SEARCH
from schema_app.providers.common import (
    HttpProvider,
    dedupe,
    fill_absolute,
    parse_date,
    safe_image_url,
    year_of,
)
from schema_app.providers.http import TokenBucket

IMAGE_BASE = "https://image.tmdb.org/t/p/w154"  # the UI shows 36x54 px thumbnails (w154 stays sharp at 3x)
ANIMATION_GENRE = 16
GROUP_ABSOLUTE = 2
GROUP_DVD = 3
_POSTER_PATH = re.compile(r"^/[A-Za-z0-9_-]+\.(?:jpg|jpeg|png|webp)$")


def _poster(path: str | None) -> str | None:
    if not isinstance(path, str) or not _POSTER_PATH.match(path):
        return None
    return safe_image_url(f"{IMAGE_BASE}{path}")


class TmdbProvider(HttpProvider):
    name: ClassVar[str] = "tmdb"
    label: ClassVar[str] = "The Movie Database"
    kinds: ClassVar[frozenset[MediaKind]] = frozenset({MediaKind.MOVIE, MediaKind.SERIES, MediaKind.ANIME})
    requires_key: ClassVar[bool] = True
    rate_limit_label: ClassVar[str | None] = "20 requests / s"
    BASE: ClassVar[str] = "https://api.themoviedb.org/3"

    def __init__(self, credentials, **kwargs: Any) -> None:  # type: ignore[no-untyped-def]
        super().__init__(credentials, **kwargs)
        self.http = self.make_client(self.BASE, limiter=TokenBucket(20.0, 20.0, sleep=self.sleep))

    def is_configured(self) -> bool:
        return bool(self.credentials.tmdb_api_key)

    def _auth(self) -> tuple[dict[str, str], dict[str, str]]:
        key = (self.credentials.tmdb_api_key or "").strip()
        if not key:
            raise ProviderNotConfigured("tmdb: API key missing")
        if key.startswith("eyJ"):
            return {}, {"Authorization": f"Bearer {key}"}
        return {"api_key": key}, {}

    async def _get(self, path: str, params: dict[str, Any] | None = None, *, ttl: float | None = TTL_SEARCH) -> Any:
        auth_params, headers = self._auth()
        merged = {"language": self.language, **(params or {})}
        return await self.http.get_json(path, params=merged, auth_params=auth_params, headers=headers, ttl=ttl)

    # ------------------------------------------------------------------ movies

    def _movie_from(self, r: dict[str, Any]) -> MovieInfo:
        ids = {"tmdb": str(r["id"])}
        imdb = r.get("imdb_id") or (r.get("external_ids") or {}).get("imdb_id")
        if imdb:
            ids["imdb"] = imdb
        aliases = [t.get("title") for t in (r.get("alternative_titles") or {}).get("titles", []) if t.get("title")]
        cert = None
        for country in (r.get("release_dates") or {}).get("results", []):
            if country.get("iso_3166_1") == "US":
                cert = next((d.get("certification") for d in country.get("release_dates", []) if d.get("certification")), None)
        collection = (r.get("belongs_to_collection") or {}).get("name")
        return MovieInfo(
            ref=ProviderRef(provider=self.name, id=str(r["id"])),
            title=r.get("title") or r.get("original_title") or "",
            original_title=r.get("original_title"),
            year=year_of(r.get("release_date")),
            release_date=parse_date(r.get("release_date")),
            aliases=dedupe(aliases),
            overview=r.get("overview") or None,
            poster_url=_poster(r.get("poster_path")),
            ids=ids,
            popularity=r.get("popularity"),
            runtime_minutes=r.get("runtime") or None,
            genres=[g["name"] for g in r.get("genres", []) if g.get("name")],
            collection=collection,
            certification=cert,
            rating=r.get("vote_average"),
            original_language=r.get("original_language"),
        )

    async def search_movies(self, query: str, year: int | None = None) -> list[MovieInfo]:
        params: dict[str, Any] = {"query": query, "include_adult": "false"}
        if year:
            params["year"] = year
        data = await self._get("search/movie", params)
        return [self._movie_from(r) for r in data.get("results", []) if r.get("id")]

    async def get_movie(self, movie_id: str) -> MovieInfo:
        data = await self._get(
            f"movie/{movie_id}",
            {"append_to_response": "external_ids,alternative_titles,release_dates"},
            ttl=TTL_DETAILS,
        )
        return self._movie_from(data)

    # ------------------------------------------------------------------ series

    def _series_from(self, r: dict[str, Any]) -> SeriesInfo:
        ext = r.get("external_ids") or {}
        ids = {"tmdb": str(r["id"])}
        if ext.get("tvdb_id"):
            ids["tvdb"] = str(ext["tvdb_id"])
        if ext.get("imdb_id"):
            ids["imdb"] = ext["imdb_id"]
        genre_ids = set(r.get("genre_ids") or []) | {g.get("id") for g in r.get("genres", [])}
        japanese = r.get("original_language") == "ja" or "JP" in (r.get("origin_country") or [])
        aliases = [t.get("title") for t in (r.get("alternative_titles") or {}).get("results", []) if t.get("title")]
        seasons = [s for s in r.get("seasons", []) if (s.get("season_number") or 0) > 0]
        networks = r.get("networks") or []
        return SeriesInfo(
            ref=ProviderRef(provider=self.name, id=str(r["id"])),
            title=r.get("name") or r.get("original_name") or "",
            original_title=r.get("original_name"),
            year=year_of(r.get("first_air_date")),
            first_aired=parse_date(r.get("first_air_date")),
            aliases=dedupe(aliases),
            overview=r.get("overview") or None,
            poster_url=_poster(r.get("poster_path")),
            ids=ids,
            popularity=r.get("popularity"),
            status=r.get("status"),
            network=networks[0].get("name") if networks else None,
            genres=[g["name"] for g in r.get("genres", []) if g.get("name")],
            is_anime=ANIMATION_GENRE in genre_ids and japanese,
            season_count=len(seasons) if seasons else r.get("number_of_seasons"),
            rating=r.get("vote_average"),
            original_language=r.get("original_language"),
        )

    async def search_series(self, query: str, year: int | None = None, *, anime: bool = False) -> list[SeriesInfo]:
        params: dict[str, Any] = {"query": query, "include_adult": "false"}
        if year:
            params["first_air_date_year"] = year
        data = await self._get("search/tv", params)
        return [self._series_from(r) for r in data.get("results", []) if r.get("id")]

    async def _tv(self, series_id: str) -> dict[str, Any]:
        return await self._get(
            f"tv/{series_id}", {"append_to_response": "external_ids,alternative_titles"}, ttl=TTL_DETAILS
        )

    async def get_series(self, series_id: str) -> SeriesInfo:
        return self._series_from(await self._tv(series_id))

    async def get_episodes(self, series_id: str, order: EpisodeOrder = EpisodeOrder.AIRED) -> list[EpisodeInfo]:
        tv = await self._tv(series_id)
        numbers = sorted({s.get("season_number") for s in tv.get("seasons", []) if s.get("season_number") is not None})
        ref = ProviderRef(provider=self.name, id=str(series_id))
        seasons = await asyncio.gather(
            *(self._get(f"tv/{series_id}/season/{n}", ttl=TTL_EPISODES) for n in numbers)
        )
        episodes: list[EpisodeInfo] = []
        for season in seasons:
            for e in season.get("episodes", []):
                if e.get("episode_number") is None:
                    continue
                episodes.append(EpisodeInfo(
                    series_ref=ref,
                    season=e.get("season_number"),
                    episode=e.get("episode_number"),
                    title=e.get("name") or None,
                    air_date=parse_date(e.get("air_date")),
                    ids={"tmdb": str(e["id"])} if e.get("id") else {},
                ))
        if order == EpisodeOrder.DVD:
            return await self._regroup(series_id, episodes, GROUP_DVD) or fill_absolute(episodes)
        if order == EpisodeOrder.ABSOLUTE:
            grouped = await self._absolute_from_group(series_id, episodes)
            if grouped is not None:
                return grouped
            return _absolute_by_airdate(episodes)
        return fill_absolute(episodes)

    async def _group_detail(self, series_id: str, group_type: int) -> dict[str, Any] | None:
        try:
            groups = await self._get(f"tv/{series_id}/episode_groups", ttl=TTL_DETAILS)
        except ProviderError:
            return None
        match = next((g for g in groups.get("results", []) if g.get("type") == group_type), None)
        if match is None:
            return None
        return await self._get(f"tv/episode_group/{match['id']}", ttl=TTL_DETAILS)

    async def _regroup(self, series_id: str, episodes: list[EpisodeInfo], group_type: int) -> list[EpisodeInfo] | None:
        detail = await self._group_detail(series_id, group_type)
        if not detail:
            return None
        by_id = {e.ids.get("tmdb"): e for e in episodes}
        out: list[EpisodeInfo] = []
        for group in sorted(detail.get("groups", []), key=lambda g: g.get("order", 0)):
            season_no = group.get("order", 0)
            for idx, ge in enumerate(sorted(group.get("episodes", []), key=lambda x: x.get("order", 0)), start=1):
                base = by_id.get(str(ge.get("id")))
                out.append(EpisodeInfo(
                    series_ref=episodes[0].series_ref if episodes else ProviderRef(provider=self.name, id=str(series_id)),
                    season=season_no,
                    episode=idx,
                    title=(base.title if base else None) or ge.get("name"),
                    air_date=(base.air_date if base else None) or parse_date(ge.get("air_date")),
                    ids={"tmdb": str(ge.get("id"))},
                ))
        return fill_absolute(out) if out else None

    async def _absolute_from_group(self, series_id: str, episodes: list[EpisodeInfo]) -> list[EpisodeInfo] | None:
        detail = await self._group_detail(series_id, GROUP_ABSOLUTE)
        if not detail:
            return None
        by_id = {e.ids.get("tmdb"): e for e in episodes}
        n = 0
        for group in sorted(detail.get("groups", []), key=lambda g: g.get("order", 0)):
            if group.get("order", 0) == 0 and len(detail.get("groups", [])) > 1:
                continue  # a "Specials" group does not count towards absolute numbering
            for ge in sorted(group.get("episodes", []), key=lambda x: x.get("order", 0)):
                base = by_id.get(str(ge.get("id")))
                n += 1
                if base is not None and base.season:
                    base.absolute = n
        return episodes if n else None

    async def healthcheck(self) -> None:
        auth_params, headers = self._auth()
        await self.http.request("GET", "configuration", auth_params=auth_params, headers=headers)

    async def lookup_series_id(self, source: str, value: str) -> str | None:
        external = {"tvdb": "tvdb_id", "imdb": "imdb_id", "tvmaze": None}.get(source)
        if not external or not value:
            return None
        data = await self._get(f"find/{value}", {"external_source": external}, ttl=TTL_DETAILS)
        results = data.get("tv_results") or []
        return str(results[0]["id"]) if results else None


def _absolute_by_airdate(episodes: list[EpisodeInfo]) -> list[EpisodeInfo]:
    """Absolute order inferred from air dates over regular seasons (FileBot's fallback)."""
    regular = [e for e in episodes if e.season and e.season > 0]
    far = parse_date("9999-12-31")
    regular.sort(key=lambda e: (e.air_date or far, e.season or 0, e.episode or 0))
    for n, e in enumerate(regular, start=1):
        e.absolute = n
    return episodes
