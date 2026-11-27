"""Job / item / operation persistence helpers and API serialization.

Everything here is synchronous and takes an open SQLAlchemy ``Session``; async callers run it in
a worker thread. The orchestration (matching, execution, undo) lives in ``core/pipeline.py``.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from pathlib import PurePath
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from schema_app.api_models import (
    FormatOut,
    ItemStatus,
    JobDetail,
    JobItemOut,
    JobItemPage,
    JobOut,
    JobSource,
    JobStatus,
    OperationOut,
    WatchFolderOut,
)
from schema_app.core.format import DEFAULT_FORMATS
from schema_app.db import utcnow
from schema_app.domain import (
    Candidate,
    EpisodeInfo,
    FolderRename,
    MediaKind,
    MovieInfo,
    ParsedMedia,
    ProbeInfo,
    RenameMode,
    SeriesInfo,
    SidecarMove,
)
from schema_app.models import (
    Format,
    Job,
    JobItem,
    JobItemCandidates,
    Operation,
    WatchFolder,
    card_kind,
    search_text_of,
)


class ApiError(Exception):
    """Raised by services; the app turns it into ``{"detail": ...}`` with ``status_code``."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def not_found(what: str) -> ApiError:
    return ApiError(404, f"{what} not found")


def conflict(detail: str) -> ApiError:
    return ApiError(409, detail)


# ---------------------------------------------------------------------- state sets

ACTIVE_JOB_STATUSES = frozenset({JobStatus.QUEUED, JobStatus.MATCHING, JobStatus.EXECUTING})
SETTLED_JOB_STATUSES = frozenset({JobStatus.REVIEW, JobStatus.READY, JobStatus.COMPLETED})
TERMINAL_JOB_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED})
RUNNABLE_ITEM_STATUSES = frozenset({ItemStatus.MATCHED, ItemStatus.APPROVED})
OPEN_ITEM_STATUSES = frozenset(
    {ItemStatus.PENDING, ItemStatus.MATCHED, ItemStatus.REVIEW, ItemStatus.APPROVED, ItemStatus.EXECUTING}
)


# ---------------------------------------------------------------------- item (de)serialization


def parsed_of(item: JobItem) -> ParsedMedia:
    return parsed_from(item.source_path, item.parsed)


def parsed_from(source_path: str, parsed: dict[str, Any] | None) -> ParsedMedia:
    if parsed:
        return ParsedMedia.model_validate(parsed)
    name = PurePath(source_path).name
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return ParsedMedia(path=source_path, filename=name, extension=ext)


def candidates_of(detail: JobItemCandidates | None) -> list[Candidate]:
    return [Candidate.model_validate(c) for c in (detail.candidates or [])] if detail is not None else []


def selected_candidate(item: JobItem, candidates: list[Candidate]) -> Candidate | None:
    if not item.selected_candidate_id:
        return None
    return next((c for c in candidates if c.id == item.selected_candidate_id), None)


def probe_of(detail: JobItemCandidates | None) -> ProbeInfo | None:
    return ProbeInfo.model_validate(detail.probe) if detail is not None and detail.probe else None


def load_details(session: Session, item_ids: Iterable[int]) -> dict[int, JobItemCandidates]:
    """The stored candidates + probe of ``item_ids`` (bulk ``IN`` lookups, no per-item round trips)."""
    ids = list(item_ids)
    out: dict[int, JobItemCandidates] = {}
    for start in range(0, len(ids), KNOWN_PATH_CHUNK):
        chunk = ids[start : start + KNOWN_PATH_CHUNK]
        out.update({d.item_id: d for d in session.scalars(select(JobItemCandidates).where(
            JobItemCandidates.item_id.in_(chunk)))})  # fmt: skip
    return out


# ---------------------------------------------------------------------- what an item stores

STORED_CANDIDATES = 3  # suggestions kept per open item (the matcher ranks more to score them)
FINISHED_ITEM_STATUSES = frozenset({ItemStatus.DONE.value, ItemStatus.SKIPPED.value})


def keep_candidates_raw(candidates: list[dict[str, Any]], selected_id: str | None, status: str) -> list[dict[str, Any]]:
    """What is stored: the top ``STORED_CANDIDATES`` (the selected one included even when it ranks lower);
    once an item is done or skipped, only the selected one."""
    selected = next((c for c in candidates if c.get("id") == selected_id), None) if selected_id else None
    if status in FINISHED_ITEM_STATUSES:
        return [selected] if selected is not None else []
    kept = list(candidates[:STORED_CANDIDATES])
    if selected is not None and all(c.get("id") != selected_id for c in kept):
        kept = [*kept[: STORED_CANDIDATES - 1], selected]
    return kept


def trim_candidates(candidates: Sequence[Candidate], best: Candidate | None) -> list[Candidate]:
    """``keep_candidates_raw`` for an open item's in-memory candidates (a 10-deep match result)."""
    kept = list(candidates[:STORED_CANDIDATES])
    if best is not None and all(c.id != best.id for c in kept):
        kept = [*kept[: STORED_CANDIDATES - 1], best]
    return kept


def _pick(data: dict[str, Any] | None, keys: tuple[str, ...]) -> dict[str, Any] | None:
    return {k: data[k] for k in keys if k in data} if data else None


_SLIM_KEYS = ("id", "kind", "provider", "title", "year", "score", "episode_order", "part")
_SLIM_MOVIE = ("ref", "title", "year", "poster_url")
_SLIM_SERIES = ("ref", "title", "year", "poster_url", "network", "is_anime")
_SLIM_EPISODE = ("series_ref", "season", "episode", "absolute", "title", "air_date")


def slim_raw(candidate: dict[str, Any]) -> dict[str, Any]:
    """``slim_candidate`` on a stored (JSON) candidate, without validating it. A file mapped to more
    episodes than the slim form shows keeps every (season, episode, absolute) in ``episode_keys`` (the
    batch context tells multi-episode files apart by them)."""
    out = _pick(candidate, _SLIM_KEYS) or {}
    out["movie"] = _pick(candidate.get("movie"), _SLIM_MOVIE)
    out["series"] = _pick(candidate.get("series"), _SLIM_SERIES)
    episodes = candidate.get("episodes") or []
    out["episodes"] = [_pick(e, _SLIM_EPISODE) for e in episodes[:SLIM_MAX_EPISODES]]
    if len(episodes) > SLIM_MAX_EPISODES:
        out["episode_keys"] = [[e.get("season"), e.get("episode"), e.get("absolute")] for e in episodes]
    return out


def match_columns(candidates: list[dict[str, Any]], selected_id: str | None, parsed: dict[str, Any] | None,
                  source_path: str, proposed_path: str | None) -> dict[str, Any]:  # fmt: skip
    """The derived ``job_items`` columns for the stored ``candidates`` (see ``models.JobItem``)."""
    selected = next((c for c in candidates if c.get("id") == selected_id), None) if selected_id else None
    chosen = selected or (candidates[0] if candidates else None)
    episodes = (selected or {}).get("episodes") or [{}]
    match_kind = selected.get("kind") if selected else None
    parsed = parsed or {}
    return {
        "candidate_count": len(candidates),
        "chosen": slim_raw(chosen) if chosen is not None else None,
        "match_title": selected.get("title") if selected else None,
        "match_year": selected.get("year") if selected else None,
        "match_kind": match_kind,
        "match_season": episodes[0].get("season"),
        "match_episode": episodes[0].get("episode"),
        "kind": card_kind(match_kind or parsed.get("kind")),
        "search_text": search_text_of(source_path, proposed_path, parsed.get("title")),
    }


def apply_match_columns(item: JobItem, candidates: list[dict[str, Any]]) -> None:
    for key, value in match_columns(candidates, item.selected_candidate_id, item.parsed, item.source_path,
                                    item.proposed_path).items():  # fmt: skip
        setattr(item, key, value)


_PRUNE_SQL = text(
    "UPDATE job_item_candidates SET candidates = coalesce((SELECT json_group_array(json(c.value)) "
    "FROM json_each(job_item_candidates.candidates) AS c WHERE json_extract(c.value, '$.id') = :selected), "
    "json('[]')) WHERE item_id = :id"
)


def prune_finished(session: Session, items: Iterable[JobItem]) -> None:
    """Items that just became done or skipped keep only their selected candidate (in SQL: the list is
    never decoded here). Updates the derived columns to match."""
    todo = [i for i in items if i.status in FINISHED_ITEM_STATUSES
            and (i.candidate_count or 0) > (1 if i.selected_candidate_id else 0)]  # fmt: skip
    if not todo:
        return
    session.flush()
    session.execute(_PRUNE_SQL, [{"id": i.id, "selected": i.selected_candidate_id or ""} for i in todo])
    for item in todo:
        chosen = item.chosen if item.selected_candidate_id and item.chosen and \
            item.chosen.get("id") == item.selected_candidate_id else None  # fmt: skip
        item.candidate_count = 1 if chosen is not None else 0
        item.chosen = chosen


SLIM_MAX_EPISODES = 8  # a multi-episode file maps to a handful; cap pathological season packs


def slim_candidate(c: Candidate) -> Candidate:
    """What list rows need from a candidate: identity, score, poster and the mapped episode numbers.

    Drops overview, aliases, genres, provider id maps and scoring reasons (the bulk of the payload).
    """
    movie = series = None
    if c.movie is not None:
        movie = MovieInfo(ref=c.movie.ref, title=c.movie.title, year=c.movie.year, poster_url=c.movie.poster_url)
    if c.series is not None:
        series = SeriesInfo(
            ref=c.series.ref,
            title=c.series.title,
            year=c.series.year,
            poster_url=c.series.poster_url,
            network=c.series.network,
            is_anime=c.series.is_anime,
        )
    episodes = [
        EpisodeInfo(series_ref=e.series_ref, season=e.season, episode=e.episode, absolute=e.absolute, title=e.title,
                    air_date=e.air_date)  # fmt: skip
        for e in c.episodes[:SLIM_MAX_EPISODES]
    ]
    return Candidate(
        id=c.id,
        kind=c.kind,
        provider=c.provider,
        title=c.title,
        year=c.year,
        score=c.score,
        movie=movie,
        series=series,
        episodes=episodes,
        episode_order=c.episode_order,
        part=c.part,
    )


def slim_item(out: JobItemOut) -> JobItemOut:
    """Slim form of an already-built item."""
    if out.slim:
        return out
    chosen: Candidate | None = None
    if out.candidates:
        chosen = next((c for c in out.candidates if c.id == out.selected_candidate_id), out.candidates[0])
    return out.model_copy(
        update={"candidates": [slim_candidate(chosen)] if chosen else [], "candidate_count": len(out.candidates),
                "probe": None, "slim": True}  # fmt: skip
    )


def item_out(item: JobItem, detail: JobItemCandidates | None = None, *, slim: bool = False) -> JobItemOut:
    """The API form of an item. Slim (lists, pushes) reads only the ``job_items`` row: the chosen
    candidate in its slim form, no probe. Full (the inspector, single-item answers) needs ``detail``,
    the item's ``job_item_candidates`` row (None: nothing stored yet)."""
    if slim:
        candidates = [Candidate.model_validate(item.chosen)] if item.chosen else []
        count = item.candidate_count or 0
        probe = None
    else:
        candidates = candidates_of(detail)
        count = len(candidates)
        probe = probe_of(detail)
    return JobItemOut(
        id=item.id,
        job_id=item.job_id,
        status=ItemStatus(item.status),
        source_path=item.source_path,
        filename=os.path.basename(item.source_path) or item.source_path,
        parsed=parsed_of(item),
        candidates=candidates,
        candidate_count=count,
        slim=slim,
        selected_candidate_id=item.selected_candidate_id,
        confidence=item.confidence or 0.0,
        proposed_path=item.proposed_path,
        proposed_relative=item.proposed_relative,
        path_overridden=bool(item.path_overridden),
        final_path=item.final_path,
        folder_rename=FolderRename.model_validate(item.folder_rename) if item.folder_rename else None,
        sidecars=list(item.sidecars or []),
        warnings=list(item.warnings or []),
        error=item.error,
        crc_ok=item.crc_ok,
        probe=probe,
        updated_at=item.updated_at or utcnow(),
    )


def full_item_out(session: Session, item: JobItem) -> JobItemOut:
    return item_out(item, session.get(JobItemCandidates, item.id))


def item_outs(session: Session, items: Sequence[JobItem], *, slim: bool) -> list[JobItemOut]:
    """``item_out`` for many items; full ones load their details with bulk ``IN`` queries."""
    if slim:
        return [item_out(item, slim=True) for item in items]
    details = load_details(session, [item.id for item in items])
    return [item_out(item, details.get(item.id)) for item in items]


def load_item_outs(session: Session, item_ids: Sequence[int], *, slim: bool) -> list[JobItemOut]:
    """Items by id (ordered by id) in their API form, read in chunks."""
    rows: list[JobItem] = []
    ids = sorted(set(item_ids))
    for start in range(0, len(ids), KNOWN_PATH_CHUNK):
        rows.extend(session.scalars(select(JobItem).where(JobItem.id.in_(ids[start : start + KNOWN_PATH_CHUNK]))
                                    .order_by(JobItem.id)))  # fmt: skip
    return item_outs(session, rows, slim=slim)


def add_warning(item: JobItem, warning: str) -> None:
    warnings = list(item.warnings or [])
    if warning not in warnings:
        warnings.append(warning)
    item.warnings = warnings


# ---------------------------------------------------------------------- jobs


def item_counts(session: Session, job_ids: Iterable[int]) -> dict[int, dict[str, int]]:
    ids = list(job_ids)
    counts: dict[int, dict[str, int]] = {job_id: {} for job_id in ids}
    if not ids:
        return counts
    rows = session.execute(
        select(JobItem.job_id, JobItem.status, func.count(JobItem.id))
        .where(JobItem.job_id.in_(ids))
        .group_by(JobItem.job_id, JobItem.status)
    )
    for job_id, status, count in rows:
        counts[job_id][status] = int(count)
    return counts


def job_out(job: Job, counts: dict[str, int] | None = None) -> JobOut:
    counts = counts or {}
    return JobOut(
        id=job.id,
        label=job.label,
        source=JobSource(job.source),
        status=JobStatus(job.status),
        kind=MediaKind(job.kind) if job.kind else None,
        mode=RenameMode(job.mode),
        format_id=job.format_id,
        dest_root=job.dest_root,
        auto_execute=bool(job.auto_execute),
        created_at=job.created_at,
        updated_at=job.updated_at or job.created_at,
        finished_at=job.finished_at,
        counts=dict(counts),
        total=sum(counts.values()),
        progress=float(job.progress or 0.0),
        error=job.error,
    )


def get_job(session: Session, job_id: int) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise not_found("job")
    return job


def get_item(session: Session, item_id: int) -> JobItem:
    item = session.get(JobItem, item_id)
    if item is None:
        raise not_found("item")
    return item


def load_job_out(session: Session, job_id: int) -> JobOut:
    job = get_job(session, job_id)
    return job_out(job, item_counts(session, [job.id])[job.id])


ITEM_PAGE_DEFAULT = 200  # GET /jobs/{id}/items
ITEM_PAGE_MAX = 1000
JOB_DETAIL_DEFAULT_LIMIT = 1000  # GET /jobs/{id} without ``limit`` (small jobs: everything, as before)


def item_filter(
    job_id: int, statuses: Iterable[ItemStatus | str] | None = None, q: str | None = None
) -> list[Any]:
    """WHERE clauses for "items of a job matching a status set and free text".

    The text matches the source path (so the filename), the proposed path or the parsed title,
    case-insensitively, like the workspace's search box.
    """
    clauses: list[Any] = [JobItem.job_id == job_id]
    wanted = [ItemStatus(s).value for s in statuses] if statuses else []
    if wanted:
        clauses.append(JobItem.status.in_(wanted))
    search = search_clause(q)
    if search is not None:
        clauses.append(search)
    return clauses


def search_clause(q: str | None) -> Any:
    """Case-insensitive "contains" on the source path, proposed path or parsed title (one LIKE on the
    pre-lowered ``search_text`` column). None for an empty query."""
    needle = (q or "").strip().lower()
    if not needle:
        return None
    return JobItem.search_text.contains(needle, autoescape=True)


def parse_statuses(values: Iterable[str] | None) -> list[ItemStatus] | None:
    """``?status=review&status=matched`` and ``?status=review,matched`` both work."""
    out: list[ItemStatus] = []
    for value in values or []:
        for part in value.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                out.append(ItemStatus(part))
            except ValueError as exc:
                raise ApiError(422, f"unknown item status {part!r}") from exc
    return out or None


def matching_item_ids(
    session: Session, job_id: int, statuses: Iterable[ItemStatus | str] | None = None, q: str | None = None
) -> list[int]:
    return list(session.scalars(select(JobItem.id).where(*item_filter(job_id, statuses, q)).order_by(JobItem.id)))


def item_page(
    session: Session,
    job_id: int,
    *,
    statuses: Iterable[ItemStatus | str] | None = None,
    q: str | None = None,
    limit: int = ITEM_PAGE_DEFAULT,
    offset: int = 0,
    after: int | None = None,
    slim: bool = True,
) -> JobItemPage:
    """One page of a job's items ordered by id. ``after`` (keyset, an item id) is stable while items
    change status underneath the client; ``offset`` is applied on top of it."""
    get_job(session, job_id)
    clauses = item_filter(job_id, statuses, q)
    total = int(session.scalar(select(func.count(JobItem.id)).where(*clauses)) or 0)
    query = select(JobItem).where(*clauses)
    if after is not None:
        query = query.where(JobItem.id > after)
    rows = list(session.scalars(query.order_by(JobItem.id).offset(offset).limit(limit + 1)))
    more = len(rows) > limit
    rows = rows[:limit]
    return JobItemPage(
        job_id=job_id,
        items=item_outs(session, rows, slim=slim),
        total=total,
        offset=offset,
        limit=limit,
        next_after=rows[-1].id if more and rows else None,
        counts=item_counts(session, [job_id])[job_id],
    )


def job_detail(
    session: Session,
    job_id: int,
    *,
    statuses: Iterable[ItemStatus | str] | None = None,
    q: str | None = None,
    limit: int = JOB_DETAIL_DEFAULT_LIMIT,
    offset: int = 0,
    after: int | None = None,
    slim: bool = False,
) -> JobDetail:
    job = get_job(session, job_id)
    page = item_page(session, job_id, statuses=statuses, q=q, limit=limit, offset=offset, after=after, slim=slim)
    base = job_out(job, page.counts)
    return JobDetail(**base.model_dump(), items=page.items, items_total=page.total, next_after=page.next_after)


def list_jobs(
    session: Session, statuses: list[JobStatus] | None = None, *, limit: int = 50, offset: int = 0
) -> list[JobOut]:
    query = select(Job).order_by(Job.id.desc()).limit(limit).offset(offset)
    if statuses:
        query = query.where(Job.status.in_([s.value for s in statuses]))
    jobs = session.scalars(query).all()
    counts = item_counts(session, [job.id for job in jobs])
    return [job_out(job, counts[job.id]) for job in jobs]


def settled_status(counts: dict[str, int]) -> JobStatus:
    """Status of a job whose matching is finished, from its item counts (SPEC §3.3)."""
    if counts.get(ItemStatus.REVIEW, 0):
        return JobStatus.REVIEW
    if counts.get(ItemStatus.MATCHED, 0) or counts.get(ItemStatus.APPROVED, 0) or counts.get(ItemStatus.PENDING, 0):
        return JobStatus.READY
    if counts.get(ItemStatus.ERROR, 0) and not counts.get(ItemStatus.DONE, 0):
        return JobStatus.FAILED
    return JobStatus.COMPLETED


def settle_job(session: Session, job: Job, *, force: bool = False) -> bool:
    """Recompute review/ready/completed for a job that is not running. Returns True if changed."""
    current = JobStatus(job.status)
    if not force and current not in SETTLED_JOB_STATUSES and current != JobStatus.FAILED:
        return False
    counts = item_counts(session, [job.id])[job.id]
    if not counts and current == JobStatus.FAILED:
        return False
    new = settled_status(counts)
    changed = new != current
    job.status = new.value
    if new in TERMINAL_JOB_STATUSES:
        if job.finished_at is None or changed:
            job.finished_at = utcnow()
        failed = counts.get(ItemStatus.ERROR, 0)
        if new == JobStatus.FAILED:
            job.error = job.error or f"{failed} item(s) failed"
        elif failed:
            job.error = f"{failed} item(s) failed"
    else:
        job.finished_at = None
    if changed:
        job.updated_at = utcnow()
    return changed


def open_item_paths(session: Session, paths: Iterable[str] | None = None) -> set[str]:
    """Normalized source paths of items that are still in flight (used to avoid duplicate watch jobs).

    With ``paths``, only those are looked up (indexed ``IN`` queries, like ``already_handled``);
    without, every open item is read (slow on a big library: callers should pass the batch)."""
    open_clauses = [
        JobItem.status.in_([s.value for s in OPEN_ITEM_STATUSES]),
        Job.status.notin_([s.value for s in TERMINAL_JOB_STATUSES]),
    ]
    query = select(JobItem.source_path).join(Job, Job.id == JobItem.job_id).where(*open_clauses)
    busy: set[str] = set()
    if paths is None:
        busy.update(_norm(path) for path in session.scalars(query))
    else:
        # the stored spelling is normpath'd; the batch may not be
        wanted = list(dict.fromkeys(variant for p in paths for variant in (p, os.path.normpath(p))))
        for start in range(0, len(wanted), KNOWN_PATH_CHUNK):
            chunk = wanted[start : start + KNOWN_PATH_CHUNK]
            busy.update(_norm(path) for path in session.scalars(query.where(JobItem.source_path.in_(chunk))))
    # Jobs that haven't expanded their input paths into items yet.
    for inputs in session.scalars(select(Job.input_paths).where(Job.status == JobStatus.QUEUED.value)):
        busy.update(_norm(p) for p in (inputs or []))
    return busy


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def queued_input_dirs(session: Session) -> list[str]:
    """Normalized folder inputs of jobs that haven't expanded them into items yet (a queued manual
    import of ``/downloads`` already covers every file below it)."""
    out: list[str] = []
    for paths in session.scalars(select(Job.input_paths).where(Job.status == JobStatus.QUEUED.value)):
        out.extend(_norm(p).rstrip("\\/") + os.sep for p in (paths or []))
    return out


KNOWN_PATH_CHUNK = 500  # stays far below SQLite's bound-parameter limit


def already_handled(session: Session, files: dict[str, float | None]) -> set[str]:
    """Of ``files`` (path -> mtime in seconds, None if unknown), the normalized paths some job already
    took in and that haven't changed since that job was created.

    Automation (the folder watcher) must not hand such a file over again: a restart re-offers
    everything still sitting in a watch folder, and a file that failed, was skipped or is waiting in
    review would otherwise run again unprompted. Only an explicit retry (rematch / execute / Home
    "Retry") or a changed file (newer mtime) processes it again.
    """
    if not files:
        return set()
    newest: dict[str, Any] = {}
    paths = list(files)
    for start in range(0, len(paths), KNOWN_PATH_CHUNK):
        chunk = paths[start : start + KNOWN_PATH_CHUNK]
        rows = session.execute(
            select(JobItem.source_path, func.max(Job.created_at))
            .join(Job, Job.id == JobItem.job_id)
            .where(JobItem.source_path.in_(chunk))
            .group_by(JobItem.source_path)
        )
        for source, created in rows:
            key = _norm(source)
            if created is not None and (key not in newest or created > newest[key]):
                newest[key] = created
    handled: set[str] = set()
    for path, mtime in files.items():
        key = _norm(path)
        created = newest.get(key)
        if created is None:
            continue
        if mtime is None or mtime <= created.timestamp() + 1.0:
            handled.add(key)
    return handled


# ---------------------------------------------------------------------- formats


def format_out(row: Format) -> FormatOut:
    return FormatOut(
        id=row.id,
        name=row.name,
        kind=MediaKind(row.kind),
        expression=row.expression,
        is_default=bool(row.is_default),
        builtin=bool(row.builtin),
        created_at=row.created_at,
        updated_at=row.updated_at or row.created_at,
    )


def default_expression(session: Session, kind: MediaKind) -> str:
    """The expression of the default format for ``kind`` (user default first, then builtin)."""
    rows = session.scalars(
        select(Format).where(Format.kind == kind.value, Format.is_default.is_(True)).order_by(Format.builtin, Format.id)
    ).all()
    if rows:
        return rows[0].expression
    fallback_kind = kind if kind in DEFAULT_FORMATS else MediaKind.MOVIE
    return DEFAULT_FORMATS[fallback_kind]


def format_expression(session: Session, format_id: int | None) -> str | None:
    if format_id is None:
        return None
    row = session.get(Format, format_id)
    return row.expression if row is not None else None


# ---------------------------------------------------------------------- operations


def operation_out(op: Operation) -> OperationOut:
    status = op.status if op.status in {"done", "failed", "undone", "undo_failed"} else "failed"
    result = op.result or {}  # the full FileOpResult
    return OperationOut(
        id=op.id,
        job_id=op.job_id,
        item_id=op.item_id,
        mode=RenameMode(op.mode),
        source=op.source,
        destination=op.destination,
        status=status,  # type: ignore[arg-type]
        bytes=op.bytes or 0,
        sidecars=[SidecarMove.model_validate(s) for s in (op.sidecars or [])],
        folder_rename=FolderRename.model_validate(result["folder_rename"]) if result.get("folder_rename") else None,
        removed_dirs=list(result.get("removed_dirs") or []),
        error=op.error,
        created_at=op.created_at,
        undone_at=op.undone_at,
    )


def get_operation(session: Session, op_id: int) -> Operation:
    op = session.get(Operation, op_id)
    if op is None:
        raise not_found("operation")
    return op


# ---------------------------------------------------------------------- watch folders


def watch_folder_out(row: WatchFolder, status: str | None) -> WatchFolderOut:
    if not row.enabled:
        status = "stopped"
    if status not in {"watching", "stopped", "error", "missing"}:
        status = "stopped"
    return WatchFolderOut(
        id=row.id,
        path=row.path,
        enabled=bool(row.enabled),
        recursive=bool(row.recursive),
        kind=MediaKind(row.kind) if row.kind else None,
        auto_execute=bool(row.auto_execute),
        settle_seconds=row.settle_seconds,
        format_id=row.format_id,
        dest_root=row.dest_root,
        mode=RenameMode(row.mode) if row.mode else None,
        status=status,  # type: ignore[arg-type]
        last_event_at=row.last_event_at,
        last_error=row.last_error,
        files_seen=row.files_seen or 0,
    )


def dump_candidates(candidates: Iterable[Candidate]) -> list[dict[str, Any]]:
    return [c.model_dump(mode="json") for c in candidates]
