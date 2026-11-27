"""Matcher: ParsedMedia -> MatchResult using the provider fallback chain.

Contract: docs/SPEC.md §6.2-§6.4.

Per item: build a query (title, alt titles, folder title, year, numbering; hints override)
-> walk ``registry.chain(kind)`` (unknown kind: series chain when numbered, else movie and
series) -> per provider search every query (year, then without year), keep names with
similarity >= ``CANDIDATE_CUTOFF`` (else the sole result / top 5), fetch details and
episodes for the best few, resolve the file's episodes, score -> stop at the first
provider whose best candidate scores >= 0.85 *and* resolved -> merge across providers by
shared ids -> episode-level fallback through another provider that knows the same series.

``match_many`` shares one memo of provider calls across the batch (each title/year group
is searched once), runs groups with bounded concurrency, then assigns episodes one-to-one
per show in score tiers: two files with different numbering claiming the same episode both
drop to review instead of one being guessed.

Provider failures never escape ``match``/``match_many``: they land in
``MatchResult.provider_errors`` and the chain falls through.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from schema_app.core.normalize import normalize_title, title_key
from schema_app.core.scoring import (
    AMBIGUITY_WINDOW,
    CANDIDATE_CUTOFF,
    EPISODE_TITLE_AGREE,
    EPISODE_TITLE_MARGIN,
    EPISODE_TITLE_STRONG,
    EPISODIC,
    MAX_RESULTS_PER_QUERY,
    SPECIAL_TITLE_THRESHOLD,
    STOP_SCORE,
    EpisodeCheck,
    best_title_similarity,
    checkable_episode_title,
    confidence_from,
    episode_title_similarity,
    popularity_bonus,
    score_candidate,
    split_part,
    title_similarity,
)
from schema_app.domain import (
    Candidate,
    EpisodeInfo,
    EpisodeOrder,
    MatchHint,
    MatchResult,
    MediaKind,
    MovieInfo,
    ParsedMedia,
    ProviderRef,
    ProviderSettings,
    ProviderStatus,
    SeriesInfo,
)
from schema_app.providers.base import (
    MetadataProvider,
    ProviderError,
    ProviderNotConfigured,
    ProviderNotFound,
)
from schema_app.providers.cache import CACHE_FILENAME, ResponseCache
from schema_app.providers.registry import ProviderRegistry

log = logging.getLogger("schema.matcher")

MAX_CANDIDATES = 10  # MatchResult.candidates length
GROUP_CONCURRENCY = 4  # title groups matched in parallel by match_many
TITLE_ENOUGH = 0.90  # stop trying alternative queries once a result matches this well
EPISODE_TITLE_THRESHOLD = 0.85  # "Show - Episode Title" against regular episodes
MOVIE_DETAILS = 2  # movie candidates per provider that get a details call

FOLDER_AFFINITY = 0.25  # score boost for the show most other files in the same folder matched
FOLDER_MIN_SIBLINGS = 2  # confident siblings needed before the folder's show counts as a prior

TIER_EXACT, TIER_ABSOLUTE, TIER_DERIVED, TIER_TITLE = 0, 1, 2, 3
ID_TYPES = ("tvdb", "tmdb", "imdb", "anidb", "tvmaze", "trakt", "fixtures")
MISMATCH = "episode_title_mismatch"

Info = MovieInfo | SeriesInfo


# --------------------------------------------------------------------------- internal records


@dataclass
class _Query:
    parsed: ParsedMedia
    kind: MediaKind
    forced_kind: bool
    titles: list[str]
    year: int | None
    season: int | None
    episodes: list[int]
    absolute: list[int]
    air_date: date | None
    special: bool
    special_title: str | None
    order: EpisodeOrder
    provider: str | None = None
    episode_title: str | None = None  # from the filename; checks (or replaces) the numbering

    @property
    def numbered(self) -> bool:
        return bool(self.episodes or self.absolute or self.air_date or self.special)

    @property
    def wants_episode(self) -> bool:
        return self.numbered or bool(self.special_title or self.episode_title)


@dataclass
class _Resolution:
    episodes: list[EpisodeInfo]
    check: EpisodeCheck | None
    tier: int = TIER_EXACT
    ident: tuple | None = None
    season_count: int | None = None
    part: int | None = None  # "Part 2" of a release that splits this one provider episode

    @property
    def mismatch(self) -> bool:
        return bool(self.check and self.check.found and self.check.title_mismatch)


@dataclass
class _Scored:
    candidate: Candidate
    provider: str
    resolution: _Resolution | None
    score_args: dict[str, Any]
    keys: frozenset[tuple[str, str, str]]
    popularity: float = 0.0
    extra_reasons: list[str] = field(default_factory=list)

    def rescore(self, check: EpisodeCheck | None, episodes: list[EpisodeInfo]) -> None:
        res = score_candidate(**dict(self.score_args, episode=check))
        self.candidate.score = res.score
        self.candidate.reasons = res.reasons + self.extra_reasons
        self.candidate.episodes = [e.model_copy() for e in episodes]
        self.candidate.part = self.resolution.part if self.resolution and episodes else None


class _Session:
    """Memo of provider calls shared by every item of one match / match_many call."""

    def __init__(self) -> None:
        self.memo: dict[tuple, asyncio.Future[Any]] = {}

    async def call(self, key: tuple, factory: Callable[[], Awaitable[Any]]) -> Any:
        fut = self.memo.get(key)
        if fut is not None:
            return await fut
        fut = asyncio.get_running_loop().create_future()
        self.memo[key] = fut
        try:
            result = await factory()
        except asyncio.CancelledError:
            self.memo.pop(key, None)
            fut.cancel()
            raise
        except Exception as exc:
            fut.set_exception(exc)
            fut.exception()  # mark retrieved; later awaiters re-raise it
            raise
        fut.set_result(result)
        return result


# --------------------------------------------------------------------------- helpers


def _names(info: Info) -> list[str]:
    return [n for n in (info.title, info.original_title, *info.aliases) if n]


def _fmt_se(season: int | None, episodes: Sequence[int | None]) -> str:
    eps = [e for e in episodes if e is not None]
    if season is None or not eps:
        return ""
    head = f"S{season:02d}E{eps[0]:02d}"
    if len(eps) == 1:
        return head
    if eps == list(range(eps[0], eps[0] + len(eps))):
        return f"{head}-E{eps[-1]:02d}"
    return head + "".join(f"E{e:02d}" for e in eps[1:])


def _fmt_eps(eps: Sequence[EpisodeInfo]) -> str:
    if not eps:
        return ""
    seasons = {e.season for e in eps}
    if len(seasons) == 1:
        return _fmt_se(eps[0].season, [e.episode for e in eps])
    return " & ".join(_fmt_se(e.season, [e.episode]) for e in eps)


def _merge(details: Info, search: Info) -> Info:
    """Details win; search fields fill gaps; ids and aliases are unioned."""
    update: dict[str, Any] = {}
    for name in type(details).model_fields:
        if name in ("ref",):
            continue
        dv, sv = getattr(details, name), getattr(search, name)
        if dv in (None, "", [], {}) and sv not in (None, "", [], {}):
            update[name] = sv
    update["ids"] = {**search.ids, **details.ids}
    aliases = list(dict.fromkeys([*details.aliases, *search.aliases]))
    update["aliases"] = [a for a in aliases if a != details.title]
    return details.model_copy(update=update)


def _identity(family: str, provider: str, info: Info) -> frozenset[tuple[str, str, str]]:
    keys = {(family, provider, str(info.ref.id))}
    for k, v in info.ids.items():
        if k in ID_TYPES and v:
            keys.add((family, k, str(v)))
    return frozenset(keys)


def _ep_keys(scored: _Scored) -> list[tuple[str, str, int | None, int | None, int | None]]:
    # The part is in the key: "Part 1" and "Part 2" of one provider episode don't compete.
    res = scored.resolution
    assert res is not None
    return [(e.series_ref.provider, e.series_ref.id, e.season, e.episode, res.part) for e in res.episodes]


def _folder(parsed: ParsedMedia) -> str:
    return str(Path(parsed.path).parent).casefold()


def _split_see(number: int) -> tuple[int, int] | None:
    """301 -> (3, 1), 1012 -> (10, 12): a season+episode code read as an absolute number."""
    raw = str(number)
    if len(raw) not in (3, 4) or int(raw[-2:]) == 0:
        return None
    return int(raw[:-2]), int(raw[-2:])


# --------------------------------------------------------------------------- matcher


class Matcher:
    def __init__(self, registry: ProviderRegistry | None = None, *, concurrency: int = GROUP_CONCURRENCY) -> None:
        self.registry = registry if registry is not None else ProviderRegistry(ProviderSettings())
        self.concurrency = max(1, concurrency)

    # ================================================================== public API

    async def match(self, parsed: ParsedMedia, hint: MatchHint | None = None) -> MatchResult:
        """Identify one file. Never raises for provider failures; they land in
        ``MatchResult.provider_errors`` and the chain falls through."""
        result, _claim = await self._safe_match(_Session(), parsed, hint)
        return result

    async def match_many(
        self, items: Sequence[ParsedMedia], hint: MatchHint | None = None
    ) -> list[MatchResult]:
        """Batch identify. Groups items by (kind, normalized title, year) so each show or movie
        is searched once; results are returned in input order."""
        items = list(items)
        session = _Session()
        results: list[MatchResult] = [MatchResult() for _ in items]
        claims: list[_Scored | None] = [None] * len(items)
        groups: dict[tuple, list[int]] = {}
        for i, parsed in enumerate(items):
            q = self._query(parsed, hint)
            key = (q.kind, title_key(q.titles[0]) if q.titles else f"#{i}", q.year)
            groups.setdefault(key, []).append(i)
        sem = asyncio.Semaphore(self.concurrency)

        async def run(indices: list[int]) -> None:
            async with sem:
                for i in indices:
                    results[i], claims[i] = await self._safe_match(session, items[i], hint)

        await asyncio.gather(*(run(ix) for ix in groups.values()))
        try:
            await self._folder_affinity(session, items, results, claims, hint)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive: the prior is a bonus, never a loss
            log.exception("folder affinity pass failed")
        try:
            self._assign_one_to_one(results, claims)
        except Exception:  # pragma: no cover - defensive: assignment must not lose results
            log.exception("one-to-one episode assignment failed")
        return results

    async def search(
        self, query: str, kind: MediaKind, year: int | None = None, provider: str | None = None
    ) -> list[Candidate]:
        """Free-text search for the manual override picker (candidates carry no episodes)."""
        kinds = [kind] if kind != MediaKind.UNKNOWN else [MediaKind.MOVIE, MediaKind.SERIES]
        q = _Query(
            parsed=ParsedMedia(path=query, filename=query, extension=""), kind=kind, forced_kind=True,
            titles=[query], year=year, season=None, episodes=[], absolute=[], air_date=None,
            special=False, special_title=None, order=EpisodeOrder.AIRED, provider=provider,
        )
        scored: list[_Scored] = []
        for k in kinds:
            if provider:
                p = self.registry.get(provider)
                providers = [p] if p is not None and self.registry.available(provider, k) else []
            else:
                providers = self.registry.chain(k)
            batches = await asyncio.gather(*(self._search_only(p, k, q) for p in providers))
            for batch in batches:
                scored.extend(batch)
        merged = self._dedupe(scored)
        return [s.candidate for s in merged[:30]]

    async def resolve(self, parsed: ParsedMedia, ref: ProviderRef, kind: MediaKind,
                      hint: MatchHint | None = None) -> Candidate:
        """User picked a specific show/movie: build the full Candidate for this file,
        resolving episodes (incl. absolute -> seasonal conversion).

        Raises ``ProviderError`` (or a subclass) when the provider is unknown or fails."""
        q = self._query(parsed, hint, kind_override=kind if kind != MediaKind.UNKNOWN else None)
        scored = await self._resolve_ref(_Session(), q, ref)
        scored.candidate.reasons.append("manual pick")
        return scored.candidate

    def provider_statuses(self) -> list[ProviderStatus]:
        return self.registry.statuses()

    async def test_provider(self, name: str) -> ProviderStatus:
        """Healthcheck one provider. Raises ``KeyError`` for an unknown provider name."""
        return await self.registry.test(name)

    async def reconfigure(self, settings: ProviderSettings) -> None:
        """Apply new credentials / priority / language without restarting."""
        await self.registry.reconfigure(settings)

    async def aclose(self) -> None:
        await self.registry.aclose()

    # ================================================================== query building

    def _query(self, parsed: ParsedMedia, hint: MatchHint | None, kind_override: MediaKind | None = None) -> _Query:
        h = hint or MatchHint()
        kind = kind_override or h.kind or parsed.kind
        forced = kind_override is not None or h.kind is not None
        typed = h.query or h.title
        raw_titles = [typed] if typed else [parsed.title, *parsed.alt_titles, parsed.folder_title]
        titles: list[str] = []
        seen: set[str] = set()
        for t in raw_titles:
            key = normalize_title(t) if t else ""
            if t and key and key not in seen:
                seen.add(key)
                titles.append(t.strip())
        season, episodes, absolute = parsed.season, list(parsed.episodes), list(parsed.absolute)
        special = parsed.special
        if h.episodes is not None:
            episodes, absolute = list(h.episodes), []
            if h.season is not None:
                season = h.season
            special = season == 0
        elif h.absolute is not None:
            absolute, episodes, season = list(h.absolute), [], h.season
            special = False
        elif h.season is not None:
            season = h.season
            special = special or season == 0
        return _Query(
            parsed=parsed, kind=kind, forced_kind=forced, titles=titles,
            year=h.year if h.year is not None else parsed.year,
            season=season, episodes=episodes, absolute=absolute, air_date=parsed.air_date,
            special=special or season == 0, special_title=parsed.special_title,
            order=h.episode_order or EpisodeOrder.AIRED, provider=h.provider,
            # numbers the user typed are not second-guessed by the filename's episode title
            episode_title=None if (h.episodes is not None or h.absolute is not None) else parsed.episode_title,
        )

    @staticmethod
    def _search_kinds(q: _Query) -> list[MediaKind]:
        if q.kind in (MediaKind.MOVIE, MediaKind.SERIES, MediaKind.ANIME):
            return [q.kind]
        if q.numbered:
            return [MediaKind.SERIES]
        return [MediaKind.MOVIE, MediaKind.SERIES]

    # ================================================================== per item

    async def _safe_match(self, session: _Session, parsed: ParsedMedia,
                          hint: MatchHint | None) -> tuple[MatchResult, _Scored | None]:
        try:
            return await self._match_item(session, parsed, hint)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a matcher bug must not take a whole job down
            log.exception("matching %s failed", parsed.filename)
            return MatchResult(warnings=[f"matcher_error: {type(exc).__name__}"]), None

    async def _match_item(self, session: _Session, parsed: ParsedMedia,
                          hint: MatchHint | None) -> tuple[MatchResult, _Scored | None]:
        q = self._query(parsed, hint)
        result = MatchResult()
        if hint is not None and hint.ref is not None:
            return await self._match_ref(session, q, hint.ref, result)
        if not q.titles:
            result.warnings.append("no_title")
            return result, None
        pool: list[_Scored] = []
        for kind in self._search_kinds(q):
            pool.extend(await self._walk_chain(session, q, kind, result))
        return self._finish(q, self._dedupe(pool), result)

    def _finish(self, q: _Query, pool: list[_Scored], result: MatchResult) -> tuple[MatchResult, _Scored | None]:
        pool = pool[:MAX_CANDIDATES]
        result.candidates = [s.candidate for s in pool]
        if not pool:
            result.warnings.append("no_match")
            return result, None
        top = pool[0]
        confidence, ambiguous = confidence_from([s.candidate.score for s in pool])
        if ambiguous:
            result.warnings.append("ambiguous_match")
            top.candidate.reasons.append(f"runner-up within {AMBIGUITY_WINDOW:.2f}")
        result.confidence = confidence
        if self._resolved(top, q):
            result.best = top.candidate
            self._flag_mismatch(result, top)
            return result, top
        result.warnings.append("episode_not_found")
        return result, None

    @staticmethod
    def _flag_mismatch(result: MatchResult, scored: _Scored) -> None:
        """The number found an episode whose title isn't the one in the filename: never auto-run."""
        if scored.resolution is not None and scored.resolution.mismatch and MISMATCH not in result.warnings:
            result.warnings.append(MISMATCH)

    @staticmethod
    def _resolved(scored: _Scored, q: _Query) -> bool:
        if scored.candidate.kind == MediaKind.MOVIE:
            return True
        res = scored.resolution
        return bool(res and res.check and res.check.found)

    async def _match_ref(self, session: _Session, q: _Query, ref: ProviderRef,
                         result: MatchResult) -> tuple[MatchResult, _Scored | None]:
        result.providers_tried.append(ref.provider)
        try:
            scored = await self._resolve_ref(session, q, ref)
        except ProviderError as exc:
            result.provider_errors[ref.provider] = str(exc)
            result.warnings.append("manual_pick_failed")
            return result, None
        scored.candidate.reasons.append("manual pick")
        result.candidates = [scored.candidate]
        if self._resolved(scored, q):
            result.best = scored.candidate
            result.confidence = 1.0
            self._flag_mismatch(result, scored)
            return result, scored
        result.confidence = scored.candidate.score
        result.warnings.append("episode_not_found")
        return result, None

    async def _resolve_ref(self, session: _Session, q: _Query, ref: ProviderRef) -> _Scored:
        p = self.registry.get(ref.provider)
        if p is None:
            raise ProviderError(f"unknown provider '{ref.provider}'")
        kind = q.kind
        if kind == MediaKind.UNKNOWN:
            kind = MediaKind.SERIES if q.wants_episode else MediaKind.MOVIE
        if kind == MediaKind.MOVIE:
            try:
                movie = await p.get_movie(ref.id)
            except ProviderNotFound:
                if q.kind != MediaKind.UNKNOWN:
                    raise
                kind = MediaKind.SERIES
            else:
                return await self._build(session, p, q, MediaKind.MOVIE, movie, details=False)
        series = await p.get_series(ref.id)
        return await self._build(session, p, q, kind, series, details=False, strict_episodes=True)

    # ================================================================== chain walk

    def _providers(self, q: _Query, kind: MediaKind, result: MatchResult) -> list[MetadataProvider]:
        if q.provider:
            p = self.registry.get(q.provider)
            if p is None:
                result.provider_errors[q.provider] = "unknown provider"
                return []
            if not self.registry.available(q.provider, kind):
                result.provider_errors[q.provider] = f"not configured, disabled or no {kind.value} support"
                return []
            return [p]
        return self.registry.chain(kind)

    async def _walk_chain(self, session: _Session, q: _Query, kind: MediaKind,
                          result: MatchResult) -> list[_Scored]:
        providers = self._providers(q, kind, result)
        acc: list[_Scored] = []
        for p in providers:
            if p.name not in result.providers_tried:
                result.providers_tried.append(p.name)
            try:
                scored = await self._provider_candidates(session, p, q, kind)
            except ProviderNotConfigured as exc:
                result.provider_errors[p.name] = f"not configured: {exc}"
                continue
            except ProviderError as exc:
                result.provider_errors[p.name] = str(exc)
                if not isinstance(exc, ProviderNotFound):
                    self.registry.record_error(p.name, exc)
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a provider bug falls through like any provider failure
                log.exception("provider %s crashed while matching", p.name)
                result.provider_errors[p.name] = f"internal error: {type(exc).__name__}"
                self.registry.record_error(p.name, exc)
                continue
            acc.extend(scored)
            if scored and scored[0].candidate.score >= STOP_SCORE and self._resolved(scored[0], q):
                break
        acc = self._dedupe(acc)
        if acc and kind in EPISODIC:
            top = acc[0]
            res = top.resolution
            if res is not None and res.check is not None and res.check.found is False:
                await self._episode_fallback(session, q, top, providers, result)
                acc.sort(key=lambda s: (s.candidate.score, s.popularity), reverse=True)
        return acc

    async def _search(self, session: _Session, p: MetadataProvider, kind: MediaKind,
                      query: str, year: int | None) -> list[Info]:
        family = "movie" if kind == MediaKind.MOVIE else "series"
        key = ("search", p.name, family, kind == MediaKind.ANIME, normalize_title(query), year)

        async def run() -> list[Info]:
            if kind == MediaKind.MOVIE:
                return list(await p.search_movies(query, year))
            return list(await p.search_series(query, year, anime=kind == MediaKind.ANIME))

        return await session.call(key, run)

    @staticmethod
    def _select(query: str, results: list[Info]) -> list[Info]:
        """FileBot-style pick: names >= CANDIDATE_CUTOFF (max 5); else the sole result; else the top 5."""
        scored = [(best_title_similarity([query], _names(r)), i, r) for i, r in enumerate(results)]
        scored.sort(key=lambda s: (-s[0], s[1]))
        accepted = [r for s, _i, r in scored if s >= CANDIDATE_CUTOFF]
        if accepted:
            return accepted[:MAX_RESULTS_PER_QUERY]
        if len(results) == 1:
            return [results[0]]
        return [r for _s, _i, r in scored[:MAX_RESULTS_PER_QUERY]]

    def _prescore(self, q: _Query, info: Info) -> float:
        score = 0.7 * best_title_similarity(q.titles, _names(info))
        if q.year is not None and info.year is not None:
            diff = abs(q.year - info.year)
            score += 0.2 if diff == 0 else 0.1 if diff == 1 else -0.25
        return score + popularity_bonus(info.popularity)

    async def _provider_candidates(self, session: _Session, p: MetadataProvider, q: _Query,
                                   kind: MediaKind) -> list[_Scored]:
        infos: dict[str, Info] = {}
        for query in q.titles:
            results = await self._search(session, p, kind, query, q.year)
            if not results and q.year is not None:
                results = await self._search(session, p, kind, query, None)
            picked = self._select(query, results)
            for info in picked:
                infos.setdefault(str(info.ref.id), info)
            if any(best_title_similarity([query], _names(i)) >= TITLE_ENOUGH for i in picked):
                break
        if not infos:
            return []
        ranked = sorted(infos.values(), key=lambda i: self._prescore(q, i), reverse=True)
        if kind == MediaKind.MOVIE:
            builds = [self._build(session, p, q, kind, info, details=n < MOVIE_DETAILS)
                      for n, info in enumerate(ranked[:MAX_RESULTS_PER_QUERY * 2])]
        else:
            limit = int(getattr(p, "max_candidates", MAX_RESULTS_PER_QUERY))
            builds = [self._build(session, p, q, kind, info, details=True) for info in ranked[:limit]]
        out = list(await asyncio.gather(*builds))  # the provider's limiter paces the requests
        out.sort(key=lambda s: (s.candidate.score, s.popularity), reverse=True)
        return out

    def _note_failure(self, p: MetadataProvider, exc: ProviderError, notes: list[str], what: str) -> None:
        notes.append(f"{what} unavailable ({type(exc).__name__})")
        if not isinstance(exc, ProviderNotFound):
            self.registry.record_error(p.name, exc)

    def _series_kind(self, q: _Query, series: SeriesInfo) -> MediaKind:
        if q.forced_kind and q.kind in EPISODIC:
            return q.kind
        if q.kind == MediaKind.ANIME or series.is_anime:
            return MediaKind.ANIME
        return MediaKind.SERIES

    async def _build(self, session: _Session, p: MetadataProvider, q: _Query, kind: MediaKind, info: Info,
                     *, details: bool, strict_episodes: bool = False) -> _Scored:
        notes: list[str] = []
        if kind == MediaKind.MOVIE:
            movie = info
            if details:
                try:
                    full = await session.call(("movie", p.name, str(info.ref.id)), lambda: p.get_movie(str(info.ref.id)))
                    movie = _merge(full, info)
                except ProviderError as exc:
                    self._note_failure(p, exc, notes, "details")
            assert isinstance(movie, MovieInfo)
            args = dict(
                file_kind=q.kind, cand_kind=MediaKind.MOVIE,
                title_sim=best_title_similarity(q.titles, _names(movie)),
                file_year=q.year, cand_year=movie.year, popularity=movie.popularity,
            )
            res = score_candidate(**args)
            cand = Candidate(
                id=f"{p.name}:{MediaKind.MOVIE.value}:{movie.ref.id}", kind=MediaKind.MOVIE, provider=p.name,
                title=movie.title, year=movie.year, score=res.score, movie=movie, reasons=res.reasons + notes,
            )
            return _Scored(cand, p.name, None, args, _identity("movie", p.name, movie),
                           movie.popularity or 0.0, notes)

        series = info
        sid = str(info.ref.id)
        if details:
            try:
                full = await session.call(("series", p.name, sid), lambda: p.get_series(sid))
                series = _merge(full, info)
            except ProviderError as exc:
                self._note_failure(p, exc, notes, "details")
        assert isinstance(series, SeriesInfo)
        resolution: _Resolution | None = None
        try:
            episodes = await session.call(("episodes", p.name, sid, q.order), lambda: p.get_episodes(sid, q.order))
        except ProviderError as exc:
            if strict_episodes and q.wants_episode:
                raise  # a manual pick that cannot load its episodes should fail loudly
            self._note_failure(p, exc, notes, "episodes")
            if q.wants_episode:
                resolution = _Resolution([], EpisodeCheck(False, self._label(q)))
        else:
            resolution = await self._resolve_episodes(session, p, q, series, list(episodes))
        cand_kind = self._series_kind(q, series)
        season_count = (resolution.season_count if resolution and resolution.season_count else None) or series.season_count
        args = dict(
            file_kind=q.kind, cand_kind=cand_kind,
            title_sim=best_title_similarity(q.titles, _names(series)),
            file_year=q.year, cand_year=series.year, popularity=series.popularity, is_anime=series.is_anime,
            file_season=q.season if (q.season and q.episodes and not q.air_date) else None,
            season_count=season_count,
            episode=resolution.check if resolution else None,
        )
        res = score_candidate(**args)
        found = [e.model_copy() for e in resolution.episodes] if resolution and resolution.check and resolution.check.found else []
        # Scoring above used the provider's own record; naming uses the mapped series (if any).
        shown = await self._mapped_series(session, p, series, found, notes)
        cand = Candidate(
            id=f"{p.name}:{cand_kind.value}:{series.ref.id}", kind=cand_kind, provider=p.name,
            title=shown.title, year=shown.year, score=res.score, series=shown,
            episodes=found, episode_order=q.order, part=resolution.part if found and resolution else None,
            reasons=res.reasons + notes,
        )
        return _Scored(cand, p.name, resolution, args, _identity("series", p.name, shown),
                       series.popularity or 0.0, notes)

    async def _mapped_series(self, session: _Session, p: MetadataProvider, series: SeriesInfo,
                             episodes: list[EpisodeInfo], notes: list[str]) -> SeriesInfo:
        """AniDB has one entry per season/cour; Anime-Lists renumbers its episodes onto one TVDB
        (or TMDb) series. Name the series after that TVDB/TMDb series (its title and first-air
        year) so every season entry renders into the same series folder, and carry the mapped
        id so the id tag agrees with the numbering. Without the TVDB/TMDb provider (no key,
        disabled, lookup failed) the entry keeps its own name; the id is still carried."""
        targets = {(e.ids.get("mapped"), e.ids.get("mapped_series")) for e in episodes}
        if len(targets) != 1:
            return series
        target, target_id = targets.pop()
        if target not in ("tvdb", "tmdb") or not target_id or target == p.name:
            return series
        update: dict[str, Any] = {"ids": {**series.ids, target: str(target_id)}}
        mapped: SeriesInfo | None = None
        provider = self.registry.get(target)
        if provider is not None and self.registry.available(target):
            sid = str(target_id)
            try:
                info = await session.call(("series", target, sid), lambda: provider.get_series(sid))
            except ProviderError as exc:
                self._note_failure(provider, exc, notes, f"{provider.label} series name")
            except Exception as exc:  # a provider bug must not cost the AniDB match
                log.exception("mapped series lookup %s:%s failed", target, sid)
                notes.append(f"{provider.label} series name unavailable ({type(exc).__name__})")
            else:
                mapped = info if isinstance(info, SeriesInfo) and info.title else None
        if mapped is None:
            return series.model_copy(update=update)
        update["ids"] = {
            **{k: str(v) for k, v in mapped.ids.items() if k in ID_TYPES and v},
            **update["ids"],
        }
        update.update(
            title=mapped.title,
            original_title=mapped.original_title or series.original_title,
            year=mapped.year or (mapped.first_aired.year if mapped.first_aired else None),
            first_aired=mapped.first_aired,
            aliases=[a for a in dict.fromkeys([series.title, *series.aliases]) if a != mapped.title],
            season_count=mapped.season_count or series.season_count,
        )
        notes.append(f"series named after {provider.label if provider else target} {target_id} (Anime-Lists mapping)")
        return series.model_copy(update=update)

    async def _search_only(self, p: MetadataProvider, kind: MediaKind, q: _Query) -> list[_Scored]:
        try:
            if kind == MediaKind.MOVIE:
                results: list[Info] = list(await p.search_movies(q.titles[0], q.year))
                if not results and q.year is not None:
                    results = list(await p.search_movies(q.titles[0], None))
            else:
                results = list(await p.search_series(q.titles[0], q.year, anime=kind == MediaKind.ANIME))
                if not results and q.year is not None:
                    results = list(await p.search_series(q.titles[0], None, anime=kind == MediaKind.ANIME))
        except ProviderError as exc:
            if not isinstance(exc, (ProviderNotConfigured, ProviderNotFound)):
                self.registry.record_error(p.name, exc)
            return []
        out: list[_Scored] = []
        for info in results[:15]:
            if isinstance(info, MovieInfo):
                c_kind, family = MediaKind.MOVIE, "movie"
            else:
                c_kind = self._series_kind(q, info) if kind != MediaKind.MOVIE else MediaKind.SERIES
                family = "series"
            args = dict(file_kind=kind, cand_kind=c_kind, title_sim=best_title_similarity(q.titles, _names(info)),
                        file_year=q.year, cand_year=info.year, popularity=info.popularity,
                        is_anime=getattr(info, "is_anime", False))
            res = score_candidate(**args)
            cand = Candidate(
                id=f"{p.name}:{c_kind.value}:{info.ref.id}", kind=c_kind, provider=p.name, title=info.title,
                year=info.year, score=res.score, reasons=res.reasons,
                movie=info if isinstance(info, MovieInfo) else None,
                series=info if isinstance(info, SeriesInfo) else None,
            )
            out.append(_Scored(cand, p.name, None, args, _identity(family, p.name, info), info.popularity or 0.0))
        return out

    # ================================================================== merging

    @staticmethod
    def _dedupe(scored: list[_Scored]) -> list[_Scored]:
        ordered = sorted(scored, key=lambda s: (s.candidate.score, s.popularity), reverse=True)
        out: list[_Scored] = []
        for s in ordered:
            dup = next((o for o in out if o.keys & s.keys), None)
            if dup is None:
                out.append(s)
                continue
            if dup is s:
                continue
            # same title at two providers: keep the better-scored record, adopt missing ids
            info = dup.candidate.series or dup.candidate.movie
            other = s.candidate.series or s.candidate.movie
            if info is not None and other is not None:
                for k, v in other.ids.items():
                    info.ids.setdefault(k, v)
            dup.keys = dup.keys | s.keys
        return out

    # ================================================================== episodes

    @staticmethod
    def _label(q: _Query) -> str:
        if q.air_date:
            return f"aired {q.air_date.isoformat()}"
        if q.season is not None and q.episodes:
            return _fmt_se(q.season, q.episodes)
        if q.absolute:
            return "abs " + "-".join(str(n) for n in q.absolute)
        if q.special_title:
            return f"special '{q.special_title}'" if q.special else f"episode '{q.special_title}'"
        if q.episode_title:
            return f"episode '{q.episode_title}'"
        return "special" if q.special else "episode"

    async def _resolve_episodes(self, session: _Session, p: MetadataProvider, q: _Query,
                                series: SeriesInfo, episodes: list[EpisodeInfo]) -> _Resolution:
        anime = q.kind == MediaKind.ANIME or series.is_anime
        res = self._resolve_in(q, episodes, anime=anime)
        res.season_count = max((e.season for e in episodes if e.season and e.season > 0), default=None)
        expand = getattr(p, "franchise_episodes", None)
        if (
            expand is not None
            and res.check is not None
            and res.check.found is False
            and (q.absolute or (q.season and q.episodes))
        ):
            sid = str(series.ref.id)
            try:
                more = await session.call(("franchise", p.name, sid), lambda: expand(sid))
            except ProviderError:
                more = None
            if more:
                alt = self._resolve_in(q, list(more), anime=anime)
                if alt.check is not None and alt.check.found:
                    alt.check.note = "via Anime-Lists sibling entries"
                    alt.season_count = max((e.season for e in more if e.season and e.season > 0), default=None)
                    return alt
        return res

    def _resolve_in(self, q: _Query, episodes: list[EpisodeInfo], *, anime: bool = False) -> _Resolution:
        regular = [e for e in episodes if e.season and e.season > 0 and e.episode is not None]
        by_se = {(e.season, e.episode): e for e in episodes if e.season is not None and e.episode is not None}
        by_abs: dict[int, EpisodeInfo] = {}
        if any(e.absolute for e in regular):
            for e in regular:
                if e.absolute:
                    by_abs.setdefault(e.absolute, e)
        else:
            for n, e in enumerate(sorted(regular, key=lambda x: (x.season or 0, x.episode or 0)), start=1):
                by_abs[n] = e
        label = self._label(q)

        # 1. daily shows: air date
        if q.air_date:
            hits = [e for e in episodes if e.air_date == q.air_date]
            ident = ("date", q.air_date)
            if hits:
                hits.sort(key=lambda e: (e.season == 0, e.season or 0, e.episode or 0))
                return _Resolution(hits[:1], EpisodeCheck(True, f"{label} → {_fmt_eps(hits[:1])}"), TIER_EXACT, ident)
            return _Resolution([], EpisodeCheck(False, label), TIER_EXACT, ident)

        nums_se, nums_abs = list(q.episodes), list(q.absolute)
        if q.order == EpisodeOrder.ABSOLUTE and nums_se and not nums_abs:
            nums_se, nums_abs = [], nums_se

        # 2. seasonal numbers (incl. S00Exx specials)
        if q.season is not None and nums_se:
            ident = ("se", q.season, tuple(nums_se))
            hits = [by_se.get((q.season, n)) for n in nums_se]
            if all(hits):
                return self._verify(q, _Resolution(list(hits), EpisodeCheck(True, label), TIER_EXACT, ident),  # type: ignore[arg-type]
                                    episodes)
            # Not in that season. The filename's episode title says which episode it is ("S1E14 -
            # Return to Spider-Skull Island" is S01E13 at TMDb); the number alone never jumps seasons.
            by_title = self._title_resolution(q, episodes)
            if by_title is not None:
                return by_title
            if q.season == 1 and anime:  # continuous "S01E15" numbering on a multi-season anime
                alt = [by_abs.get(n) for n in nums_se]
                if all(alt):
                    res = _Resolution(list(alt), EpisodeCheck(True, f"{label} → {_fmt_eps(alt)}",  # type: ignore[arg-type]
                                                              note="read as absolute"), TIER_DERIVED, ident)
                    return self._verify(q, res, episodes)
            return _Resolution([h for h in hits if h], EpisodeCheck(False, label), TIER_EXACT, ident)

        # 3. absolute numbers (anime) -> seasonal via the provider's absolute order
        if nums_abs:
            ident = ("abs", tuple(nums_abs))
            hits = [by_abs.get(n) for n in nums_abs]
            if all(hits):
                res = _Resolution(list(hits), EpisodeCheck(True, f"{label} → {_fmt_eps(hits)}"),  # type: ignore[arg-type]
                                  TIER_ABSOLUTE, ident)
                return self._verify(q, res, episodes)
            if q.season is None:
                alt = [by_se.get((1, n)) for n in nums_abs]
                if all(alt):
                    res = _Resolution(list(alt), EpisodeCheck(True, f"{label} → {_fmt_eps(alt)}"),  # type: ignore[arg-type]
                                      TIER_DERIVED, ident)
                    return self._verify(q, res, episodes)
                if not anime:  # "The Venture Bros - 301 - ..." with no season folder: S03E01
                    alt = [by_se.get(se) if se else None for se in map(_split_see, nums_abs)]
                    if all(alt):
                        res = _Resolution(list(alt), EpisodeCheck(True, f"{label} → {_fmt_eps(alt)}",  # type: ignore[arg-type]
                                                                  note="read as season + episode"), TIER_DERIVED, ident)
                        return self._verify(q, res, episodes)
            by_title = self._title_resolution(q, episodes)
            if by_title is not None:
                return by_title
            return _Resolution([], EpisodeCheck(False, label), TIER_ABSOLUTE, ident)

        # 4a. no episode number, but an episode title ("The IT Crowd S05 The Internet is Coming")
        if q.episode_title and not q.special:
            by_title = self._title_resolution(q, episodes)
            if by_title is not None:
                return by_title
            if not q.special_title:
                return _Resolution([], EpisodeCheck(False, label), TIER_TITLE, ("title", normalize_title(q.episode_title)))

        # 4. specials without numbers (by title, or a lone OVA) and title-only episodes
        if q.special or q.special_title:
            specials = [e for e in episodes if e.season == 0]
            if q.special_title:
                ident = ("title", normalize_title(q.special_title))
                hit = self._by_title(q.special_title, specials, SPECIAL_TITLE_THRESHOLD)
                if hit is None and not q.special:
                    hit = self._by_title(q.special_title, regular, EPISODE_TITLE_THRESHOLD)
                if hit is not None:
                    episode, sim = hit
                    kind_word = "special" if episode.season == 0 else "episode"
                    return _Resolution([episode], EpisodeCheck(
                        True, f"{kind_word} '{q.special_title}' → {_fmt_eps([episode])}",
                        note=f"episode title {round(sim * 100)}%"),
                        TIER_TITLE, ident)
            if q.special and not q.special_title and len(specials) == 1:
                return _Resolution(specials, EpisodeCheck(True, f"{label} → {_fmt_eps(specials)}",
                                                          note="only special of the series"),
                                   TIER_TITLE, ("lone-special",))
            if q.special or q.wants_episode:
                return _Resolution([], EpisodeCheck(False, label), TIER_TITLE,
                                   ("title", normalize_title(q.special_title or "")))
        return _Resolution([], None)

    @staticmethod
    def _by_title(title: str, episodes: list[EpisodeInfo], threshold: float) -> tuple[EpisodeInfo, float] | None:
        best: tuple[EpisodeInfo, float] | None = None
        for e in episodes:
            if not e.title:
                continue
            sim = title_similarity(title, e.title)
            if sim >= threshold and (best is None or sim > best[1]):
                best = (e, sim)
        return best

    @staticmethod
    def _lookup_title(title: str, episodes: list[EpisodeInfo]) -> EpisodeInfo | None:
        """The one episode of the whole series (specials included) whose title clearly matches."""
        scored = sorted(
            ((episode_title_similarity(title, e.title), i, e) for i, e in enumerate(episodes)
             if e.episode is not None and e.season is not None and checkable_episode_title(e.title)),
            key=lambda t: (-t[0], t[1]),
        )  # fmt: skip
        if not scored:
            return None
        best, _i, episode = scored[0]
        runner_up = next((s for s, _j, e in scored[1:] if (e.season, e.episode) != (episode.season, episode.episode)), 0.0)
        if best >= EPISODE_TITLE_STRONG and best - runner_up >= EPISODE_TITLE_MARGIN:
            return episode
        return None

    @staticmethod
    def _part(parsed_title: str, episode_title: str | None) -> int | None:
        """'Just Say Yes Part 2' resolved to the single episode 'Just Say Yes' -> part 2."""
        mine, theirs = split_part(parsed_title)[1], split_part(episode_title)[1]
        return mine if mine is not None and theirs is None else None

    def _title_resolution(self, q: _Query, episodes: list[EpisodeInfo]) -> _Resolution | None:
        """Resolve by the filename's episode title alone (numbering missing, out of range or wrong)."""
        if not q.episode_title or not checkable_episode_title(q.episode_title):
            return None
        episode = self._lookup_title(q.episode_title, episodes)
        if episode is None:
            return None
        part = self._part(q.episode_title, episode.title)
        target = _fmt_eps([episode])
        ident = ("title", normalize_title(split_part(q.episode_title)[0]), part)
        check = EpisodeCheck(True, target, note=f"title match: {self._label(q)} → {target}")
        return _Resolution([episode], check, TIER_TITLE, ident, part=part)

    def _verify(self, q: _Query, res: _Resolution, episodes: list[EpisodeInfo]) -> _Resolution:
        """Check a number-resolved episode against the filename's episode title. Agree: keep it.
        Disagree: the title's own episode when exactly one clearly matches, else keep the number's
        episode flagged ``title_mismatch`` (review, never auto-run)."""
        title = q.episode_title
        if not title or res.check is None or not res.check.found or len(res.episodes) != 1:
            return res
        current = res.episodes[0]
        if not checkable_episode_title(title) or not checkable_episode_title(current.title):
            return res
        if episode_title_similarity(title, current.title) >= EPISODE_TITLE_AGREE:
            res.part = self._part(title, current.title)
            return res
        by_title = self._title_resolution(q, episodes)
        if by_title is not None:
            return by_title
        res.check.title_mismatch = True
        res.check.note = "; ".join(n for n in (res.check.note, f"episode title '{title}' ≠ '{current.title}'") if n)
        return res

    async def _episode_fallback(self, session: _Session, q: _Query, top: _Scored,
                                providers: list[MetadataProvider], result: MatchResult) -> None:
        """The series is right but this provider lacks the episode: ask another provider that
        knows the same series (via shared ids) for just that episode."""
        series = top.candidate.series
        if series is None:
            return
        ids = dict(series.ids)
        for p2 in providers:
            if p2.name == top.provider:
                continue
            sid = ids.get(p2.name)
            lookup = getattr(p2, "lookup_series_id", None)
            if not sid and lookup is not None:
                for src in ("tvdb", "tmdb", "imdb"):
                    value = ids.get(src)
                    if not value:
                        continue
                    try:
                        sid = await session.call(("lookup", p2.name, src, value),
                                                 lambda lk=lookup, s=src, v=value: lk(s, v))
                    except ProviderError:
                        sid = None
                    if sid:
                        break
            if not sid:
                continue
            sid = str(sid)
            try:
                eps = await session.call(("episodes", p2.name, sid, q.order),
                                         lambda pp=p2, s=sid: pp.get_episodes(s, q.order))
            except ProviderError as exc:
                result.provider_errors.setdefault(p2.name, str(exc))
                continue
            alt = self._resolve_in(q, list(eps), anime=top.candidate.kind == MediaKind.ANIME)
            if alt.check is not None and alt.check.found:
                alt.check.note = f"episode from {p2.label}"
                alt.season_count = top.resolution.season_count if top.resolution else None
                top.resolution = alt
                top.rescore(alt.check, alt.episodes)
                if p2.name not in result.providers_tried:
                    result.providers_tried.append(p2.name)
                return

    # ================================================================== batch assignment

    async def _folder_affinity(self, session: _Session, items: list[ParsedMedia], results: list[MatchResult],
                               claims: list[_Scored | None], hint: MatchHint | None) -> None:
        """Files in one folder are usually one show. When most confident files of a folder matched
        the same series, a sibling that matched another show (or none) is scored against that series
        too, with a ``FOLDER_AFFINITY`` boost, and switches when it then wins ("Ted/Ted.S01E08..." had
        matched "Wa$ted!" while its 15 siblings matched "ted (2024)")."""
        if hint is not None and (hint.ref is not None or hint.query or hint.title):
            return  # the user named the show
        by_folder: dict[str, list[int]] = {}
        for i, parsed in enumerate(items):
            by_folder.setdefault(_folder(parsed), []).append(i)
        for indices in by_folder.values():
            if len(indices) <= FOLDER_MIN_SIBLINGS:
                continue
            confident = [
                s for i in indices
                if (s := claims[i]) is not None and results[i].best is not None and s.candidate.kind in EPISODIC
                and results[i].confidence >= STOP_SCORE and MISMATCH not in results[i].warnings
            ]  # fmt: skip
            votes = Counter(s.candidate.id for s in confident)
            if not votes:
                continue
            show_id, count = votes.most_common(1)[0]
            if count < FOLDER_MIN_SIBLINGS or count * 2 <= len(confident):
                continue
            leader = next(s for s in confident if s.candidate.id == show_id)
            provider = self.registry.get(leader.provider)
            series = leader.candidate.series
            if provider is None or series is None:
                continue
            for i in indices:
                result = results[i]
                if result.best is not None and result.best.id == show_id:
                    continue
                q = self._query(items[i], hint)
                if not q.wants_episode:
                    continue
                try:
                    scored = await self._build(session, provider, q, leader.candidate.kind, series, details=False)
                except ProviderError:
                    continue
                if not self._resolved(scored, q) or (scored.resolution is not None and scored.resolution.mismatch):
                    continue
                boosted = round(min(1.0, scored.candidate.score + FOLDER_AFFINITY), 4)
                if result.best is not None and boosted <= result.best.score:
                    continue
                note = f"same show as {count} other files in this folder"
                scored.extra_reasons.append(note)
                scored.candidate.score = boosted
                scored.candidate.reasons.append(note)
                result.candidates = [scored.candidate, *(c for c in result.candidates if c.id != show_id)][:MAX_CANDIDATES]
                result.best = scored.candidate
                result.confidence = confidence_from([c.score for c in result.candidates])[0]
                stale = {"episode_not_found", "ambiguous_match", "no_match", MISMATCH}
                result.warnings = [w for w in result.warnings if w not in stale]
                claims[i] = scored

    def _assign_one_to_one(self, results: list[MatchResult], claims: list[_Scored | None]) -> None:
        """Tiered one-to-one episode assignment per show (FileBot's mutually-exclusive pairs).

        Within a show, claims are processed from the strongest tier (exact S/E or date) to the
        weakest (title match). A claim is committed only if no *different* numbering claims any
        of its episodes at the same tier and no stronger claim already owns them. Identical
        numbering (two versions of the same episode) is not a conflict. Losers lose ``best``."""
        by_show: dict[str, list[int]] = {}
        for i, s in enumerate(claims):
            if s is None or s.resolution is None or s.resolution.ident is None or not s.resolution.episodes:
                continue
            if s.candidate.kind == MediaKind.MOVIE:
                continue
            by_show.setdefault(s.candidate.id, []).append(i)
        for indices in by_show.values():
            if len(indices) < 2:
                continue
            committed: dict[tuple, tuple] = {}
            losers: list[int] = []
            for tier in sorted({claims[i].resolution.tier for i in indices}):  # type: ignore[union-attr]
                group = [i for i in indices if claims[i].resolution.tier == tier]  # type: ignore[union-attr]
                claimants: dict[tuple, set[tuple]] = {}
                for i in group:
                    for k in _ep_keys(claims[i]):  # type: ignore[arg-type]
                        claimants.setdefault(k, set()).add(claims[i].resolution.ident)  # type: ignore[union-attr]
                winners = []
                for i in group:
                    ident = claims[i].resolution.ident  # type: ignore[union-attr]
                    keys = _ep_keys(claims[i])  # type: ignore[arg-type]
                    if all(claimants[k] == {ident} and committed.get(k, ident) == ident for k in keys):
                        winners.append(i)
                    else:
                        losers.append(i)
                for i in winners:
                    for k in _ep_keys(claims[i]):  # type: ignore[arg-type]
                        committed[k] = claims[i].resolution.ident  # type: ignore[union-attr]
            for i in losers:
                self._demote(results[i], claims[i])  # type: ignore[arg-type]

    @staticmethod
    def _demote(result: MatchResult, scored: _Scored) -> None:
        res = scored.resolution
        label = res.check.label if res and res.check else ""
        scored.extra_reasons = [*scored.extra_reasons, "episode also claimed by another file in this batch"]
        scored.rescore(EpisodeCheck(False, label), [])
        result.best = None
        if "episode_conflict" not in result.warnings:
            result.warnings.append("episode_conflict")
        result.candidates.sort(key=lambda c: c.score, reverse=True)
        result.confidence = confidence_from([c.score for c in result.candidates])[0]


def create_matcher(settings: ProviderSettings, *, cache_dir: Path, demo: bool = False) -> Matcher:
    """Build the provider registry + matcher. ``demo=True`` puts the offline ``fixtures``
    provider first in every chain (and makes it the only one that needs no network)."""
    cache = ResponseCache(Path(cache_dir) / CACHE_FILENAME)
    try:  # expired rows are otherwise only replaced when the same key is fetched again
        purged = cache.purge_expired()
        if purged:
            log.info("purged %d expired HTTP cache entries", purged)
    except Exception:  # a broken cache must not stop the matcher from starting
        log.warning("could not purge the HTTP cache", exc_info=True)
    registry = ProviderRegistry(settings, cache=cache, demo=demo)
    return Matcher(registry)
