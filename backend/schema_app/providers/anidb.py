"""AniDB HTTP API provider (anime) - SPEC §6.2 / §6.4.

AniDB bans clients that hammer it, so this provider is deliberately conservative:

* title search never touches the API: it runs against the daily ``anime-titles.xml.gz``
  dump (downloaded at most once per 24 h, cached) with a local fuzzy index;
* API calls (``request=anime&aid=``) go through a sliding-window limiter - at most
  5 requests per 60 s and 200 per 24 h, at least 2.5 s apart - whose history is
  persisted in the cache DB so restarts don't reset it; responses are cached 7 days;
* a "banned" answer (``<error>Banned</error>``, error code 500/555) or a daily-limit error
  latches a 24 h pause, also persisted, surfaced through ``status_error``.

Episodes come back in AniDB's per-entry numbering (one AniDB id per season/cour) and are
mapped onto TMDb or TVDB seasons through Anime-Lists when a mapping exists.
``franchise_episodes`` walks the Anime-Lists siblings that share a TVDB id to resolve
absolute numbers beyond one entry.

AniDB's HTTP API only speaks plain HTTP (port 9001 has no TLS; the https host's certificate does
not cover ``api.anidb.net``), so its answers are treated as untrusted: size-capped before and
after gunzip, parsed in a worker thread, and an answer about another anime than the one asked for
is refused rather than cached. XML parsing, the title-index build and fuzzy search all run off the
event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar

from rapidfuzz import fuzz, process

from schema_app.core.normalize import normalize_title
from schema_app.core.scoring import title_similarity
from schema_app.domain import EpisodeInfo, EpisodeOrder, MediaKind, ProviderRef, SeriesInfo
from schema_app.providers.animelists import AnimeLists
from schema_app.providers.base import (
    ProviderError,
    ProviderNotConfigured,
    ProviderNotFound,
    ProviderRateLimited,
)
from schema_app.providers.cache import DAY, TTL_ANIDB, TTL_TITLES_DUMP
from schema_app.providers.common import HttpProvider, dedupe, parse_date
from schema_app.providers.http import MIB, TokenBucket, WindowLimiter, bounded_gunzip

log = logging.getLogger("schema.providers.anidb")

API_URL = "http://api.anidb.net:9001/httpapi"  # no HTTPS endpoint exists for the HTTP API
TITLES_URL = "https://anidb.net/api/anime-titles.xml.gz"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
BAN_KEY = "anidb|ban"
LIMITER_KEY = "anidb|limiter"
BAN_SECONDS = DAY
MAX_SIBLINGS = 8
SEARCH_LIMIT = 10
ANIME_MAX_BYTES = 8 * MIB  # one anime's XML is typically 10-200 KB
TITLES_MAX_BYTES = 32 * MIB  # the gzipped dump is a few MB
TITLES_XML_MAX_BYTES = 64 * MIB  # ... and its XML ~10 MB


class AnidbBanned(ProviderRateLimited):
    """AniDB answered 'banned' (or a daily limit): all AniDB calls pause for 24 h."""


@dataclass
class _Episode:
    eid: str
    kind: int  # 1 regular, 2 special, 3 credit, 4 trailer, 5 parody, 6 other
    number: int
    airdate: str | None
    titles: dict[str, str] = field(default_factory=dict)


@dataclass
class _Anime:
    aid: str
    type: str | None
    episode_count: int | None
    start: str | None
    titles: list[tuple[str, str, str]]  # (lang, type, text)
    rating: float | None
    votes: int | None
    episodes: list[_Episode]


def _maybe_gunzip(body: bytes, max_bytes: int = ANIME_MAX_BYTES) -> bytes:
    """Gunzip a gzip body (AniDB sometimes sends gzip without Content-Encoding), never past
    ``max_bytes``; other bodies are returned as they are."""
    return bounded_gunzip(body, max_bytes, what="anidb") if body[:2] == b"\x1f\x8b" else body


def _xml(body: bytes, max_bytes: int = ANIME_MAX_BYTES) -> ET.Element:
    try:
        return ET.fromstring(_maybe_gunzip(body, max_bytes))
    except ET.ParseError as exc:
        raise ProviderError("anidb: malformed XML") from exc


def _check_error(root: ET.Element) -> None:
    if root.tag != "error":
        return
    code = (root.get("code") or "").strip()
    text = (root.text or "").strip()
    low = text.lower()
    if "banned" in low or code in {"500", "555"}:
        raise AnidbBanned(f"anidb: banned ({text or code})")
    if "limit" in low:
        raise AnidbBanned(f"anidb: daily limit reached ({text})")
    if "client" in low or code in {"302", "303"}:
        raise ProviderNotConfigured(f"anidb: client rejected ({text or code}) - check client name/version")
    if "no such anime" in low or "not found" in low:
        raise ProviderNotFound(f"anidb: {text}")
    raise ProviderError(f"anidb: {text or 'error ' + code}")


def parse_anime_xml(body: bytes, expected_aid: str | None = None) -> _Anime:
    """Parse one ``request=anime`` answer; AniDB error answers raise (see ``_check_error``).
    ``expected_aid``: refuse an answer about another anime (a tampered or mixed-up response)."""
    root = _xml(body)
    _check_error(root)
    if root.tag != "anime":
        raise ProviderError("anidb: unexpected response")
    if expected_aid is not None and (root.get("id") or "") != str(expected_aid):
        raise ProviderError(f"anidb: asked for anime {expected_aid}, got {root.get('id') or 'no id'}")
    titles = [
        (t.get(XML_LANG) or "", t.get("type") or "", (t.text or "").strip())
        for t in root.findall("./titles/title")
        if (t.text or "").strip()
    ]
    episodes: list[_Episode] = []
    for ep in root.findall("./episodes/episode"):
        epno = ep.find("epno")
        if epno is None or not (epno.text or "").strip():
            continue
        raw = epno.text.strip()
        kind = int(epno.get("type") or (1 if raw.isdigit() else 6))
        digits = "".join(ch for ch in raw if ch.isdigit())
        if not digits:
            continue
        episodes.append(_Episode(
            eid=ep.get("id") or "",
            kind=kind,
            number=int(digits),
            airdate=ep.findtext("airdate"),
            titles={(t.get(XML_LANG) or ""): (t.text or "").strip() for t in ep.findall("title") if t.text},
        ))
    permanent = root.find("./ratings/permanent")
    rating = votes = None
    if permanent is not None and permanent.text:
        try:
            rating = float(permanent.text)
            votes = int(permanent.get("count") or 0)
        except ValueError:
            pass
    count = root.findtext("episodecount")
    return _Anime(
        aid=root.get("id") or "",
        type=root.findtext("type"),
        episode_count=int(count) if count and count.isdigit() else None,
        start=root.findtext("startdate"),
        titles=titles,
        rating=rating,
        votes=votes,
        episodes=episodes,
    )


def parse_titles_dump(body: bytes) -> list[tuple[str, str, list[str]]]:
    """anime-titles.xml(.gz) -> [(aid, main title, all titles)]."""
    root = _xml(body, TITLES_XML_MAX_BYTES)
    out: list[tuple[str, str, list[str]]] = []
    for anime in root.iter("anime"):
        aid = anime.get("aid") or ""
        titles = [(t.get("type") or "", (t.text or "").strip()) for t in anime.findall("title") if (t.text or "").strip()]
        if not aid or not titles:
            continue
        main = next((text for kind, text in titles if kind == "main"), titles[0][1])
        out.append((aid, main, dedupe([text for _k, text in titles])))
    return out


@dataclass(frozen=True)
class _TitleIndex:
    titles: list[tuple[str, str, list[str]]]  # (aid, main title, all titles)
    flat: list[str]  # normalized titles, the fuzzy-search corpus
    owner: list[int]  # flat[i] belongs to titles[owner[i]]


def build_title_index(body: bytes) -> _TitleIndex:
    """Parse the dump and normalize every title (~90k: a few hundred ms - run it in a thread)."""
    titles = parse_titles_dump(body)
    flat: list[str] = []
    owner: list[int] = []
    for idx, (_aid, _main, names) in enumerate(titles):
        for name in names:
            key = normalize_title(name)
            if key:
                flat.append(key)
                owner.append(idx)
    return _TitleIndex(titles, flat, owner)


def rank_titles(index: _TitleIndex, query: str, key: str) -> list[int]:
    """Indexes into ``index.titles`` of the best matches for ``query`` (``key`` = its normalized
    form), best first. CPU-bound (rapidfuzz over the whole corpus): run it in a thread."""
    rough = process.extract(key, index.flat, scorer=fuzz.token_set_ratio, limit=80, score_cutoff=60)
    best: dict[int, float] = {}
    for _text, _score, pos in rough:
        idx = index.owner[pos]
        if idx in best:
            continue
        _aid, _main, names = index.titles[idx]
        best[idx] = max(title_similarity(query, n) for n in names)
    return [i for _s, i in sorted(((s, i) for i, s in best.items() if s >= 0.6), reverse=True)[:SEARCH_LIMIT]]


class AnidbProvider(HttpProvider):
    name: ClassVar[str] = "anidb"
    label: ClassVar[str] = "AniDB"
    kinds: ClassVar[frozenset[MediaKind]] = frozenset({MediaKind.ANIME})
    requires_key: ClassVar[bool] = True
    rate_limit_label: ClassVar[str | None] = "5 requests / 60 s and 200 / 24 h"
    max_candidates: ClassVar[int] = 3  # detail lookups per search are expensive here

    def __init__(
        self,
        credentials,  # type: ignore[no-untyped-def]
        *,
        animelists: AnimeLists | None = None,
        mapping_target: str = "tvdb",
        clock: Callable[[], float] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(credentials, **kwargs)
        self.animelists = animelists or AnimeLists(cache=self.cache, transport=self.transport, sleep=self.sleep)
        self.mapping_target = mapping_target if mapping_target in ("tvdb", "tmdb") else "tvdb"
        self.limiter = WindowLimiter(
            [(5, 60.0), (200, DAY)], min_interval=2.5, max_wait=75.0, history=self._load_history(),
            sleep=self.sleep, on_acquire=self._save_history, clock=clock or time.time,
        )
        self.api = self.make_client(limiter=self.limiter, max_retries=1, backoff_base=5.0)
        self.titles_http = self.make_client(limiter=TokenBucket(1 / 60, 1, sleep=self.sleep), max_retries=1,
                                            max_bytes=TITLES_MAX_BYTES)  # fmt: skip
        self._index: _TitleIndex | None = None
        self._titles_loaded_at = 0.0
        self._titles_lock: asyncio.Lock | None = None
        self._ban_until = self._load_ban()
        if self._ban_until:
            self.status_error = self._ban_message()

    def is_configured(self) -> bool:
        return bool(self.credentials.anidb_client and self.credentials.anidb_client_version)

    # ------------------------------------------------------------------ ban latch + limiter persistence

    def _load_ban(self) -> float:
        if self.cache is None:
            return 0.0
        raw = self.cache.get(BAN_KEY)
        if not raw:
            return 0.0
        try:
            until = float(json.loads(raw)["until"])
        except (ValueError, KeyError, TypeError):
            return 0.0
        return until if until > time.time() else 0.0

    def _ban_message(self) -> str:
        when = datetime.fromtimestamp(self._ban_until, UTC).strftime("%Y-%m-%d %H:%M UTC")
        return f"AniDB ban latch active: AniDB returned 'banned' or a daily-limit error; requests paused until {when}"

    def _latch(self, reason: str) -> None:
        self._ban_until = time.time() + BAN_SECONDS
        self.status_error = self._ban_message() + f" ({reason})"
        log.warning("%s - pausing AniDB for 24 h", reason)
        if self.cache is not None:
            self.cache.set(BAN_KEY, json.dumps({"until": self._ban_until, "reason": reason}).encode(), BAN_SECONDS)

    def _check_latch(self) -> None:
        if self._ban_until and time.time() < self._ban_until:
            raise AnidbBanned(self.status_error or self._ban_message())
        if self._ban_until:
            self._ban_until = 0.0
            self.status_error = None

    def _load_history(self) -> list[float]:
        if self.cache is None:
            return []
        raw = self.cache.get(LIMITER_KEY)
        try:
            return [float(t) for t in json.loads(raw)] if raw else []
        except (ValueError, TypeError):
            return []

    def _save_history(self, history: list[float]) -> None:
        if self.cache is not None:
            self.cache.set(LIMITER_KEY, json.dumps(history[-250:]).encode(), DAY)

    # ------------------------------------------------------------------ API

    def _auth(self) -> dict[str, Any]:
        if not self.is_configured():
            raise ProviderNotConfigured("anidb: client name/version missing (register an HTTP API client)")
        return {
            "client": str(self.credentials.anidb_client).strip().lower(),
            "clientver": self.credentials.anidb_client_version,
            "protover": 1,
        }

    async def _anime(self, aid: str, *, ttl: float | None = TTL_ANIDB) -> _Anime:
        self._check_latch()
        auth = self._auth()
        try:
            # Parsed once, in a thread; the parse also validates (error answers are never cached).
            return await self.api.get_parsed(
                API_URL, lambda body: parse_anime_xml(body, expected_aid=aid), params={"request": "anime", "aid": aid},
                auth_params=auth, ttl=ttl, max_bytes=ANIME_MAX_BYTES, thread=True,
            )  # fmt: skip
        except AnidbBanned as exc:
            self._latch(str(exc))
            raise

    def _series_from(self, anime: _Anime) -> SeriesInfo:
        main = next((t for lang, kind, t in anime.titles if kind == "main"), None)
        lang = (self.language or "en").split("-")[0]
        official = {l_: t for l_, kind, t in anime.titles if kind == "official"}
        title = main or official.get(lang) or (anime.titles[0][2] if anime.titles else anime.aid)
        mapping = self.animelists.entry(anime.aid)
        ids = mapping.ids() if mapping else {"anidb": anime.aid}
        return SeriesInfo(
            ref=ProviderRef(provider=self.name, id=anime.aid),
            title=title,
            original_title=official.get("ja") or main,
            year=int(anime.start[:4]) if anime.start and anime.start[:4].isdigit() else None,
            first_aired=parse_date(anime.start),
            aliases=dedupe([t for _l, _k, t in anime.titles if t != title]),
            ids=ids,
            popularity=float(anime.votes) if anime.votes else None,
            genres=["Anime"],
            is_anime=True,
            rating=anime.rating,
            original_language="ja",
        )

    async def get_series(self, series_id: str) -> SeriesInfo:
        await self.animelists.ensure_loaded()
        return self._series_from(await self._anime(str(series_id)))

    def _episode_title(self, ep: _Episode) -> str | None:
        lang = (self.language or "en").split("-")[0]
        return ep.titles.get(lang) or ep.titles.get("x-jat") or next(iter(ep.titles.values()), None)

    def _map(self, aid: str, anidb_season: int, number: int) -> tuple[int, int, str | None]:
        targets = [self.mapping_target, "tvdb" if self.mapping_target == "tmdb" else "tmdb"]
        for target in targets:
            mapped = self.animelists.map_episode(aid, anidb_season, number, target)  # type: ignore[arg-type]
            if mapped is not None and mapped[0] is not None:
                return mapped[0], mapped[1], target
        return (1 if anidb_season else 0), number, None

    async def get_episodes(self, series_id: str, order: EpisodeOrder = EpisodeOrder.AIRED) -> list[EpisodeInfo]:
        await self.animelists.ensure_loaded()
        anime = await self._anime(str(series_id))
        ref = ProviderRef(provider=self.name, id=anime.aid or str(series_id))
        mapping = self.animelists.entry(ref.id)
        out: list[EpisodeInfo] = []
        for ep in sorted(anime.episodes, key=lambda e: (e.kind, e.number)):
            if ep.kind not in (1, 2):
                continue
            anidb_season = 1 if ep.kind == 1 else 0
            season, number, target = self._map(ref.id, anidb_season, ep.number)
            ids = {"anidb": ep.eid, "anidb_epno": f"{'' if anidb_season else 'S'}{ep.number}"}
            if target:
                # The numbering now belongs to that TVDB / TMDb series: record which one, so the
                # matcher can name the series after it and the id tag can follow the numbering.
                ids["mapped"] = target
                mapped_series = (mapping.tvdb_id if target == "tvdb" else mapping.tmdb_tv) if mapping else None
                if mapped_series:
                    ids["mapped_series"] = mapped_series
            out.append(EpisodeInfo(
                series_ref=ref,
                season=season,
                episode=number,
                absolute=ep.number if anidb_season else None,
                title=self._episode_title(ep),
                air_date=parse_date(ep.airdate),
                ids=ids,
            ))
        return out

    async def franchise_episodes(self, series_id: str) -> list[EpisodeInfo]:
        """Episodes of every Anime-Lists sibling (same TVDB series) with franchise-wide absolute
        numbers, so ``abs 26`` can land in the third cour's AniDB entry."""
        await self.animelists.ensure_loaded()
        sibs = self.animelists.siblings(series_id)[:MAX_SIBLINGS]
        if len(sibs) <= 1:
            return await self.get_episodes(series_id)
        out: list[EpisodeInfo] = []
        cumulative = 0
        for sib in sibs:
            try:
                eps = await self.get_episodes(sib.aid)
            except ProviderNotFound:
                continue
            regular = [e for e in eps if e.absolute]
            for e in regular:
                e.absolute = cumulative + (e.absolute or 0)
            cumulative += max((int(e.ids.get("anidb_epno", "0") or 0) for e in regular), default=0)
            out.extend(regular)
            if sib.aid == str(series_id):
                out.extend(e for e in eps if not e.absolute)
        return out

    # ------------------------------------------------------------------ title index / search

    async def _ensure_titles(self) -> _TitleIndex:
        index = self._index
        if index is not None and time.time() - self._titles_loaded_at < TTL_TITLES_DUMP:
            return index
        if self._titles_lock is None:
            self._titles_lock = asyncio.Lock()
        async with self._titles_lock:
            index = self._index
            if index is not None and time.time() - self._titles_loaded_at < TTL_TITLES_DUMP:
                return index

            def validate(body: bytes) -> None:
                if body[:2] != b"\x1f\x8b" and not body[:200].lstrip().startswith(b"<"):
                    raise ProviderError("anidb: titles dump is neither gzip nor XML")

            body = await self.titles_http.get_bytes(TITLES_URL, ttl=TTL_TITLES_DUMP, validate=validate)
            index = await asyncio.to_thread(build_title_index, body)  # parse + normalize ~90k titles
            self._index = index
            self._titles_loaded_at = time.time()
            return index

    def load_titles(self, body: bytes) -> None:
        """Seed the title index directly (tests / offline import)."""
        self._index = build_title_index(body)
        self._titles_loaded_at = time.time()

    async def search_series(self, query: str, year: int | None = None, *, anime: bool = False) -> list[SeriesInfo]:
        self._check_latch()
        if not self.is_configured():
            raise ProviderNotConfigured("anidb: client name/version missing")
        index = await self._ensure_titles()
        await self.animelists.ensure_loaded()
        key = normalize_title(query)
        if not key:
            return []
        ranked = await asyncio.to_thread(rank_titles, index, query, key)
        out: list[SeriesInfo] = []
        for idx in ranked:
            aid, main, names = index.titles[idx]
            mapping = self.animelists.entry(aid)
            out.append(SeriesInfo(
                ref=ProviderRef(provider=self.name, id=aid),
                title=main,
                aliases=[n for n in names if n != main],
                ids=mapping.ids() if mapping else {"anidb": aid},
                is_anime=True,
                genres=["Anime"],
            ))
        return out

    async def healthcheck(self) -> None:
        self._check_latch()
        self._auth()
        await self._anime("1", ttl=None)
