"""Core domain models shared by every Schema module.

This file is the cross-team contract (see docs/SPEC.md §4). The parser produces
``ParsedMedia``; providers produce ``MovieInfo`` / ``SeriesInfo`` / ``EpisodeInfo``;
the matcher produces ``MatchResult``; file operations consume ``FileOpPlan``.

Changing a field here is an API change: update docs/SPEC.md alongside it.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- enums


class MediaKind(StrEnum):
    MOVIE = "movie"
    SERIES = "series"
    ANIME = "anime"
    UNKNOWN = "unknown"


class EpisodeOrder(StrEnum):
    AIRED = "aired"  # default / official broadcast order
    DVD = "dvd"
    ABSOLUTE = "absolute"


class RenameMode(StrEnum):
    MOVE = "move"
    COPY = "copy"
    HARDLINK = "hardlink"
    SYMLINK = "symlink"
    KEEPLINK = "keeplink"  # move, then leave a symlink at the original location (seeding torrents)
    TEST = "test"  # dry run: plan + validate only, touch nothing


class ConflictPolicy(StrEnum):
    SKIP = "skip"  # leave the source untouched, mark item as conflict
    OVERWRITE = "overwrite"  # replace the existing destination
    UPGRADE = "upgrade"  # replace only if the incoming file ranks higher (QualityInfo.rank)
    INDEX = "index"  # append " (1)", " (2)", ... to the destination stem
    FAIL = "fail"  # abort the item with an error


# --------------------------------------------------------------------------- parsing

_RESOLUTION_RANK = {"4320p": 7, "2160p": 6, "1440p": 5, "1080p": 4, "720p": 3, "576p": 2, "480p": 1}
_SOURCE_RANK = {
    "BluRay": 6,
    "WEB-DL": 5,
    "WEBRip": 4,
    "HDTV": 3,
    "DVD": 2,
    "SDTV": 1,
    "VHS": 0,
    "Telesync": -1,
    "CAM": -2,
}


class QualityInfo(BaseModel):
    """Release/quality attributes recovered from a filename (or a media probe)."""

    resolution: str | None = None  # canonical: 480p 576p 720p 1080p 2160p 4320p
    source: str | None = None  # canonical: BluRay WEB-DL WEBRip HDTV DVD SDTV VHS Telesync CAM
    remux: bool = False
    video_codec: str | None = None  # canonical: x264 x265 AV1 VP9 XviD DivX VC-1 MPEG-2
    bit_depth: int | None = None  # 8 / 10 / 12
    hdr: list[str] = Field(default_factory=list)  # HDR10 HDR10+ DV HLG
    audio_codec: str | None = None  # AAC AC3 DDP TrueHD DTS DTS-HD MA DTS-X FLAC Opus MP3
    audio_channels: str | None = None  # 2.0 5.1 7.1
    atmos: bool = False
    streaming_service: str | None = None  # AMZN NF DSNP HMAX ATVP HULU PCOK ...
    proper: bool = False
    repack: bool = False
    s3d: str | None = None  # SBS / TAB / OU (stereoscopic 3D)

    def rank(self) -> int:
        """Comparable quality score used by ConflictPolicy.UPGRADE and multi-version handling.

        Resolution dominates, then remux, then source, then HDR / audio niceties.
        """
        score = _RESOLUTION_RANK.get(self.resolution or "", 0) * 1000
        score += 500 if self.remux else 0
        score += _SOURCE_RANK.get(self.source or "", 0) * 50
        score += 30 if self.hdr else 0
        score += 20 if self.atmos else 0
        score += 10 if (self.proper or self.repack) else 0
        score += (self.bit_depth or 8) - 8
        return score


class ParsedMedia(BaseModel):
    """Everything the filename parser could recover from a path, before any provider lookup."""

    path: str  # full path exactly as given
    filename: str  # basename including extension
    extension: str  # lowercase, no dot ("mkv")
    kind: MediaKind = MediaKind.UNKNOWN  # parser's guess
    title: str | None = None  # cleaned query title ("Starfall Station")
    alt_titles: list[str] = Field(default_factory=list)  # other usable query titles (folder name, aka)
    year: int | None = None
    season: int | None = None  # 0 means specials
    episodes: list[int] = Field(default_factory=list)  # seasonal numbers; >1 entry = multi-episode
    absolute: list[int] = Field(default_factory=list)  # absolute numbers (anime style)
    air_date: date | None = None  # daily shows ("2024.03.04")
    special: bool = False  # S00 / OVA / OAD / SP / "Special"
    special_title: str | None = None  # e.g. "Christmas Special" when no number is present
    episode_title: str | None = None  # text after the numbering ("S01E02 - Dia de los Dangerous")
    part: int | None = None  # CD1 / Part 2 for multi-file movies
    edition: str | None = None  # "Director's Cut", "Extended", "Remastered", "IMAX" ...
    release_group: str | None = None
    crc32: str | None = None  # 8 uppercase hex chars found in the filename
    version: int | None = None  # anime release revision: "05v2" -> 2
    quality: QualityInfo = Field(default_factory=QualityInfo)
    languages: list[str] = Field(default_factory=list)  # ISO 639-1 codes (MULTi, GERMAN, ".en.srt")
    is_sample: bool = False
    is_extra: bool = False  # trailer, featurette, NCOP/NCED, behind the scenes ...
    folder_title: str | None = None  # title guessed from parent folder(s)
    confidence: float = 0.0  # 0..1 parser confidence in kind + title
    tokens: list[str] = Field(default_factory=list)  # leftover unrecognised tokens (debug aid)


class ProbeInfo(BaseModel):
    """Technical facts read from the container itself (mediainfo / ffprobe). All optional."""

    container: str | None = None
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    video_codec: str | None = None
    bit_depth: int | None = None
    frame_rate: float | None = None
    hdr: list[str] = Field(default_factory=list)
    audio_codec: str | None = None
    audio_channels: str | None = None
    audio_languages: list[str] = Field(default_factory=list)
    subtitle_languages: list[str] = Field(default_factory=list)
    bitrate_kbps: int | None = None


# --------------------------------------------------------------------------- metadata


class ProviderRef(BaseModel):
    provider: str  # "tmdb" "tvdb" "anidb" "trakt" "tvmaze" "fixtures"
    id: str

    def key(self) -> str:
        return f"{self.provider}:{self.id}"


class MovieInfo(BaseModel):
    ref: ProviderRef
    title: str
    original_title: str | None = None
    year: int | None = None
    release_date: date | None = None
    aliases: list[str] = Field(default_factory=list)
    overview: str | None = None
    poster_url: str | None = None
    ids: dict[str, str] = Field(default_factory=dict)  # {"tmdb": "603", "imdb": "tt0133093"}
    popularity: float | None = None
    runtime_minutes: int | None = None
    genres: list[str] = Field(default_factory=list)
    collection: str | None = None
    certification: str | None = None
    rating: float | None = None
    original_language: str | None = None


class SeriesInfo(BaseModel):
    ref: ProviderRef
    title: str
    original_title: str | None = None
    year: int | None = None
    first_aired: date | None = None
    aliases: list[str] = Field(default_factory=list)
    overview: str | None = None
    poster_url: str | None = None
    ids: dict[str, str] = Field(default_factory=dict)
    popularity: float | None = None
    status: str | None = None  # Continuing / Ended
    network: str | None = None
    genres: list[str] = Field(default_factory=list)
    is_anime: bool = False
    season_count: int | None = None
    certification: str | None = None
    rating: float | None = None
    original_language: str | None = None


class EpisodeInfo(BaseModel):
    series_ref: ProviderRef
    season: int | None = None  # 0 = specials
    episode: int | None = None
    absolute: int | None = None
    title: str | None = None
    air_date: date | None = None
    ids: dict[str, str] = Field(default_factory=dict)

    @property
    def special(self) -> bool:
        return self.season == 0


class Candidate(BaseModel):
    """One possible identity for a file, scored by the matcher."""

    id: str  # stable: f"{provider}:{kind}:{provider_id}", e.g. "tmdb:movie:603"
    kind: MediaKind
    provider: str
    title: str
    year: int | None = None
    score: float  # 0..1
    movie: MovieInfo | None = None
    series: SeriesInfo | None = None
    episodes: list[EpisodeInfo] = Field(default_factory=list)  # episodes this file maps to
    episode_order: EpisodeOrder | None = None
    part: int | None = None  # "Title Part 2" release split of one provider episode (named "- part2")
    reasons: list[str] = Field(default_factory=list)  # human-readable scoring notes


class MatchHint(BaseModel):
    """Manual or contextual overrides passed into the matcher."""

    kind: MediaKind | None = None
    provider: str | None = None
    query: str | None = None
    title: str | None = None  # alias of ``query`` (the show/movie title to search for)
    year: int | None = None
    season: int | None = None
    episodes: list[int] | None = None
    absolute: list[int] | None = None
    ref: ProviderRef | None = None  # force a specific show/movie
    episode_order: EpisodeOrder | None = None


class MatchResult(BaseModel):
    candidates: list[Candidate] = Field(default_factory=list)  # best first, at most ~10
    best: Candidate | None = None  # candidates[0] when it resolved (episodes found for episodic kinds)
    confidence: float = 0.0  # 0..1; the pipeline auto-approves when >= settings.min_confidence
    warnings: list[str] = Field(default_factory=list)
    providers_tried: list[str] = Field(default_factory=list)
    provider_errors: dict[str, str] = Field(default_factory=dict)  # provider -> error, for fallbacks


# --------------------------------------------------------------------------- configuration


class ProviderCredentials(BaseModel):
    tmdb_api_key: str | None = None  # v3 key, or v4 read-access token (JWT, starts with "eyJ")
    tvdb_api_key: str | None = None
    tvdb_pin: str | None = None  # subscriber PIN (optional)
    anidb_client: str | None = None  # registered HTTP API client name
    anidb_client_version: int | None = None
    trakt_client_id: str | None = None


def default_priority() -> dict[MediaKind, list[str]]:
    # TMDb leads for series (free key; FileBot also defaults to TheMovieDB::TV). AniDB leads anime when configured because it
    # owns absolute numbering; unconfigured providers are skipped, so order only matters among
    # the ones that have credentials.
    return {
        MediaKind.MOVIE: ["tmdb", "trakt"],
        MediaKind.SERIES: ["tmdb", "tvdb", "tvmaze", "trakt"],
        MediaKind.ANIME: ["anidb", "tmdb", "tvdb", "tvmaze"],
    }


class ProviderSettings(BaseModel):
    credentials: ProviderCredentials = Field(default_factory=ProviderCredentials)
    language: str = "en"
    priority: dict[MediaKind, list[str]] = Field(default_factory=default_priority)
    disabled: list[str] = Field(default_factory=list)


class ProviderStatus(BaseModel):
    name: str  # "tmdb"
    label: str  # "The Movie Database"
    kinds: list[MediaKind]
    requires_key: bool
    configured: bool
    enabled: bool
    healthy: bool | None = None  # None = never checked
    last_error: str | None = None
    last_checked: datetime | None = None
    rate_limit: str | None = None  # human readable, e.g. "1 request / 2 s"
    requests_made: int = 0
    cache_hits: int = 0


class PathMapping(BaseModel):
    """Translate a path as seen by another system into the path Schema sees (or vice versa)."""

    source: str  # e.g. "/downloads" (inside the qBittorrent container)
    target: str  # e.g. "/data/downloads" (inside the Schema container)


class MediaServerConfig(BaseModel):
    id: str  # uuid4 hex
    kind: str  # "plex" | "jellyfin" | "emby"
    name: str
    url: str  # e.g. "http://192.168.1.20:32400"
    token: str  # Plex token or Jellyfin/Emby API key
    enabled: bool = True
    path_mappings: list[PathMapping] = Field(default_factory=list)  # Schema path -> server path


class WatchFolderSpec(BaseModel):
    """What the watcher service needs to know about one watched folder."""

    id: int
    path: str
    recursive: bool = True
    enabled: bool = True
    settle_seconds: float = 30  # a file is "ready" once size+mtime are unchanged this long
    kind_hint: MediaKind | None = None


# --------------------------------------------------------------------------- file operations


class SidecarMove(BaseModel):
    source: str
    destination: str


class FolderRename(BaseModel):
    """The source's own folder renamed in place ("Alien.1979" -> "Alien (1979)") instead of moving
    the file out of it into a new folder."""

    source: str
    destination: str


class FileOpPlan(BaseModel):
    source: str
    destination: str  # absolute, sanitized
    mode: RenameMode
    policy: ConflictPolicy
    sidecars: list[SidecarMove] = Field(default_factory=list)
    conflict: str = "none"  # "none" | "exists" | "same_file" | "duplicate_in_batch"
    incoming_rank: int | None = None  # QualityInfo.rank() of the source, for UPGRADE
    existing_rank: int | None = None
    folder_rename: FolderRename | None = None  # move only: rename the folder, then the file inside it
    remove_empty_dirs: bool = False  # move only: remove the source folder (and empty parents) left empty
    protected_dirs: list[str] = Field(default_factory=list)  # library / watch roots: never renamed or removed


class FileOpResult(BaseModel):
    ok: bool
    mode: RenameMode
    source: str
    destination: str  # final destination (may differ from the plan under ConflictPolicy.INDEX)
    sidecars: list[SidecarMove] = Field(default_factory=list)  # sidecars actually moved
    bytes: int = 0
    verified: bool = False  # size (and hash when cross-device) verified after transfer
    skipped: bool = False
    replaced: str | None = None  # path of a file displaced by OVERWRITE/UPGRADE (kept in trash dir)
    dest_mtime_ns: int | None = None  # destination mtime after the operation; undo refuses if it changed
    folder_rename: FolderRename | None = None  # the folder renamed before the file; undo renames it back last
    removed_dirs: list[str] = Field(default_factory=list)  # empty source folders removed, innermost first
    warnings: list[str] = Field(default_factory=list)  # notes on success (sidecar left behind, tie on size ...)
    error: str | None = None
    error_code: str | None = None  # typed failure: "source_missing" | "destination_exists" | "error"
    duration_ms: int = 0
