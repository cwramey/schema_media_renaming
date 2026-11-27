"""TVmaze provider: keyless series fallback (SPEC §6.2).

TVmaze allows 20 calls per 10 seconds per IP; we enforce exactly that with a sliding
window. Specials come back with ``number: null`` inside the season they aired in; we
move them to season 0 and number them in air-date order.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from schema_app.domain import EpisodeInfo, EpisodeOrder, MediaKind, ProviderRef, SeriesInfo
from schema_app.providers.base import ProviderNotFound
from schema_app.providers.cache import TTL_DETAILS, TTL_EPISODES, TTL_SEARCH
from schema_app.providers.common import (
    HttpProvider,
    dedupe,
    fill_absolute,
    parse_date,
    safe_image_url,
    year_of,
)
from schema_app.providers.http import WindowLimiter

_LANGS = {
    "english": "en", "japanese": "ja", "german": "de", "french": "fr", "spanish": "es", "korean": "ko",
    "italian": "it", "dutch": "nl", "swedish": "sv", "danish": "da", "norwegian": "no", "chinese": "zh",
    "portuguese": "pt", "russian": "ru", "polish": "pl", "finnish": "fi", "turkish": "tr", "hindi": "hi",
}


def _strip_html(text: str | None) -> str | None:
    if not text:
        return None
    return re.sub(r"<[^>]+>", "", text).strip() or None


class TvmazeProvider(HttpProvider):
    name: ClassVar[str] = "tvmaze"
    label: ClassVar[str] = "TVmaze"
    kinds: ClassVar[frozenset[MediaKind]] = frozenset({MediaKind.SERIES, MediaKind.ANIME})
    requires_key: ClassVar[bool] = False
    rate_limit_label: ClassVar[str | None] = "20 requests / 10 s"
    BASE: ClassVar[str] = "https://api.tvmaze.com"

    def __init__(self, credentials, **kwargs: Any) -> None:  # type: ignore[no-untyped-def]
        super().__init__(credentials, **kwargs)
        self.http = self.make_client(
            self.BASE, limiter=WindowLimiter([(20, 10.0)], max_wait=15.0, sleep=self.sleep)
        )

    def is_configured(self) -> bool:
        return True

    def _series_from(self, show: dict[str, Any], akas: list[str] | None = None) -> SeriesInfo:
        ext = show.get("externals") or {}
        ids = {"tvmaze": str(show["id"])}
        if ext.get("thetvdb"):
            ids["tvdb"] = str(ext["thetvdb"])
        if ext.get("imdb"):
            ids["imdb"] = str(ext["imdb"])
        genres = list(show.get("genres") or [])
        language = str(show.get("language") or "").lower()
        network = (show.get("network") or {}).get("name") or (show.get("webChannel") or {}).get("name")
        is_anime = "Anime" in genres or (show.get("type") == "Animation" and language == "japanese")
        return SeriesInfo(
            ref=ProviderRef(provider=self.name, id=str(show["id"])),
            title=show.get("name") or "",
            year=year_of(show.get("premiered")),
            first_aired=parse_date(show.get("premiered")),
            aliases=dedupe(akas or []),
            overview=_strip_html(show.get("summary")),
            poster_url=safe_image_url((show.get("image") or {}).get("medium")),
            ids=ids,
            popularity=show.get("weight"),
            status=show.get("status"),
            network=network,
            genres=genres,
            is_anime=is_anime,
            rating=(show.get("rating") or {}).get("average"),
            original_language=_LANGS.get(language),
        )

    async def search_series(self, query: str, year: int | None = None, *, anime: bool = False) -> list[SeriesInfo]:
        data = await self.http.get_json("search/shows", params={"q": query}, ttl=TTL_SEARCH)
        out = [self._series_from(hit["show"]) for hit in data or [] if (hit or {}).get("show", {}).get("id")]
        if year is not None:  # TVmaze has no year parameter; filter like the other providers do
            out = [s for s in out if s.year is None or abs(s.year - year) <= 1]
        return out

    async def get_series(self, series_id: str) -> SeriesInfo:
        show = await self.http.get_json(f"shows/{series_id}", ttl=TTL_DETAILS)
        try:
            akas = await self.http.get_json(f"shows/{series_id}/akas", ttl=TTL_DETAILS)
        except ProviderNotFound:
            akas = []
        names = [a.get("name") for a in akas or [] if a.get("name")]
        return self._series_from(show, names)

    async def get_episodes(self, series_id: str, order: EpisodeOrder = EpisodeOrder.AIRED) -> list[EpisodeInfo]:
        raw = await self.http.get_json(f"shows/{series_id}/episodes", params={"specials": 1}, ttl=TTL_EPISODES)
        ref = ProviderRef(provider=self.name, id=str(series_id))
        regular: list[EpisodeInfo] = []
        specials: list[tuple[str, EpisodeInfo]] = []
        for e in raw or []:
            is_special = e.get("number") is None or "special" in str(e.get("type") or "")
            info = EpisodeInfo(
                series_ref=ref,
                season=0 if is_special else e.get("season"),
                episode=None if is_special else e.get("number"),
                title=e.get("name") or None,
                air_date=parse_date(e.get("airdate")),
                ids={"tvmaze": str(e["id"])} if e.get("id") else {},
            )
            if is_special:
                specials.append((e.get("airdate") or "9999", info))
            else:
                regular.append(info)
        specials.sort(key=lambda item: item[0])
        for n, (_d, info) in enumerate(specials, start=1):
            info.episode = n
        return fill_absolute(regular + [info for _d, info in specials])

    async def healthcheck(self) -> None:
        await self.http.request("GET", "shows/1")

    async def lookup_series_id(self, source: str, value: str) -> str | None:
        param = {"tvdb": "thetvdb", "imdb": "imdb"}.get(source)
        if not param or not value:
            return None
        try:
            show = await self.http.get_json("lookup/shows", params={param: value}, ttl=TTL_DETAILS)
        except ProviderNotFound:
            return None
        return str(show["id"]) if isinstance(show, dict) and show.get("id") else None
