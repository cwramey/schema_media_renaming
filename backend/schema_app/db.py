"""SQLite engine and sessions (SQLAlchemy 2.0, synchronous; async code calls it via threads)."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import DateTime, Engine, create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator

log = logging.getLogger("schema.db")

SCHEMA_VERSION = 6  # PRAGMA user_version; bump together with a migration step in Database.init()

CACHE_KIB = 8192  # page cache per connection (default is 2 MiB)
MMAP_BYTES = 256 * 1024 * 1024  # reads served from the OS page cache instead of copied into each connection
ANALYSIS_LIMIT = 1000  # rows ANALYZE samples per index: keeps PRAGMA optimize cheap on a big database
MIGRATE_CHUNK = 500  # rows per transaction while moving candidates out of job_items (v6)
UNDONE_SQL = "instr(warnings, '\"undone\"') > 0"  # job_items.undone (a VIRTUAL generated column)


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Stores naive UTC in SQLite, hands back timezone-aware UTC datetimes."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is not None:
            value = value.astimezone(UTC)
        return value.replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


def _on_connect(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        # Truncate the WAL back to 64 MiB after checkpoints (a big match save can otherwise leave a
        # WAL as large as the job's JSON on the NAS disk) and keep temp b-trees off the HDD.
        cursor.execute("PRAGMA journal_size_limit=67108864")
        cursor.execute("PRAGMA temp_store=MEMORY")
        cursor.execute(f"PRAGMA cache_size=-{CACHE_KIB}")
        cursor.execute(f"PRAGMA mmap_size={MMAP_BYTES}")
        cursor.execute(f"PRAGMA analysis_limit={ANALYSIS_LIMIT}")
    finally:
        cursor.close()


def make_engine(path: Path | str) -> Engine:
    url = "sqlite://" if str(path) == ":memory:" else f"sqlite:///{Path(path)}"
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False, "timeout": 30},
        pool_pre_ping=False,
    )
    event.listen(engine, "connect", _on_connect)
    return engine


class Database:
    """Owns the engine and the session factory."""

    def __init__(self, path: Path | str) -> None:
        self.path = path
        self.engine = make_engine(path)
        self.sessionmaker = sessionmaker(self.engine, expire_on_commit=False, class_=Session)

    def init(self) -> None:
        """Create tables (``create_all``), migrate older databases and stamp ``PRAGMA user_version``.

        Every step is idempotent (it checks what is already there), so a migration interrupted by a
        restart simply continues; the version is stamped only once all of them are done."""
        from schema_app import models  # noqa: F401 - register tables on Base.metadata

        self._backup_before_upgrade()
        Base.metadata.create_all(self.engine)
        with self.engine.begin() as conn:
            current = conn.execute(text("PRAGMA user_version")).scalar() or 0
            if current < 3:
                # v3: users.session_epoch (session revocation; create_all never adds columns).
                columns = _columns(conn, "users")
                if "session_epoch" not in columns:
                    conn.execute(text("ALTER TABLE users ADD COLUMN session_epoch INTEGER NOT NULL DEFAULT 0"))
            # v4: job_items.folder_rename (folders renamed in place instead of left behind).
            if current < 4 and "folder_rename" not in _columns(conn, "job_items"):
                conn.execute(text("ALTER TABLE job_items ADD COLUMN folder_rename JSON"))
            if current < 6:
                # v6: derived columns that lists, Home, History and search read instead of the blobs.
                columns = _columns(conn, "job_items")
                for name, ddl in _V6_ITEM_COLUMNS.items():
                    if name not in columns:
                        conn.execute(text(f"ALTER TABLE job_items ADD COLUMN {name} {ddl}"))
        if current < 6:
            # v6: candidates + probe move to job_item_candidates (chunked, one transaction per chunk).
            self._move_item_blobs()
        with self.engine.begin() as conn:
            if current < 6:
                for legacy in ("candidates", "probe"):
                    if legacy in _columns(conn, "job_items"):  # emptied above: dropping them is cheap
                        conn.execute(text(f"ALTER TABLE job_items DROP COLUMN {legacy}"))
                # covered by (status, updated_at) and (source_path, status, updated_at)
                conn.execute(text("DROP INDEX IF EXISTS ix_job_items_status"))
                conn.execute(text("DROP INDEX IF EXISTS ix_job_items_source_path"))
            if current < SCHEMA_VERSION:
                # v2, v5, v6: indexes added to existing tables (create_all only creates them with new tables).
                # By name: SQLAlchemy's checkfirst can't see expression indexes and would create them twice.
                existing = {row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'index'"))}
                for table in Base.metadata.sorted_tables:
                    for index in table.indexes:
                        if index.name not in existing:
                            index.create(conn)
                conn.execute(text(f"PRAGMA user_version = {SCHEMA_VERSION}"))
        if 0 < current < SCHEMA_VERSION:
            self.optimize(all_tables=True)  # statistics for the new indexes
        self._restrict_permissions()

    def backup_path(self) -> Path | None:
        """Where :meth:`init` keeps a copy of an older database before upgrading it (None in memory)."""
        if str(self.path) == ":memory:":
            return None
        base = Path(self.path)
        return base.with_name(f"{base.name}.pre-v{SCHEMA_VERSION}.bak")

    def _backup_before_upgrade(self) -> None:
        """Copy an existing database that is about to be migrated (online SQLite backup, so the WAL is
        included) to ``<db>.pre-v<N>.bak``, once per target version and owner-only. Upgrades rewrite and
        drop columns; this keeps the user's job history recoverable if one goes wrong. A failed backup
        is logged and the upgrade goes ahead (refusing to start would take the whole app down)."""
        target = self.backup_path()
        if target is None or target.exists() or not Path(self.path).is_file():
            return
        with self.engine.connect() as conn:
            current = int(conn.execute(text("PRAGMA user_version")).scalar() or 0)
        if not 0 < current < SCHEMA_VERSION:
            return
        partial = target.with_name(target.name + ".partial")
        try:
            src = sqlite3.connect(str(self.path))
            try:
                dst = sqlite3.connect(str(partial))
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
            os.chmod(partial, 0o600)
            os.replace(partial, target)
            log.info("Backed up the v%d database to %s before upgrading it to v%d", current, target, SCHEMA_VERSION)
        except Exception:
            log.exception("Could not back up the database before upgrading it; upgrading anyway")
            with contextlib.suppress(OSError):
                partial.unlink()

    def _move_item_blobs(self) -> None:
        """v6: copy every item's candidates (trimmed to what is kept now) and probe into
        ``job_item_candidates``, fill the derived columns and empty the old blob columns."""
        from schema_app.services.jobs import keep_candidates_raw, match_columns

        with self.engine.connect() as conn:
            columns = _columns(conn, "job_items")
            if "candidates" not in columns:
                return
            remaining = conn.execute(text("SELECT count(*) FROM job_items WHERE candidates != '[]' OR "
                                          "probe IS NOT NULL")).scalar() or 0  # fmt: skip
        if remaining:
            log.info("Moving the stored matches of %d item(s) out of job_items (one-time upgrade)", remaining)
        last, moved = 0, 0
        while True:
            with self.engine.begin() as conn:
                rows = conn.exec_driver_sql(
                    "SELECT id, status, source_path, proposed_path, parsed, candidates, selected_candidate_id, probe "
                    "FROM job_items WHERE id > ? ORDER BY id LIMIT ?",
                    (last, MIGRATE_CHUNK),
                ).fetchall()
                if not rows:
                    break
                side: list[tuple[Any, ...]] = []
                updates: list[dict[str, Any]] = []
                for item_id, status, source, proposed, parsed_raw, candidates_raw, selected, probe_raw in rows:
                    parsed = _loads(parsed_raw)
                    candidates = _loads(candidates_raw) or []
                    kept = keep_candidates_raw(candidates, selected, status)
                    if kept or probe_raw:
                        side.append((item_id, json.dumps(kept), probe_raw))
                    derived = match_columns(kept, selected, parsed, source, proposed)
                    derived["chosen"] = json.dumps(derived["chosen"]) if derived["chosen"] is not None else None
                    updates.append({**derived, "matched": int(status != "pending"), "id": item_id})
                if side:
                    conn.exec_driver_sql("INSERT OR REPLACE INTO job_item_candidates (item_id, candidates, probe) "
                                         "VALUES (?, ?, ?)", side)  # fmt: skip
                conn.execute(text(
                    "UPDATE job_items SET matched = :matched, candidate_count = :candidate_count, chosen = :chosen, "
                    "match_title = :match_title, match_year = :match_year, match_kind = :match_kind, "
                    "match_season = :match_season, match_episode = :match_episode, kind = :kind, "
                    "search_text = :search_text, candidates = '[]', probe = NULL WHERE id = :id"
                ), updates)  # fmt: skip
                last = rows[-1][0]
                moved += len(rows)
            if moved % 10_000 < MIGRATE_CHUNK:
                log.info("... %d item(s) upgraded", moved)

    def optimize(self, *, all_tables: bool = False) -> None:
        """``PRAGMA optimize``: refresh planner statistics where they went stale (cheap; bounded by
        ``analysis_limit``). Run after migrations, periodically and on shutdown."""
        try:
            with self.engine.begin() as conn:
                conn.exec_driver_sql("PRAGMA optimize=0x10002" if all_tables else "PRAGMA optimize")
        except Exception:  # pragma: no cover - housekeeping must never take anything down
            log.warning("PRAGMA optimize failed", exc_info=True)

    def _restrict_permissions(self) -> None:
        """The DB holds password hashes and provider/media-server tokens: owner-only, whatever UMASK is."""
        if str(self.path) == ":memory:":
            return
        base = Path(self.path)
        for path in (base, base.with_name(base.name + "-wal"), base.with_name(base.name + "-shm")):
            try:
                if path.exists():
                    os.chmod(path, 0o600)
            except OSError:  # pragma: no cover - e.g. some network filesystems
                log.warning("could not restrict permissions on %s", path)

    def user_version(self) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(text("PRAGMA user_version")).scalar() or 0)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Transactional scope: commits on success, rolls back on error."""
        session = self.sessionmaker()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        self.optimize()
        self.engine.dispose()


_V6_ITEM_COLUMNS = {
    "matched": "BOOLEAN NOT NULL DEFAULT 0",
    "candidate_count": "INTEGER NOT NULL DEFAULT 0",
    "chosen": "JSON",
    "match_title": "TEXT",
    "match_year": "INTEGER",
    "match_kind": "VARCHAR(16)",
    "match_season": "INTEGER",
    "match_episode": "INTEGER",
    "kind": "VARCHAR(16)",
    "search_text": "TEXT",
    "undone": f"BOOLEAN GENERATED ALWAYS AS ({UNDONE_SQL}) VIRTUAL",
}


def _columns(conn: Any, table: str) -> set[str]:
    # table_xinfo, not table_info: the latter leaves out generated columns (job_items.undone)
    return {row[1] for row in conn.execute(text(f"PRAGMA table_xinfo({table})"))}


def _loads(raw: Any) -> Any:
    if raw is None or raw == "":
        return None
    if isinstance(raw, str | bytes):
        try:
            return json.loads(raw)
        except ValueError:
            return None
    return raw
