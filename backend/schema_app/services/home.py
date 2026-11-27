"""The Home flow board (``GET /api/home``): incoming -> needs review -> renaming -> library.

Everything here is synchronous and takes an open ``Session`` (the route runs in the threadpool).
It is polled every few seconds, so it reads a handful of narrow, indexed queries: no per-item
round trips and no candidate blobs (the matched title, year, kind and episode are columns of
``job_items``; lanes are read newest-first per status from the ``(status, updated_at)`` index).

"Needs review" rules
--------------------
* Items in ``review`` or ``error`` make a card, unless the user dismissed it or a newer item for the
  same source path exists (a re-import replaces the old card).
* An item whose newer copy ended ``done`` / ``skipped`` is *solved*: its card stays (without
  counting, with a Dismiss action) for ``clear_solved_after_hours`` after that, then clears
  (0 = right away, None = never).
* Error events that no item card covers (system, watcher, provider, media server, a job that failed
  without items) make ``event`` cards until dismissed or superseded (the job was retried, a newer
  identical message); they also clear once older than ``clear_solved_after_hours`` (when > 0).
* Refresh (``POST /api/home/refresh``) dismisses item cards whose source file is gone from disk;
  ``POST /api/home/dismiss-all`` dismisses every card.
"""

from __future__ import annotations

import functools
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import PurePath
from typing import Any

from sqlalchemy import Text, and_, cast, delete, func, not_, or_, select
from sqlalchemy.orm import Session, aliased

from schema_app.api_models import (
    AppSettings,
    HomeAction,
    HomeMediaServer,
    HomeOut,
    HomeStatus,
    HomeWatchFolder,
    IncomingFile,
    ItemStatus,
    JobStatus,
    LibraryCard,
    LibrarySummary,
    NeedsYouCard,
    RenamingCard,
)
from schema_app.core import parser as parser_mod
from schema_app.db import utcnow
from schema_app.domain import ProviderStatus
from schema_app.models import Event, HomeDismissal, Job, JobItem, Operation, WatchFolder, card_kind
from schema_app.services.jobs import already_handled, watch_folder_out

NEEDS_YOU_MAX = 50
EVENT_SCAN = 200  # newest error events looked at for event cards
RENAMING_MAX = 20
LIBRARY_RECENT = 12
DISMISSAL_RETENTION = timedelta(days=90)

ACTIVE_JOBS = (JobStatus.QUEUED.value, JobStatus.MATCHING.value, JobStatus.EXECUTING.value)
SOLVED_ITEM_STATUSES = (ItemStatus.DONE.value, ItemStatus.SKIPPED.value)
CATEGORY_LABELS = {
    "job": "Import",
    "watcher": "Watch folder",
    "provider": "Metadata lookup",
    "fileops": "Moving files",
    "webhook": "Download client",
    "mediaserver": "Media server",
    "system": "Schema",
}


# --------------------------------------------------------------------------- shared helpers


def display_title(title: str | None, year: Any) -> str | None:
    if not title:
        return None
    return f"{title} ({year})" if year else str(title)


@dataclass
class ItemFacts:
    """The columns of a ``job_items`` row the board needs (no candidate / probe blobs)."""

    id: int
    job_id: int
    status: str
    source_path: str
    warnings: list[str]
    error: str | None
    confidence: float
    proposed_path: str | None
    updated_at: datetime
    selected_id: str | None
    cand_id: str | None
    cand_title: str | None
    cand_year: Any
    cand_kind: str | None
    parsed_title: str | None
    parsed_kind: str | None
    cand_season: Any = None
    cand_episode: Any = None

    @property
    def filename(self) -> str:
        return os.path.basename(self.source_path) or self.source_path

    @property
    def candidate_title(self) -> str | None:
        return display_title(self.cand_title, self.cand_year)

    @property
    def title(self) -> str:
        return self.candidate_title or self.parsed_title or PurePath(self.filename).stem

    @property
    def full_title(self) -> str | None:
        """The matched title plus the episode for series ("Starfall Station (2016) · S01E03")."""
        title = self.candidate_title
        if title and isinstance(self.cand_episode, int):
            season = self.cand_season if isinstance(self.cand_season, int) else 1
            return f"{title} · S{season:02d}E{self.cand_episode:02d}"
        return title

    @property
    def kind(self) -> str:
        return card_kind(self.cand_kind or self.parsed_kind)


ITEM_COLUMNS = (
    JobItem.id, JobItem.job_id, JobItem.status, JobItem.source_path, JobItem.warnings, JobItem.error,
    JobItem.confidence, JobItem.proposed_path, JobItem.updated_at, JobItem.selected_candidate_id,
    JobItem.match_title, JobItem.match_year, JobItem.match_kind,
    func.json_extract(JobItem.parsed, "$.title"), func.json_extract(JobItem.parsed, "$.kind"),
    JobItem.match_season, JobItem.match_episode,
)  # fmt: skip


def item_facts(session: Session, rows: Sequence[Any]) -> list[ItemFacts]:
    """Rows selected with ``ITEM_COLUMNS`` -> facts. The ``match_*`` columns describe the selected
    candidate (NULL without a selection: suggestions are not a match)."""
    return [
        ItemFacts(id=r[0], job_id=r[1], status=r[2], source_path=r[3], warnings=list(r[4] or []), error=r[5],
                  confidence=float(r[6] or 0.0), proposed_path=r[7], updated_at=r[8], selected_id=r[9],
                  cand_id=r[9] if r[9] and r[10] is not None else None, cand_title=r[10], cand_year=r[11],
                  cand_kind=r[12], parsed_title=r[13], parsed_kind=r[14], cand_season=r[15], cand_episode=r[16])  # fmt: skip
        for r in rows
    ]


def newest_rows(session: Session, columns: Sequence[Any], clauses: Sequence[Any], statuses: Sequence[str],
                limit: int) -> list[Any]:  # fmt: skip
    """The ``limit`` newest items (by ``updated_at``, then id) in ``statuses`` matching ``clauses``;
    ``columns`` start with ``JobItem.id`` and include ``JobItem.updated_at``.

    One query per status, each read backwards from the ``(status, updated_at)`` index and stopping
    after ``limit`` rows, then merged: a single ``status IN (...) ORDER BY updated_at`` would sort every
    matching row of a big library first."""
    ts = next(i for i, column in enumerate(columns) if column is JobItem.updated_at)
    rows: list[Any] = []
    for status in statuses:
        rows.extend(session.execute(
            select(*columns).where(JobItem.status == status, *clauses)
            .order_by(JobItem.updated_at.desc(), JobItem.id.desc()).limit(limit)
        ).all())  # fmt: skip
    rows.sort(key=lambda r: (r[ts], r[0]), reverse=True)
    return rows[:limit]


SOURCE_MISSING_REASON = "The file was moved or deleted before Schema could rename it"
SOURCE_MISSING_GROUP_MIN = 5  # this many source_missing cards from one job collapse into one card


def friendly_error(error: str | None) -> str:
    """Plain-English text for a raw error. The reason *code* never comes from here: items carry a
    typed warning (``source_missing``, ``destination_exists``) for that."""
    text = (error or "").strip()
    low = text.lower()
    if not text:
        return "Something went wrong with this file"
    if low.startswith("source file no longer exists"):  # rows recorded before the typed warning
        return SOURCE_MISSING_REASON
    if "already exists" in low:
        return "A file with that name already exists"
    if "space" in low or "disk full" in low or "enospc" in low:
        return "Not enough free space to move this file"
    if "permission" in low or "access is denied" in low or "eacces" in low or "eperm" in low:
        return "Schema isn't allowed to move this file (check folder permissions)"
    if "allowed root" in low:
        return "The destination is outside the folders Schema may change"
    if "no destination" in low:
        return "There is no destination for this file yet"
    if "changed" in low and "undo" in low:
        return "The file changed since it was moved, so it can't be undone"
    if len(text) > 160:
        text = text[:157] + "..."
    return f"Couldn't move this file: {text}"


def _has(warnings: Iterable[str], *names: str) -> bool:
    return any(w == n or w.startswith(n + ":") for w in warnings for n in names)


def item_reason(fact: ItemFacts) -> tuple[str, str, HomeAction | None]:
    """(reason_code, plain-English reason, primary action) of an item card."""
    w = fact.warnings
    cand = fact.candidate_title
    runnable = bool(cand and fact.proposed_path)
    if fact.status == ItemStatus.ERROR.value:
        message = friendly_error(fact.error)
        if _has(w, "source_missing") or message == SOURCE_MISSING_REASON:
            return "source_missing", SOURCE_MISSING_REASON, HomeAction(label="Dismiss", action="dismiss")
        if _has(w, "destination_exists") or message.startswith("A file with that name"):
            return "conflict", "A file with that name already exists", HomeAction(label="Retry", action="retry")
        return "error", message, HomeAction(label="Retry", action="retry")
    if _has(w, "interrupted"):
        return ("interrupted", "Schema stopped while moving this file. Check it, then try again.",
                HomeAction(label="Retry", action="retry"))  # fmt: skip
    if _has(w, "extra"):
        if _has(w, "replaces_existing", "upgrade_existing"):
            reason = f"Bonus clip · would overwrite {cand}" if cand else "Bonus clip · would overwrite a file"
        elif _has(w, "destination_exists", "duplicate_in_batch"):
            reason = f"Looks like a bonus clip. {cand} is already in your library." if cand else (
                "Looks like a bonus clip, and a file with that name already exists.")  # fmt: skip
        else:
            reason = f"Looks like a bonus clip of {cand}." if cand else "Looks like a bonus clip."
        return "extra", reason, HomeAction(label="Skip as extra", action="skip")
    if _has(w, "sample"):
        return "sample", "Looks like a sample, not the full file.", HomeAction(label="Skip sample", action="skip")
    if _has(w, "crc_mismatch"):
        return ("crc_mismatch", "The checksum in the file name doesn't match. The file may be damaged.",
                HomeAction(label="Skip", action="skip"))  # fmt: skip
    if _has(w, "destination_exists"):
        reason = f"A file with that name already exists ({cand})" if cand else "A file with that name already exists"
        return "conflict", reason, HomeAction(label="Skip", action="skip")
    if _has(w, "duplicate_in_batch"):
        return ("conflict", "Another file in this import would get the same name",
                HomeAction(label="Skip", action="skip"))  # fmt: skip
    if _has(w, "no_match") or not fact.selected_id:
        if _has(w, "match_error", "matcher_unavailable", "provider_error"):
            return ("error", "Couldn't look this file up. Check Connections, then retry.",
                    HomeAction(label="Retry", action="retry"))  # fmt: skip
        return "no_match", "No match found", None
    if _has(w, "episode_title_mismatch"):
        reason = (f"The episode title in the file name doesn't match {fact.full_title or cand}"
                  if cand else "The episode title in the file name doesn't match the episode number")  # fmt: skip
        return "episode_mismatch", reason, None
    if _has(w, "low_confidence"):
        pct = round(fact.confidence * 100)
        reason = f"Not sure this is {cand} ({pct}% match)"
        return "low_confidence", reason, HomeAction(label=f"Use {cand}", action="approve") if runnable else None
    if _has(w, "no_library_root"):
        return "error", "No library folder is set for this kind of file", None
    if _has(w, "format_error", "no_destination", "plan_error"):
        return "error", "The naming formula couldn't build a name for this file", None
    if _has(w, "parse_error"):
        return "error", "Couldn't read this file name", None
    if runnable:
        return "low_confidence", f"Check this is {cand}", HomeAction(label=f"Use {cand}", action="approve")
    return "error", "This file needs a look", None


@functools.lru_cache(maxsize=16384)  # keyed by path; above the files a big download folder holds at once
def _title_guess(path: str) -> str | None:
    try:
        parsed = parser_mod.parse(path)
    except Exception:
        return None
    return display_title(parsed.title, parsed.year)


# --------------------------------------------------------------------------- dismissals


def dismiss(session: Session, kind: str, ref_id: int) -> None:
    if session.get(HomeDismissal, (kind, ref_id)) is None:
        session.add(HomeDismissal(kind=kind, ref_id=ref_id))
    # Keep the table small: old dismissals refer to long-pruned events / deleted items.
    session.execute(delete(HomeDismissal).where(HomeDismissal.dismissed_at < utcnow() - DISMISSAL_RETENTION))


def _dismiss_new(session: Session, kind: str, ref_ids: Sequence[int]) -> int:
    """Bulk ``dismiss`` for ids already filtered with ``_not_dismissed`` (one insert, one prune)."""
    session.add_all([HomeDismissal(kind=kind, ref_id=ref_id) for ref_id in ref_ids])
    session.execute(delete(HomeDismissal).where(HomeDismissal.dismissed_at < utcnow() - DISMISSAL_RETENTION))
    return len(ref_ids)


def dismiss_many(session: Session, kind: str, ref_ids: Sequence[int]) -> int:
    """``dismiss`` for many cards at once (a grouped card): skips the ones already dismissed."""
    wanted = list(dict.fromkeys(ref_ids))
    known: set[int] = set()
    for start in range(0, len(wanted), 500):
        known.update(session.scalars(select(HomeDismissal.ref_id).where(
            HomeDismissal.kind == kind, HomeDismissal.ref_id.in_(wanted[start : start + 500]))))  # fmt: skip
    return _dismiss_new(session, kind, [i for i in wanted if i not in known])


def dismiss_all(session: Session) -> int:
    """"Dismiss all" on the Needs review column: every item card (open, solved, grouped) and every
    error event card, including the ones past the 50 shown. Returns how many were dismissed."""
    items = session.scalars(select(JobItem.id).where(*_base_item_clauses())).all()
    events = session.scalars(select(Event.id).where(Event.level == "error", _not_dismissed("event", Event.id))).all()
    return _dismiss_new(session, "item", items) + _dismiss_new(session, "event", events)


def _not_dismissed(kind: str, column: Any) -> Any:
    # SELECT of the key (not *): answered from the primary key index alone
    return ~select(HomeDismissal.ref_id).where(HomeDismissal.kind == kind, HomeDismissal.ref_id == column).exists()


# --------------------------------------------------------------------------- refresh


def _source_state(path: str) -> bool | None:
    """True: the file is there. False: it is gone. None: can't tell (permissions, share offline)."""
    try:
        os.stat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return None
    return True


def _under(path: str, root: str) -> bool:
    p, r = (os.path.normcase(os.path.normpath(x)) for x in (path, root))
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def clear_vanished(session: Session) -> int:
    """The Refresh button: dismiss every review / error card whose source file is no longer on disk
    (the user cleaned up the downloads folder by hand). Returns how many cards were cleared.

    A watch folder that is itself missing (an unmounted share) proves nothing about its files, so
    cards under it stay; so do cards whose file can't be checked at all."""
    roots = [row.path for row in session.scalars(select(WatchFolder))]
    offline = [r for r in roots if not os.path.isdir(r)]
    rows = session.execute(select(JobItem.id, JobItem.source_path).where(*_base_item_clauses())).all()
    gone = [item_id for item_id, source in rows
            if not any(_under(source, r) for r in offline) and _source_state(source) is False]  # fmt: skip
    return _dismiss_new(session, "item", gone)


# --------------------------------------------------------------------------- needs you


def source_missing_clause() -> Any:
    """Failed items whose source vanished before the rename (typed warning, or an older row's error)."""
    return and_(
        JobItem.status == ItemStatus.ERROR.value,
        or_(cast(JobItem.warnings, Text).like('%"source_missing"%'),
            JobItem.error.like("Source file no longer exists%")),
    )  # fmt: skip


def open_item_clauses(*, listing: bool = False) -> list[Any]:
    later = aliased(JobItem)
    # "a newer item for the same file exists" from the source index alone (SELECT id, not *)
    newer = select(later.id).where(later.source_path == JobItem.source_path, later.id > JobItem.id).exists()
    return [*_base_item_clauses(listing=listing), ~newer]


NEEDS_STATUSES = (ItemStatus.REVIEW.value, ItemStatus.ERROR.value)


def _base_item_clauses(*, listing: bool = False) -> list[Any]:
    """Item cards: waiting for the user, not dismissed, not undone (an undone move is the user's own
    doing: it shows in History, not as a problem).

    Counting and collecting ids read the ``(undone, status, source_path)`` index alone. ``listing``
    (newest first, a page of rows) hides ``undone`` from the planner, so it reads the
    ``(status, updated_at)`` index backwards and stops after a page instead of sorting every card."""
    not_undone = func.coalesce(JobItem.undone, False).is_(False) if listing else JobItem.undone.is_(False)
    return [JobItem.status.in_(NEEDS_STATUSES), _not_dismissed("item", JobItem.id), not_undone]


def _missing_groups(session: Session, open_clauses: list[Any]) -> list[NeedsYouCard]:
    """One card per job with many vanished sources (a whole download folder moved after the scan)."""
    rows = session.execute(
        select(JobItem.job_id, func.count(JobItem.id), func.max(JobItem.updated_at), Job.label)
        .join(Job, Job.id == JobItem.job_id)
        .where(*open_clauses, source_missing_clause())
        .group_by(JobItem.job_id, Job.label)
        .having(func.count(JobItem.id) >= SOURCE_MISSING_GROUP_MIN)
    ).all()  # fmt: skip
    cards: list[NeedsYouCard] = []
    for job_id, count, ts, label in rows:
        name = f"job '{label}'" if label else f"job #{job_id}"
        cards.append(NeedsYouCard(
            key=f"group:{job_id}", kind="group", job_id=job_id, count=int(count),
            title=f"{int(count):,} files from {name} no longer exist at their source",
            reason=SOURCE_MISSING_REASON, reason_code="source_missing",
            primary=HomeAction(label="Dismiss all", action="dismiss"), ts=ts,
        ))  # fmt: skip
    return cards


def _item_cards(session: Session, settings: AppSettings, now: datetime) -> tuple[list[NeedsYouCard], int]:
    base = _base_item_clauses()
    open_clauses = open_item_clauses()
    count = int(session.scalar(select(func.count(JobItem.id)).where(*open_clauses)) or 0)
    cards = _missing_groups(session, open_clauses)
    grouped = [c.job_id for c in cards]
    listed = open_item_clauses(listing=True)
    if grouped:
        listed.append(not_(and_(JobItem.job_id.in_(grouped), source_missing_clause())))
    rows = newest_rows(session, ITEM_COLUMNS, listed, NEEDS_STATUSES, NEEDS_YOU_MAX)
    for fact in item_facts(session, rows):
        code, reason, primary = item_reason(fact)
        cards.append(NeedsYouCard(key=f"item:{fact.id}", kind="item", item_id=fact.id, job_id=fact.job_id,
                                  title=fact.filename, reason=reason, reason_code=code, primary=primary,  # type: ignore[arg-type]
                                  candidate_title=fact.candidate_title,
                                  confidence=fact.confidence if fact.selected_id else None,
                                  ts=fact.updated_at))  # fmt: skip
    hours = settings.clear_solved_after_hours
    if hours == 0:
        return cards, count
    # Solved: a newer copy of the same file was moved or skipped. Shown (not counted) for a while.
    solver = aliased(JobItem)
    solved_at = (
        select(func.max(solver.updated_at))
        .where(solver.source_path == JobItem.source_path, solver.id > JobItem.id,
               solver.status.in_(SOLVED_ITEM_STATUSES))  # fmt: skip
        .scalar_subquery()
    )
    solved_clauses = [*base, solved_at.is_not(None)]
    if hours is not None:
        solved_clauses.append(solved_at >= now - timedelta(hours=hours))
    # Few cards are solved: find them from the attention index (the solver probe reads the source
    # index alone), then sort just those.
    solved_rows = session.execute(
        select(*ITEM_COLUMNS).where(*solved_clauses).order_by(JobItem.updated_at.desc(), JobItem.id.desc())
        .limit(NEEDS_YOU_MAX)
    ).all()  # fmt: skip
    for fact in item_facts(session, solved_rows):
        _code, reason, _primary = item_reason(fact)
        cards.append(NeedsYouCard(key=f"item:{fact.id}", kind="item", item_id=fact.id, job_id=fact.job_id,
                                  title=fact.filename, reason=f"Solved: a newer copy was handled. ({reason})",
                                  reason_code=_code, primary=HomeAction(label="Dismiss", action="dismiss"),  # type: ignore[arg-type]
                                  candidate_title=fact.candidate_title, confidence=None,
                                  ts=fact.updated_at))  # fmt: skip
    return cards, count


def _event_cards(session: Session, settings: AppSettings, now: datetime) -> list[NeedsYouCard]:
    clauses = [Event.level == "error", _not_dismissed("event", Event.id)]
    hours = settings.clear_solved_after_hours
    if hours:  # 0 / None: only dismissing (or a superseding retry) clears an event
        clauses.append(Event.ts >= now - timedelta(hours=hours))
    events = session.scalars(select(Event).where(*clauses).order_by(Event.id.desc()).limit(EVENT_SCAN)).all()
    job_ids = {e.job_id for e in events if e.job_id is not None}
    jobs: dict[int, str] = {}
    with_errors: set[int] = set()
    if job_ids:
        jobs = dict(session.execute(select(Job.id, Job.status).where(Job.id.in_(job_ids))).all())
        with_errors = set(session.scalars(
            select(JobItem.job_id).where(JobItem.job_id.in_(job_ids), JobItem.status == ItemStatus.ERROR.value)
            .distinct()
        ))  # fmt: skip
    cards: list[NeedsYouCard] = []
    seen: set[str] = set()
    for event in events:
        # Covered by the job's item cards, or superseded (the job was retried / deleted).
        if event.job_id is not None and (
            jobs.get(event.job_id) != JobStatus.FAILED.value or event.job_id in with_errors
        ):
            continue
        if event.message in seen:
            continue  # superseded by a newer identical message
        seen.add(event.message)
        label = CATEGORY_LABELS.get(event.category, event.category.capitalize())
        title = event.message if len(event.message) <= 120 else event.message[:117] + "..."
        cards.append(NeedsYouCard(key=f"event:{event.id}", kind="event", event_id=event.id, job_id=event.job_id,
                                  title=title, reason=f"{label} reported a problem", reason_code="event",
                                  primary=HomeAction(label="Dismiss", action="dismiss"), ts=event.ts))  # fmt: skip
    return cards


# --------------------------------------------------------------------------- the other lanes


RENAMING_STATUSES = (ItemStatus.EXECUTING.value, ItemStatus.PENDING.value, ItemStatus.MATCHED.value,
                     ItemStatus.APPROVED.value)  # fmt: skip


def _renaming(session: Session) -> list[RenamingCard]:
    """Executing items first, then by job and id. Each (active job, status) is read in id order from
    the ``(job_id, status)`` index with a limit: a 50k-item job never gets sorted as a whole."""
    progress = dict(session.execute(select(Job.id, Job.progress).where(Job.status.in_(ACTIVE_JOBS))).all())
    rows: list[Any] = []
    for job_id in sorted(progress):
        for status in RENAMING_STATUSES:
            rows.extend(session.execute(
                select(*ITEM_COLUMNS).where(JobItem.job_id == job_id, JobItem.status == status)
                .order_by(JobItem.id).limit(RENAMING_MAX)
            ).all())  # fmt: skip
    rows.sort(key=lambda r: (r[2] != ItemStatus.EXECUTING.value, r[1], r[0]))
    cards: list[RenamingCard] = []
    for fact in item_facts(session, rows[:RENAMING_MAX]):
        status = "matching" if fact.status == ItemStatus.PENDING.value else fact.status
        job_progress = progress.get(fact.job_id)
        cards.append(RenamingCard(item_id=fact.id, job_id=fact.job_id, title=fact.title, filename=fact.filename,
                                  status=status,  # type: ignore[arg-type]
                                  job_progress=float(job_progress) if job_progress is not None else None))  # fmt: skip
    return cards


def _done_ops() -> Any:
    return and_(Operation.status == "done", Operation.mode != "test")


def _library(session: Session) -> LibrarySummary:
    total_files = int(session.scalar(select(func.count(Operation.id)).where(_done_ops())) or 0)
    total_bytes = int(session.scalar(select(func.coalesce(func.sum(Operation.bytes), 0)).where(_done_ops())) or 0)
    # newest first by id, stopping after a few ("|| ''" keeps the planner from sorting every done move)
    recent_done = and_((Operation.status + "") == "done", Operation.mode != "test")
    ops = session.scalars(select(Operation).where(recent_done).order_by(Operation.id.desc()).limit(LIBRARY_RECENT)).all()
    facts: dict[int, ItemFacts] = {}
    item_ids = [op.item_id for op in ops if op.item_id is not None]
    if item_ids:
        rows = session.execute(select(*ITEM_COLUMNS).where(JobItem.id.in_(item_ids))).all()
        facts = {f.id: f for f in item_facts(session, rows)}
    active = set(session.scalars(select(Job.id).where(Job.status.in_(ACTIVE_JOBS))))
    recent: list[LibraryCard] = []
    for op in ops:
        fact = facts.get(op.item_id) if op.item_id is not None else None
        title = (fact.full_title if fact else None) or PurePath(op.destination).stem
        episode = fact.cand_episode if fact and fact.candidate_title and isinstance(fact.cand_episode, int) else None
        season = (fact.cand_season if isinstance(fact.cand_season, int) else 1) if fact and episode is not None else None
        recent.append(LibraryCard(operation_id=op.id, title=title, kind=fact.kind if fact else "unknown",  # type: ignore[arg-type]
                                  destination=op.destination, ts=op.created_at, bytes=op.bytes or 0,
                                  can_undo=bool(op.result) and op.job_id not in active,
                                  show=fact.candidate_title if fact and episode is not None else None,
                                  season=season, episode=episode))  # fmt: skip
    return LibrarySummary(recent=recent, total_files=total_files, total_bytes=total_bytes)


def _incoming(session: Session, watched: Sequence[Any], folders: dict[int, WatchFolder]) -> list[IncomingFile]:
    waiting = [f for f in watched if f.folder_id in folders and folders[f.folder_id].enabled]
    if not waiting:
        return []
    settling = {f.path: (f.mtime_ns / 1e9 if f.mtime_ns and f.mtime_ns > 0 else None)
                for f in waiting if f.state == "settling"}  # fmt: skip
    # After a restart the watcher waits out the settle time of files it will then not hand over
    # (already handled): don't show those as incoming.
    known = already_handled(session, settling)
    out: list[IncomingFile] = []
    for f in waiting:
        if f.state == "settling" and os.path.normcase(os.path.normpath(f.path)) in known:
            continue
        out.append(IncomingFile(
            folder_id=f.folder_id, path=f.path, name=os.path.basename(f.path), size=max(0, int(f.size)),
            state="partial" if f.state == "partial" else "settling", settle_seconds=round(f.settle_seconds),
            stable_since=f.stable_since, settles_at=f.settles_at,
            title_guess=_title_guess(_strip_partial_suffix(f.path) if f.state == "partial" else f.path),
        ))  # fmt: skip
    return out


def _strip_partial_suffix(path: str) -> str:
    stem, _, _ext = path.rpartition(".")
    return stem or path


def build_home(session: Session, settings: AppSettings, *, watch: Any, providers: list[ProviderStatus]) -> HomeOut:
    now = utcnow()
    item_cards, item_count = _item_cards(session, settings, now)
    event_cards = _event_cards(session, settings, now)
    cards = sorted([*item_cards, *event_cards], key=lambda c: c.ts, reverse=True)[:NEEDS_YOU_MAX]
    folders = {row.id: row for row in session.scalars(select(WatchFolder).order_by(WatchFolder.id))}
    watch_status = []
    for row in folders.values():
        out = watch_folder_out(row, watch.status(row.id) if watch is not None else None)
        watch_status.append(HomeWatchFolder(id=row.id, path=row.path, status=out.status, enabled=bool(row.enabled)))
    servers = [
        HomeMediaServer(id=s.id, kind=s.kind, name=s.name, url=s.url, enabled=s.enabled)  # type: ignore[arg-type]
        for s in settings.media_servers if s.kind in {"jellyfin", "plex", "emby"}
    ]  # fmt: skip
    return HomeOut(
        needs_you_count=item_count + len(event_cards),
        incoming=_incoming(session, watch.incoming() if watch is not None else [], folders),
        needs_you=cards,
        renaming=_renaming(session),
        library=_library(session),
        status=HomeStatus(watch=watch_status, providers=providers, media_servers=servers),
        server_time=now,
    )


__all__ = [
    "ITEM_COLUMNS",
    "ItemFacts",
    "build_home",
    "card_kind",
    "clear_vanished",
    "dismiss",
    "dismiss_all",
    "dismiss_many",
    "display_title",
    "friendly_error",
    "item_facts",
    "item_reason",
    "newest_rows",
    "open_item_clauses",
    "source_missing_clause",
]
