"""Anime-Lists mapping helper (AniDB <-> TVDB / TMDb season + episode mapping, SPEC §6.4).

Source: the public ``anime-list-master.xml`` (downloaded at most once a day, cached).
Each ``<anime>`` element maps one AniDB id onto a TVDB series (``tvdbid``,
``defaulttvdbseason`` - a number or ``a`` for TVDB absolute order - and
``episodeoffset``), optionally with an explicit ``<mapping-list>``. FileBot's extended
variant also carries TMDb TV fields (``tmdbtv``, ``tmdbseason``, ``tmdboffset``); when
present they are used for AniDB -> TMDb conversion.
"""

from __future__ import annotations

import asyncio
import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Literal

import httpx

from schema_app.providers.base import ProviderError
from schema_app.providers.cache import TTL_ANIME_LISTS, ResponseCache
from schema_app.providers.http import MIB, HttpClient, Sleep, TokenBucket

log = logging.getLogger("schema.providers.animelists")

ANIME_LISTS_URL = "https://raw.githubusercontent.com/Anime-Lists/anime-lists/master/anime-list-master.xml"
RELOAD_AFTER = TTL_ANIME_LISTS
FAILURE_BACKOFF = 3600.0  # don't retry a failed download more than once an hour

Target = Literal["tvdb", "tmdb"]


@dataclass
class EpisodeMap:
    anidb_season: int  # 1 = regular episodes, 0 = specials
    season: int  # target season
    start: int | None = None
    end: int | None = None
    offset: int = 0
    pairs: dict[int, int] = field(default_factory=dict)  # explicit anidb ep -> target ep (0 = unmapped)


@dataclass
class AnimeMapping:
    aid: str
    name: str | None = None
    tvdb_id: str | None = None
    default_season: int | Literal["a"] | None = None
    episode_offset: int = 0
    tmdb_tv: str | None = None
    tmdb_season: int | Literal["a"] | None = None
    tmdb_offset: int = 0
    tmdb_movie: str | None = None
    imdb_id: str | None = None
    tvdb_maps: list[EpisodeMap] = field(default_factory=list)
    tmdb_maps: list[EpisodeMap] = field(default_factory=list)

    def ids(self) -> dict[str, str]:
        out = {"anidb": self.aid}
        if self.tvdb_id:
            out["tvdb"] = self.tvdb_id
        if self.tmdb_tv:
            out["tmdb"] = self.tmdb_tv
        if self.imdb_id:
            out["imdb"] = self.imdb_id
        return out


def _int(raw: str | None, default: int | None = None) -> int | None:
    try:
        return int(str(raw).strip()) if raw not in (None, "") else default
    except ValueError:
        return default


def _season(raw: str | None) -> int | Literal["a"] | None:
    if raw is None or raw == "":
        return None
    if raw.strip().lower() == "a":
        return "a"
    return _int(raw)


def _pairs(text: str | None) -> dict[int, int]:
    out: dict[int, int] = {}
    for chunk in (text or "").split(";"):
        if "-" not in chunk:
            continue
        a, _, b = chunk.partition("-")
        for target in b.split("+"):  # "1-2+3" maps one AniDB episode onto two target episodes
            ai, bi = _int(a), _int(target)
            if ai is not None and bi is not None:
                out.setdefault(ai, bi)
    return out


def parse_anime_lists(xml_bytes: bytes) -> dict[str, AnimeMapping]:
    root = ET.fromstring(xml_bytes)
    out: dict[str, AnimeMapping] = {}
    for el in root.iter("anime"):
        aid = (el.get("anidbid") or "").strip()
        if not aid:
            continue
        tvdb = (el.get("tvdbid") or "").strip()
        tmdb_tv = (el.get("tmdbtv") or "").strip()
        m = AnimeMapping(
            aid=aid,
            name=(el.findtext("name") or None),
            tvdb_id=tvdb if tvdb.isdigit() else None,
            default_season=_season(el.get("defaulttvdbseason")),
            episode_offset=_int(el.get("episodeoffset"), 0) or 0,
            tmdb_tv=tmdb_tv if tmdb_tv.isdigit() else None,
            tmdb_season=_season(el.get("tmdbseason")),
            tmdb_offset=_int(el.get("tmdboffset"), 0) or 0,
            tmdb_movie=(el.get("tmdbid") or "").strip() or None,
            imdb_id=(el.get("imdbid") or "").strip() or None,
        )
        if m.imdb_id and not m.imdb_id.startswith("tt"):
            m.imdb_id = None
        for mp in el.iter("mapping"):
            anidb_season = _int(mp.get("anidbseason"), 1)
            base = {
                "anidb_season": anidb_season if anidb_season is not None else 1,
                "start": _int(mp.get("start")),
                "end": _int(mp.get("end")),
                "offset": _int(mp.get("offset"), 0) or 0,
                "pairs": _pairs(mp.text),
            }
            tvdb_season = _int(mp.get("tvdbseason"))
            if tvdb_season is not None:
                m.tvdb_maps.append(EpisodeMap(season=tvdb_season, **base))  # type: ignore[arg-type]
            tmdb_season = _int(mp.get("tmdbseason"))
            if tmdb_season is not None:
                tmdb_base = dict(base, offset=_int(mp.get("tmdboffset"), base["offset"]) or 0)
                m.tmdb_maps.append(EpisodeMap(season=tmdb_season, **tmdb_base))  # type: ignore[arg-type]
        out[aid] = m
    return out


class AnimeLists:
    def __init__(
        self,
        *,
        cache: ResponseCache | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep | None = None,
        url: str = ANIME_LISTS_URL,
    ) -> None:
        self.url = url
        self.http = HttpClient(
            "animelists", cache=cache, transport=transport, sleep=sleep,
            limiter=TokenBucket(1 / 60, 1, sleep=sleep), max_retries=1,
            max_bytes=32 * MIB,  # anime-list-master.xml is ~3.5 MB
        )
        self._by_aid: dict[str, AnimeMapping] | None = None
        self._by_tvdb: dict[str, list[AnimeMapping]] = {}
        self._loaded_at = 0.0
        self._failed_at = 0.0
        self._lock: asyncio.Lock | None = None
        self.last_error: str | None = None

    def load_from_bytes(self, xml_bytes: bytes) -> None:
        self._index(parse_anime_lists(xml_bytes))

    def _index(self, by_aid: dict[str, AnimeMapping]) -> None:
        self._by_aid = by_aid
        by_tvdb: dict[str, list[AnimeMapping]] = {}
        for m in by_aid.values():
            if m.tvdb_id:
                by_tvdb.setdefault(m.tvdb_id, []).append(m)
        self._by_tvdb = by_tvdb
        self._loaded_at = time.time()

    async def ensure_loaded(self) -> bool:
        """Load (or refresh daily) the mapping. Returns False when unavailable; never raises."""
        fresh = self._by_aid is not None and time.time() - self._loaded_at < RELOAD_AFTER
        if fresh:
            return True
        if self._failed_at and time.time() - self._failed_at < FAILURE_BACKOFF:
            return self._by_aid is not None
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._by_aid is not None and time.time() - self._loaded_at < RELOAD_AFTER:
                return True
            try:
                body = await self.http.get_bytes(self.url, ttl=TTL_ANIME_LISTS, validate=_validate_xml)
                parsed = await asyncio.to_thread(parse_anime_lists, body)
            except (ProviderError, ET.ParseError) as exc:
                self._failed_at = time.time()
                self.last_error = str(exc)
                log.warning("Anime-Lists mapping unavailable: %s", exc)
                return self._by_aid is not None
            self._index(parsed)
            self.last_error = None
            return True

    def entry(self, aid: str | int) -> AnimeMapping | None:
        return (self._by_aid or {}).get(str(aid))

    def by_tvdb(self, tvdb_id: str | int) -> list[AnimeMapping]:
        return list(self._by_tvdb.get(str(tvdb_id), []))

    def siblings(self, aid: str | int) -> list[AnimeMapping]:
        """Entries sharing this entry's TVDB id that hold regular seasons, in season order."""
        me = self.entry(aid)
        if me is None or not me.tvdb_id:
            return [me] if me else []
        sibs = [m for m in self._by_tvdb.get(me.tvdb_id, []) if isinstance(m.default_season, int) and m.default_season >= 1]
        sibs.sort(key=lambda m: (m.default_season if isinstance(m.default_season, int) else 0, m.episode_offset, int(m.aid)))
        return sibs

    def map_episode(self, aid: str | int, anidb_season: int, episode: int, target: Target = "tvdb") -> tuple[int | None, int] | None:
        """AniDB (season 1 regular / 0 special, episode) -> (target season, episode).

        Returns ``(None, n)`` when the target uses absolute numbering (``defaulttvdbseason="a"``)
        and None when no mapping exists for this target."""
        m = self.entry(aid)
        if m is None:
            return None
        maps = m.tvdb_maps if target == "tvdb" else m.tmdb_maps
        default = m.default_season if target == "tvdb" else m.tmdb_season
        offset = m.episode_offset if target == "tvdb" else m.tmdb_offset
        has_series = m.tvdb_id if target == "tvdb" else m.tmdb_tv
        if not has_series:
            return None
        for mp in maps:
            if mp.anidb_season != anidb_season:
                continue
            if episode in mp.pairs:
                target_ep = mp.pairs[episode]
                return (mp.season, target_ep) if target_ep > 0 else None
            if mp.start is not None and mp.end is not None and mp.start <= episode <= mp.end:
                return mp.season, episode + mp.offset
        if anidb_season == 0:
            return 0, episode
        if default == "a":
            return None, episode + offset
        if isinstance(default, int):
            return default, episode + offset
        return None

    async def aclose(self) -> None:
        await self.http.aclose()


def _validate_xml(body: bytes) -> None:
    head = body[:512].lstrip()
    if not head.startswith(b"<"):
        raise ProviderError("animelists: response is not XML")
