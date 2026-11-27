"""Job pipeline (SPEC §3.2-§3.3): ingest -> parse -> CRC -> match -> probe -> render -> plan -> execute.

Two asyncio workers match queued jobs (one lane for manual imports, one for watch-folder and webhook
jobs, so a huge import never holds up a new download) and one executes approved items (automation
jobs first, slipped in between the files of a long manual run), all in the app's event loop. Blocking
work runs in threads: SQLite and short filesystem checks in the default pool, long file work (CRC,
probes, file operations) in a small pool of its own.

Big jobs (tens of thousands of files) are matched in chunks that are saved as they finish: a match
pass (parse, CRC alongside the metadata lookups, probe) and then a decide pass (bonus clips, render,
plan, decide) that sees the whole job through narrow rows. Memory stays flat, every transaction is
short, and a restart resumes from what was saved. Every state change is persisted first and then
pushed over the event bus (``job.updated``, ``job.items_changed`` for batches, ``item.updated`` for
single items, ``operation.created``).

Sibling modules are called through their module objects (``fileops.execute(...)``) so tests can
monkeypatch them.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import functools
import json
import logging
import os
import re
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from sqlalchemy import Text, func, insert, select, type_coerce, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from schema_app.api_models import (
    AppSettings,
    BulkItemAction,
    ExecuteRequest,
    ItemSearch,
    ItemStatus,
    ItemUpdate,
    JobCreate,
    JobItemOut,
    JobOut,
    JobSource,
    JobStatus,
    OperationOut,
    PreviewItem,
    PreviewRequest,
)
from schema_app.config import Config
from schema_app.core import parser as parser_mod
from schema_app.core.format import (
    Bindings,
    FormatSyntaxError,
    build_binding,
    compile_format,
)
from schema_app.core.format.bindings import _vf_from_dims
from schema_app.core.scoring import best_title_similarity
from schema_app.db import Database, utcnow
from schema_app.domain import (
    Candidate,
    ConflictPolicy,
    FileOpResult,
    FolderRename,
    MatchHint,
    MatchResult,
    MediaKind,
    ParsedMedia,
    ProbeInfo,
    ProviderRef,
    ProviderStatus,
    RenameMode,
    WatchFolderSpec,
)
from schema_app.models import (
    Job,
    JobItem,
    JobItemCandidates,
    Operation,
    WatchFolder,
    card_kind,
    search_text_of,
)
from schema_app.services import fileops, hashing, mediaservers
from schema_app.services import paths as paths_mod
from schema_app.services import probe as probe_mod
from schema_app.services import release as release_mod
from schema_app.services import watcher as watcher_mod
from schema_app.services.events import EventBus
from schema_app.services.jobs import (
    ACTIVE_JOB_STATUSES,
    KNOWN_PATH_CHUNK,
    RUNNABLE_ITEM_STATUSES,
    ApiError,
    add_warning,
    already_handled,
    candidates_of,
    conflict,
    default_expression,
    dump_candidates,
    format_expression,
    get_item,
    get_job,
    get_operation,
    item_out,
    keep_candidates_raw,
    load_details,
    load_item_outs,
    load_job_out,
    match_columns,
    not_found,
    open_item_paths,
    operation_out,
    parsed_from,
    parsed_of,
    probe_of,
    prune_finished,
    queued_input_dirs,
    selected_candidate,
    settle_job,
    slim_item,
    slim_raw,
    trim_candidates,
    watch_folder_out,
)
from schema_app.services.settings_store import SettingsStore

log = logging.getLogger("schema.pipeline")

T = TypeVar("T")

MATCH_CHUNK = 50  # files per matcher.match_many call
SAVE_CHUNK = 500  # files matched / decided and saved per transaction (bounded memory, short write locks)
_EXISTING_POLICY_WARNING = {
    ConflictPolicy.OVERWRITE: "replaces_existing",
    ConflictPolicy.UPGRADE: "upgrade_existing",
    ConflictPolicy.INDEX: "indexed_existing",
}
PROBE_CONCURRENCY = 3  # parallel mediainfo/ffprobe processes per job
FILE_WORKERS = PROBE_CONCURRENCY + 2  # long file work (probes, a CRC read, a file operation) gets its own threads
PROGRESS_INTERVAL = 1.0  # seconds between job progress updates while executing
SHUTDOWN_GRACE = 15.0
HOUSEKEEPING_INTERVAL = 3600.0  # seconds between PRAGMA optimize runs
CHANGED_IDS_MAX = 1000  # job.items_changed lists at most this many ids; above it the client refetches the job
AUTO_SOURCES = frozenset({JobSource.WATCH.value, JobSource.WEBHOOK.value})  # automation: its own match lane
# Progress of a match: the match pass fills MATCH_LO..MATCH_HI (lookups, then probes per chunk), the
# decide pass the rest.
MATCH_LO, MATCH_HI, MATCH_LOOKUP_SHARE = 0.05, 0.8, 0.7

# Warning families recomputed on every render (others, e.g. "sample", "crc_mismatch", stick).
_RECOMPUTED = (
    "no_match",
    "low_confidence",
    "destination_exists",
    "duplicate_in_batch",
    "no_library_root",
    "no_destination",
    "format_error",
    "format:",
    "already_in_place",
    "plan_error",
    "dry_run",
)
# A dry run (RenameMode.TEST) reports through these; each new run replaces the previous report.
_DRY_RUN_STALE = ("dry_run", "destination_exists", "already_in_place")
_DRY_RUN_ERROR = "Dry run: "
_SOURCE_MISSING = "source_missing"  # the file was moved or deleted between the scan and the rename
# Matcher warnings that always need a person, however confident the score (never auto-run).
_REVIEW_WARNINGS = ("episode_title_mismatch",)


def _strip_recomputed(warnings: list[str]) -> list[str]:
    return [w for w in warnings if not w.startswith(_RECOMPUTED)]


# --------------------------------------------------------------------------- fallback matcher


class UnavailableMatcher:
    """Stand-in used when the real matcher cannot be built: every file goes to review."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def match(self, parsed: ParsedMedia, hint: MatchHint | None = None) -> MatchResult:
        return MatchResult(warnings=[f"matcher_unavailable: {self.reason}"])

    async def match_many(self, items: Sequence[ParsedMedia], hint: MatchHint | None = None) -> list[MatchResult]:
        return [await self.match(item, hint) for item in items]

    async def search(self, query: str, kind: MediaKind, year: int | None = None,
                     provider: str | None = None) -> list[Candidate]:  # fmt: skip
        return []

    async def resolve(self, parsed: ParsedMedia, ref: Any, kind: MediaKind, hint: MatchHint | None = None) -> Candidate:
        raise ApiError(503, f"metadata matcher unavailable: {self.reason}")

    def provider_statuses(self) -> list[ProviderStatus]:
        return []

    async def test_provider(self, name: str) -> ProviderStatus:
        raise ApiError(503, f"metadata matcher unavailable: {self.reason}")

    async def reconfigure(self, settings: Any) -> None:
        return None

    async def aclose(self) -> None:
        return None


# --------------------------------------------------------------------------- working state


@dataclass
class JobInfo:
    id: int
    kind: MediaKind | None
    mode: RenameMode
    format_id: int | None
    dest_root: str | None
    auto_execute: bool
    label: str | None
    input_paths: list[str]
    source: str = JobSource.MANUAL.value

    @classmethod
    def of(cls, job: Job) -> JobInfo:
        return cls(
            id=job.id,
            source=job.source or JobSource.MANUAL.value,
            kind=MediaKind(job.kind) if job.kind else None,
            mode=RenameMode(job.mode),
            format_id=job.format_id,
            dest_root=job.dest_root,
            auto_execute=bool(job.auto_execute),
            label=job.label,
            input_paths=list(job.input_paths or []),
        )


MatchKey = tuple[str, tuple[tuple[Any, ...], ...], int | None]  # (candidate id, its episodes, its part)


@dataclass(slots=True)
class Work:
    """In-memory state of one item while the pipeline works on it.

    A *light* work stands for a sibling the pipeline only needs as batch context (collisions,
    ``di``/``dc``/``pc``, shared folders): built from narrow columns, without parsed data, candidates or
    probe. Its selected candidate is known by id and, decoded only on demand, by its slim form."""

    item_id: int
    source_path: str
    parsed: ParsedMedia | None
    status: ItemStatus = ItemStatus.PENDING
    candidates: list[Candidate] = field(default_factory=list)
    best: Candidate | None = None
    confidence: float = 0.0
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    crc_ok: bool | None = None
    probe: ProbeInfo | None = None
    sidecars: list[str] = field(default_factory=list)
    relative: str | None = None
    proposed_path: str | None = None
    path_overridden: bool = False
    manual: bool = False
    kind: MediaKind = MediaKind.UNKNOWN
    bindings: Bindings | None = None
    render_failed: bool = False
    conflict: str = "none"
    duplicate: bool = False
    parse_failed: bool = False
    folder_rename: FolderRename | None = None
    allow_extra: bool = False  # the user acted on this item: never re-skip it as a bonus clip
    likely_extra: bool = False  # tiny next to the same title already at the destination (set by planning)
    matched: bool = False  # the match pass is done (saved with the item: a resumed job skips it)
    # Set when rendered: the multi-version suffix (" - 1080p Remux") and the file extension of the name.
    # None = not rendered in this pass (a collision then can't tell this file's quality).
    version_tag: str | None = None
    ext_suffix: str = ""
    light: bool = False
    selected_id: str | None = None  # light: the selected candidate's id
    chosen_raw: Any = None  # light: the slim chosen candidate (JSON text) until ``match_key`` reads it
    parsed_part: int | None = None  # light: parsed.part
    cached_key: Any = False  # light: ``match_key`` once computed (False = not yet)

    def warn(self, warning: str) -> None:
        if warning not in self.warnings:
            self.warnings.append(warning)

    @property
    def best_id(self) -> str | None:
        if self.best is not None:
            return self.best.id
        return self.selected_id if self.light else None

    @property
    def part(self) -> int | None:
        return self.parsed.part if self.parsed is not None else self.parsed_part

    def match_key(self) -> MatchKey | None:
        """What the batch context compares: the selected candidate's id, its (season, episode,
        absolute) episodes and its part. A light work decodes its slim candidate on first use."""
        if self.best is not None:
            episodes = tuple((e.season, e.episode, e.absolute) for e in self.best.episodes)
            return self.best.id, episodes, self.best.part
        if not self.light or not self.selected_id:
            return None
        if self.cached_key is False:
            if not self.chosen_raw:
                return None  # not loaded (``Pipeline._hydrate``)
            chosen = self.chosen_raw if isinstance(self.chosen_raw, dict) else json.loads(self.chosen_raw)
            self.chosen_raw = None  # keep only the key: a big job's context stays small
            if chosen.get("id") != self.selected_id:
                self.cached_key = None
            else:
                keys = chosen.get("episode_keys") or [
                    [e.get("season"), e.get("episode"), e.get("absolute")] for e in chosen.get("episodes") or []
                ]
                self.cached_key = (self.selected_id, tuple(tuple(k) for k in keys), chosen.get("part"))
        return self.cached_key

    @classmethod
    def from_item(cls, item: JobItem, detail: JobItemCandidates | None) -> Work:
        candidates = candidates_of(detail)
        return cls(
            item_id=item.id,
            source_path=item.source_path,
            parsed=parsed_of(item) if item.parsed else None,
            status=ItemStatus(item.status),
            candidates=candidates,
            best=selected_candidate(item, candidates),
            confidence=item.confidence or 0.0,
            warnings=list(item.warnings or []),
            error=item.error,
            crc_ok=item.crc_ok,
            probe=probe_of(detail),
            sidecars=list(item.sidecars or []),
            relative=item.proposed_relative,
            proposed_path=item.proposed_path,
            path_overridden=bool(item.path_overridden),
            folder_rename=FolderRename.model_validate(item.folder_rename) if item.folder_rename else None,
            matched=bool(item.matched),
        )

    def row(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """The ``job_items`` values (derived columns included) and the ``job_item_candidates`` values."""
        selected = self.best.id if self.best else None
        parsed = self.parsed.model_dump(mode="json") if self.parsed else None
        kept = keep_candidates_raw(dump_candidates(self.candidates), selected, self.status.value)
        values = {
            "id": self.item_id,
            "status": self.status.value,
            "parsed": parsed,
            "selected_candidate_id": selected,
            "confidence": float(self.confidence or 0.0),
            "warnings": list(self.warnings),
            "error": self.error,
            "crc_ok": self.crc_ok,
            "sidecars": list(self.sidecars),
            "proposed_relative": self.relative,
            "proposed_path": self.proposed_path,
            "path_overridden": self.path_overridden,
            "folder_rename": self.folder_rename.model_dump(mode="json") if self.folder_rename else None,
            "matched": self.matched,
            "updated_at": utcnow(),
            **match_columns(kept, selected, parsed, self.source_path, self.proposed_path),
        }
        side = {"item_id": self.item_id, "candidates": kept,
                "probe": self.probe.model_dump(mode="json") if self.probe else None}  # fmt: skip
        return values, side

    def as_light(self) -> Work:
        """This item as batch context once it is saved (drops candidates, bindings, probe)."""
        chosen = None
        if self.best is not None:
            chosen = slim_raw(self.best.model_dump(mode="json"))
        return Work(item_id=self.item_id, source_path=self.source_path, parsed=None, status=self.status,
                    proposed_path=self.proposed_path, path_overridden=self.path_overridden, light=True,
                    selected_id=self.best.id if self.best else None, chosen_raw=chosen,
                    parsed_part=self.parsed.part if self.parsed else None)  # fmt: skip

    def reset_for_rematch(self) -> None:
        self.status = ItemStatus.PENDING
        self.candidates, self.best, self.confidence = [], None, 0.0
        self.warnings, self.error = [], None
        self.relative, self.bindings = None, None
        if not self.path_overridden:
            self.proposed_path = None
        self.manual = self.render_failed = self.duplicate = False
        self.conflict = "none"
        self.folder_rename = None
        self.matched = False


@dataclass(slots=True)
class ExtraFacts:
    """What deciding "is this matched movie really a bonus clip?" needs, gathered while the item is
    in memory, so the decision can wait until the whole job is matched."""

    item_id: int
    source_path: str
    best_id: str
    best_title: str
    runtime: bool  # the runtime alone says so
    title_differs: bool
    bonus_words: bool
    folder_similar: bool

    @classmethod
    def of(cls, work: Work) -> ExtraFacts | None:
        if not _extra_eligible(work):
            return None
        assert work.best is not None and work.parsed is not None
        folder = os.path.basename(os.path.dirname(work.source_path))
        return cls(
            item_id=work.item_id,
            source_path=work.source_path,
            best_id=work.best.id,
            best_title=work.best.title,
            runtime=_runtime_says_extra(work),
            title_differs=_title_differs(work),
            bonus_words=bool(_EXTRA_TITLE_WORDS.search(work.parsed.title or work.parsed.filename)),
            folder_similar=best_title_similarity([folder, work.parsed.folder_title], [work.best.title])
            >= EXTRA_FOLDER_SIMILAR,
        )


@dataclass
class ExecOutcome:
    item: JobItemOut | None
    operation: OperationOut | None
    touched: list[str]
    ok: bool


def _norm_key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path)).casefold()


def _norm_case(path: str) -> str:
    """The key ``services.jobs.already_handled`` returns."""
    return os.path.normcase(os.path.normpath(path))


def _mtimes(paths: Iterable[str]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for path in paths:
        try:
            out[path] = os.stat(path).st_mtime
        except OSError:
            out[path] = None
    return out


def _merge_context(siblings: list[Work], works: list[Work]) -> list[Work]:
    """All items of the job, with the in-flight ``works`` replacing their stale sibling copies."""
    replaced = {w.item_id: w for w in works}
    merged = [replaced.pop(w.item_id, w) for w in siblings]
    merged.extend(replaced.values())
    return merged


@dataclass
class FolderPolicy:
    """What a job's moves may do with source folders (SPEC §7.2): rename a lone movie's folder in
    place instead of leaving it behind, and remove source folders a move left empty."""

    rename: bool = False
    remove_empty: bool = False
    protected: list[str] = field(default_factory=list)  # allowed, library, watch-folder and download roots
    shared: frozenset[str] = frozenset()  # _norm_key of source folders that other items of the job use

    def renames(self, source: str) -> bool:
        return self.rename and _norm_key(os.path.dirname(source)) not in self.shared


def shared_source_dirs(items: Iterable[tuple[str, ItemStatus]]) -> frozenset[str]:
    """Source folders (``_norm_key``) holding more than one item of a job: a second file right in the
    folder (whatever its status), or one in a sub-folder that isn't skipped (it is still to be moved,
    or already was). Such a folder is never renamed in place: a collection keeps its name even once
    only its last movie is left in it."""
    direct: Counter[str] = Counter()
    nested: set[str] = set()
    for source, status in items:
        folder = os.path.dirname(os.path.normpath(source))
        direct[_norm_key(folder)] += 1
        if status == ItemStatus.SKIPPED:
            continue
        current = folder
        while (parent := os.path.dirname(current)) != current:
            nested.add(_norm_key(parent))
            current = parent
    return frozenset({key for key, count in direct.items() if count > 1} | nested)


def item_kind(job_kind: MediaKind | None, best: Candidate | None, parsed: ParsedMedia | None) -> MediaKind:
    kind = best.kind if best else (job_kind or (parsed.kind if parsed else MediaKind.UNKNOWN))
    if kind == MediaKind.UNKNOWN and job_kind:
        kind = job_kind
    if kind == MediaKind.UNKNOWN:
        episodic = parsed is not None and bool(
            parsed.episodes or parsed.absolute or parsed.air_date or parsed.season is not None
        )
        kind = MediaKind.SERIES if episodic else MediaKind.MOVIE
    return kind


def dest_root_for(job_dest: str | None, kind: MediaKind, settings: AppSettings) -> str | None:
    if job_dest:
        return job_dest
    roots = settings.library_roots
    root = roots.get(kind)
    if not root and kind == MediaKind.ANIME:
        root = roots.get(MediaKind.SERIES)
    if not root and kind == MediaKind.UNKNOWN:
        root = roots.get(MediaKind.MOVIE)
    return root or None


def join_dest(root: str, relative: str) -> str:
    return str(Path(root, *[part for part in relative.split("/") if part]))


def relative_to_root(path: str | None, root: str | None) -> str | None:
    """A hand-typed destination as a "/"-joined path below the library root (None when outside it)."""
    if not path or not root:
        return None
    try:
        rel = os.path.relpath(path, root)
    except ValueError:  # another drive on Windows
        return None
    if rel == os.curdir or rel.startswith(os.pardir) or os.path.isabs(rel):
        return None
    return rel.replace(os.sep, "/")


def _version_suffix(bindings: Bindings | None) -> str:
    if bindings is None:
        return ""
    parts: list[str] = []
    vf = bindings.get("vf")
    if vf:
        parts.append(str(vf))
    if bindings.get("remux"):
        parts.append("Remux")
    if bindings.get("hdr"):
        parts.append("HDR")
    return (" - " + " ".join(parts)) if parts else ""


def _insert_suffix(path: str, suffix: str, ext: str) -> str:
    if ext and path.endswith(ext):
        return path[: -len(ext)] + suffix + ext
    return path + suffix


_VERSION_TAIL = re.compile(r" - (?:\d{3,4}p|Remux|HDR)(?: (?:Remux|HDR))*$")


def _strip_version(path: str) -> str:
    """'Strike - 1080p.mkv' -> 'Strike.mkv' (the multi-version suffix ``_version_suffix`` adds)."""
    stem, ext = os.path.splitext(path)
    m = _VERSION_TAIL.search(stem)
    return stem[: m.start()] + ext if m else path


def _unsuffix(work: Work) -> None:
    """Give an item its plain name back (no multi-version suffix, no longer a duplicate)."""
    work.proposed_path = _strip_version(work.proposed_path or "")
    if work.relative:
        work.relative = _strip_version(work.relative)
    work.warnings = [x for x in work.warnings if x != "duplicate_in_batch"]


def _is_ignored(name: str, patterns: list[str]) -> bool:
    lowered = name.lower()
    return any(fnmatch.fnmatch(lowered, pattern.lower()) for pattern in patterns)


def expand_paths(input_paths: list[str], settings: AppSettings) -> list[str]:
    """Folders -> video files (recursive), filtered by extension and ignore patterns.

    Only regular files are taken (FIFOs, sockets and devices named like videos would hang the CRC
    check or the probe), and a symlink only when it resolves to a regular file inside the allowed
    roots: everything picked here gets read (CRC, probe) long before execution checks boundaries."""
    import stat  # local: this function is the only user in this module

    exts = {e.lower().lstrip(".") for e in settings.video_extensions}
    ignore = list(settings.ignore_patterns)
    roots = list(settings.allowed_roots)
    found: list[str] = []
    seen: set[str] = set()

    def consider(path: str) -> None:
        name = os.path.basename(path)
        if name.startswith(".") or _is_ignored(name, ignore) or ".schema-tmp" in name:
            return
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in exts:
            return
        try:
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode):
                if not paths_mod.is_within(path, roots):
                    log.info("skipping %s: it is a symlink to somewhere outside the allowed roots", path)
                    return
                mode = os.stat(path).st_mode
        except OSError:
            return
        if not stat.S_ISREG(mode):
            log.info("skipping %s: not a regular file", path)
            return
        key = _norm_key(path)
        if key not in seen:
            seen.add(key)
            found.append(path)

    for raw in input_paths:
        path = os.path.normpath(raw)
        if os.path.isfile(path):
            consider(path)
        elif os.path.isdir(path):
            batch: list[str] = []
            for root, dirs, files in os.walk(path):
                dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not _is_ignored(d, ignore))
                batch.extend(os.path.join(root, f) for f in files)
            for file_path in sorted(batch, key=lambda p: p.casefold()):
                consider(file_path)
    return found


# --------------------------------------------------------------------------- bonus clips

# Bonus clips that the filename doesn't give away ("The Funboy Fight.mkv" next to "The Crow (1994).mkv")
# get matched to the feature and then collide with it. They are told apart by size (a clip is at most
# a quarter of the feature beside it or already at the destination), by runtime when probing knows
# both, and by bonus-material words in the title.
EXTRA_SIZE_RATIO = 4
EXTRA_RUNTIME_SHARE = 0.25
EXTRA_SHORT_SECONDS = 20 * 60  # under this is never a feature-length movie ...
EXTRA_FEATURE_SECONDS = 60 * 60  # ... once the matched title runs at least an hour
EXTRA_MIN_RUNTIME_SECONDS = 40 * 60  # below this the matched title may itself be a short film
EXTRA_TITLE_SIMILAR = 0.8
EXTRA_FOLDER_SIMILAR = 0.85
MAX_FOLDER_SCAN = 2000  # entries; a huge dump folder isn't a movie's own folder
_EXTRA_TITLE_WORDS = re.compile(
    r"(?i)(?<![a-z])(?:making[\s._-]+of|behind[\s._-]+the[\s._-]+scenes|featurettes?|deleted[\s._-]+scenes?"
    r"|gag[\s._-]+reels?|bloopers?|outtakes?|vfx[\s._-]+(?:breakdown|reel)|bonus[\s._-]+(?:clip|feature|material)s?"
    r"|alternate[\s._-]+(?:ending|opening)s?|screen[\s._-]+tests?)(?![a-z])"
)


def _extra_eligible(work: Work) -> bool:
    """Only a matched, single-file movie the user hasn't acted on can be re-classified as a clip."""
    parsed = work.parsed
    if work.status == ItemStatus.SKIPPED or work.manual or work.allow_extra:
        return False
    if work.best is None or work.best.kind != MediaKind.MOVIE or parsed is None:
        return False
    return not (parsed.episodes or parsed.absolute or parsed.air_date or parsed.season is not None or parsed.part)


def _title_differs(work: Work) -> bool:
    """The file's own title isn't the matched title (so the match came from the folder or a guess)."""
    assert work.best is not None and work.parsed is not None
    names = [work.best.title]
    if work.best.movie is not None:
        names.extend([work.best.movie.original_title, *work.best.movie.aliases])
    return best_title_similarity([work.parsed.title], names) < EXTRA_TITLE_SIMILAR


def _runtime_says_extra(work: Work) -> bool:
    assert work.best is not None
    movie, probe = work.best.movie, work.probe
    if probe is None or not probe.duration_seconds or movie is None or not movie.runtime_minutes:
        return False
    runtime = movie.runtime_minutes * 60
    if runtime < EXTRA_MIN_RUNTIME_SECONDS:
        return False
    duration = probe.duration_seconds
    return duration < EXTRA_RUNTIME_SHARE * runtime or (runtime >= EXTRA_FEATURE_SECONDS and duration < EXTRA_SHORT_SECONDS)


def _video_sizes(folder: str, extensions: set[str]) -> dict[str, int]:
    """Blocking: ``_norm_key(path) -> size`` of the video files directly in ``folder``."""
    sizes: dict[str, int] = {}
    try:
        with os.scandir(folder) as entries:
            for count, entry in enumerate(entries):
                if count >= MAX_FOLDER_SCAN:
                    return {}
                name = entry.name
                if name.startswith(".") or "." not in name or name.rsplit(".", 1)[1].lower() not in extensions:
                    continue
                with contextlib.suppress(OSError):
                    if entry.is_file():
                        sizes[_norm_key(entry.path)] = entry.stat().st_size
    except OSError:
        return {}
    return sizes


def _sibling_says_extra(facts: ExtraFacts, sizes: dict[str, int], by_path: dict[str, Work]) -> bool:
    """A much bigger video sits in the same folder and is (or looks like) the matched title's feature."""
    own = _norm_key(facts.source_path)
    size = sizes.get(own, 0)
    if size <= 0:
        return False
    bigger = [key for key, other in sizes.items() if key != own and other >= EXTRA_SIZE_RATIO * size]
    if not bigger or not facts.title_differs:
        return False
    if facts.bonus_words:
        return True
    for key in bigger:
        other = by_path.get(key)
        if other is not None and other.item_id != facts.item_id and other.best_id == facts.best_id:
            return True
    return facts.folder_similar


def _extras_among(facts: Iterable[ExtraFacts], context: Iterable[Work], settings: AppSettings) -> list[ExtraFacts]:
    """Blocking (folder listings): which of the eligible matched movies are really bonus clips."""
    extensions = {e.lower().lstrip(".") for e in settings.video_extensions}
    by_path = {_norm_key(w.source_path): w for w in context}
    listings: dict[str, dict[str, int]] = {}
    found: list[ExtraFacts] = []
    for item in facts:
        folder = os.path.dirname(item.source_path)
        key = _norm_key(folder)
        if key not in listings:
            listings[key] = _video_sizes(folder, extensions)
        if item.runtime or _sibling_says_extra(item, listings[key], by_path):
            found.append(item)
            log.info("%s looks like a bonus clip of %s; skipped", item.source_path, item.best_title)
    return found


def _dwarfed_by_existing(work: Work) -> bool:
    """Blocking: the file already at the destination is at least ``EXTRA_SIZE_RATIO`` times bigger."""
    if not work.proposed_path:
        return False
    try:
        existing = fileops.existing_destination(work.proposed_path) or work.proposed_path
    except Exception:
        existing = work.proposed_path
    try:
        incoming, there = os.path.getsize(work.source_path), os.path.getsize(existing)
    except OSError:
        return False
    return incoming > 0 and there >= EXTRA_SIZE_RATIO * incoming


# --------------------------------------------------------------------------- media refresh


class MediaRefresher:
    """Debounced, folder-deduplicated media-server refresh (SPEC §7.6)."""

    def __init__(self, store: SettingsStore, bus: EventBus, *, delay: float = 5.0) -> None:
        self.store = store
        self.bus = bus
        self.delay = delay
        self._debouncer: Any = None

    def _ensure(self) -> Any:
        if self._debouncer is None:
            factory = getattr(mediaservers, "RefreshDebouncer", None)
            if factory is not None:
                self._debouncer = factory(
                    delay=self.delay,
                    refresh=lambda config, paths: mediaservers.refresh_paths(config, paths),
                    on_error=self._on_error,
                )
        return self._debouncer

    async def _on_error(self, config: Any, exc: Exception) -> None:
        await self.bus.alog("warning", "mediaserver", f"Refresh of {config.name} failed: {exc}", server=config.name)

    def schedule(self, folders: list[str]) -> None:
        folders = sorted({f for f in folders if f})
        if not folders:
            return
        servers = [s for s in self.store.get_app().media_servers if s.enabled]
        if not servers:
            return
        debouncer = self._ensure()
        for server in servers:
            if debouncer is not None:
                debouncer.add(server, folders)
            else:  # pragma: no cover - only without the DevOps helper
                asyncio.ensure_future(self._refresh_now(server, folders))

    async def _refresh_now(self, server: Any, folders: list[str]) -> None:
        try:
            await mediaservers.refresh_paths(server, folders)
        except Exception as exc:
            await self._on_error(server, exc)

    async def flush(self) -> None:
        if self._debouncer is not None:
            await self._debouncer.flush()

    async def aclose(self) -> None:
        if self._debouncer is not None:
            with contextlib.suppress(Exception):
                await self._debouncer.aclose()


# --------------------------------------------------------------------------- pipeline


ExecEntry = tuple[int, list[int] | None, RenameMode | None]
ProgressFn = Callable[[float], Any]  # async: fraction (0..1) of the current step

# Narrow columns of a sibling item (``Work.light``): enough for collisions, di/dc/pc and shared folders.
_LIGHT_COLUMNS = (
    JobItem.id, JobItem.source_path, JobItem.status, JobItem.proposed_path, JobItem.path_overridden,
    JobItem.selected_candidate_id, func.json_extract(JobItem.parsed, "$.part"),
)  # fmt: skip
# Warnings planning and deciding add, dropped before an item is planned and decided again.
_REDECIDED = (
    "no_match", "low_confidence", "destination_exists", "duplicate_in_batch", "no_destination", "already_in_place",
    "plan_error", *_EXISTING_POLICY_WARNING.values(),
)  # fmt: skip


class Pipeline:
    def __init__(self, *, config: Config, db: Database, store: SettingsStore, bus: EventBus, matcher: Any) -> None:
        self.config = config
        self.db = db
        self.store = store
        self.bus = bus
        self.matcher = matcher
        self.refresher = MediaRefresher(store, bus)
        self._match_queue: asyncio.Queue[int | None] | None = None  # manual imports
        self._auto_queue: asyncio.Queue[int | None] | None = None  # watch-folder and webhook jobs
        self._exec_queue: asyncio.Queue[ExecEntry | None] | None = None  # manual runs
        self._exec_auto: asyncio.Queue[ExecEntry | None] | None = None  # automation runs: always next
        self._exec_wake: asyncio.Event | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._housekeeping_task: asyncio.Task[None] | None = None
        self._files: ThreadPoolExecutor | None = None
        self._cancel: set[int] = set()
        self._busy: set[int] = set()
        self._stopping = False

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._stopping = False
        self._match_queue, self._auto_queue = asyncio.Queue(), asyncio.Queue()
        self._exec_queue, self._exec_auto = asyncio.Queue(), asyncio.Queue()
        self._exec_wake = asyncio.Event()
        queued = await asyncio.to_thread(self._recover_sync)
        for job_id, source in queued:
            self.enqueue_match(job_id, source)
        self._tasks = [
            asyncio.create_task(self._match_worker(self._match_queue), name="schema-match-worker"),
            asyncio.create_task(self._match_worker(self._auto_queue), name="schema-match-worker-auto"),
            asyncio.create_task(self._exec_worker(), name="schema-exec-worker"),
        ]
        self._housekeeping_task = asyncio.create_task(self._housekeeping(), name="schema-db-housekeeping")

    async def stop(self) -> None:
        self._stopping = True
        task, self._housekeeping_task = self._housekeeping_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for queue in (self._match_queue, self._auto_queue, self._exec_auto):
            if queue is not None:
                queue.put_nowait(None)
        if self._exec_wake is not None:
            self._exec_wake.set()
        if self._tasks:
            _, pending = await asyncio.wait(self._tasks, timeout=SHUTDOWN_GRACE)
            for pending_task in pending:
                pending_task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        files, self._files = self._files, None
        if files is not None:
            files.shutdown(wait=False, cancel_futures=True)
        await self.refresher.aclose()

    def _recover_sync(self) -> list[tuple[int, str]]:
        """Startup recovery (SPEC §3.3). A job that was matching is queued again and resumes from its
        saved chunks. Returns the queued jobs as (id, source)."""
        recovered_items = 0
        with self.db.session() as session:
            for item in session.scalars(select(JobItem).where(JobItem.status == ItemStatus.EXECUTING.value)):
                item.status = ItemStatus.REVIEW.value
                add_warning(item, "interrupted")
                recovered_items += 1
            for job in session.scalars(select(Job).where(Job.status == JobStatus.EXECUTING.value)):
                job.status = JobStatus.REVIEW.value
                settle_job(session, job)
            for job in session.scalars(select(Job).where(Job.status == JobStatus.MATCHING.value)):
                job.status = JobStatus.QUEUED.value
                job.progress = 0.0
            queued = [(job_id, source) for job_id, source in session.execute(
                select(Job.id, Job.source).where(Job.status == JobStatus.QUEUED.value).order_by(Job.id))]  # fmt: skip
        if recovered_items:
            self.bus.log("warning", "job", f"{recovered_items} item(s) were interrupted while executing; check the journal")
        return queued

    async def wait_idle(self, timeout: float = 10.0) -> None:
        """Wait until every queue is drained and no job is being worked on (tests, demo reset)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        queues = (self._match_queue, self._auto_queue, self._exec_queue, self._exec_auto)
        while loop.time() < deadline:
            if not self._busy and all(q is None or q.empty() for q in queues):
                return
            await asyncio.sleep(0.02)

    async def _housekeeping(self) -> None:
        """Keep the planner's statistics fresh while the app runs for weeks."""
        while True:
            await asyncio.sleep(HOUSEKEEPING_INTERVAL)
            await asyncio.to_thread(self.db.optimize)

    # ------------------------------------------------------------------ helpers

    async def _in_files(self, fn: Callable[..., T], *args: Any) -> T:
        """Run long file work (CRC reads, probes, file operations) on the pipeline's own threads, so it
        never queues up the short SQLite calls of the API in the default pool."""
        if self._files is None:
            self._files = ThreadPoolExecutor(max_workers=FILE_WORKERS, thread_name_prefix="schema-files")
        return await asyncio.get_running_loop().run_in_executor(self._files, functools.partial(fn, *args))

    def _publish_job(self, job: JobOut | None) -> None:
        if job is not None:
            self.bus.publish("job.updated", job)

    def _publish_items(self, items: Sequence[JobItemOut]) -> None:
        """``item.updated`` for single-item changes, in the slim form (the inspector fetches
        GET /api/items/{id} for the rest). Batches use ``_publish_changed``."""
        for item in items:
            self.bus.publish("item.updated", slim_item(item))

    def _publish_changed(self, job_id: int, item_ids: Sequence[int]) -> None:
        """One ``job.items_changed`` for a batch of a job's items (a saved chunk, a bulk action, a
        rematch): ``{"job_id", "item_ids"}``; ``item_ids`` is left out above CHANGED_IDS_MAX, which
        means "refetch the job's items"."""
        if not item_ids:
            return
        data: dict[str, Any] = {"job_id": job_id}
        if len(item_ids) <= CHANGED_IDS_MAX:
            data["item_ids"] = sorted(item_ids)
        self.bus.publish("job.items_changed", data)

    def _progress(self, job_id: int, lo: float, hi: float) -> ProgressFn:
        """An async ``report(fraction)`` that maps a step onto the job's lo..hi progress (rate limited)."""
        last = 0.0

        async def report(fraction: float) -> None:
            nonlocal last
            now = time.monotonic()
            if now - last < PROGRESS_INTERVAL and fraction < 1.0:
                return
            last = now
            await self._set_progress(job_id, lo + (hi - lo) * max(0.0, min(1.0, fraction)))

        return report

    def _job_out(self, job_id: int) -> JobOut:
        with self.db.session() as session:
            return load_job_out(session, job_id)

    async def _set_progress(self, job_id: int, progress: float) -> None:
        def run() -> JobOut | None:
            with self.db.session() as session:
                job = session.get(Job, job_id)
                if job is None:
                    return None
                job.progress = max(0.0, min(1.0, progress))
                job.updated_at = utcnow()
                session.flush()
                return load_job_out(session, job_id)

        self._publish_job(await asyncio.to_thread(run))

    def _expression_lookup(self, job: JobInfo) -> Callable[[MediaKind], str]:
        with self.db.session() as session:
            explicit = format_expression(session, job.format_id)
            defaults = {kind: default_expression(session, kind) for kind in MediaKind}
        return lambda kind: explicit or defaults[kind]

    # ------------------------------------------------------------------ job creation

    def validate_create(self, req: JobCreate, settings: AppSettings) -> list[str]:
        """Check paths against ``allowed_roots`` (403) and existence (400). Returns normalized paths."""
        roots = settings.allowed_roots
        result: list[str] = []
        for raw in req.paths:
            path = raw.strip()
            if not path:
                continue
            if not os.path.isabs(path):
                raise ApiError(400, f"path must be absolute: {path}")
            if not paths_mod.is_within(path, roots):
                raise ApiError(403, f"path is outside the allowed roots: {path}")
            if not os.path.exists(path):
                raise ApiError(400, f"path does not exist: {path}")
            norm = os.path.normpath(path)
            if norm not in result:
                result.append(norm)
        if not result:
            raise ApiError(400, "no paths given")
        if req.dest_root:
            if not os.path.isabs(req.dest_root):
                raise ApiError(400, "dest_root must be an absolute path")
            if not paths_mod.is_within(req.dest_root, roots):
                raise ApiError(403, "dest_root is outside the allowed roots")
        if req.format_id is not None:
            with self.db.session() as session:
                if format_expression(session, req.format_id) is None:
                    raise ApiError(400, f"format {req.format_id} does not exist")
        return result

    def _insert_job(self, req: JobCreate, paths: list[str], source: JobSource, settings: AppSettings,
                    watch_folder_id: int | None = None) -> JobOut:  # fmt: skip
        label = req.label
        if not label:
            first = os.path.basename(paths[0].rstrip("/\\")) or paths[0]
            label = first if len(paths) == 1 else f"{first} (+{len(paths) - 1} more)"
        with self.db.session() as session:
            job = Job(
                label=label[:255],
                source=source.value,
                status=JobStatus.QUEUED.value,
                kind=req.kind.value if req.kind else None,
                mode=(req.mode or settings.default_mode).value,
                format_id=req.format_id,
                dest_root=req.dest_root,
                auto_execute=req.auto_execute,
                input_paths=paths,
                watch_folder_id=watch_folder_id,
            )
            session.add(job)
            session.flush()
            return load_job_out(session, job.id)

    async def create_job(self, req: JobCreate, *, source: JobSource = JobSource.MANUAL,
                         watch_folder_id: int | None = None, validate: bool = True) -> JobOut:  # fmt: skip
        settings = self.store.get_app()
        if validate:
            paths = await asyncio.to_thread(self.validate_create, req, settings)
        else:
            paths = [os.path.normpath(p) for p in req.paths]
        out = await asyncio.to_thread(self._insert_job, req, paths, source, settings, watch_folder_id)
        self._publish_job(out)
        await self.bus.alog("info", "job", f"Job #{out.id} queued ({source.value}): {out.label}", job_id=out.id)
        self.enqueue_match(out.id, source.value)
        return out

    def enqueue_match(self, job_id: int, source: str | None = None) -> None:
        """Queue a job for matching: watch-folder and webhook jobs have a lane of their own."""
        queue = self._auto_queue if source in AUTO_SOURCES else self._match_queue
        if queue is not None:
            queue.put_nowait(job_id)

    # ------------------------------------------------------------------ match worker

    async def _match_worker(self, queue: asyncio.Queue[int | None]) -> None:
        while True:
            job_id = await queue.get()
            if job_id is None or self._stopping:
                return
            self._busy.add(job_id)
            try:
                await self._run_match(job_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # keep the worker alive
                log.exception("matching job %s failed", job_id)
                await asyncio.to_thread(self._fail_job, job_id, f"internal error: {exc}")
            finally:
                self._busy.discard(job_id)
                self._cancel.discard(job_id)

    def _fail_job(self, job_id: int, message: str) -> None:
        with self.db.session() as session:
            job = session.get(Job, job_id)
            if job is None:
                return
            job.status = JobStatus.FAILED.value
            job.error = message
            job.finished_at = utcnow()
            session.flush()
            out = load_job_out(session, job_id)
        self._publish_job(out)
        self.bus.log("error", "job", f"Job #{job_id} failed: {message}", job_id=job_id)

    def _finish_empty(self, job_id: int) -> None:
        with self.db.session() as session:
            job = session.get(Job, job_id)
            if job is None:
                return
            job.status = JobStatus.COMPLETED.value
            job.progress = 1.0
            job.finished_at = utcnow()
            session.flush()
            out = load_job_out(session, job_id)
        self._publish_job(out)

    def _begin_match(self, job_id: int) -> tuple[JobInfo, JobOut] | None:
        with self.db.session() as session:
            job = session.get(Job, job_id)
            if job is None or job.status != JobStatus.QUEUED.value:
                return None
            job.status = JobStatus.MATCHING.value
            job.progress = 0.0
            job.error = None
            job.finished_at = None
            session.flush()
            return JobInfo.of(job), load_job_out(session, job_id)

    def _expand_items(self, job: JobInfo, settings: AppSettings) -> tuple[int, int]:
        """Create the job's items from ``input_paths`` the first time (one transaction, one bulk
        insert). Returns how many items the job has, and how many files a download-client hook
        re-sent that an earlier job already took in."""
        known = 0
        with self.db.session() as session:
            total = int(session.scalar(select(func.count(JobItem.id)).where(JobItem.job_id == job.id)) or 0)
            if total:
                return total, 0  # created by an earlier run of this job: resume
            files = expand_paths(job.input_paths, settings)
            if job.source == JobSource.WEBHOOK.value and files:
                # A re-fired hook (torrent re-check, a client retry) for files that already failed,
                # were skipped or wait in review must not run them again by itself.
                handled = already_handled(session, _mtimes(files))
                kept = [f for f in files if _norm_case(f) not in handled]
                known, files = len(files) - len(kept), kept
            rows: list[dict[str, Any]] = []
            for path in files:
                sidecars: list[str] = []
                if settings.move_sidecars:
                    try:
                        sidecars = [str(p) for p in fileops.find_sidecars(path, settings.sidecar_extensions)]
                    except Exception:  # sidecars are best effort at this stage
                        sidecars = []
                rows.append({"job_id": job.id, "source_path": path, "status": ItemStatus.PENDING.value,
                             "sidecars": sidecars, "warnings": [], "updated_at": utcnow(), "kind": card_kind(None),
                             "search_text": search_text_of(path, None, None)})  # fmt: skip
            for start in range(0, len(rows), KNOWN_PATH_CHUNK * 4):
                session.execute(insert(JobItem), rows[start : start + KNOWN_PATH_CHUNK * 4])
            return len(rows), known

    async def _run_match(self, job_id: int) -> None:
        begun = await asyncio.to_thread(self._begin_match, job_id)
        if begun is None:
            return
        job, out = begun
        self._publish_job(out)
        settings = self.store.get_app()
        total, known = await asyncio.to_thread(self._expand_items, job, settings)
        if total == 0 and known:
            await asyncio.to_thread(self._finish_empty, job_id)
            await self.bus.alog("info", "job", f"Job #{job_id}: all {known} file(s) were already handled earlier; "
                                "nothing to do (retry them from Home or their job)", job_id=job_id)  # fmt: skip
            return
        if total == 0:
            await asyncio.to_thread(self._fail_job, job_id, "No video files found under the given paths")
            return
        self._publish_job(await asyncio.to_thread(self._job_out, job_id))
        facts: dict[int, ExtraFacts] = {}
        cancelled = await self._match_pass(job, settings, facts)
        if not cancelled:
            cancelled = await self._decide_pass(job, settings, facts)
        job_out = await asyncio.to_thread(self._finish_match, job_id, cancelled)
        self._publish_job(job_out)
        if job_out is None:
            return
        counts = job_out.counts
        await self.bus.alog(
            "info",
            "job",
            f"Job #{job_id} matched: {counts.get('matched', 0)} matched, {counts.get('review', 0)} need review, "
            f"{counts.get('skipped', 0)} skipped",
            job_id=job_id,
        )
        if not cancelled and job.auto_execute and counts.get(ItemStatus.MATCHED.value, 0):
            await self._enqueue_execute(job_id, None, None, statuses={ItemStatus.MATCHED})

    def _finish_match(self, job_id: int, cancelled: bool) -> JobOut | None:
        with self.db.session() as session:
            job = session.get(Job, job_id)
            if job is None:  # deleted while matching
                return None
            if cancelled or job.status == JobStatus.CANCELLED.value:
                job.status = JobStatus.CANCELLED.value
                job.finished_at = job.finished_at or utcnow()
            else:
                job.status = JobStatus.REVIEW.value
                settle_job(session, job, force=True)
            job.progress = 1.0
            session.flush()
            return load_job_out(session, job_id)

    # ------------------------------------------------------------------ loading and saving items

    def _pending_ids(self, job_id: int, matched: bool) -> list[int]:
        with self.db.session() as session:
            return list(session.scalars(
                select(JobItem.id).where(JobItem.job_id == job_id, JobItem.status == ItemStatus.PENDING.value,
                                         JobItem.matched == matched).order_by(JobItem.id)
            ))  # fmt: skip

    def _load_full_works(self, item_ids: Sequence[int]) -> list[Work]:
        """Items by id (ordered by id) with their candidates and probe: bulk ``IN`` loads."""
        ids = list(item_ids)
        with self.db.session() as session:
            items: list[JobItem] = []
            for start in range(0, len(ids), KNOWN_PATH_CHUNK):
                items.extend(session.scalars(select(JobItem).where(JobItem.id.in_(ids[start : start + KNOWN_PATH_CHUNK]))))
            details = load_details(session, [item.id for item in items])
            items.sort(key=lambda item: item.id)
            return [Work.from_item(item, details.get(item.id)) for item in items]

    @staticmethod
    def _light_works(session: Any, job_id: int, *, chosen: bool = False) -> list[Work]:
        """Every item of the job as a light work (``chosen``: with the slim chosen candidate, else it
        is loaded on demand by ``_hydrate``)."""
        columns = (*_LIGHT_COLUMNS, type_coerce(JobItem.chosen, Text)) if chosen else _LIGHT_COLUMNS
        rows = session.execute(select(*columns).where(JobItem.job_id == job_id).order_by(JobItem.id))
        return [
            Work(item_id=r[0], source_path=r[1], parsed=None, status=ItemStatus(r[2]), proposed_path=r[3],
                 path_overridden=bool(r[4]), light=True, selected_id=r[5], parsed_part=r[6],
                 chosen_raw=r[7] if chosen else None)  # fmt: skip
            for r in rows
        ]

    def _job_light_works(self, job_id: int, chosen: bool = False) -> list[Work]:
        with self.db.session() as session:
            return self._light_works(session, job_id, chosen=chosen)

    def _hydrate(self, context: Sequence[Work], best_ids: set[str]) -> None:
        """Blocking: load the slim chosen candidate of the light works selected as one of ``best_ids``
        (only those matter for di/dc/pc next to the items being rendered)."""
        need = {w.item_id: w for w in context
                if w.light and w.selected_id in best_ids and w.chosen_raw is None and w.cached_key is False}  # fmt: skip
        if not need:
            return
        ids = list(need)
        with self.db.session() as session:
            for start in range(0, len(ids), KNOWN_PATH_CHUNK):
                chunk = ids[start : start + KNOWN_PATH_CHUNK]
                for item_id, raw in session.execute(select(JobItem.id, type_coerce(JobItem.chosen, Text))
                                                    .where(JobItem.id.in_(chunk))):  # fmt: skip
                    need[item_id].chosen_raw = raw

    def _save_chunk(self, job_id: int, works: Sequence[Work]) -> list[int]:
        """Save ``works`` in one short transaction; returns the ids saved (deleted items are skipped)."""
        if not works:
            return []
        with self.db.session() as session:
            return self._write_works(session, works)

    @staticmethod
    def _write_works(session: Any, works: Sequence[Work]) -> list[int]:
        """Bulk UPDATE of the items by primary key plus an upsert of their candidates/probe rows."""
        ids = [w.item_id for w in works]
        existing: set[int] = set()
        for start in range(0, len(ids), KNOWN_PATH_CHUNK):
            existing.update(session.scalars(select(JobItem.id).where(JobItem.id.in_(ids[start : start + KNOWN_PATH_CHUNK]))))
        rows: list[dict[str, Any]] = []
        sides: list[dict[str, Any]] = []
        for work in works:
            if work.item_id in existing:
                values, side = work.row()
                rows.append(values)
                sides.append(side)
        if not rows:
            return []
        session.execute(update(JobItem), rows)
        upsert = sqlite_insert(JobItemCandidates)
        upsert = upsert.on_conflict_do_update(
            index_elements=[JobItemCandidates.item_id],
            set_={"candidates": upsert.excluded.candidates, "probe": upsert.excluded.probe},
        )
        session.execute(upsert, sides)
        return [row["id"] for row in rows]

    # ------------------------------------------------------------------ the two passes of a job match

    async def _match_pass(self, job: JobInfo, settings: AppSettings, facts: dict[int, ExtraFacts]) -> bool:
        """Parse, CRC, look up and probe every item not matched yet, SAVE_CHUNK at a time; each chunk is
        saved (items stay pending) before the next is loaded. Collects ``facts`` for the bonus-clip
        check. Returns cancelled."""
        ids = await asyncio.to_thread(self._pending_ids, job.id, False)
        hint = MatchHint(kind=job.kind) if job.kind else None
        span = (MATCH_HI - MATCH_LO) / max(1, len(ids))
        for start in range(0, len(ids), SAVE_CHUNK):
            if self._is_cancelled(job.id):
                return True
            chunk_ids = ids[start : start + SAVE_CHUNK]
            works = await asyncio.to_thread(self._load_full_works, chunk_ids)
            works = [w for w in works if w.status == ItemStatus.PENDING and not w.matched]
            report = self._progress(job.id, MATCH_LO + span * start, MATCH_LO + span * (start + len(chunk_ids)))
            if await self._match_works(job, works, settings, hint, report):
                return True  # this chunk is not saved: a rematch / resume does it again
            for work in works:
                work.matched = True
                if settings.skip_extras and (found := ExtraFacts.of(work)) is not None:
                    facts[work.item_id] = found
            self._publish_changed(job.id, await asyncio.to_thread(self._save_chunk, job.id, works))
        return False

    async def _decide_pass(self, job: JobInfo, settings: AppSettings, facts: dict[int, ExtraFacts]) -> bool:
        """Skip bonus clips, then render, plan and decide the matched items SAVE_CHUNK at a time with
        the whole job (narrow rows) as context, then settle the names files of the batch share.
        Returns cancelled."""
        light = await asyncio.to_thread(self._job_light_works, job.id, True)
        at = {w.item_id: index for index, w in enumerate(light)}
        targets = [w.item_id for w in light if w.status == ItemStatus.PENDING]
        if not targets:
            return False
        if settings.skip_extras:
            missing = [i for i in targets if i not in facts and light[at[i]].selected_id]  # a resumed job
            if missing:
                facts.update(await asyncio.to_thread(self._extra_facts_of, missing))
            eligible = [facts[i] for i in targets if i in facts]
            if eligible:
                skipped = await asyncio.to_thread(self._skip_extras, eligible, light, settings)
                for item_id in skipped:
                    light[at[item_id]].status = ItemStatus.SKIPPED
                self._publish_changed(job.id, skipped)
                targets = [i for i in targets if light[at[i]].status == ItemStatus.PENDING]
        expressions = await asyncio.to_thread(self._expression_lookup, job)
        overrides = await asyncio.to_thread(self._context_overrides, light)
        parts = await asyncio.to_thread(self._part_counts, light)
        folders = await asyncio.to_thread(self._folder_policy, job, job.mode, settings,
                                          [(w.source_path, w.status) for w in light])  # fmt: skip
        for start in range(0, len(targets), SAVE_CHUNK):
            if self._is_cancelled(job.id):
                return True
            works = await asyncio.to_thread(self._load_full_works, targets[start : start + SAVE_CHUNK])
            works = [w for w in works if w.status == ItemStatus.PENDING]
            await asyncio.to_thread(self._render_and_decide, job, works, settings, expressions, overrides, parts,
                                    folders)  # fmt: skip
            saved = await asyncio.to_thread(self._save_chunk, job.id, works)
            for work in works:
                entry = light[at[work.item_id]]
                entry.status, entry.proposed_path = work.status, work.proposed_path
                entry.version_tag, entry.ext_suffix = work.version_tag, work.ext_suffix
            self._publish_changed(job.id, saved)
            await self._set_progress(job.id, MATCH_HI + (0.98 - MATCH_HI) * min(1.0, (start + SAVE_CHUNK) / len(targets)))
        await self._settle_collisions(job, settings, light, set(targets), folders)
        return False

    def _render_and_decide(self, job: JobInfo, works: list[Work], settings: AppSettings,
                           expressions: Callable[[MediaKind], str], overrides: dict[int, dict[str, Any]],
                           parts: dict[tuple[Any, ...], set[int]], folders: FolderPolicy) -> None:  # fmt: skip
        """Blocking: render, plan and decide one chunk (collisions inside the batch are settled after
        every chunk is rendered, by ``_settle_collisions``)."""
        for work in works:
            # Only the warning survives the save between the passes.
            work.parse_failed = any(w.startswith("parse_error") for w in work.warnings)
            self._render_one(work, job, settings, expressions, overrides.get(work.item_id, {}))
        self._part_suffixes(works, parts)
        self._plan_all(job, works, settings, folders=folders)
        for work in works:
            self._decide(work, settings)

    async def _settle_collisions(self, job: JobInfo, settings: AppSettings, light: list[Work], targets: set[int],
                                 folders: FolderPolicy) -> None:  # fmt: skip
        """Two files of the batch got the same name: add the multi-version suffix where it tells them
        apart, flag what still collides, and plan + decide just those items again."""

        def resolve() -> dict[int, Work]:
            mine = [w for w in light if w.item_id in targets]
            before = {w.item_id: w.proposed_path for w in mine}
            self._resolve_collisions(mine, light)
            return {w.item_id: w for w in mine if w.duplicate or w.proposed_path != before[w.item_id]}

        settled = await asyncio.to_thread(resolve)
        ids = sorted(settled)
        for start in range(0, len(ids), SAVE_CHUNK):
            if self._is_cancelled(job.id):
                return
            works = await asyncio.to_thread(self._load_full_works, ids[start : start + SAVE_CHUNK])
            await asyncio.to_thread(self._redecide, job, works, settled, settings, folders)
            self._publish_changed(job.id, await asyncio.to_thread(self._save_chunk, job.id, works))

    def _redecide(self, job: JobInfo, works: list[Work], settled: dict[int, Work], settings: AppSettings,
                  folders: FolderPolicy) -> None:  # fmt: skip
        """Blocking: apply what ``_settle_collisions`` found to the saved items, then plan and decide them again."""
        for work in works:
            entry = settled[work.item_id]
            if entry.proposed_path != work.proposed_path and work.proposed_path and entry.version_tag:
                work.proposed_path = entry.proposed_path
                if work.relative:
                    work.relative = _insert_suffix(work.relative, entry.version_tag, entry.ext_suffix)
            work.duplicate = entry.duplicate
            work.warnings = [w for w in work.warnings if not w.startswith(_REDECIDED)]
            work.parse_failed = any(w.startswith("parse_error") for w in work.warnings)
            work.status = ItemStatus.PENDING
        self._plan_all(job, works, settings, folders=folders)
        for work in works:
            self._decide(work, settings)

    def _extra_facts_of(self, item_ids: list[int]) -> dict[int, ExtraFacts]:
        """Blocking: bonus-clip facts of items matched by an earlier (interrupted) run of the job."""
        out: dict[int, ExtraFacts] = {}
        for start in range(0, len(item_ids), SAVE_CHUNK):
            for work in self._load_full_works(item_ids[start : start + SAVE_CHUNK]):
                if (found := ExtraFacts.of(work)) is not None:
                    out[work.item_id] = found
        return out

    def _skip_extras(self, facts: list[ExtraFacts], context: list[Work], settings: AppSettings) -> list[int]:
        """Blocking: skip the matched movies of the job that are really bonus clips. Returns their ids."""
        found = [f.item_id for f in _extras_among(facts, context, settings)]
        if not found:
            return []
        with self.db.session() as session:
            items: list[JobItem] = []
            for start in range(0, len(found), KNOWN_PATH_CHUNK):
                items.extend(session.scalars(select(JobItem).where(JobItem.id.in_(found[start : start + KNOWN_PATH_CHUNK]))))
            for item in items:
                item.status = ItemStatus.SKIPPED.value
                add_warning(item, "extra")
                item.updated_at = utcnow()
            prune_finished(session, items)
            return [item.id for item in items]

    # ------------------------------------------------------------------ processing steps

    def _is_cancelled(self, job_id: int) -> bool:
        return job_id in self._cancel or self._stopping

    async def _match_works(self, job: JobInfo, works: list[Work], settings: AppSettings, hint: MatchHint | None,
                           report: ProgressFn | None = None) -> bool:  # fmt: skip
        """Parse, skip clutter, look up and probe ``works`` in place. CRCs are verified alongside the
        lookups (one reads the disk, the other waits on the network). Returns cancelled."""
        if not works:
            return False
        await asyncio.to_thread(self._parse_all, works)
        for work in works:
            self._apply_clutter(work, settings)  # rule 1
        active = [w for w in works if w.status != ItemStatus.SKIPPED]
        crc = asyncio.create_task(self._verify_crcs(job.id, active)) if settings.verify_crc else None  # rule 2
        try:
            cancelled = await self._lookup(job.id, active, hint, report)
        except BaseException:
            if crc is not None:
                crc.cancel()
            raise
        if crc is not None:
            await crc
        if cancelled or self._is_cancelled(job.id):
            return True
        # Probing is minutes for a big batch: the report keeps the job's progress and updated_at moving.
        await self._probe_all(active, settings, report=report)
        return self._is_cancelled(job.id)

    async def _lookup(self, job_id: int, works: list[Work], hint: MatchHint | None, report: ProgressFn | None) -> bool:
        for start in range(0, len(works), MATCH_CHUNK):
            if self._is_cancelled(job_id):
                return True
            chunk = works[start : start + MATCH_CHUNK]
            parsed = [w.parsed for w in chunk if w.parsed is not None]
            try:
                results = await self.matcher.match_many(parsed, hint)
                if len(results) != len(parsed):
                    raise RuntimeError("matcher returned a different number of results")
            except Exception as exc:
                log.exception("matcher failed")
                results = [MatchResult(warnings=[f"match_error: {exc}"]) for _ in parsed]
            for work, result in zip(chunk, results, strict=False):
                self._apply_match(work, result)
            if report is not None:
                await report(MATCH_LOOKUP_SHARE * (start + len(chunk)) / max(1, len(works)))
        return False

    async def _verify_crcs(self, job_id: int, works: list[Work]) -> None:
        """One file after the other (a RAID reads one big file faster than several at once)."""
        for work in works:
            if self._is_cancelled(job_id):
                return
            await self._verify_crc(work)

    async def _process(self, job: JobInfo, works: list[Work], settings: AppSettings, *,
                       siblings: list[Work] | None = None, hint: MatchHint | None = None,
                       folders: FolderPolicy | None = None) -> bool:  # fmt: skip
        """Parse, CRC, match, probe, render, plan and decide ``works`` (a few hundred at most) in place,
        with ``siblings`` (light works of the rest of the job) as context. Returns cancelled."""
        if not works:
            return False
        base_hint = hint or (MatchHint(kind=job.kind) if job.kind else None)
        if await self._match_works(job, works, settings, base_hint):
            return True
        context = _merge_context(siblings or [], works)
        active = [w for w in works if w.status != ItemStatus.SKIPPED]
        # Bonus clips the filename didn't give away (runtime, the feature right next to them); before
        # rendering, so they never collide with the feature they were matched to.
        if settings.skip_extras:
            await asyncio.to_thread(self._flag_extras, active, context, settings)
            active = [w for w in active if w.status != ItemStatus.SKIPPED]
        expressions = await asyncio.to_thread(self._expression_lookup, job)
        await asyncio.to_thread(self._hydrate, context, {w.best.id for w in active if w.best is not None})
        # CPU-bound (bindings + template evaluation for every item): keep it off the event loop.
        await asyncio.to_thread(self._render_batch, job, active, settings, expressions, context)
        # plan + decide (one thread hop for the whole batch instead of one per item)
        await asyncio.to_thread(self._plan_all, job, active, settings, context, folders)
        for work in active:
            self._decide(work, settings)
        for work in works:
            work.matched = True
        return False

    def _parse_all(self, works: list[Work]) -> None:
        for work in works:
            if work.parsed is not None:
                continue
            try:
                work.parsed = parser_mod.parse(work.source_path)
            except Exception as exc:
                name = os.path.basename(work.source_path)
                ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                work.parsed = ParsedMedia(path=work.source_path, filename=name, extension=ext)
                work.parse_failed = True
                work.warn(f"parse_error: {exc}")

    @staticmethod
    def _apply_clutter(work: Work, settings: AppSettings) -> None:
        parsed = work.parsed
        if parsed is None:
            return
        if parsed.is_sample and settings.skip_samples:
            work.status = ItemStatus.SKIPPED
            work.warn("sample")
        elif parsed.is_extra and settings.skip_extras:
            work.status = ItemStatus.SKIPPED
            work.warn("extra")

    @staticmethod
    def _flag_extras(works: list[Work], context: list[Work], settings: AppSettings) -> None:
        """Blocking (folder listings): skip matched movies that are really bonus clips."""
        facts = [f for f in (ExtraFacts.of(w) for w in works) if f is not None]
        if not facts:
            return
        by_id = {w.item_id: w for w in works}
        for found in _extras_among(facts, context, settings):
            work = by_id[found.item_id]
            work.status = ItemStatus.SKIPPED
            work.warn("extra")

    async def _verify_crc(self, work: Work) -> None:
        if work.parsed is None or not work.parsed.crc32 or work.crc_ok is not None:
            if work.crc_ok is False:
                work.warn("crc_mismatch")
            return
        try:
            actual = await self._in_files(hashing.crc32_file, work.source_path)
        except Exception as exc:
            work.warn(f"crc_error: {exc}")
            return
        work.crc_ok = actual.upper() == work.parsed.crc32.upper()
        if not work.crc_ok:
            work.warn("crc_mismatch")

    @staticmethod
    def _apply_match(work: Work, result: MatchResult) -> None:
        # The matcher ranks up to ten to score them; an item keeps the top few (and its pick).
        work.candidates = trim_candidates(result.candidates, result.best)
        work.best = result.best
        work.confidence = float(result.confidence or 0.0)
        for warning in result.warnings:
            work.warn(warning)
        for provider, error in result.provider_errors.items():
            work.warn(f"provider_error: {provider}: {error}")

    async def _probe_all(self, works: list[Work], settings: AppSettings, *, report: ProgressFn | None = None) -> None:
        """Probe matched items. ``report`` gets the share of the step done (lookups were the first part)."""
        if not settings.probe_media:
            return
        try:
            backend = await asyncio.to_thread(probe_mod.available)
        except Exception:
            backend = None
        if not backend:
            return
        todo = [w for w in works if w.best is not None and w.probe is None]
        if not todo:
            return
        done = 0
        queue = iter(todo)

        # mediainfo/ffprobe spend most of their time waiting on the disk and process start-up: a few
        # workers in parallel keep a RAID busy without one task (or process) per file of a big job.
        async def worker() -> None:
            nonlocal done
            for work in queue:  # shared iterator: each file goes to exactly one worker
                try:
                    work.probe = await self._in_files(probe_mod.probe, work.source_path)
                except Exception as exc:  # probing is optional
                    log.debug("probe failed for %s: %s", work.source_path, exc)
                done += 1
                if report is not None:
                    await report(MATCH_LOOKUP_SHARE + (1 - MATCH_LOOKUP_SHARE) * done / len(todo))

        await asyncio.gather(*(worker() for _ in range(min(PROBE_CONCURRENCY, len(todo)))))

    # ------------------------------------------------------------------ rendering

    @staticmethod
    def _context_overrides(context: Sequence[Work], only: set[str] | None = None) -> dict[int, dict[str, Any]]:
        """Batch facts the format engine can't know alone: ``pc``, ``di``, ``dc``. With ``only`` (the
        candidate ids of the items being rendered), siblings matched to anything else are skipped
        without reading their candidate."""
        groups: dict[tuple[Any, ...], list[Work]] = defaultdict(list)
        parts: dict[str, int] = defaultdict(int)
        for work in context:
            best_id = work.best_id
            if best_id is None or work.status == ItemStatus.SKIPPED or (only is not None and best_id not in only):
                continue
            key = work.match_key()
            if key is None:
                continue
            part = work.part
            groups[(key[0], key[1], part)].append(work)
            if part:
                parts[key[0]] = max(parts[key[0]], part)
        overrides: dict[int, dict[str, Any]] = defaultdict(dict)
        for (best_id, _episodes, part), members in groups.items():
            for index, work in enumerate(sorted(members, key=lambda w: w.item_id), start=1):
                overrides[work.item_id]["di"] = index
                overrides[work.item_id]["dc"] = len(members)
                if part and best_id in parts:
                    overrides[work.item_id]["pc"] = parts[best_id]
        return overrides

    @staticmethod
    def _part_counts(context: Sequence[Work], only: set[str] | None = None) -> dict[tuple[Any, ...], set[int]]:
        """(candidate id, its (season, episode)s) -> the split-episode parts the batch has of it."""
        parts: dict[tuple[Any, ...], set[int]] = defaultdict(set)
        for work in context:
            best_id = work.best_id
            if best_id is None or work.status == ItemStatus.SKIPPED or (only is not None and best_id not in only):
                continue
            key = work.match_key()
            if key is not None and key[2] is not None:
                parts[(key[0], tuple((season, episode) for season, episode, _abs in key[1]))].add(key[2])
        return parts

    def _render_one(self, work: Work, job: JobInfo, settings: AppSettings,
                    expressions: Callable[[MediaKind], str], overrides: dict[str, Any]) -> None:  # fmt: skip
        work.warnings = _strip_recomputed(work.warnings)
        work.render_failed = False
        work.duplicate = False
        work.conflict = "none"
        work.folder_rename = None
        work.bindings = None
        work.version_tag, work.ext_suffix = None, ""
        work.kind = item_kind(job.kind, work.best, work.parsed)
        if work.best is None or work.parsed is None:
            work.relative = None
            if not work.path_overridden:
                work.proposed_path = None
            return
        try:
            template = compile_format(expressions(work.kind))
        except FormatSyntaxError as exc:
            work.render_failed = True
            work.warn(f"format_error: {exc.message}")
            work.relative = None
            if not work.path_overridden:
                work.proposed_path = None
            return
        extra = dict(overrides)
        if work.crc_ok and work.parsed.crc32:
            extra.setdefault("crc32", work.parsed.crc32)
        bindings = build_binding(work.parsed, work.best, work.probe, overrides=extra)
        work.bindings = bindings
        work.version_tag, work.ext_suffix = _version_suffix(bindings), bindings.extension_suffix
        result = template.render_detailed(bindings)
        for issue in result.issues:
            if issue.severity == "warning":
                work.warn(f"format: {issue.message}")
            else:
                work.warn(f"format_error: {issue.message}")
        if result.path is None:
            work.render_failed = True
            work.relative = None
            if not work.path_overridden:
                work.proposed_path = None
            return
        work.relative = result.path
        root = dest_root_for(job.dest_root, work.kind, settings)
        if work.path_overridden:
            work.relative = relative_to_root(work.proposed_path, root)
            return
        if not root:
            work.warn("no_library_root")
            work.proposed_path = None
            return
        work.proposed_path = join_dest(root, result.path)

    def _render_batch(self, job: JobInfo, works: list[Work], settings: AppSettings,
                      expressions: Callable[[MediaKind], str], context: list[Work]) -> None:  # fmt: skip
        """Blocking: render ``works`` with ``context`` (the whole job, ``_hydrate``-d for their matches)."""
        only = {w.best.id for w in works if w.best is not None}
        overrides = self._context_overrides(context, only)
        for work in works:
            self._render_one(work, job, settings, expressions, overrides.get(work.item_id, {}))
        self._part_suffixes(works, self._part_counts(context, only))
        self._resolve_collisions(works, context)

    @staticmethod
    def _part_suffixes(works: list[Work], parts: dict[tuple[Any, ...], set[int]]) -> None:
        """A release that splits one provider episode in two ("Just Say Yes Part 1" / "Part 2", both
        TMDb's S01E01) gets " - part1" / " - part2", once both parts are in the job (``_part_counts``)."""
        for w in works:
            if w.best is None or w.best.part is None or w.path_overridden or not w.proposed_path or w.bindings is None:
                continue
            if len(parts.get((w.best.id, tuple((e.season, e.episode) for e in w.best.episodes)), ())) < 2:
                continue
            suffix, ext = f" - part{w.best.part}", w.ext_suffix
            w.proposed_path = _insert_suffix(w.proposed_path, suffix, ext)
            if w.relative:
                w.relative = _insert_suffix(w.relative, suffix, ext)

    @staticmethod
    def _resolve_collisions(works: list[Work], context: Sequence[Work]) -> None:
        """Multi-version suffix (SPEC §8.6) for batch collisions; flag what still collides. The suffix
        is only added where it tells the files apart (two 1080p copies stay a plain duplicate)."""
        targets = {w.item_id for w in works}

        def groups() -> dict[str, list[Work]]:
            grouped: dict[str, list[Work]] = defaultdict(list)
            for w in context:
                if w.status in {ItemStatus.SKIPPED, ItemStatus.DONE} and w.item_id not in targets:
                    continue
                if w.item_id in targets and w.status == ItemStatus.SKIPPED:
                    continue
                if w.proposed_path:
                    grouped[_norm_key(w.proposed_path)].append(w)
            return grouped

        for members in groups().values():
            if len(members) < 2:
                continue
            # None = a sibling not rendered in this pass (its quality is unknown here)
            suffixes = {w.item_id: w.version_tag for w in members}
            for w in members:
                if w.item_id not in targets or w.path_overridden or w.version_tag is None:
                    continue
                suffix = suffixes[w.item_id]
                if not suffix or all(s == suffix for i, s in suffixes.items() if i != w.item_id):
                    continue
                ext = w.ext_suffix
                w.proposed_path = _insert_suffix(w.proposed_path or "", suffix, ext)
                if w.relative:
                    w.relative = _insert_suffix(w.relative, suffix, ext)
        for members in groups().values():
            if len(members) < 2:
                continue
            for w in members:
                if w.item_id in targets:
                    w.duplicate = True

    def _folder_policy(self, job: JobInfo, mode: RenameMode, settings: AppSettings,
                       items: Iterable[tuple[str, ItemStatus]]) -> FolderPolicy:  # fmt: skip
        """Folder handling for ``mode`` (a dry run of a move job plans the folder renames a real run
        would do), given every item of the job as (source path, status)."""
        moves = (job.mode if mode == RenameMode.TEST else mode) == RenameMode.MOVE
        if not moves or not (settings.rename_folders_in_place or settings.remove_empty_source_folders):
            return FolderPolicy()
        with self.db.session() as session:
            watched = list(session.scalars(select(WatchFolder.path)))
        protected = [*settings.allowed_roots, *settings.library_roots.values(), *watched,
                     *(m.target for m in settings.path_mappings), job.dest_root or ""]  # fmt: skip
        return FolderPolicy(
            rename=settings.rename_folders_in_place,
            remove_empty=settings.remove_empty_source_folders and mode == RenameMode.MOVE,
            protected=sorted({p for p in protected if p}),
            shared=shared_source_dirs(items) if settings.rename_folders_in_place else frozenset(),
        )

    def _plan_all(self, job: JobInfo, works: list[Work], settings: AppSettings, context: Sequence[Work] | None = None,
                  folders: FolderPolicy | None = None) -> None:  # fmt: skip
        """Blocking: plan ``works``; the folder policy comes from ``context`` (every item of the job)
        unless the caller computed it once for many chunks."""
        if folders is None:
            folders = self._folder_policy(job, job.mode, settings, ((w.source_path, w.status) for w in context or works))
        for work in works:
            self._plan_sync(work, job.mode, settings, folders)
            # A clip matched to a title whose (much bigger) feature is already in the library.
            work.likely_extra = (
                settings.skip_extras and work.conflict == "exists" and _extra_eligible(work)
                and _title_differs(work) and _dwarfed_by_existing(work)
            )  # fmt: skip

    def _plan_sync(self, work: Work, mode: RenameMode, settings: AppSettings,
                   folders: FolderPolicy | None = None) -> None:  # fmt: skip
        work.folder_rename = None
        if not work.proposed_path or work.parsed is None:
            return
        folders = folders or FolderPolicy()
        try:
            plan = fileops.plan_operation(
                work.source_path,
                work.proposed_path,
                mode,
                settings.conflict_policy,
                sidecar_extensions=settings.sidecar_extensions if settings.move_sidecars else None,
                incoming_rank=work.parsed.quality.rank(),
                rename_folder=folders.renames(work.source_path),
                protected_dirs=folders.protected,
            )
        except Exception as exc:
            work.warn(f"plan_error: {exc}")
            work.render_failed = True
            return
        work.sidecars = [s.source for s in plan.sidecars]
        work.conflict = plan.conflict
        work.folder_rename = plan.folder_rename
        if os.path.abspath(plan.destination) != os.path.abspath(work.proposed_path):
            # fileops reused an existing folder's spelling ("Doctor Who (2005)" for "Doctor who
            # (2005)"); preview what execution will do. Only casing changes, so the count holds.
            work.proposed_path = plan.destination
            if work.relative and not work.path_overridden:
                depth = len(work.relative.split("/"))
                work.relative = "/".join(Path(plan.destination).parts[-depth:])

    @staticmethod
    def _decide(work: Work, settings: AppSettings) -> None:
        """Item rules 2-5 (SPEC §3.3); rule 1 (clutter) ran before matching."""
        if work.status == ItemStatus.SKIPPED:
            return
        if work.likely_extra and settings.skip_extras and not work.manual and not work.allow_extra:
            work.status = ItemStatus.SKIPPED
            work.warn("extra")
            return
        review = False
        if work.crc_ok is False or work.parse_failed:
            review = True
        if work.best is None:
            work.warn("no_match")
            review = True
        elif work.confidence < settings.min_confidence and not work.manual:
            work.warn("low_confidence")
            review = True
        if not work.manual and any(w in _REVIEW_WARNINGS for w in work.warnings):
            review = True
        if work.best is not None and not work.proposed_path:
            if not work.render_failed and not any(w == "no_library_root" for w in work.warnings):
                work.warn("no_destination")
            review = True
        if work.render_failed:
            review = True
        if work.conflict == "exists":
            if settings.conflict_policy in {ConflictPolicy.SKIP, ConflictPolicy.FAIL}:
                work.warn("destination_exists")
                review = True
            else:
                # The policy resolves it, but the preview must still say that a file will be replaced
                # or that this one gets a numbered name (it used to look like a plain "matched").
                work.warn(_EXISTING_POLICY_WARNING[settings.conflict_policy])
        if work.conflict == "same_file":
            work.warn("already_in_place")
        if work.duplicate:
            work.warn("duplicate_in_batch")
            review = True
        if review:
            work.status = ItemStatus.REVIEW
        else:
            work.status = ItemStatus.APPROVED if work.manual else ItemStatus.MATCHED

    # ------------------------------------------------------------------ job actions

    def _check_idle_job(self, session: Any, job_id: int) -> Job:
        job = get_job(session, job_id)
        if JobStatus(job.status) in ACTIVE_JOB_STATUSES:
            raise conflict(f"job is {job.status}; wait for it to finish or cancel it")
        return job

    async def execute_job(self, job_id: int, req: ExecuteRequest) -> JobOut:
        def check() -> None:
            with self.db.session() as session:
                job = self._check_idle_job(session, job_id)
                if job.status == JobStatus.CANCELLED.value:
                    raise conflict("job is cancelled; rematch it first")
                query = select(JobItem.id).where(
                    JobItem.job_id == job_id, JobItem.status.in_([s.value for s in RUNNABLE_ITEM_STATUSES])
                )
                if req.item_ids:
                    query = query.where(JobItem.id.in_(req.item_ids))
                if not session.scalars(query).first():
                    raise conflict("nothing to execute: no matched or approved items")

        await asyncio.to_thread(check)
        return await self._enqueue_execute(job_id, req.item_ids, req.mode)

    async def _enqueue_execute(self, job_id: int, item_ids: list[int] | None, mode: RenameMode | None,
                               statuses: set[ItemStatus] | None = None) -> JobOut:  # fmt: skip
        wanted = statuses or set(RUNNABLE_ITEM_STATUSES)

        def mark() -> tuple[list[int], JobOut, str]:
            with self.db.session() as session:
                job = get_job(session, job_id)
                query = select(JobItem.id).where(
                    JobItem.job_id == job_id, JobItem.status.in_([s.value for s in wanted])
                ).order_by(JobItem.id)
                ids: list[int] = []
                if item_ids:
                    for start in range(0, len(item_ids), KNOWN_PATH_CHUNK):
                        ids.extend(session.scalars(query.where(JobItem.id.in_(item_ids[start : start + KNOWN_PATH_CHUNK]))))
                    ids.sort()
                else:
                    ids = list(session.scalars(query))
                job.status = JobStatus.EXECUTING.value
                job.progress = 0.0
                job.finished_at = None
                job.updated_at = utcnow()
                session.flush()
                return ids, load_job_out(session, job_id), job.source

        ids, out, source = await asyncio.to_thread(mark)
        self._cancel.discard(job_id)
        self._busy.add(job_id)
        self._publish_job(out)
        queue = self._exec_auto if source in AUTO_SOURCES else self._exec_queue
        assert queue is not None and self._exec_wake is not None
        queue.put_nowait((job_id, ids, mode))
        self._exec_wake.set()
        return out

    async def _exec_worker(self) -> None:
        while True:
            entry = await self._next_exec()
            if entry is None or self._stopping:
                return
            await self._run_exec_entry(entry)

    async def _next_exec(self) -> ExecEntry | None:
        """The next run: watch-folder / webhook runs before manual ones."""
        assert self._exec_auto is not None and self._exec_queue is not None and self._exec_wake is not None
        while True:
            for queue in (self._exec_auto, self._exec_queue):
                if not queue.empty():
                    return queue.get_nowait()
            self._exec_wake.clear()
            await self._exec_wake.wait()

    async def _run_exec_entry(self, entry: ExecEntry) -> None:
        job_id, ids, mode = entry
        self._busy.add(job_id)
        try:
            await self._run_execute(job_id, ids or [], mode)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("executing job %s failed", job_id)
            await asyncio.to_thread(self._fail_job, job_id, f"internal error: {exc}")
        finally:
            self._busy.discard(job_id)
            self._cancel.discard(job_id)

    async def _run_waiting_automation(self) -> None:
        """Between two files of a manual run: execute the watch-folder / webhook runs queued meanwhile
        (a new download shouldn't wait hours behind a library import)."""
        queue = self._exec_auto
        while queue is not None and not queue.empty() and not self._stopping:
            entry = queue.get_nowait()
            if entry is None:  # shutting down: leave the sentinel for the worker
                queue.put_nowait(None)
                return
            await self._run_exec_entry(entry)

    async def _run_execute(self, job_id: int, item_ids: list[int], mode_override: RenameMode | None) -> None:
        def load() -> JobInfo | None:
            with self.db.session() as session:
                job = session.get(Job, job_id)
                return JobInfo.of(job) if job is not None else None

        job = await asyncio.to_thread(load)
        if job is None:
            return
        mode = mode_override or job.mode
        settings = self.store.get_app()
        folders = await asyncio.to_thread(self._job_folder_policy, job, mode, settings)
        touched: list[str] = []
        done = failed = 0
        cancelled = False
        last_progress = 0.0
        for index, item_id in enumerate(item_ids):
            if self._is_cancelled(job_id):
                cancelled = True
                break
            marked = await asyncio.to_thread(self._mark_executing, item_id)
            if marked is None:
                continue
            marked_out, prior = marked
            self._publish_items([marked_out])
            outcome = await self._in_files(self._execute_item_sync, job, item_id, mode, settings, prior, folders)
            if outcome.item is not None:
                self._publish_items([outcome.item])
            if outcome.operation is not None:
                self.bus.publish("operation.created", outcome.operation)
            touched.extend(outcome.touched)
            done += 1 if outcome.ok else 0
            failed += 0 if outcome.ok else 1
            # Each progress update re-counts the job's items: at most once a second (and at the end),
            # not once per file, so large batches stay O(n).
            now = time.monotonic()
            if now - last_progress >= PROGRESS_INTERVAL or index + 1 == len(item_ids):
                last_progress = now
                await self._set_progress(job_id, (index + 1) / max(1, len(item_ids)))
            if job.source not in AUTO_SOURCES:
                await self._run_waiting_automation()
        cancelled = cancelled or job_id in self._cancel
        if mode == RenameMode.MOVE and done and not cancelled and settings.clean_release_folders:
            await self._clean_releases(job, settings)  # before the job reads as finished
        out = await asyncio.to_thread(self._finish_execute, job_id, cancelled)
        self._publish_job(out)
        level = "error" if failed and not done else ("warning" if failed else "info")
        await self.bus.alog(level, "fileops", f"Job #{job_id}: {done} file(s) processed ({mode.value}), {failed} failed",
                            job_id=job_id)  # fmt: skip
        if mode != RenameMode.TEST and touched:
            self.refresher.schedule(touched)

    async def _clean_releases(self, job: JobInfo, settings: AppSettings) -> None:
        try:
            held, kept = await asyncio.to_thread(self._clean_releases_sync, job, settings)
        except Exception:
            log.exception("release folder cleanup failed for job #%s", job.id)
            return
        for entry in held:
            await self.bus.alog(
                "info", "fileops",
                f"Cleaned up the download folder {os.path.basename(entry.original)} ({entry.files} leftover "
                f"file(s), {fileops._fmt_bytes(entry.bytes)}); it is kept in {os.path.dirname(os.path.dirname(entry.held))} "
                f"for {release_mod.LEFTOVER_KEEP_HOURS} h, then deleted",
                job_id=job.id,
            )  # fmt: skip
        for folder, reason in kept:
            await self.bus.alog("info", "fileops", f"Left the download folder {os.path.basename(folder)} in place: "
                                f"{reason}", job_id=job.id)  # fmt: skip

    def _clean_releases_sync(self, job: JobInfo, settings: AppSettings
                             ) -> tuple[list[release_mod.Held], list[tuple[str, str]]]:  # fmt: skip
        """After a move job: move each download folder the job took a video from into the holding
        folder once nothing in it is still needed (see :mod:`schema_app.services.release`).

        Only for watch-folder and webhook jobs, and only a folder that provably belongs to that one
        download: starting at the folder a webhook named, or at the first non-category folder below a
        watch folder, the first folder down towards the video that is named after it (same title and
        year / season), never a protected folder (allowed, library or watch roots). An unknown category
        folder (``/downloads/4k``) or a folder of home videos never qualifies. The folder is kept when
        it holds anything but that download's clutter (:func:`release_mod.inspect`: temp files of a
        download client, other folders, archives, documents, music, more than a few MB of leftovers,
        links) or a video Schema has not moved, other than one it skipped as a sample or extra.

        One walk per folder decides and is reused for the move, and the holding folders used are
        pruned once at the end."""
        if job.source not in (JobSource.WATCH.value, JobSource.WEBHOOK.value):
            return [], []
        with self.db.session() as session:
            watched = [p for p, on in session.execute(select(WatchFolder.path, WatchFolder.enabled)) if on]
            rows = session.execute(
                select(JobItem.source_path, JobItem.status).where(JobItem.job_id == job.id)
            ).all()
        protected = [*settings.allowed_roots, *settings.library_roots.values(), *watched,
                     *(m.target for m in settings.path_mappings), job.dest_root or ""]  # fmt: skip
        protected = [p for p in protected if p]
        containers = release_mod.container_names(settings.category_kinds)
        videos = {e.lower().lstrip(".") for e in settings.video_extensions} | fileops.VIDEO_EXTENSIONS
        named = [
            os.path.abspath(p) for p in job.input_paths
            if job.source == JobSource.WEBHOOK.value and os.path.isdir(p)
        ]  # fmt: skip

        def top_of(source: str) -> Path | None:
            """Where the search for the download's own folder starts."""
            for folder in named:  # the download folder a webhook reported
                if release_mod.owning_root(source, [folder]) is None:
                    continue
                if os.path.basename(folder).casefold() not in containers:
                    return Path(folder)
                top = release_mod.release_folder(source, [folder], containers)
                if top is not None:
                    return top
            return release_mod.release_folder(source, watched, containers)

        done = [source for source, status in rows if ItemStatus(status) == ItemStatus.DONE]
        releases: dict[str, Path] = {}
        matched_tops: set[str] = set()
        unmatched: dict[str, tuple[Path, str]] = {}
        for source in done:
            top = top_of(source)
            if top is None:
                continue  # a loose file in a watch folder or a category folder: nothing to clean
            folder = release_mod.owned_release(source, top)
            if folder is None:
                unmatched.setdefault(_norm_key(str(top)), (top, os.path.basename(source)))
                continue
            matched_tops.add(_norm_key(str(top)))
            releases.setdefault(_norm_key(str(folder)), folder)
        kept: list[tuple[str, str]] = [
            (str(top), f"its name does not match {name}, so it may hold other downloads")
            for key, (top, name) in unmatched.items()
            if key not in matched_tops and os.path.isdir(top)
        ]  # fmt: skip
        held: list[release_mod.Held] = []
        pruning: dict[str, tuple[Path, Path]] = {}
        for folder in releases.values():
            if not os.path.isdir(folder) or os.path.islink(folder):
                continue  # already gone (removed as empty, or cleaned by an earlier run)
            if not paths_mod.is_within(folder, settings.allowed_roots) or fileops.is_protected(folder, protected):
                continue
            root = release_mod.owning_root(folder, [*watched, *settings.allowed_roots])
            if root is None:
                kept.append((str(folder), "it is not below a watch folder or allowed root as written"))
                continue
            stray = self._release_stray_video(folder, done)
            if stray:
                kept.append((str(folder), f"its name does not match {stray}, which sat right in it"))
                continue
            contents = release_mod.inspect(
                folder, video_extensions=videos, busy_patterns=settings.ignore_patterns,
                subdir_ok=lambda sub, release=folder.name: release_mod.names_match(release, sub),
            )  # fmt: skip
            reason = self._release_still_needed(contents)
            if reason:
                kept.append((str(folder), reason))
                continue
            try:
                entry = release_mod.hold_release(folder, root, contents=contents)
            except OSError as exc:
                kept.append((str(folder), f"it could not be moved aside ({exc.strerror or exc})"))
                continue
            held.append(entry)
            pruning.setdefault(_norm_key(entry.base), (Path(entry.base), root))
        for base, root in pruning.values():
            release_mod.prune_holding(base, root=root)
        return held, kept

    @staticmethod
    def _release_stray_video(folder: Path, done: list[str]) -> str | None:
        """A moved video that sat right in ``folder`` without being named after it (other than an extra
        or sample of the release): then the folder is somebody's collection, not this download's."""
        for source in done:
            if _norm_key(os.path.dirname(source)) != _norm_key(str(folder)):
                continue
            name = os.path.basename(source)
            if release_mod.names_match(folder.name, name):
                continue
            parsed = parser_mod.parse(name)
            if not (parsed.is_extra or parsed.is_sample):
                return name
        return None

    def _release_still_needed(self, contents: release_mod.Contents) -> str | None:
        """Why the inspected folder must stay (None: nothing in it is needed any more): what
        :func:`release_mod.inspect` found, or a video Schema has not moved, other than one it skipped
        as a sample or extra."""
        if contents.problem:
            return contents.problem
        present = list(contents.videos)
        if not present:
            return None
        rows: list[Any] = []
        with self.db.session() as session:
            for start in range(0, len(present), 500):  # far below SQLite's bound-parameter limit
                rows.extend(session.execute(
                    select(JobItem.id, JobItem.source_path, JobItem.status, JobItem.warnings)
                    .where(JobItem.source_path.in_(present[start:start + 500]))
                ).all())  # fmt: skip
        rows.sort(key=lambda r: r[0])
        latest = {_norm_key(source): (ItemStatus(status), warnings or []) for _id, source, status, warnings in rows}
        for path in present:
            status, warnings = latest.get(_norm_key(path), (None, []))
            clutter = any(w == n or w.startswith(n + ":") for w in warnings for n in ("sample", "extra"))
            if status != ItemStatus.SKIPPED or not clutter:
                return f"{os.path.basename(path)} has not been moved to the library yet"
        return None

    def _job_folder_policy(self, job: JobInfo, mode: RenameMode, settings: AppSettings) -> FolderPolicy:
        with self.db.session() as session:
            rows = session.execute(select(JobItem.source_path, JobItem.status).where(JobItem.job_id == job.id)).all()
        return self._folder_policy(job, mode, settings, [(source, ItemStatus(status)) for source, status in rows])

    def _mark_executing(self, item_id: int) -> tuple[JobItemOut, ItemStatus] | None:
        """Mark a runnable item EXECUTING; returns it and the status it had (a dry run restores it)."""
        with self.db.session() as session:
            item = session.get(JobItem, item_id)
            if item is None or ItemStatus(item.status) not in RUNNABLE_ITEM_STATUSES:
                return None
            prior = ItemStatus(item.status)
            item.status = ItemStatus.EXECUTING.value
            item.updated_at = utcnow()
            session.flush()
            return item_out(item, slim=True), prior

    def _existing_rank(self, destination: str) -> int | None:
        """Quality of the file already at the destination, or None when it can't be told.

        Files Schema named itself usually carry no quality tags ("The Matrix (1999).mkv"), which used
        to rank 0, so any incoming 720p "upgraded" an existing 2160p. Without a resolution in the
        name, the file is probed; if that is impossible the upgrade policy skips instead of guessing.
        """
        if not os.path.exists(destination):
            return None
        try:
            quality = parser_mod.parse(destination).quality
            if not quality.resolution:
                info = probe_mod.probe(destination) if probe_mod.available() else None
                resolution = _vf_from_dims(info.width, info.height) if info is not None else None
                if not resolution:
                    return None
                quality = quality.model_copy(
                    update={"resolution": resolution, "bit_depth": info.bit_depth or quality.bit_depth}
                )
            return quality.rank()
        except Exception:
            return None

    def _execute_item_sync(self, job: JobInfo, item_id: int, mode: RenameMode, settings: AppSettings,
                           prior: ItemStatus = ItemStatus.APPROVED,
                           folders: FolderPolicy | None = None) -> ExecOutcome:  # fmt: skip
        """Plan + execute + journal one item. Runs in a thread and records its own outcome, so a
        shutdown that abandons the awaiting coroutine still leaves a consistent journal."""
        with self.db.session() as session:
            row = session.execute(
                select(JobItem.source_path, JobItem.proposed_path, JobItem.parsed).where(JobItem.id == item_id)
            ).first()
        if row is None:
            return ExecOutcome(None, None, [], False)
        source, destination, parsed_raw = row
        parsed = parsed_from(source, parsed_raw)
        folders = folders or FolderPolicy()
        if not destination:
            result = FileOpResult(ok=False, mode=mode, source=source, destination="", error="no destination path")
        else:
            try:
                existing = (
                    self._existing_rank(fileops.existing_destination(destination) or destination)
                    if settings.conflict_policy == ConflictPolicy.UPGRADE
                    else None
                )
                plan = fileops.plan_operation(
                    source,
                    destination,
                    mode,
                    settings.conflict_policy,
                    sidecar_extensions=settings.sidecar_extensions if settings.move_sidecars else None,
                    incoming_rank=parsed.quality.rank(),
                    existing_rank=existing,
                    rename_folder=folders.renames(source),
                    remove_empty_dirs=folders.remove_empty,
                    protected_dirs=folders.protected,
                )
                # fileops adds its own <day>/<slot> sub-folders below the trash dir.
                result = fileops.execute(plan, allowed_roots=settings.allowed_roots, trash_dir=self.config.trash_dir)
            except Exception as exc:
                log.exception("file operation crashed for %s", source)
                result = FileOpResult(ok=False, mode=mode, source=source, destination=destination, error=str(exc))
        return self._record_result(job, item_id, mode, result, prior)

    def _record_result(self, job: JobInfo, item_id: int, mode: RenameMode, result: FileOpResult,
                       prior: ItemStatus = ItemStatus.APPROVED) -> ExecOutcome:  # fmt: skip
        op_out: OperationOut | None = None
        op: Operation | None = None
        touched: list[str] = []
        dry_run = mode == RenameMode.TEST
        with self.db.session() as session:
            item = session.get(JobItem, item_id)
            if item is None:
                return ExecOutcome(None, None, [], False)
            ok = False
            stale = (*_DRY_RUN_STALE, _SOURCE_MISSING) if dry_run else ("dry_run", _SOURCE_MISSING)
            item.warnings = [w for w in (item.warnings or []) if not w.startswith(stale)]
            if item.error and item.error.startswith(_DRY_RUN_ERROR):
                item.error = None
            if dry_run:
                # A dry run only reports: the item keeps its runnable status (never DONE, no final_path)
                # so a real run can follow; the outcome shows up as warnings / error on the item.
                op = _record_dry_run(job.id, item, prior, result)
                if op is not None:
                    session.add(op)
                ok = result.ok
            elif result.ok and not result.skipped:
                op = Operation(
                    job_id=job.id, item_id=item_id, mode=mode.value, source=result.source,
                    destination=result.destination, status="done", bytes=result.bytes,
                    sidecars=[s.model_dump(mode="json") for s in result.sidecars], replaced=result.replaced,
                    error=result.error, result=result.model_dump(mode="json"),
                )  # fmt: skip
                session.add(op)
                item.status = ItemStatus.DONE.value
                item.final_path = result.destination
                item.folder_rename = result.folder_rename.model_dump(mode="json") if result.folder_rename else None
                item.error = None
                _note_fileops_warnings(item, result)
                # A renamed folder's old path no longer exists, so media servers rescan its parent and
                # drop the stale entry. For removed empty folders, the deepest folder still there.
                left = os.path.dirname(result.removed_dirs[-1] if result.removed_dirs else result.source)
                touched = [os.path.dirname(result.destination), left]
                ok = True
            elif result.ok and result.skipped:
                item.status = ItemStatus.DONE.value
                item.final_path = result.destination
                item.folder_rename = None
                add_warning(item, "already_in_place")
                _note_fileops_warnings(item, result)
                ok = True
            elif result.skipped:
                item.status = ItemStatus.REVIEW.value
                add_warning(item, "destination_exists")
                item.error = result.error
            else:
                op = Operation(
                    job_id=job.id, item_id=item_id, mode=mode.value, source=result.source,
                    destination=result.destination or (item.proposed_path or ""), status="failed",
                    bytes=0, sidecars=[], error=result.error, result=result.model_dump(mode="json"),
                )  # fmt: skip
                session.add(op)
                item.status = ItemStatus.ERROR.value
                item.error = result.error or "file operation failed"
                if result.error_code in {_SOURCE_MISSING, "destination_exists"}:  # Home maps reasons by these
                    add_warning(item, result.error_code)
            item.updated_at = utcnow()
            prune_finished(session, [item])  # done: only the selected candidate is kept
            session.flush()
            if op is not None:
                op_out = operation_out(op)
            out = item_out(item, slim=True)
        if not ok and result.error:
            level, prefix = ("warning", _DRY_RUN_ERROR) if dry_run else ("error", "")
            self.bus.log(level, "fileops", f"{prefix}{os.path.basename(result.source)}: {result.error}", job_id=job.id)
        return ExecOutcome(out, op_out, touched, ok)

    def _finish_execute(self, job_id: int, cancelled: bool) -> JobOut | None:
        with self.db.session() as session:
            job = session.get(Job, job_id)
            if job is None:
                return None
            # Items left EXECUTING (cancel between mark and run) go back to approved.
            for item in session.scalars(
                select(JobItem).where(JobItem.job_id == job_id, JobItem.status == ItemStatus.EXECUTING.value)
            ):
                item.status = ItemStatus.APPROVED.value
            if cancelled:
                job.status = JobStatus.CANCELLED.value
                job.finished_at = utcnow()
            else:
                job.status = JobStatus.REVIEW.value
                settle_job(session, job, force=True)
            job.progress = 1.0
            session.flush()
            return load_job_out(session, job_id)

    async def cancel_job(self, job_id: int) -> JobOut:
        def run() -> tuple[JobOut, bool]:
            with self.db.session() as session:
                job = get_job(session, job_id)
                status = JobStatus(job.status)
                if status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
                    raise conflict(f"job is already {status.value}")
                running = status in {JobStatus.MATCHING, JobStatus.EXECUTING}
                if not running:
                    job.status = JobStatus.CANCELLED.value
                    job.finished_at = utcnow()
                    session.flush()
                return load_job_out(session, job_id), running

        self._cancel.add(job_id)
        out, running = await asyncio.to_thread(run)
        if not running and job_id not in self._busy:
            self._cancel.discard(job_id)
        self._publish_job(out)
        await self.bus.alog("info", "job", f"Job #{job_id} cancelled", job_id=job_id)
        return out

    async def rematch_job(self, job_id: int) -> JobOut:
        def run() -> tuple[JobOut, str]:
            with self.db.session() as session:
                job = self._check_idle_job(session, job_id)
                rows = session.execute(
                    select(JobItem.id, JobItem.source_path, JobItem.proposed_path, JobItem.path_overridden,
                           func.json_extract(JobItem.parsed, "$.title"), func.json_extract(JobItem.parsed, "$.kind"))
                    .where(JobItem.job_id == job_id,
                           JobItem.status.notin_([ItemStatus.DONE.value, ItemStatus.EXECUTING.value]))
                ).all()  # fmt: skip
                now = utcnow()
                reset = [
                    {"id": item_id, "status": ItemStatus.PENDING.value, "selected_candidate_id": None,
                     "confidence": 0.0, "warnings": [], "error": None, "proposed_relative": None,
                     "folder_rename": None, "proposed_path": proposed if overridden else None, "matched": False,
                     "updated_at": now,
                     **match_columns([], None, {"title": title, "kind": kind}, source,
                                     proposed if overridden else None)}  # fmt: skip
                    for item_id, source, proposed, overridden, title, kind in rows
                ]
                if reset:
                    session.execute(update(JobItem), reset)
                    ids = [r["id"] for r in reset]
                    for start in range(0, len(ids), KNOWN_PATH_CHUNK):
                        session.execute(update(JobItemCandidates)  # the probe stays: the file didn't change
                                        .where(JobItemCandidates.item_id.in_(ids[start : start + KNOWN_PATH_CHUNK]))
                                        .values(candidates=[]))  # fmt: skip
                job.status = JobStatus.QUEUED.value
                job.progress = 0.0
                job.error = None
                job.finished_at = None
                session.flush()
                return load_job_out(session, job_id), job.source

        out, source = await asyncio.to_thread(run)
        self._cancel.discard(job_id)
        self._publish_job(out)
        self.enqueue_match(job_id, source)
        return out

    async def delete_job(self, job_id: int) -> None:
        def run() -> None:
            with self.db.session() as session:
                job = get_job(session, job_id)
                if job.status == JobStatus.EXECUTING.value:
                    raise conflict("job is executing; cancel it first")
                session.delete(job)

        self._cancel.add(job_id)
        await asyncio.to_thread(run)
        self.bus.publish("job.deleted", {"id": job_id})

    # ------------------------------------------------------------------ item actions

    def _item_context(self, item_id: int, siblings: bool = True) -> tuple[JobInfo, Work, list[Work]]:
        """The item (with its candidates) and, with ``siblings``, the rest of its job as light works
        (narrow columns, nothing decoded): one row read in full, however big the job."""
        with self.db.session() as session:
            item = get_item(session, item_id)
            job = self._check_idle_job(session, item.job_id)
            target = Work.from_item(item, session.get(JobItemCandidates, item_id))
            context = _merge_context(self._light_works(session, job.id), [target]) if siblings else [target]
            return JobInfo.of(job), target, context

    def _save_one(self, job_id: int, work: Work) -> tuple[JobItemOut, JobOut]:
        with self.db.session() as session:
            if not self._write_works(session, [work]):
                raise not_found("item")
            job = get_job(session, job_id)
            settle_job(session, job)
            session.flush()
            item = get_item(session, work.item_id)
            return item_out(item, session.get(JobItemCandidates, work.item_id)), load_job_out(session, job_id)

    async def _rerender(self, job: JobInfo, work: Work, works: list[Work], settings: AppSettings,
                        previous: str | None = None) -> list[Work]:  # fmt: skip
        """Render + plan ``work`` again. Siblings that only carried a multi-version suffix because they
        collided with its old name (``previous``, default: its current path) get their plain name back;
        they are returned for saving."""
        previous = previous or work.proposed_path
        before = _strip_version(previous) if previous else None
        expressions = await asyncio.to_thread(self._expression_lookup, job)
        context = _merge_context(works, [work])
        if work.best is not None:
            await asyncio.to_thread(self._hydrate, context, {work.best.id})
        await asyncio.to_thread(self._render_batch, job, [work], settings, expressions, context)
        await asyncio.to_thread(self._plan_all, job, [work], settings, context)
        if before is None or work.proposed_path is None or _norm_key(_strip_version(work.proposed_path)) == _norm_key(before):
            return []
        found = self._unsuffix_siblings(work, before, context)
        if not found:
            return []
        # The context has them as light works: give the real rows their plain name and plan them again.
        freed = await asyncio.to_thread(self._load_full_works, [w.item_id for w in found])
        for sibling in freed:
            _unsuffix(sibling)
        context = _merge_context(context, freed)
        await asyncio.to_thread(self._plan_all, job, freed, settings, context)
        for sibling in freed:
            if sibling.conflict == "exists" and settings.conflict_policy in {ConflictPolicy.SKIP, ConflictPolicy.FAIL}:
                sibling.warn("destination_exists")
                sibling.status = ItemStatus.REVIEW
        return freed

    @staticmethod
    def _unsuffix_siblings(work: Work, base: str, context: list[Work]) -> list[Work]:
        live = [w for w in context if w.status not in {ItemStatus.SKIPPED, ItemStatus.DONE} and w.proposed_path]
        claims = Counter(_norm_key(_strip_version(w.proposed_path or "")) for w in live)
        key = _norm_key(base)
        freed: list[Work] = []
        for w in live:
            if w.item_id == work.item_id or w.path_overridden or w.status == ItemStatus.EXECUTING:
                continue
            plain = _strip_version(w.proposed_path or "")
            if plain == w.proposed_path or _norm_key(plain) != key or claims[key] != 1:
                continue
            _unsuffix(w)
            freed.append(w)
        return freed

    async def update_item(self, item_id: int, update: ItemUpdate) -> JobItemOut:
        # Only a new match or a new path needs the rest of the job (collisions, di/dc): a plain
        # approve / skip / review (Home's buttons) reads just the item.
        renders = update.selected_candidate_id is not None or update.reset_path or update.proposed_path is not None
        job, work, works = await asyncio.to_thread(self._item_context, item_id, renders)
        settings = self.store.get_app()
        if work.status in {ItemStatus.EXECUTING, ItemStatus.DONE}:
            raise conflict(f"item is {work.status.value}")
        work.allow_extra = True  # the user is handling this item: no bonus-clip guessing
        changed = False
        prior = work.status
        if update.selected_candidate_id is not None:
            chosen = next((c for c in work.candidates if c.id == update.selected_candidate_id), None)
            if chosen is None:
                # an id from POST /items/{id}/search (not yet one of the item's candidates)
                chosen = await self._resolve_candidate_id(work, job, update.selected_candidate_id)
                work.candidates = [chosen, *work.candidates]
            work.best = chosen
            work.confidence = max(work.confidence, chosen.score)
            work.manual = True
            if update.proposed_path is None:
                work.path_overridden = False
            changed = True
        if update.reset_path:
            work.path_overridden = False
            changed = True
        if update.proposed_path is not None:
            work.proposed_path = await asyncio.to_thread(self._resolve_override, job, work, update.proposed_path, settings)
            work.path_overridden = True
            work.manual = True
            changed = True
        freed: list[Work] = []
        if changed:
            if work.status == ItemStatus.SKIPPED and update.status is None:
                work.status = ItemStatus.REVIEW
            freed = await self._rerender(job, work, works, settings)
            if work.status != ItemStatus.SKIPPED:
                work.status = ItemStatus.PENDING
                self._decide(work, settings)
                # Editing the path only changes where the file goes, not whether it was approved.
                path_only = update.selected_candidate_id is None
                if path_only and work.status == ItemStatus.APPROVED and prior != ItemStatus.APPROVED:
                    work.status = ItemStatus.MATCHED if prior == ItemStatus.MATCHED else ItemStatus.REVIEW
        if update.status is not None:
            target = ItemStatus(update.status)
            if target == ItemStatus.APPROVED:
                if not work.proposed_path:
                    raise conflict("item has no destination path; pick a candidate or type a path first")
                if work.best is None and not work.path_overridden:
                    raise conflict("item has no match; pick a candidate first")
            work.status = target
        await self._save_freed(job.id, freed)
        out, job_out = await asyncio.to_thread(self._save_one, job.id, work)
        self._publish_items([out])
        self._publish_job(job_out)
        return out

    async def _save_freed(self, job_id: int, freed: list[Work]) -> None:
        for sibling in freed:
            out, _job = await asyncio.to_thread(self._save_one, job_id, sibling)
            self._publish_items([out])

    async def _resolve_candidate_id(self, work: Work, job: JobInfo, candidate_id: str) -> Candidate:
        """'tmdb:series:1399' (from the search picker) -> a full candidate for this file."""
        provider, _, rest = candidate_id.partition(":")
        kind_raw, _, ref_id = rest.partition(":")
        if not provider or not ref_id or kind_raw not in {k.value for k in MediaKind} or work.parsed is None:
            raise ApiError(400, f"candidate {candidate_id!r} is not one of this item's candidates")
        try:
            return await self.matcher.resolve(work.parsed, ProviderRef(provider=provider, id=ref_id), MediaKind(kind_raw))
        except ApiError:
            raise
        except Exception as exc:
            raise ApiError(502, f"could not resolve {candidate_id}: {exc}") from exc

    def _resolve_override(self, job: JobInfo, work: Work, raw: str, settings: AppSettings) -> str:
        raw = raw.strip()
        if not raw:
            raise ApiError(400, "proposed_path must not be empty")
        if os.path.isabs(raw):
            path = os.path.normpath(raw)
        else:
            root = dest_root_for(job.dest_root, item_kind(job.kind, work.best, work.parsed), settings)
            if not root:
                raise ApiError(400, "relative path given but no library root is configured for this kind")
            try:
                relative = paths_mod.sanitize_relative_path(raw)
            except ValueError as exc:
                raise ApiError(400, str(exc)) from exc
            path = join_dest(root, relative)
        if not paths_mod.is_within(path, settings.allowed_roots):
            raise ApiError(403, "destination is outside the allowed roots")
        return path

    async def rematch_item(self, item_id: int, hint: MatchHint) -> JobItemOut:
        job, work, works = await asyncio.to_thread(self._item_context, item_id)
        if work.status in {ItemStatus.EXECUTING, ItemStatus.DONE}:
            raise conflict(f"item is {work.status.value}")
        settings = self.store.get_app()
        await asyncio.to_thread(self._parse_all, [work])
        parsed = work.parsed
        assert parsed is not None
        previous = list(work.candidates)
        previous_path = work.proposed_path
        work.reset_for_rematch()
        work.allow_extra = True  # an explicit rematch is not re-skipped as a bonus clip
        self._apply_clutter(work, settings)  # keeps the "sample"/"extra" warning ...
        work.status = ItemStatus.PENDING  # ... but an explicit rematch overrides the clutter skip
        if work.crc_ok is False:
            work.warn("crc_mismatch")
        if hint.ref is not None:
            kind = hint.kind or job.kind or (parsed.kind if parsed.kind != MediaKind.UNKNOWN else None)
            kind = kind or item_kind(None, None, parsed)
            try:
                chosen = await self.matcher.resolve(parsed, hint.ref, kind, hint)
            except ApiError:
                raise
            except Exception as exc:
                raise ApiError(502, f"could not resolve {hint.ref.provider}:{hint.ref.id}: {exc}") from exc
            others = [c for c in previous if c.id != chosen.id]
            result = MatchResult(candidates=[chosen, *others], best=chosen, confidence=max(chosen.score, 1.0))
            work.manual = True
        else:
            merged = hint if hint.kind or not job.kind else hint.model_copy(update={"kind": job.kind})
            try:
                result = await self.matcher.match(parsed, merged)
            except Exception as exc:
                result = MatchResult(warnings=[f"match_error: {exc}"])
        work.status = ItemStatus.PENDING
        self._apply_match(work, result)
        await self._probe_all([work], settings)
        freed = await self._rerender(job, work, works, settings, previous_path)
        self._decide(work, settings)
        await self._save_freed(job.id, freed)
        out, job_out = await asyncio.to_thread(self._save_one, job.id, work)
        self._publish_items([out])
        self._publish_job(job_out)
        return out

    async def search_item(self, item_id: int, req: ItemSearch) -> list[Candidate]:
        def check() -> None:
            with self.db.session() as session:
                get_item(session, item_id)

        await asyncio.to_thread(check)
        return await self.matcher.search(req.query, req.kind, req.year, req.provider)

    async def bulk(self, action: BulkItemAction, *, slim: bool = False, respond: bool = True) -> list[JobItemOut]:
        """Approve / skip / review / rematch ``action.item_ids`` (any number: worked through
        SAVE_CHUNK at a time in short transactions). Returns every found item afterwards, slim or
        full, ordered by id; nothing with ``respond=False`` (the client refetches what it shows)."""
        ids = sorted(set(action.item_ids or []))
        if action.action == "rematch":
            return await self._bulk_rematch(ids, slim=slim, respond=respond)
        target = {"approve": ItemStatus.APPROVED, "skip": ItemStatus.SKIPPED, "review": ItemStatus.REVIEW}[action.action]

        def run() -> tuple[dict[int, list[int]], list[JobOut]]:
            changed: dict[int, list[int]] = defaultdict(list)
            found = False
            for start in range(0, len(ids), SAVE_CHUNK):
                with self.db.session() as session:
                    rows = session.execute(
                        select(JobItem.id, JobItem.job_id, JobItem.status, JobItem.proposed_path,
                               JobItem.selected_candidate_id, JobItem.path_overridden)
                        .where(JobItem.id.in_(ids[start : start + SAVE_CHUNK]))
                    ).all()  # fmt: skip
                    found = found or bool(rows)
                    statuses = dict(session.execute(select(Job.id, Job.status).where(
                        Job.id.in_({r.job_id for r in rows}))).all())  # fmt: skip
                    eligible = [
                        r.id for r in rows
                        if r.job_id in statuses and JobStatus(statuses[r.job_id]) not in ACTIVE_JOB_STATUSES
                        and ItemStatus(r.status) not in {ItemStatus.EXECUTING, ItemStatus.DONE, ItemStatus.PENDING}
                        and (target != ItemStatus.APPROVED
                             or bool(r.proposed_path and (r.selected_candidate_id or r.path_overridden)))
                    ]  # fmt: skip
                    if not eligible:
                        continue
                    items = session.scalars(select(JobItem).where(JobItem.id.in_(eligible))).all()
                    for item in items:
                        item.status = target.value
                        item.updated_at = utcnow()
                        changed[item.job_id].append(item.id)
                    prune_finished(session, items)  # skipped: only the selected candidate is kept
            if not found:
                raise not_found_items()
            jobs: list[JobOut] = []
            with self.db.session() as session:
                for job_id in sorted(changed):
                    job = session.get(Job, job_id)
                    if job is not None:
                        settle_job(session, job)
                        session.flush()
                        jobs.append(load_job_out(session, job_id))
            return changed, jobs

        changed, jobs = await asyncio.to_thread(run)
        for job_id, item_ids in changed.items():
            self._publish_changed(job_id, item_ids)
        for job in jobs:
            self._publish_job(job)
        return await asyncio.to_thread(self._item_outs, ids, slim) if respond else []

    def _item_outs(self, item_ids: list[int], slim: bool) -> list[JobItemOut]:
        with self.db.session() as session:
            return load_item_outs(session, item_ids, slim=slim)

    async def _bulk_rematch(self, item_ids: list[int], *, slim: bool = False, respond: bool = True) -> list[JobItemOut]:
        """Rematch the items of each job SAVE_CHUNK at a time; the rest of the job is read once (narrow
        rows) and kept up to date between the chunks."""

        def load() -> list[tuple[JobInfo, list[Work]]]:
            job_ids: set[int] = set()
            with self.db.session() as session:
                for start in range(0, len(item_ids), KNOWN_PATH_CHUNK):
                    job_ids.update(session.scalars(select(JobItem.job_id).where(
                        JobItem.id.in_(item_ids[start : start + KNOWN_PATH_CHUNK])).distinct()))  # fmt: skip
                if not job_ids:
                    raise not_found_items()
                jobs = [JobInfo.of(self._check_idle_job(session, job_id)) for job_id in sorted(job_ids)]
                return [(job, self._light_works(session, job.id)) for job in jobs]

        grouped = await asyncio.to_thread(load)
        settings = self.store.get_app()
        wanted = set(item_ids)
        rematched: list[int] = []
        for job, light in grouped:
            at = {w.item_id: index for index, w in enumerate(light)}
            targets = [w.item_id for w in light
                       if w.item_id in wanted and w.status not in {ItemStatus.EXECUTING, ItemStatus.DONE}]  # fmt: skip
            folders = await asyncio.to_thread(self._folder_policy, job, job.mode, settings,
                                              [(w.source_path, w.status) for w in light])  # fmt: skip
            for start in range(0, len(targets), SAVE_CHUNK):
                works = await asyncio.to_thread(self._load_full_works, targets[start : start + SAVE_CHUNK])
                for work in works:
                    work.reset_for_rematch()
                if await self._process(job, works, settings, siblings=light, folders=folders):
                    break  # cancelled (shutting down): what is saved so far stays
                saved = await asyncio.to_thread(self._save_chunk, job.id, works)
                for work in works:
                    light[at[work.item_id]] = work.as_light()
                rematched.extend(saved)
                self._publish_changed(job.id, saved)
            self._publish_job(await asyncio.to_thread(self._job_out, job.id))
        return await asyncio.to_thread(self._item_outs, rematched, slim) if respond else []

    # ------------------------------------------------------------------ undo

    async def undo_operation(self, op_id: int) -> OperationOut:
        def load() -> tuple[FileOpResult, int | None, int | None]:
            with self.db.session() as session:
                op = get_operation(session, op_id)
                if op.status not in {"done", "undo_failed"}:
                    raise conflict(f"operation is {op.status}; only completed operations can be undone")
                if op.job_id is not None:
                    job = session.get(Job, op.job_id)
                    if job is not None and JobStatus(job.status) in ACTIVE_JOB_STATUSES:
                        raise conflict(f"job is {job.status}; wait for it to finish")
                if not op.result:
                    raise conflict("operation has no undo information")
                return FileOpResult.model_validate(op.result), op.item_id, op.job_id

        result, item_id, job_id = await asyncio.to_thread(load)
        settings = self.store.get_app()
        try:
            undone = await asyncio.to_thread(fileops.undo, result, allowed_roots=settings.allowed_roots)
        except Exception as exc:
            undone = FileOpResult(ok=False, mode=result.mode, source=result.destination, destination=result.source,
                                  error=str(exc))  # fmt: skip

        def record() -> tuple[OperationOut, JobItemOut | None, JobOut | None]:
            with self.db.session() as session:
                op = get_operation(session, op_id)
                item_o: JobItemOut | None = None
                job_o: JobOut | None = None
                if undone.ok:
                    op.status = "undone"
                    op.undone_at = utcnow()
                    op.error = None
                    # A dry run changed nothing, so undoing it must not touch the item either (it may
                    # have been really executed since).
                    dry = RenameMode(result.mode) == RenameMode.TEST
                    item = session.get(JobItem, item_id) if item_id and not dry else None
                    if item is not None:
                        item.status = ItemStatus.REVIEW.value
                        item.final_path = None
                        add_warning(item, "undone")
                        item.updated_at = utcnow()
                    session.flush()
                    if item is not None:
                        item_o = item_out(item, slim=True)
                    job = session.get(Job, job_id) if job_id else None
                    if job is not None:
                        settle_job(session, job)
                        session.flush()
                        job_o = load_job_out(session, job.id)
                else:
                    op.status = "undo_failed"
                    op.error = undone.error or "undo failed"
                session.flush()
                return operation_out(op), item_o, job_o

        op_out, item_o, job_o = await asyncio.to_thread(record)
        self.bus.publish("operation.updated", op_out)
        if item_o is not None:
            self._publish_items([item_o])
        self._publish_job(job_o)
        if undone.ok:
            await self.bus.alog("info", "fileops", f"Undid {os.path.basename(result.destination)}", job_id=job_id)
            if result.mode != RenameMode.TEST:
                self.refresher.schedule([os.path.dirname(result.destination), os.path.dirname(result.source)])
        else:
            await self.bus.alog("error", "fileops", f"Undo failed for {os.path.basename(result.destination)}: "
                                f"{undone.error}", job_id=job_id)  # fmt: skip
        return op_out

    # ------------------------------------------------------------------ stateless preview

    async def preview(self, req: PreviewRequest) -> list[PreviewItem]:
        def resolve_expression() -> Callable[[MediaKind], str]:
            with self.db.session() as session:
                explicit = req.expression or format_expression(session, req.format_id)
                if req.format_id is not None and explicit is None:
                    raise ApiError(400, f"format {req.format_id} does not exist")
                defaults = {kind: default_expression(session, kind) for kind in MediaKind}
            return lambda kind: explicit or defaults[kind]

        expressions = await asyncio.to_thread(resolve_expression)
        if req.expression:
            try:
                compile_format(req.expression)
            except FormatSyntaxError as exc:
                raise ApiError(400, f"{exc.message} (at {exc.start})") from exc

        def parse_all() -> list[tuple[ParsedMedia, str | None]]:
            out: list[tuple[ParsedMedia, str | None]] = []
            for name in req.names:
                try:
                    out.append((parser_mod.parse(name), None))
                except Exception as exc:
                    base = os.path.basename(name)
                    ext = base.rsplit(".", 1)[-1].lower() if "." in base else ""
                    out.append((ParsedMedia(path=name, filename=base, extension=ext), f"parse error: {exc}"))
            return out

        parsed = await asyncio.to_thread(parse_all)
        hint = MatchHint(kind=req.kind) if req.kind else None
        try:
            results = await self.matcher.match_many([p for p, _ in parsed], hint)
        except Exception as exc:
            results = [MatchResult(warnings=[f"match_error: {exc}"]) for _ in parsed]
        items: list[PreviewItem] = []
        for (media, error), result in zip(parsed, results, strict=False):
            rendered: str | None = None
            if result.best is not None:
                kind = item_kind(req.kind, result.best, media)
                try:
                    template = compile_format(expressions(kind))
                except FormatSyntaxError as exc:
                    error = error or f"format error: {exc.message}"
                else:
                    rendering = template.render_detailed(build_binding(media, result.best, None))
                    rendered = rendering.path
                    errors = [i.message for i in rendering.issues if i.severity == "error"]
                    error = error or (errors[0] if errors else None)
            elif error is None:
                error = "no match found"
            items.append(PreviewItem(name=media.filename or media.path, parsed=media, best=result.best,
                                     confidence=result.confidence, rendered=rendered, error=error))  # fmt: skip
        return items


def _note_fileops_warnings(item: JobItem, result: FileOpResult, prefix: str = "fileops") -> None:
    """Non-fatal notes from a successful file operation become item warnings."""
    for note in getattr(result, "warnings", None) or []:
        add_warning(item, f"{prefix}: {note}")
    if result.error:  # older fileops put success notes in ``error``
        add_warning(item, f"{prefix}: {result.error}")


def _record_dry_run(job_id: int, item: JobItem, prior: ItemStatus, result: FileOpResult) -> Operation | None:
    """Apply a RenameMode.TEST outcome to ``item`` without marking it done.

    The item returns to ``prior`` (matched/approved). Success adds ``dry_run_ok: <destination>``
    (the path a real run would produce, which a conflict policy may have changed) plus any notes as
    ``dry_run_note: ...``; a problem the real run would hit is shown as ``dry_run_failed`` with the
    reason in ``item.error``. The journal row keeps mode "test" (history only, undo is a no-op).
    """
    item.status = prior.value
    if result.ok:
        item.folder_rename = result.folder_rename.model_dump(mode="json") if result.folder_rename else None
        if result.skipped:
            add_warning(item, "already_in_place")
        else:
            add_warning(item, f"dry_run_ok: {result.destination}")
        _note_fileops_warnings(item, result, prefix="dry_run_note")
    else:
        add_warning(item, "dry_run_failed")
        if result.skipped:
            add_warning(item, "destination_exists")
        item.error = f"{_DRY_RUN_ERROR}{result.error or 'the file operation would fail'}"
    if result.skipped:
        return None  # same as a real run: nothing to journal for a skip
    return Operation(
        job_id=job_id, item_id=item.id, mode=RenameMode.TEST.value, source=result.source,
        destination=result.destination or (item.proposed_path or ""), status="done" if result.ok else "failed",
        bytes=result.bytes, sidecars=[s.model_dump(mode="json") for s in result.sidecars], replaced=result.replaced,
        error=result.error, result=result.model_dump(mode="json"),
    )  # fmt: skip


def not_found_items() -> ApiError:
    return ApiError(404, "items not found")


def _folder_name(path: str) -> str:
    return os.path.basename(path.rstrip("/\\")) or path


# --------------------------------------------------------------------------- watch folders


class WatchManager:
    """Owns the ``FolderWatcher``: syncs it with the ``watch_folders`` table and turns ready
    batches into jobs (source ``watch``)."""

    # Full rescans cost ~40 µs of Python per file. A folder with working live events only needs one now
    # and then (files that arrived while Schema was down are found by the rescan at start); a folder
    # without them (SMB / NFS, inotify limits) is polled every minute.
    RESCAN_SECONDS = 60.0
    LIVE_RESCAN_SECONDS = 20 * 60.0

    def __init__(self, *, db: Database, store: SettingsStore, bus: EventBus, pipeline: Pipeline) -> None:
        self.db = db
        self.store = store
        self.bus = bus
        self.pipeline = pipeline
        self.watcher: Any = None
        self.error: str | None = None
        self._prune_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._prune_task is None or self._prune_task.done():
            self._prune_task = asyncio.create_task(self._prune_loop(), name="schema-leftovers-prune")
        settings = self.store.get_app()
        try:
            self.watcher = watcher_mod.FolderWatcher(
                self._on_ready,
                self._on_status,
                rescan_interval=self.RESCAN_SECONDS,
                live_rescan_interval=self.LIVE_RESCAN_SECONDS,
                ignore_patterns=list(settings.ignore_patterns),
                video_extensions=list(settings.video_extensions),
                release_quiet=float(settings.release_quiet_seconds),
                container_names=list(settings.category_kinds),
                is_known=self._is_known,
            )
            await self.watcher.start()
        except Exception as exc:
            self.watcher = None
            self.error = str(exc) or exc.__class__.__name__
            await self.bus.alog("error", "watcher", f"Folder watcher unavailable: {self.error}")
            return
        await self.sync()

    async def stop(self) -> None:
        task, self._prune_task = self._prune_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self.watcher is not None:
            with contextlib.suppress(Exception):
                await self.watcher.stop()

    def _prune_leftovers(self) -> int:
        """Delete held download leftovers older than a day: the holding folders of the watch folders
        and allowed roots, and those a level or two below a watch folder (a release whose watch folder's
        holding folder sits on another disk is held in its parent folder)."""
        settings = self.store.get_app()
        with self.db.session() as session:
            watched = list(session.scalars(select(WatchFolder.path)))
        removed = 0
        for root in dict.fromkeys([*watched, *settings.allowed_roots]):
            if root:
                removed += len(release_mod.prune_holding(release_mod.holding_base(root), root=root))
        for root in dict.fromkeys(watched):
            for base in release_mod.nested_holding_bases(root) if root else ():
                removed += len(release_mod.prune_holding(base, root=root))
        return removed

    async def _is_known(self, files: dict[str, float | None]) -> set[str]:
        """For the watcher: the files (path -> mtime) a job already took in and that haven't changed
        since (:func:`already_handled`, normalized keys). Seeding files in copy / link mode then never
        settle again, nor get their download folders walked, after a restart."""

        def run() -> set[str]:
            with self.db.session() as session:
                return already_handled(session, files)

        return await asyncio.to_thread(run)

    async def _prune_loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self._prune_leftovers)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("could not prune held download leftovers")
            await asyncio.sleep(3600)

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    def _specs(self) -> list[WatchFolderSpec]:
        with self.db.session() as session:
            rows = session.scalars(select(WatchFolder).order_by(WatchFolder.id)).all()
            return [
                WatchFolderSpec(id=row.id, path=row.path, recursive=row.recursive, enabled=row.enabled,
                                settle_seconds=row.settle_seconds,
                                kind_hint=MediaKind(row.kind) if row.kind else None)  # fmt: skip
                for row in rows
            ]

    async def sync(self) -> None:
        if self.watcher is None:
            return
        specs = await asyncio.to_thread(self._specs)
        try:
            await self.watcher.sync(specs)
        except Exception as exc:
            await self.bus.alog("error", "watcher", f"Could not update watched folders: {exc}")

    def status(self, folder_id: int) -> str:
        if self.watcher is None:
            return "error" if self.error else "stopped"
        try:
            return self.watcher.status().get(folder_id, "stopped")
        except Exception:
            return "error"

    def folder_out(self, row: WatchFolder) -> Any:
        return watch_folder_out(row, self.status(row.id))

    def incoming(self) -> list[Any]:
        """Files waiting in the watch folders (``watcher.WatchedFile``); safe from any thread."""
        getter = getattr(self.watcher, "incoming", None) if self.watcher is not None else None
        if getter is None:
            return []
        try:
            return list(getter())
        except Exception:
            log.exception("could not read the watcher's pending files")
            return []

    async def start_now(self, folder_id: int, path: str | None = None) -> int:
        """``POST /watch-folders/{id}/start-now``: skip the rest of the settle wait."""

        def check() -> None:
            with self.db.session() as session:
                if session.get(WatchFolder, folder_id) is None:
                    raise ApiError(404, "watch folder not found")

        await asyncio.to_thread(check)
        if self.watcher is None:
            raise ApiError(503, "the folder watcher is not running")
        start_now = getattr(self.watcher, "start_now", None)
        if start_now is None:
            return 0
        return int(await start_now(folder_id, os.path.normpath(path) if path else None))

    async def scan_now(self) -> int:
        """The Home Refresh button: look in every watch folder now and start what is finished there."""
        scan_now = getattr(self.watcher, "scan_now", None) if self.watcher is not None else None
        if scan_now is None:
            return 0
        return int(await scan_now())

    async def _on_status(self, folder_id: int, status: str, error: str | None) -> None:
        def run() -> Any:
            with self.db.session() as session:
                row = session.get(WatchFolder, folder_id)
                if row is None:
                    return None
                row.last_error = error
                session.flush()
                return watch_folder_out(row, status)

        out = await asyncio.to_thread(run)
        if out is not None:
            self.bus.publish("watcher.updated", out)
            if error:
                await self.bus.alog("warning", "watcher", f"{out.path}: {error}")

    async def _on_ready(self, spec: WatchFolderSpec, files: list[Path]) -> None:
        def mtimes() -> dict[str, float | None]:
            out: dict[str, float | None] = {}
            for f in files:
                try:
                    out[str(f)] = os.stat(f).st_mtime
                except OSError:
                    continue  # gone meanwhile
            return out

        def prepare(present: dict[str, float | None]) -> tuple[JobCreate | None, list[str], Any, int]:
            with self.db.session() as session:
                row = session.get(WatchFolder, spec.id)
                if row is None or not row.enabled:
                    return None, [], None, 0
                busy = open_item_paths(session, present)
                queued_dirs = queued_input_dirs(session)
                # Bug fix: a restart re-offers every file still sitting in the folder. Files a job
                # already took in (failed, skipped, waiting in review, or copied) only run again when
                # the user retries them, never on their own.
                handled = already_handled(session, present)
                fresh: list[str] = []
                known = 0
                for path in present:
                    key = os.path.normcase(os.path.normpath(path))
                    if key in busy or any(key.startswith(d) for d in queued_dirs):
                        continue
                    if key in handled:
                        known += 1
                        continue
                    fresh.append(path)
                row.last_event_at = utcnow()
                row.files_seen = (row.files_seen or 0) + len(fresh)
                session.flush()
                req = None
                if fresh:
                    req = JobCreate(
                        paths=fresh,
                        kind=MediaKind(row.kind) if row.kind else None,
                        mode=RenameMode(row.mode) if row.mode else None,
                        format_id=row.format_id,
                        dest_root=row.dest_root,
                        auto_execute=row.auto_execute,
                        label=f"Watch: {_folder_name(row.path)}",
                    )
                return req, fresh, watch_folder_out(row, self.status(row.id)), known

        present = await asyncio.to_thread(mtimes)
        req, fresh, out, known = await asyncio.to_thread(prepare, present)
        if out is not None:
            self.bus.publish("watcher.updated", out)
        if known:
            await self.bus.alog(
                "info", "watcher",
                f"{out.path if out else spec.path}: {known} file(s) were already handled earlier; not running them "
                "again (retry them from Home or the job)",
            )  # fmt: skip
        if req is None:
            return
        settings = self.store.get_app()
        roots = list(settings.allowed_roots)
        # is_within resolves links (a syscall per path component): not on the event loop.
        outside = await asyncio.to_thread(lambda: [p for p in fresh if not paths_mod.is_within(p, roots)])
        if outside:
            await self.bus.alog("warning", "watcher", f"Ignored {len(outside)} file(s) outside the allowed roots")
            req.paths = [p for p in fresh if p not in outside]
            if not req.paths:
                return
        await self.pipeline.create_job(req, source=JobSource.WATCH, watch_folder_id=spec.id, validate=False)

    async def scan(self, folder_id: int) -> JobOut:
        """``POST /watch-folders/{id}/scan``: process what is in the folder now."""

        def load() -> WatchFolder:
            with self.db.session() as session:
                row = session.get(WatchFolder, folder_id)
                if row is None:
                    raise ApiError(404, "watch folder not found")
                session.expunge(row)
                return row

        row = await asyncio.to_thread(load)
        if not os.path.isdir(row.path):
            raise ApiError(400, f"folder does not exist: {row.path}")
        req = JobCreate(
            paths=[row.path],
            kind=MediaKind(row.kind) if row.kind else None,
            mode=RenameMode(row.mode) if row.mode else None,
            format_id=row.format_id,
            dest_root=row.dest_root,
            auto_execute=row.auto_execute,
            label=f"Scan: {_folder_name(row.path)}",
        )
        return await self.pipeline.create_job(req, source=JobSource.WATCH, watch_folder_id=row.id)
