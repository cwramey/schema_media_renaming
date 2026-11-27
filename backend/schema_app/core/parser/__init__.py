"""Filename parser: path -> ParsedMedia (no network, no disk access).

Contract: docs/SPEC.md §6.1.

Stages: split the path into name + folder context (season folders, movie folders, disc
and clutter folders) -> analyse the file stem (tags, numbering, title, year) -> merge with
the folder context -> weighted kind vote with a confidence. Everything is regex over
short strings, so a parse costs well under a millisecond.

Speed (results never depend on it; tests/metadata/test_parser_golden.py pins them): a literal
prefilter (``_prefilter``) skips the ~230 tag / numbering regexes that cannot match a name,
folder contexts are memoised per folder tuple, and cleaned titles per raw title region.
``tests/metadata/bench_parser.py`` times it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import PurePath
from types import MappingProxyType

from schema_app.core.normalize import normalize_title
from schema_app.domain import MediaKind, ParsedMedia, QualityInfo

from ._core import NameInfo, analyze, clean_title
from ._numbering import NumberingContext
from ._rules import (
    FANSUB_GROUPS,
    KNOWN_EXTENSIONS,
    SIDECAR_EXTENSIONS,
    SUBTITLE_FLAGS,
    SUBTITLE_LANGS,
    TAG_WORDS,
    VIDEO_EXTENSIONS,
)

__all__ = ["parse", "clear_caches", "VIDEO_EXTENSIONS"]

# --------------------------------------------------------------------------- folder vocab

_SEASON_FOLDER = re.compile(
    r"^(?:season|series|saison|staffel|temporada|stagione|seizoen|sezon|s[aæä]son|s[äa]song|kausi)"
    r"[ ._\-]*(\d{1,4})(?:[ ._\-]*[\(\[]?(?:19|20)\d{2}[\)\]]?)?$"
    r"|^s(\d{1,4})$",
    re.IGNORECASE,
)
_SPECIALS_FOLDER = re.compile(r"^(?:specials?|sp|s0+|season[ ._\-]*0+|series[ ._\-]*0+)$", re.IGNORECASE)
_PART_FOLDER = re.compile(r"^(?:cd|dis[ck]|dvd)[ ._\-]*(\d{1,2})$", re.IGNORECASE)
_DISK_FOLDERS = frozenset({"video_ts", "bdmv", "stream", "playlist", "audio_ts", "clipinf", "backup",
                           "certificate", "bdav", "any!", "meta"})
_SAMPLE_FOLDERS = frozenset({"sample", "samples"})
_EXTRA_FOLDERS = frozenset({
    "extras", "extra", "featurettes", "featurette", "behind the scenes", "behindthescenes",
    "deleted scenes", "deletedscenes", "interviews", "interview", "trailers", "trailer", "shorts",
    "scenes", "other", "others", "bonus", "bonus features", "bonus material", "special features",
    "making of", "ncop", "nced", "creditless", "menus", "menu", "pv", "cm",
})
_MOVIE_FOLDERS = frozenset({"movies", "movie", "films", "film", "kino", "cinema", "4k movies",
                            "uhd movies", "hd movies"})
_SERIES_FOLDERS = frozenset({"tv", "tv shows", "tvshows", "tv-shows", "shows", "series", "tv series",
                             "television", "documentary", "documentaries", "sitcoms"})
_ANIME_FOLDERS = frozenset({"anime", "animes", "animation", "japanese animation"})
_GENERIC_FOLDERS = _MOVIE_FOLDERS | _SERIES_FOLDERS | _ANIME_FOLDERS | frozenset({
    "downloads", "download", "complete", "completed", "incoming", "inbox", "finished", "done", "new",
    "media", "video", "videos", "storage", "data", "mnt", "media1", "media2", "share", "shares",
    "public", "home", "users", "user", "desktop", "documents", "torrents", "torrent", "sabnzbd",
    "nzb", "nzbs", "usenet", "unsorted", "unpacked", "temp", "tmp", "watch", "output", "input",
    "library", "plex", "jellyfin", "emby", "renamed", "sorted", "process", "processing", "_unpack",
    "intermediate", "qbittorrent", "transmission", "deluge", "rtorrent", "nzbget", "volume1",
    "volume2", "volumes", "disk1", "disk2", "srv", "opt", "config", "demo", "staging", "pending",
    "_done", "_complete", "seed", "seeding", "misc", "4k", "uhd", "hd", "sd", "1080p", "720p",
    "2160p", "x265", "x264", "remux", "anime-downloads", "tv-downloads", "movie-downloads",
})
_SPECIAL_WORD = re.compile(r"(?i)(?<![a-z])specials?(?![a-z])")
_DRIVE = re.compile(r"^[A-Za-z]:$|^\\\\|^\.+$")
_EXT_LIKE = re.compile(r"[a-z][a-z0-9]{1,3}")
_NUMBER_LIKE = re.compile(r"[a-z]?\d+[a-z]?")
_SXE_LIKE = re.compile(r"s\d+e\d+")
_GARBAGE_TITLE = re.compile(r"^(?:vts[ _]\d+[ _]\d+|\d+|[0-9a-f]{16,}|[A-Za-z0-9]{20,})$", re.IGNORECASE)


@dataclass(frozen=True)
class FolderContext:
    """What the parent folders say about a file. Shared between files (cached): read-only."""

    season: int | None = None
    title: str | None = None
    year: int | None = None
    movie_folder: bool = False  # "Title (Year)" folder with no numbering
    is_sample: bool = False
    is_extra: bool = False
    part: int | None = None
    disk: bool = False
    votes: Mapping[str, int] = field(
        default_factory=lambda: MappingProxyType({"series": 0, "movie": 0, "anime": 0})
    )


def _folder_context(folders: list[str]) -> FolderContext:
    # Only the six innermost folders are read, and every file of a folder shares the result:
    # a big import parses each season / movie folder once instead of once per file.
    return _cached_folder_context(tuple(folders[-6:]))


@lru_cache(maxsize=4096)
def _cached_folder_context(folders: tuple[str, ...]) -> FolderContext:
    season: int | None = None
    title: str | None = None
    year: int | None = None
    movie_folder = is_sample = is_extra = disk = False
    part: int | None = None
    votes = {"series": 0, "movie": 0, "anime": 0}
    title_search = True
    for depth, raw in enumerate(reversed(folders)):
        if depth >= 6:
            break
        name = raw.strip()
        low = name.lower()
        if not name or _DRIVE.match(name):
            title_search = False
            continue
        if low in _MOVIE_FOLDERS:
            votes["movie"] += 2
            votes["series"] -= 1
        elif low in _SERIES_FOLDERS:
            votes["series"] += 2
            votes["movie"] -= 1
        elif low in _ANIME_FOLDERS:
            votes["anime"] += 2
        if not title_search or depth >= 4:
            continue
        if low in _DISK_FOLDERS:
            disk = True
            continue
        pm = _PART_FOLDER.match(name)
        if pm:
            part = part or int(pm.group(1))
            continue
        if low in _SAMPLE_FOLDERS:
            is_sample = True
            continue
        if low in _EXTRA_FOLDERS:
            is_extra = True
            continue
        if _SPECIALS_FOLDER.match(name):
            if season is None:
                season = 0
            continue
        sm = _SEASON_FOLDER.match(name)
        if sm:
            if season is None:
                season = int(sm.group(1) or sm.group(2))
            continue
        if low in _GENERIC_FOLDERS:
            title_search = False
            continue
        info = analyze(name)
        if not info.title or _GARBAGE_TITLE.match(info.title):
            title_search = False
            continue
        title = info.title
        year = info.year
        num = info.numbering
        if num is not None and num.season is not None and season is None and not num.absolute:
            season = num.season
        if num is None and info.year is not None:
            movie_folder = True
        title_search = False
    return FolderContext(
        season=season, title=title, year=year, movie_folder=movie_folder, is_sample=is_sample,
        is_extra=is_extra, part=part, disk=disk, votes=MappingProxyType(votes),
    )


@lru_cache(maxsize=256)
def _numbering_context(season: int | None, show_folder: bool) -> NumberingContext:
    return NumberingContext(season=season, show_folder=show_folder)  # frozen: safe to share


def clear_caches() -> None:
    """Drop the parser's memoised folder contexts and titles (benchmarks; no effect on results)."""
    _cached_folder_context.cache_clear()
    clean_title.cache_clear()


# --------------------------------------------------------------------------- helpers


def _split_name(filename: str) -> tuple[str, str]:
    if "." not in filename.strip("."):
        return filename, ""
    base, ext = filename.rsplit(".", 1)
    low = ext.lower()
    if low in KNOWN_EXTENSIONS or (
        _EXT_LIKE.fullmatch(low)
        and low not in TAG_WORDS
        and not _NUMBER_LIKE.fullmatch(low)
        and not _SXE_LIKE.fullmatch(low)
    ):
        return base, low
    return filename, ""


def _sidecar_languages(stem: str) -> tuple[str, list[str]]:
    """'Show.S01E01.en.forced' -> ('Show.S01E01', ['en'])."""
    langs: list[str] = []
    for _ in range(3):
        if "." not in stem:
            break
        base, suffix = stem.rsplit(".", 1)
        low = suffix.lower()
        if low in SUBTITLE_FLAGS:
            stem = base
            continue
        code = SUBTITLE_LANGS.get(low)
        if code:
            langs.insert(0, code)
            stem = base
            continue
        break
    return stem, langs


_HDR_ORDER = ["DV", "HDR10+", "HDR10", "HLG"]


def _quality(info: NameInfo) -> QualityInfo:
    t = info.tags
    return QualityInfo(
        resolution=t.resolution,
        source=t.source,
        remux=t.remux,
        video_codec=t.video_codec,
        bit_depth=t.bit_depth,
        hdr=sorted(t.hdr, key=_HDR_ORDER.index),
        audio_codec=t.audio_codec,
        audio_channels=t.audio_channels,
        atmos=t.atmos,
        streaming_service=t.streaming_service,
        proper=t.proper,
        repack=t.repack,
        s3d=t.s3d,
    )


_STRICT_KINDS = {"sxe", "words", "nxn"}


def _vote(info: NameInfo, ctx: FolderContext, *, has_title: bool) -> tuple[MediaKind, float]:
    """Weighted kind vote (series / movie / anime) -> (kind, confidence)."""
    series = ctx.votes["series"]
    movie = ctx.votes["movie"]
    anime = ctx.votes["anime"]
    num = info.numbering
    if num is not None:
        if num.kind in _STRICT_KINDS or num.kind == "date":
            series += 5
            movie -= 1
        elif num.kind == "anime_sn":
            series += 3
            movie -= 1
            anime += 1
        elif num.kind == "special":
            series += 1
            movie -= 1
            anime += 2
        elif num.kind == "season":
            series += 2
            movie -= 1
        else:
            series += 1
            movie -= 1
            if num.kind in {"dash", "bare", "hash", "jp"}:
                anime += 1
    if ctx.season is not None:
        series += 2
        movie -= 1
    if info.leading_group:
        anime += 2
    if info.crc32:
        anime += 2
    if info.group and info.group.lower() in FANSUB_GROUPS:
        anime += 1
    if num is not None and num.version:
        anime += 1
    if info.year is not None and num is None:
        movie += 2
        series -= 1
    if info.part is not None or ctx.part is not None:
        movie += 2
        series -= 1
    if info.tags.editions:
        movie += 1
    if ctx.movie_folder and num is None:
        movie += 1
        series -= 1
    if info.no_digits and num is None:
        movie += 1
        series -= 1
    if info.dash_split and num is None:
        series += 1
        movie -= 1
    if num is None and info.year is None and info.tags.quality_tags >= 2:
        movie += 1

    episodic = num is not None or (ctx.season is not None and series >= 1)
    if episodic and anime >= 2 and num is not None:
        kind = MediaKind.ANIME
    elif series >= 1 and movie <= -1:
        kind = MediaKind.ANIME if anime >= 2 else MediaKind.SERIES
    elif movie >= 1 and series <= -1:
        kind = MediaKind.MOVIE
    else:
        kind = MediaKind.UNKNOWN

    if kind in (MediaKind.SERIES, MediaKind.ANIME):
        if num is None:
            conf = 0.45
        elif num.kind in _STRICT_KINDS:
            conf = 0.92
        elif num.kind in {"date", "anime_sn"}:
            conf = 0.88
        elif num.kind == "special":
            conf = 0.75
        elif num.kind == "season":
            conf = 0.6
        else:
            conf = 0.75 if (kind == MediaKind.ANIME or ctx.season is not None) else 0.55
        if num is not None and num.suspicious:
            conf -= 0.15
    elif kind == MediaKind.MOVIE:
        conf = 0.55 + min(0.35, 0.08 * (movie - 1))
        if info.year is not None:
            conf = max(conf, 0.8)
            if info.tags.quality_tags:
                conf += 0.05
    else:
        conf = 0.3 if has_title else 0.1
        if abs(series - movie) <= 1 and (series or movie):
            conf = 0.35
    if not has_title:
        conf -= 0.3
    return kind, round(max(0.0, min(1.0, conf)), 3)


# --------------------------------------------------------------------------- public API


def parse(path: str | PurePath) -> ParsedMedia:
    """Parse one path. Uses parent folder names as context (season folders, movie folders).

    Must be pure and fast (< 1 ms typical): it runs for every file and for every
    keystroke-driven preview in the UI.
    """
    raw = str(path)
    parts = [p for p in raw.replace("\\", "/").split("/") if p]  # = re.split(r"[\\/]+") minus empties
    filename = parts[-1] if parts else raw
    folders = parts[:-1]

    stem, ext = _split_name(filename)
    sidecar_langs: list[str] = []
    if ext in SIDECAR_EXTENSIONS and ext not in {"nfo", "txt"}:
        stem, sidecar_langs = _sidecar_languages(stem)

    fctx = _folder_context(folders)
    ctx = _numbering_context(fctx.season, bool(fctx.title))
    info = analyze(stem, ctx=ctx)
    num = info.numbering

    title = info.title
    alt_titles: list[str] = []
    year = info.year
    folder_title = fctx.title
    title_from_folder = False

    if not title or _GARBAGE_TITLE.match(title) or (fctx.disk and folder_title):
        if folder_title:
            title = folder_title
            title_from_folder = True
            if year is None:
                year = fctx.year
    elif folder_title:
        same = normalize_title(folder_title) == normalize_title(title)
        if not same:
            alt_titles.append(folder_title)
        if year is None and fctx.year is not None and (same or (fctx.movie_folder and num is None)):
            year = fctx.year

    special_title: str | None = None
    episode_title = info.episode_title if num is not None else None
    special = False
    season = None
    episodes: list[int] = []
    absolute: list[int] = []
    air_date = None
    if num is not None:
        season = num.season
        episodes = list(num.episodes)
        absolute = list(num.absolute)
        air_date = num.air_date
        special = num.special
        if num.kind == "special" and season is None and fctx.season == 0:
            season = 0
        if num.kind == "season" and fctx.season is not None and season is None:
            season = fctx.season
        if special:
            special_title = info.episode_title
    elif fctx.season is not None:
        season = fctx.season
        special = fctx.season == 0

    if num is None and info.dash_split and not title_from_folder:
        left, right = info.dash_split
        alt_titles.insert(0, title or left)
        title = left
        special_title = right
        if _SPECIAL_WORD.search(right):
            special = True
    elif num is None and special and info.title and title_from_folder:
        special_title = info.title

    if title and alt_titles:
        alt_titles = [a for a in dict.fromkeys(alt_titles) if a and a != title]

    kind, confidence = _vote(info, fctx, has_title=bool(title))
    if title_from_folder:
        confidence = max(0.0, round(confidence - 0.05, 3))

    languages = list(dict.fromkeys(info.tags.languages + sidecar_langs))
    # Words like "Interview" / "Reel" after an episode number are usually its episode title
    # ("The Office S02E03 The Interview"), not a bonus clip.
    episodic_name = bool(episodes or absolute or air_date)
    is_extra = info.tags.is_extra or info.plex_extra or fctx.is_extra or (info.tags.extra_word and not episodic_name)

    return ParsedMedia(
        path=raw,
        filename=filename,
        extension=ext,
        kind=kind,
        title=title,
        alt_titles=alt_titles,
        year=year,
        season=season,
        episodes=episodes,
        absolute=absolute,
        air_date=air_date,
        special=special,
        special_title=special_title,
        episode_title=episode_title,
        part=info.part if info.part is not None else fctx.part,
        edition=info.tags.editions[0] if info.tags.editions else None,
        release_group=info.group,
        crc32=info.crc32,
        version=num.version if num is not None else None,
        quality=_quality(info),
        languages=languages,
        is_sample=info.is_sample or fctx.is_sample,
        is_extra=is_extra,
        folder_title=folder_title,
        confidence=confidence,
        tokens=info.tokens,
    )


def clean(text: str) -> str | None:
    """Public helper: tidy a free-text title the same way the parser does."""
    return clean_title(text)
