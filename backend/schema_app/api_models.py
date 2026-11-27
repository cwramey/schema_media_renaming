"""HTTP/WebSocket API contract (request and response bodies).

The frontend mirrors these models in ``frontend/src/api/types.ts``. Routes are
listed in docs/SPEC.md §5. Field names are snake_case on the wire.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from schema_app.domain import (
    Candidate,
    ConflictPolicy,
    FolderRename,
    MatchHint,
    MediaKind,
    MediaServerConfig,
    ParsedMedia,
    PathMapping,
    ProbeInfo,
    ProviderStatus,
    RenameMode,
    SidecarMove,
)

SECRET_MASK = "********"  # GET responses mask secrets with this; PUT with this value keeps the stored secret


# --------------------------------------------------------------------------- auth


class AuthStatus(BaseModel):
    setup_required: bool  # no admin account exists yet -> UI shows first-run setup
    authenticated: bool
    username: str | None = None
    auth_enabled: bool  # False when SCHEMA_AUTH=disabled
    demo: bool  # SCHEMA_DEMO=1
    version: str
    setup_token_required: bool = False  # setup needs the one-time code printed in the container log


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=256)


class SetupCredentials(Credentials):
    setup_token: str | None = Field(default=None, max_length=256)


# --------------------------------------------------------------------------- jobs & items


class JobStatus(StrEnum):
    QUEUED = "queued"
    MATCHING = "matching"
    REVIEW = "review"  # matching finished; at least one item awaits a human
    READY = "ready"  # matching finished; everything is approved/skipped, waiting for execute
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ItemStatus(StrEnum):
    PENDING = "pending"  # not processed yet
    MATCHED = "matched"  # confident match, will run on execute
    REVIEW = "review"  # needs a human (low confidence, conflict, no match, CRC mismatch ...)
    APPROVED = "approved"  # human confirmed
    SKIPPED = "skipped"  # excluded (sample, extra, user choice)
    EXECUTING = "executing"
    DONE = "done"
    ERROR = "error"


class JobSource(StrEnum):
    MANUAL = "manual"
    WATCH = "watch"
    WEBHOOK = "webhook"


class JobCreate(BaseModel):
    paths: list[str] = Field(min_length=1)  # files and/or folders (folders are scanned recursively)
    kind: MediaKind | None = None  # force media kind for every file
    mode: RenameMode | None = None  # default: settings.default_mode
    format_id: int | None = None  # default: the default format for each item's kind
    dest_root: str | None = None  # default: settings.library_roots[kind]
    auto_execute: bool = False  # execute MATCHED items as soon as matching completes
    label: str | None = None


class JobItemOut(BaseModel):
    id: int
    job_id: int
    status: ItemStatus
    source_path: str
    filename: str
    parsed: ParsedMedia
    candidates: list[Candidate] = Field(default_factory=list)
    selected_candidate_id: str | None = None
    confidence: float = 0.0
    proposed_path: str | None = None  # absolute destination
    proposed_relative: str | None = None  # what the format rendered (relative to the dest root)
    path_overridden: bool = False  # user typed the destination by hand
    final_path: str | None = None  # where the file actually ended up
    folder_rename: FolderRename | None = None  # move: its folder is (or was) renamed in place, not left behind
    sidecars: list[str] = Field(default_factory=list)  # companion files that will travel with it
    crc_ok: bool | None = None  # None = no CRC in the name or not checked; False = mismatch
    probe: ProbeInfo | None = None  # mediainfo/ffprobe facts when probing ran
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    updated_at: datetime
    # Total candidates stored for the item (the top 3; only the selected one once the item is done or
    # skipped). In the slim form (list pages, ``item.updated`` pushes) ``candidates`` holds at most the
    # selected one, stripped of overview/aliases/genres/ids/reasons, and ``probe`` is left out;
    # GET /api/items/{id} returns the full item.
    candidate_count: int = 0
    slim: bool = False


class JobOut(BaseModel):
    id: int
    label: str | None = None
    source: JobSource
    status: JobStatus
    kind: MediaKind | None = None
    mode: RenameMode
    format_id: int | None = None
    dest_root: str | None = None
    auto_execute: bool = False
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None
    counts: dict[str, int] = Field(default_factory=dict)  # ItemStatus value -> count
    total: int = 0
    progress: float = 0.0  # 0..1 for the current phase
    error: str | None = None


class JobDetail(JobOut):
    """GET /api/jobs/{id}. ``items`` is one page (see ``limit``/``offset``/``after``); ``counts`` and
    ``total`` always cover the whole job."""

    items: list[JobItemOut] = Field(default_factory=list)
    items_total: int = 0  # items matching the item filter (status/q); == total when unfiltered
    next_after: int | None = None  # pass as ``after`` to fetch the next page; None = no more items


class JobItemPage(BaseModel):
    """GET /api/jobs/{id}/items: one page of (slim by default) items, ordered by id."""

    job_id: int
    items: list[JobItemOut] = Field(default_factory=list)
    total: int = 0  # items matching the filter
    offset: int = 0
    limit: int
    next_after: int | None = None  # cursor for the next page; None = this was the last page
    counts: dict[str, int] = Field(default_factory=dict)  # ItemStatus value -> count, whole job


class ItemUpdate(BaseModel):
    """PATCH /api/items/{id}: pick another candidate, approve/skip, or hand-edit the path."""

    selected_candidate_id: str | None = None
    status: Literal["approved", "skipped", "review"] | None = None
    proposed_path: str | None = None  # absolute, or relative to the job's dest root
    reset_path: bool = False  # drop a manual path override and re-render the format


class ItemRematch(BaseModel):
    """POST /api/items/{id}/rematch: re-run matching with overrides (kind, provider, show, S/E ...)."""

    hint: MatchHint


class ItemSearch(BaseModel):
    """POST /api/items/{id}/search: free-text search to populate the override picker."""

    query: str = Field(min_length=1)
    kind: MediaKind
    year: int | None = None
    provider: str | None = None


class BulkItemAction(BaseModel):
    """Act on explicit ``item_ids``, or (``item_ids`` omitted) on every item of ``job_id`` that
    matches ``statuses``/``q`` server-side, so "approve all" works without loading every item.
    The filter form answers with the slim items, or with ``[]`` when more than 1000 matched (the
    ``job.items_changed`` push tells the client to refetch)."""

    item_ids: list[int] | None = Field(default=None, min_length=1)
    action: Literal["approve", "skip", "review", "rematch"]
    job_id: int | None = None
    statuses: list[ItemStatus] | None = None  # filter for the job_id form; None = every status
    q: str | None = Field(default=None, max_length=500)  # filter for the job_id form (name/path/title)

    @model_validator(mode="after")
    def _target(self) -> BulkItemAction:
        if self.item_ids is None and self.job_id is None:
            raise ValueError("give item_ids or job_id")
        return self


class ExecuteRequest(BaseModel):
    item_ids: list[int] | None = None  # default: every MATCHED + APPROVED item in the job
    mode: RenameMode | None = None  # override the job's mode for this run
    q: str | None = Field(default=None, max_length=500)  # without item_ids: only runnable items matching this text


class PreviewRequest(BaseModel):
    """POST /api/preview: stateless parse+match+format for ad-hoc names (no job, no files touched)."""

    names: list[str] = Field(min_length=1, max_length=200)
    kind: MediaKind | None = None
    format_id: int | None = None
    expression: str | None = None  # overrides format_id


class PreviewItem(BaseModel):
    name: str
    parsed: ParsedMedia
    best: Candidate | None = None
    confidence: float = 0.0
    rendered: str | None = None
    error: str | None = None


# --------------------------------------------------------------------------- formats


class FormatIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: MediaKind
    expression: str = Field(min_length=1, max_length=4000)
    is_default: bool = False


class FormatOut(FormatIn):
    id: int
    builtin: bool = False  # seeded preset; editable copies are created instead of mutating it
    created_at: datetime
    updated_at: datetime


class FormatIssue(BaseModel):
    message: str
    start: int  # character offsets into the expression (for editor squiggles)
    end: int
    severity: Literal["error", "warning"] = "error"


class FormatSample(BaseModel):
    source: str  # sample filename
    output: str | None = None
    error: str | None = None


class FormatValidateRequest(BaseModel):
    expression: str
    kind: MediaKind
    samples: list[str] | None = None  # filenames; None = built-in samples for the kind


class FormatValidateResponse(BaseModel):
    ok: bool
    issues: list[FormatIssue] = Field(default_factory=list)
    samples: list[FormatSample] = Field(default_factory=list)


class BindingSpec(BaseModel):
    name: str  # "s00e00", "plex.id", "vf"
    description: str
    kinds: list[MediaKind]
    group: str  # "Naming" "Episode" "Movie" "Quality" "IDs" "File" "Presets"
    example: str
    type: str = "str"  # str | int | date | list | object | bool


class FilterSpec(BaseModel):
    name: str  # "pad"
    signature: str  # "pad(width)"
    description: str
    example: str  # "{e.pad(3)} -> 007"


class FormatPreset(BaseModel):
    id: str  # "plex-series"
    name: str
    kind: MediaKind
    expression: str
    description: str


class FormatCatalog(BaseModel):
    bindings: list[BindingSpec]
    filters: list[FilterSpec]
    presets: list[FormatPreset]


# --------------------------------------------------------------------------- operations (history)


class OperationOut(BaseModel):
    id: int
    job_id: int | None = None
    item_id: int | None = None
    mode: RenameMode
    source: str
    destination: str
    status: Literal["done", "failed", "undone", "undo_failed"]
    bytes: int = 0
    sidecars: list[SidecarMove] = Field(default_factory=list)
    folder_rename: FolderRename | None = None  # the source folder was renamed in place
    removed_dirs: list[str] = Field(default_factory=list)  # empty source folders removed after the move
    error: str | None = None
    created_at: datetime
    undone_at: datetime | None = None


# --------------------------------------------------------------------------- watch folders


class WatchFolderIn(BaseModel):
    path: str
    enabled: bool = True
    recursive: bool = True
    kind: MediaKind | None = None  # hint for every file found here
    auto_execute: bool = True  # watch folders are automation: execute confident matches
    settle_seconds: int = Field(default=30, ge=1, le=3600)
    format_id: int | None = None
    dest_root: str | None = None
    mode: RenameMode | None = None


class WatchFolderOut(WatchFolderIn):
    id: int
    status: Literal["watching", "stopped", "error", "missing"]
    last_event_at: datetime | None = None
    last_error: str | None = None
    files_seen: int = 0


# --------------------------------------------------------------------------- settings & providers


class AppSettings(BaseModel):
    """GET/PUT /api/settings. Secrets are masked with SECRET_MASK on GET."""

    library_roots: dict[MediaKind, str] = Field(default_factory=dict)  # movie/series/anime -> dest root
    default_mode: RenameMode = RenameMode.MOVE
    conflict_policy: ConflictPolicy = ConflictPolicy.SKIP
    min_confidence: float = Field(default=0.85, ge=0, le=1)
    verify_crc: bool = True  # check [ABCD1234] filename CRCs before matching
    probe_media: bool = True  # use mediainfo/ffprobe when available
    move_sidecars: bool = True
    rename_folders_in_place: bool = True  # move: rename the movie's own folder instead of creating a new one
    remove_empty_source_folders: bool = True  # move: delete source folders left empty (never a root)
    # move: once every video of a download folder is moved (or skipped as a sample / extra), move the
    # folder with its leftovers (.nfo, .jpg, extras, samples) to .schema-trash/leftovers for a day.
    clean_release_folders: bool = True
    # watch folders: wait until a whole download folder has been unchanged this long (0 = off).
    release_quiet_seconds: int = Field(default=120, ge=0, le=3600)
    sidecar_extensions: list[str] = Field(
        default_factory=lambda: ["srt", "ass", "ssa", "sub", "idx", "sup", "vtt", "nfo"]
    )
    video_extensions: list[str] = Field(
        default_factory=lambda: ["mkv", "mp4", "m4v", "avi", "mov", "wmv", "ts", "m2ts", "webm", "mpg", "mpeg", "flv", "ogm"]
    )
    ignore_patterns: list[str] = Field(default_factory=lambda: ["*.part", "*.!qB", "*.tmp", "*.crdownload"])
    skip_samples: bool = True
    skip_extras: bool = True
    allowed_roots: list[str] = Field(default_factory=list)  # file browser + file-op safety boundary
    language: str = "en"
    media_servers: list[MediaServerConfig] = Field(default_factory=list)
    category_kinds: dict[str, MediaKind] = Field(
        default_factory=lambda: {
            "tv": MediaKind.SERIES,
            "sonarr": MediaKind.SERIES,
            "movies": MediaKind.MOVIE,
            "radarr": MediaKind.MOVIE,
            "anime": MediaKind.ANIME,
        }
    )  # download-client category -> kind hint
    path_mappings: list[PathMapping] = Field(default_factory=list)  # webhook paths -> local paths
    webhook_api_key: str = SECRET_MASK  # read-only here; rotate via POST /api/settings/api-key
    # "Clear solved problems from Home": hours a solved problem (or a stale error event) stays on the
    # Home board. 0 = right away, 24 = 1 day, 168 = 7 days, None = never.
    clear_solved_after_hours: int | None = Field(default=24, ge=0, le=8760)


class ProvidersOut(BaseModel):
    statuses: list[ProviderStatus]
    priority: dict[MediaKind, list[str]]
    disabled: list[str]
    language: str
    credentials: dict[str, str | None]  # ProviderCredentials field -> masked value (or None if unset)


class ProvidersIn(BaseModel):
    priority: dict[MediaKind, list[str]] | None = None
    disabled: list[str] | None = None
    language: str | None = None
    credentials: dict[str, str | None] | None = None  # partial update; SECRET_MASK keeps existing


class MediaServerTest(BaseModel):
    ok: bool
    message: str


# --------------------------------------------------------------------------- dashboard, events, fs


class EventOut(BaseModel):
    id: int
    ts: datetime
    level: Literal["debug", "info", "warning", "error"]
    category: str  # "job" "watcher" "provider" "fileops" "webhook" "mediaserver" "system"
    message: str
    job_id: int | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class DashboardTotals(BaseModel):
    files_renamed_today: int = 0
    files_renamed_total: int = 0
    bytes_moved_total: int = 0
    items_pending_review: int = 0


class DashboardOut(BaseModel):
    queue: dict[str, int]  # JobStatus value -> count
    totals: DashboardTotals
    active_jobs: list[JobOut]
    recent_operations: list[OperationOut]
    recent_errors: list[EventOut]
    watchers: list[WatchFolderOut]
    providers: list[ProviderStatus]


class FsEntry(BaseModel):
    name: str
    path: str
    is_dir: bool
    size: int | None = None
    modified: datetime | None = None
    is_video: bool = False


class FsListing(BaseModel):
    path: str
    parent: str | None = None
    entries: list[FsEntry]
    roots: list[str]  # settings.allowed_roots


class WebhookAccepted(BaseModel):
    job_id: int
    paths: list[str]


# --------------------------------------------------------------------------- home (flow board)


class IncomingFile(BaseModel):
    """A file in a watch folder that is not a job yet."""

    folder_id: int
    path: str
    name: str
    size: int = 0
    state: Literal["settling", "partial"]  # partial = still downloading (.part, .!qB ...)
    settle_seconds: int
    stable_since: datetime | None = None  # when size/mtime last changed (None for partial)
    settles_at: datetime | None = None  # stable_since + settle_seconds (None for partial)
    title_guess: str | None = None  # "Never After Dark (2026)"


NeedsYouReason = Literal[
    "low_confidence", "conflict", "extra", "sample", "no_match", "crc_mismatch", "error", "interrupted", "event",
    "source_missing", "episode_mismatch",
]
HomeActionName = Literal["skip", "approve", "retry", "dismiss"]


class HomeAction(BaseModel):
    label: str
    action: HomeActionName


class NeedsYouCard(BaseModel):
    key: str  # "item:123" | "event:45" | "group:7" (one job's files that vanished before the rename)
    kind: Literal["item", "event", "group"]
    item_id: int | None = None
    job_id: int | None = None
    event_id: int | None = None
    count: int | None = None  # group cards: how many files the card stands for
    title: str  # file name for items; short message for events; summary for groups
    reason: str  # plain English
    reason_code: NeedsYouReason
    primary: HomeAction | None = None
    candidate_title: str | None = None
    confidence: float | None = None
    ts: datetime


class RenamingCard(BaseModel):
    item_id: int
    job_id: int
    title: str
    filename: str
    status: Literal["matching", "matched", "approved", "executing"]
    job_progress: float | None = None


CardKind = Literal["movie", "series", "anime", "unknown"]


class LibraryCard(BaseModel):
    operation_id: int
    title: str
    kind: CardKind
    destination: str
    ts: datetime
    bytes: int = 0
    can_undo: bool = False
    # Series episodes: the matched show ("Starfall Station (2016)") and its numbers, so Home can fold
    # a run of episodes of one show into one row. None for movies and unmatched files.
    show: str | None = None
    season: int | None = None
    episode: int | None = None


class LibrarySummary(BaseModel):
    recent: list[LibraryCard] = Field(default_factory=list)  # <= 12, newest first
    total_files: int = 0
    total_bytes: int = 0


class HomeWatchFolder(BaseModel):
    id: int
    path: str
    status: Literal["watching", "stopped", "error", "missing"]
    enabled: bool


class HomeMediaServer(BaseModel):
    id: str
    kind: Literal["jellyfin", "plex", "emby"]
    name: str
    url: str
    enabled: bool


class HomeStatus(BaseModel):
    watch: list[HomeWatchFolder] = Field(default_factory=list)
    providers: list[ProviderStatus] = Field(default_factory=list)
    media_servers: list[HomeMediaServer] = Field(default_factory=list)


class HomeOut(BaseModel):
    """GET /api/home: everything the Home flow board shows (polled every few seconds)."""

    needs_you_count: int = 0
    incoming: list[IncomingFile] = Field(default_factory=list)
    needs_you: list[NeedsYouCard] = Field(default_factory=list)  # newest first, max 50
    renaming: list[RenamingCard] = Field(default_factory=list)
    library: LibrarySummary = Field(default_factory=LibrarySummary)
    status: HomeStatus = Field(default_factory=HomeStatus)
    server_time: datetime


class HomeActionIn(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    action: HomeActionName


class OkOut(BaseModel):
    ok: bool = True


class StartNowIn(BaseModel):
    path: str | None = None  # one settling file; omitted = every settling file in the folder


class StartNowOut(BaseModel):
    started: int


class HomeClearedOut(BaseModel):
    cleared: int  # how many review cards were dismissed


class HomeRefreshOut(HomeClearedOut):
    started: int  # how many files in the watch folders were started without waiting


# --------------------------------------------------------------------------- history


class HistoryEntry(BaseModel):
    key: str  # "op:12" | "item:34"
    type: Literal["moved", "skipped", "problem", "undone"]
    ts: datetime
    title: str
    kind: CardKind
    source: str
    destination: str | None = None
    detail: str | None = None  # "Matched Blade Runner (1982)" / "A file with that name already exists"
    bytes: int = 0
    mode: str | None = None  # rename mode, for moved / undone entries
    operation_id: int | None = None
    item_id: int | None = None
    job_id: int | None = None
    can_undo: bool = False
    can_fix: bool = False


class HistoryPage(BaseModel):
    entries: list[HistoryEntry] = Field(default_factory=list)  # newest first
    next_before: str | None = None  # pass back as ``before`` for the next (older) page


# --------------------------------------------------------------------------- settings backup


class BackupFormat(BaseModel):
    id: int | None = None  # id in the backed-up install (watch folders refer to it)
    name: str = Field(min_length=1, max_length=120)
    kind: MediaKind
    expression: str = Field(min_length=1, max_length=4000)
    is_default: bool = False


class BackupProviders(BaseModel):
    priority: dict[MediaKind, list[str]] | None = None
    disabled: list[str] | None = None
    language: str | None = None
    credentials: dict[str, str | int | None] | None = None


class SettingsBackup(BaseModel):
    """GET /api/settings/backup (and the body of POST /api/settings/restore)."""

    schema_backup: Literal[1] = 1
    created_at: datetime | None = None
    app: AppSettings | None = None
    providers: BackupProviders | None = None
    watch_folders: list[WatchFolderIn] = Field(default_factory=list)
    formats: list[BackupFormat] = Field(default_factory=list)


class RestoreOut(BaseModel):
    app: bool
    providers: bool
    watch_folders: int
    formats: int


class WsMessage(BaseModel):
    """Server -> client push over /api/ws. ``data`` is the JSON of the named model."""

    type: Literal[
        "hello",  # data: {"version": str}
        "job.updated",  # data: JobOut
        "job.deleted",  # data: {"id": int}
        # data: {"job_id": int, "item_ids": [int, ...]} - a batch of the job's items changed (a saved
        # match chunk, a bulk action, a rematch): refetch those. "item_ids" is left out when more than
        # 1000 changed: refetch the job's items that are on screen.
        "job.items_changed",
        "item.updated",  # data: JobItemOut in the slim form (slim=True; GET /api/items/{id} for the rest)
        "operation.created",  # data: OperationOut
        "operation.updated",  # data: OperationOut (status change, e.g. undone)
        "event",  # data: EventOut
        "watcher.updated",  # data: WatchFolderOut
        "provider.updated",  # data: ProviderStatus
        "resync",  # data: {} - messages were dropped for this client; refetch what is on screen
    ]
    data: dict[str, Any]
