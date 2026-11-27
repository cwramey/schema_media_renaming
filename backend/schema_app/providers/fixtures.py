"""Offline ``fixtures`` provider: the fictional demo catalogue (SPEC §6.5).

Behaves like a small search engine over ``fixtures_data.json`` so the matcher runs the
exact same code path as with real providers: fuzzy title search (titles + aliases),
optional year filter (±1, like a provider's year parameter), details and full episode
lists with absolute numbers. No network, no credentials.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from functools import cached_property
from pathlib import Path
from typing import Any, ClassVar

from schema_app.core.scoring import best_title_similarity
from schema_app.domain import (
    EpisodeInfo,
    EpisodeOrder,
    MediaKind,
    MovieInfo,
    ProviderCredentials,
    ProviderRef,
    SeriesInfo,
)
from schema_app.providers.base import MetadataProvider, ProviderNotFound

DATA_FILE = Path(__file__).with_name("fixtures_data.json")
SEARCH_THRESHOLD = 0.6  # emulate a provider search: only reasonably similar names come back


def _date(raw: str | None) -> date | None:
    return date.fromisoformat(raw) if raw else None


class FixturesProvider(MetadataProvider):
    name: ClassVar[str] = "fixtures"
    label: ClassVar[str] = "Demo fixtures (offline)"
    kinds: ClassVar[frozenset[MediaKind]] = frozenset({MediaKind.MOVIE, MediaKind.SERIES, MediaKind.ANIME})
    requires_key: ClassVar[bool] = False
    rate_limit_label: ClassVar[str | None] = None

    def __init__(
        self,
        credentials: ProviderCredentials | None = None,
        *,
        language: str = "en",
        data: dict[str, Any] | None = None,
        **_: Any,
    ) -> None:
        super().__init__(credentials or ProviderCredentials(), language=language)
        self._data = data
        self.requests_made = 0

    def is_configured(self) -> bool:
        return True

    # ------------------------------------------------------------------ catalogue

    @cached_property
    def catalogue(self) -> dict[str, Any]:
        if self._data is not None:
            return self._data
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))

    @cached_property
    def _series(self) -> dict[str, dict[str, Any]]:
        return {s["id"]: s for s in self.catalogue.get("series", [])}

    @cached_property
    def _movies(self) -> dict[str, dict[str, Any]]:
        return {m["id"]: m for m in self.catalogue.get("movies", [])}

    def _ref(self, raw_id: str) -> ProviderRef:
        return ProviderRef(provider=self.name, id=str(raw_id))

    def _movie_info(self, m: dict[str, Any]) -> MovieInfo:
        return MovieInfo(
            ref=self._ref(m["id"]),
            title=m["title"],
            original_title=m.get("original_title"),
            year=m.get("year"),
            release_date=_date(m.get("release_date")),
            aliases=list(m.get("aliases", [])),
            overview=m.get("overview"),
            ids={**m.get("ids", {}), self.name: m["id"]},
            popularity=m.get("popularity"),
            runtime_minutes=m.get("runtime_minutes"),
            genres=list(m.get("genres", [])),
            collection=m.get("collection"),
            certification=m.get("certification"),
            rating=m.get("rating"),
            original_language=m.get("original_language"),
        )

    def _series_info(self, s: dict[str, Any]) -> SeriesInfo:
        regular = {e.season for e in self._episodes_for(s) if e.season}
        return SeriesInfo(
            ref=self._ref(s["id"]),
            title=s["title"],
            original_title=s.get("original_title"),
            year=s.get("year"),
            first_aired=_date(s.get("first_aired")),
            aliases=list(s.get("aliases", [])),
            overview=s.get("overview"),
            ids={**s.get("ids", {}), self.name: s["id"]},
            popularity=s.get("popularity"),
            status=s.get("status"),
            network=s.get("network"),
            genres=list(s.get("genres", [])),
            is_anime=bool(s.get("is_anime")),
            season_count=len(regular),
            certification=s.get("certification"),
            rating=s.get("rating"),
            original_language=s.get("original_language"),
        )

    def _episodes_for(self, s: dict[str, Any]) -> list[EpisodeInfo]:
        ref = self._ref(s["id"])
        out: list[EpisodeInfo] = []
        absolute = 0
        if "episodes" in s:  # explicit list (daily shows)
            for e in s["episodes"]:
                if e["season"]:
                    absolute += 1
                out.append(EpisodeInfo(
                    series_ref=ref, season=e["season"], episode=e["episode"],
                    absolute=absolute if e["season"] else None, title=e["title"],
                    air_date=_date(e.get("air_date")),
                    ids={self.name: f"{s['id']}-{e['season']}-{e['episode']}"},
                ))
        for season in sorted(s.get("seasons", []), key=lambda x: x["number"]):
            premiere = _date(season.get("premiere"))
            for i, title in enumerate(season["titles"], start=1):
                absolute += 1
                out.append(EpisodeInfo(
                    series_ref=ref, season=season["number"], episode=i, absolute=absolute, title=title,
                    air_date=premiere + timedelta(days=7 * (i - 1)) if premiere else None,
                    ids={self.name: f"{s['id']}-{season['number']}-{i}"},
                ))
        for i, sp in enumerate(s.get("specials", []), start=1):
            out.append(EpisodeInfo(
                series_ref=ref, season=0, episode=i, title=sp["title"], air_date=_date(sp.get("air_date")),
                ids={self.name: f"{s['id']}-0-{i}"},
            ))
        return out

    @staticmethod
    def _names(item: dict[str, Any]) -> list[str]:
        return [item["title"], *(item.get("aliases") or []), *([item["original_title"]] if item.get("original_title") else [])]

    def _search(self, items: list[dict[str, Any]], query: str, year: int | None) -> list[dict[str, Any]]:
        hits: list[tuple[float, float, dict[str, Any]]] = []
        for item in items:
            if year is not None and item.get("year") is not None and abs(item["year"] - year) > 1:
                continue
            sim = best_title_similarity([query], self._names(item))
            if sim >= SEARCH_THRESHOLD:
                hits.append((sim, item.get("popularity") or 0.0, item))
        hits.sort(key=lambda h: (h[0], h[1]), reverse=True)
        return [h[2] for h in hits]

    # ------------------------------------------------------------------ provider API

    async def search_movies(self, query: str, year: int | None = None) -> list[MovieInfo]:
        self.requests_made += 1
        return [self._movie_info(m) for m in self._search(list(self._movies.values()), query, year)]

    async def get_movie(self, movie_id: str) -> MovieInfo:
        self.requests_made += 1
        m = self._movies.get(str(movie_id))
        if m is None:
            raise ProviderNotFound(f"fixtures: movie {movie_id} not found")
        return self._movie_info(m)

    async def search_series(self, query: str, year: int | None = None, *, anime: bool = False) -> list[SeriesInfo]:
        self.requests_made += 1
        return [self._series_info(s) for s in self._search(list(self._series.values()), query, year)]

    async def get_series(self, series_id: str) -> SeriesInfo:
        self.requests_made += 1
        s = self._series.get(str(series_id))
        if s is None:
            raise ProviderNotFound(f"fixtures: series {series_id} not found")
        return self._series_info(s)

    async def get_episodes(self, series_id: str, order: EpisodeOrder = EpisodeOrder.AIRED) -> list[EpisodeInfo]:
        self.requests_made += 1
        s = self._series.get(str(series_id))
        if s is None:
            raise ProviderNotFound(f"fixtures: series {series_id} not found")
        return self._episodes_for(s)

    async def lookup_series_id(self, source: str, value: str) -> str | None:
        """Find our id for a series known elsewhere (episode-level fallback via shared ids)."""
        for s in self._series.values():
            if str(s.get("ids", {}).get(source)) == str(value):
                return s["id"]
        return None
