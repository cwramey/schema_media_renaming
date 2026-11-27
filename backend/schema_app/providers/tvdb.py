"""TheTVDB v4 provider (SPEC §6.2).

``POST /login {apikey[, pin]}`` returns a bearer token valid for a month; we keep it 25
days (memory + response cache, keyed by a hash of the credentials) and re-login once when
a request comes back 401. Episode lists use the season types ``default`` / ``dvd`` and
carry TVDB's native ``absoluteNumber``.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any, ClassVar

from schema_app.domain import (
    EpisodeInfo,
    EpisodeOrder,
    MediaKind,
    MovieInfo,
    ProviderRef,
    SeriesInfo,
)
from schema_app.providers.base import ProviderError, ProviderNotConfigured, ProviderNotFound
from schema_app.providers.cache import TTL_DETAILS, TTL_EPISODES, TTL_SEARCH, TTL_TOKEN
from schema_app.providers.common import (
    ISO2_TO_ISO3,
    HttpProvider,
    dedupe,
    fill_absolute,
    parse_date,
    split_title_year,
    tvdb_thumbnail,
    year_of,
)
from schema_app.providers.http import ProviderAuthError, TokenBucket

MAX_PAGES = 60
_REMOTE_SOURCES = {"imdb": "imdb", "themoviedb.com": "tmdb", "tv maze": "tvmaze", "tvmaze": "tvmaze"}


def _remote_ids(raw: list[dict[str, Any]] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for r in raw or []:
        source = _REMOTE_SOURCES.get(str(r.get("sourceName", "")).strip().lower())
        if source and r.get("id"):
            out.setdefault(source, str(r["id"]))
    return out


def _poster(thumbnail: Any, image: Any) -> str | None:
    """The small artwork for the UI's 36x54 px thumbnails: the ``_t`` variant of the full image
    (search results also carry ``thumbnail``, which may still name the retired ``_cache`` path).
    Only TVDB's artwork host passes."""
    return tvdb_thumbnail(image) or tvdb_thumbnail(thumbnail)


class TvdbProvider(HttpProvider):
    name: ClassVar[str] = "tvdb"
    label: ClassVar[str] = "TheTVDB"
    kinds: ClassVar[frozenset[MediaKind]] = frozenset({MediaKind.SERIES, MediaKind.ANIME, MediaKind.MOVIE})
    requires_key: ClassVar[bool] = True
    rate_limit_label: ClassVar[str | None] = "10 requests / s"
    BASE: ClassVar[str] = "https://api4.thetvdb.com/v4"

    def __init__(self, credentials, **kwargs: Any) -> None:  # type: ignore[no-untyped-def]
        super().__init__(credentials, **kwargs)
        self.http = self.make_client(self.BASE, limiter=TokenBucket(10.0, 10.0, sleep=self.sleep))
        self._token: str | None = None
        self._token_expires = 0.0
        self._login_lock: asyncio.Lock | None = None

    def is_configured(self) -> bool:
        return bool(self.credentials.tvdb_api_key)

    @property
    def lang3(self) -> str:
        return ISO2_TO_ISO3.get((self.language or "en").split("-")[0].lower(), "eng")

    # ------------------------------------------------------------------ auth

    def _token_cache_key(self) -> str:
        secret = f"{self.credentials.tvdb_api_key}|{self.credentials.tvdb_pin or ''}"
        return "tvdb|token|" + hashlib.sha256(secret.encode()).hexdigest()[:24]

    async def _bearer(self, *, force: bool = False) -> str:
        if not self.credentials.tvdb_api_key:
            raise ProviderNotConfigured("tvdb: API key missing")
        if not force and self._token and time.time() < self._token_expires:
            return self._token
        if self._login_lock is None:
            self._login_lock = asyncio.Lock()
        async with self._login_lock:
            if not force and self._token and time.time() < self._token_expires:
                return self._token
            key = self._token_cache_key()
            if not force and self.cache is not None:
                cached = await self.cache.aget(key)
                if cached:
                    self._token = cached.decode()
                    self._token_expires = time.time() + TTL_TOKEN
                    return self._token
            body: dict[str, str] = {"apikey": self.credentials.tvdb_api_key}
            if self.credentials.tvdb_pin:
                body["pin"] = self.credentials.tvdb_pin
            try:
                data = await self.http.post_json("login", body)
            except ProviderAuthError as exc:
                raise ProviderAuthError("tvdb: login rejected (check API key / subscriber PIN)") from exc
            token = (data.get("data") or {}).get("token")
            if not token:
                raise ProviderError("tvdb: login returned no token")
            self._token = token
            self._token_expires = time.time() + TTL_TOKEN
            if self.cache is not None:
                await self.cache.aset(key, token.encode(), TTL_TOKEN)
            return token

    async def _get(self, path: str, params: dict[str, Any] | None = None, *, ttl: float | None = TTL_SEARCH) -> Any:
        token = await self._bearer()
        try:
            return await self.http.get_json(path, params=params, headers={"Authorization": f"Bearer {token}"}, ttl=ttl)
        except ProviderAuthError:
            token = await self._bearer(force=True)  # expired / revoked token: log in again once
            return await self.http.get_json(path, params=params, headers={"Authorization": f"Bearer {token}"}, ttl=ttl)

    # ------------------------------------------------------------------ mapping

    def _name(self, record: dict[str, Any]) -> str:
        translations = record.get("translations")
        if isinstance(translations, dict) and translations.get(self.lang3):
            return str(translations[self.lang3])
        return str(record.get("name") or "")

    def _search_series(self, r: dict[str, Any]) -> SeriesInfo:
        title, suffix_year = split_title_year(self._name(r))
        ids = {"tvdb": str(r.get("tvdb_id") or r.get("id", "")).replace("series-", "")}
        ids.update(_remote_ids(r.get("remote_ids")))
        aliases = [a for a in (r.get("aliases") or []) if isinstance(a, str)]
        if r.get("name") and r.get("name") != title:
            aliases.append(split_title_year(r["name"])[0])
        return SeriesInfo(
            ref=ProviderRef(provider=self.name, id=ids["tvdb"]),
            title=title,
            year=year_of(r.get("year"), r.get("first_air_time")) or suffix_year,
            first_aired=parse_date(r.get("first_air_time")),
            aliases=dedupe(aliases),
            overview=(r.get("overviews") or {}).get(self.lang3) or r.get("overview"),
            poster_url=_poster(r.get("thumbnail"), r.get("image_url")),
            ids=ids,
            status=r.get("status"),
            network=r.get("network"),
            genres=list(r.get("genres") or []),
            is_anime=any(str(g).lower() == "anime" for g in r.get("genres") or []),
            original_language=r.get("primary_language"),
        )

    async def search_series(self, query: str, year: int | None = None, *, anime: bool = False) -> list[SeriesInfo]:
        params: dict[str, Any] = {"query": query, "type": "series"}
        if year:
            params["year"] = year
        data = await self._get("search", params)
        return [self._search_series(r) for r in data.get("data") or [] if r.get("tvdb_id") or r.get("id")]

    async def _extended(self, series_id: str) -> dict[str, Any]:
        data = await self._get(f"series/{series_id}/extended", {"meta": "translations", "short": "true"}, ttl=TTL_DETAILS)
        return data.get("data") or {}

    async def get_series(self, series_id: str) -> SeriesInfo:
        d = await self._extended(series_id)
        if not d:
            raise ProviderNotFound(f"tvdb: series {series_id} not found")
        name = d.get("name") or ""
        for t in (d.get("translations") or {}).get("nameTranslations") or []:
            if t.get("language") == self.lang3 and t.get("name"):
                name = t["name"]
                break
        title, suffix_year = split_title_year(name)
        aliases = [a.get("name") for a in d.get("aliases") or [] if isinstance(a, dict) and a.get("name")]
        if d.get("name") and split_title_year(d["name"])[0] != title:
            aliases.append(split_title_year(d["name"])[0])
        ids = {"tvdb": str(d.get("id", series_id))}
        ids.update(_remote_ids(d.get("remoteIds")))
        official = [
            s for s in d.get("seasons") or []
            if (s.get("type") or {}).get("type", "official") == "official" and (s.get("number") or 0) > 0
        ]
        genres = [g.get("name") for g in d.get("genres") or [] if g.get("name")]
        network = (d.get("originalNetwork") or {}).get("name") or (d.get("latestNetwork") or {}).get("name")
        return SeriesInfo(
            ref=ProviderRef(provider=self.name, id=ids["tvdb"]),
            title=title,
            original_title=split_title_year(d["name"])[0] if d.get("name") else None,
            year=year_of(d.get("year"), d.get("firstAired")) or suffix_year,
            first_aired=parse_date(d.get("firstAired")),
            aliases=dedupe(aliases),
            overview=d.get("overview"),
            poster_url=_poster(None, d.get("image")),
            ids=ids,
            popularity=d.get("score"),
            status=(d.get("status") or {}).get("name"),
            network=network,
            genres=genres,
            is_anime=any(g.lower() == "anime" for g in genres),
            season_count=len({s.get("number") for s in official}) or None,
            original_language=d.get("originalLanguage"),
        )

    async def get_episodes(self, series_id: str, order: EpisodeOrder = EpisodeOrder.AIRED) -> list[EpisodeInfo]:
        season_type = "dvd" if order == EpisodeOrder.DVD else "default"
        try:
            raw = await self._episode_pages(f"series/{series_id}/episodes/{season_type}/{self.lang3}")
        except ProviderNotFound:  # no translation for this language: untranslated list
            raw = await self._episode_pages(f"series/{series_id}/episodes/{season_type}")
        ref = ProviderRef(provider=self.name, id=str(series_id))
        episodes = [
            EpisodeInfo(
                series_ref=ref,
                season=e.get("seasonNumber"),
                episode=e.get("number"),
                absolute=e.get("absoluteNumber") or None,
                title=e.get("name") or None,
                air_date=parse_date(e.get("aired")),
                ids={"tvdb": str(e["id"])} if e.get("id") else {},
            )
            for e in raw
            if e.get("number") is not None
        ]
        return fill_absolute(episodes)

    async def _episode_pages(self, path: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(MAX_PAGES):
            data = await self._get(path, {"page": page}, ttl=TTL_EPISODES)
            body = data.get("data") or {}
            out.extend(body.get("episodes") or [])
            if not (data.get("links") or {}).get("next"):
                break
        return out

    async def search_movies(self, query: str, year: int | None = None) -> list[MovieInfo]:
        params: dict[str, Any] = {"query": query, "type": "movie"}
        if year:
            params["year"] = year
        data = await self._get("search", params)
        out = []
        for r in data.get("data") or []:
            tvdb_id = str(r.get("tvdb_id") or r.get("id", "")).replace("movie-", "")
            if not tvdb_id:
                continue
            title, suffix_year = split_title_year(self._name(r))
            ids = {"tvdb": tvdb_id, **_remote_ids(r.get("remote_ids"))}
            out.append(MovieInfo(
                ref=ProviderRef(provider=self.name, id=tvdb_id), title=title,
                year=year_of(r.get("year")) or suffix_year, aliases=dedupe(list(r.get("aliases") or [])),
                overview=r.get("overview"), poster_url=_poster(r.get("thumbnail"), r.get("image_url")), ids=ids,
            ))
        return out

    async def get_movie(self, movie_id: str) -> MovieInfo:
        data = await self._get(f"movies/{movie_id}/extended", {"meta": "translations", "short": "true"}, ttl=TTL_DETAILS)
        d = data.get("data") or {}
        if not d:
            raise ProviderNotFound(f"tvdb: movie {movie_id} not found")
        title, suffix_year = split_title_year(d.get("name") or "")
        ids = {"tvdb": str(d.get("id", movie_id)), **_remote_ids(d.get("remoteIds"))}
        return MovieInfo(
            ref=ProviderRef(provider=self.name, id=ids["tvdb"]), title=title,
            year=year_of(d.get("year")) or suffix_year,
            aliases=dedupe([a.get("name") for a in d.get("aliases") or [] if isinstance(a, dict)]),
            ids=ids, runtime_minutes=d.get("runtime"), poster_url=_poster(None, d.get("image")),
            genres=[g.get("name") for g in d.get("genres") or [] if g.get("name")],
        )

    async def healthcheck(self) -> None:
        await self._bearer(force=True)

    async def lookup_series_id(self, source: str, value: str) -> str | None:
        if source not in ("imdb", "tmdb") or not value:
            return None
        data = await self._get(f"search/remoteid/{value}", ttl=TTL_DETAILS)
        for hit in data.get("data") or []:
            series = hit.get("series") if isinstance(hit, dict) else None
            if series and series.get("id"):
                return str(series["id"])
        return None
