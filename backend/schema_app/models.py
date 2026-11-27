"""ORM tables (SPEC §10)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Computed, Float, ForeignKey, Index, Integer, String, Text, event, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from schema_app.db import UNDONE_SQL, Base, UTCDateTime, utcnow


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # Bumped by "sign out everywhere" and password changes: session cookies carry the epoch they were
    # issued in, and any other value is rejected (SPEC §11).
    session_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=True)


class Format(Base):
    __tablename__ = "formats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    expression: Mapped[str] = mapped_column(Text, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    builtin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    preset_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # builtin rows: presets.py id
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    format_id: Mapped[int | None] = mapped_column(ForeignKey("formats.id", ondelete="SET NULL"), nullable=True)
    dest_root: Mapped[str | None] = mapped_column(Text, nullable=True)
    auto_execute: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    input_paths: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    watch_folder_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    items: Mapped[list[JobItem]] = relationship(
        back_populates="job", cascade="all, delete-orphan", passive_deletes=True, order_by="JobItem.id"
    )

    # AUTOINCREMENT: ids are never reused (the UI caches by id, and demo reset wipes the table).
    __table_args__ = (Index("ix_jobs_status", "status"), {"sqlite_autoincrement": True})


class JobItem(Base):
    """One file of a job. Kept narrow (a few KB): the candidate list and the probe facts live in
    ``job_item_candidates`` so status changes don't rewrite them and scans never walk overflow pages.
    The ``chosen`` / ``match_*`` / ``kind`` / ``search_text`` columns are derived from them when they
    are written (``services.jobs.match_columns``); lists, Home, History and search read only those."""

    __tablename__ = "job_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    parsed: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    selected_candidate_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    proposed_relative: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    path_overridden: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    final_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    folder_rename: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)  # FolderRename
    sidecars: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    crc_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False)
    # The match pass (parse, CRC, lookup, probe) finished and was saved: a pending item with this set
    # only needs rendering when an interrupted job resumes.
    matched: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", nullable=False)
    candidate_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    # Slim form of the candidate lists show: the selected one, else the best suggestion.
    chosen: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # The selected candidate (NULL while nothing is selected), for Home / History cards.
    match_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    match_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    match_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    match_season: Mapped[int | None] = mapped_column(Integer, nullable=True)
    match_episode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kind: Mapped[str | None] = mapped_column(String(16), nullable=True)  # card kind: the match's, else the parsed one
    # lower-cased "source \x1f proposed path \x1f parsed title" for the job/History search box
    search_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The user undid this item's move (an "undone" warning): History shows it, Home doesn't. Computed
    # by SQLite from ``warnings`` (VIRTUAL: nothing stored), so every write path keeps it right.
    undone: Mapped[bool] = mapped_column(Boolean, Computed(UNDONE_SQL, persisted=False))

    job: Mapped[Job] = relationship(back_populates="items")

    __table_args__ = (
        Index("ix_job_items_job_status", "job_id", "status"),
        # Home lists and History lanes newest-first per status (and the dashboard review count)
        Index("ix_job_items_status_updated", "status", "updated_at"),
        # "is this file already known?" (watch de-dup, Home's "a newer copy exists / solved it" checks,
        # history), answered from the index alone
        Index("ix_job_items_source", "source_path", "status", "updated_at"),
        # Home's needs-review count, solved cards, refresh and dismiss-all: every open card's row id and
        # source path without reading the rows. Led by ``undone`` so only queries that filter on it use it.
        Index("ix_job_items_attention", "undone", "status", "source_path"),
        # the job search box: matched against the index instead of every row of a big job
        Index("ix_job_items_search", "job_id", "search_text"),
        {"sqlite_autoincrement": True},
    )


class JobItemCandidates(Base):
    """The heavy part of an item: stored candidates (top 3; only the selected one once the item is
    done or skipped) and the mediainfo/ffprobe facts. Read only for the full item view, matching
    and rendering."""

    __tablename__ = "job_item_candidates"

    item_id: Mapped[int] = mapped_column(ForeignKey("job_items.id", ondelete="CASCADE"), primary_key=True)
    candidates: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    probe: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class Operation(Base):
    __tablename__ = "operations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("job_items.id", ondelete="SET NULL"), nullable=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    destination: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # done | failed | undone | undo_failed
    bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sidecars: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    replaced: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)  # full FileOpResult for undo
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    undone_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (
        Index("ix_operations_job", "job_id"),
        Index("ix_operations_item", "item_id"),  # ON DELETE SET NULL when a job's items are deleted
        Index("ix_operations_created", "created_at"),
        Index("ix_operations_status_mode", "status", "mode", "bytes"),  # covering index for dashboard totals
        {"sqlite_autoincrement": True},
    )


# History sorts the journal by when an entry last happened (undone, else created): an index on exactly
# that expression lets it read the newest page instead of sorting the whole journal.
Index(
    "ix_operations_history",
    func.coalesce(Operation.__table__.c.undone_at, Operation.__table__.c.created_at),
    Operation.__table__.c.id,
)


class WatchFolder(Base):
    __tablename__ = "watch_folders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    path: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    recursive: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    auto_execute: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    settle_seconds: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    format_id: Mapped[int | None] = mapped_column(ForeignKey("formats.id", ondelete="SET NULL"), nullable=True)
    dest_root: Mapped[str | None] = mapped_column(Text, nullable=True)
    mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_event_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    files_seen: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    __table_args__ = (Index("ix_events_ts", "ts"), Index("ix_events_level", "level"), Index("ix_events_job", "job_id"))


class HomeDismissal(Base):
    """A Home "Needs you" card the user dismissed: card "item:12" is (kind "item", ref_id 12)."""

    __tablename__ = "home_dismissals"

    kind: Mapped[str] = mapped_column(String(8), primary_key=True)  # "item" | "event"
    ref_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dismissed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (Index("ix_home_dismissals_dismissed_at", "dismissed_at"),)  # retention prune


# --------------------------------------------------------------------------- derived item columns

SEARCH_SEPARATOR = "\x1f"  # never typed into a search box, so a query can't match across two fields


def search_text_of(source_path: str | None, proposed_path: str | None, title: str | None) -> str:
    return SEARCH_SEPARATOR.join((part or "").lower() for part in (source_path, proposed_path, title))


def card_kind(kind: Any) -> str:
    return kind if kind in {"movie", "series", "anime"} else "unknown"


@event.listens_for(JobItem, "before_insert")
@event.listens_for(JobItem, "before_update")
def _derive_item_columns(_mapper: Any, _connection: Any, item: JobItem) -> None:
    """Keep the search text and card kind in step with ORM writes (bulk statements set them themselves)."""
    parsed = item.parsed or {}
    item.search_text = search_text_of(item.source_path, item.proposed_path, parsed.get("title"))
    item.kind = card_kind(item.match_kind or parsed.get("kind"))
