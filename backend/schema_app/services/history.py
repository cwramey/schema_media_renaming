"""``GET /api/history``: one newest-first timeline of what happened to files.

Entries come from two tables: the operation journal (moved / undone / failed moves) and items that
ended skipped or in error. Paging is keyset-based so "Show older" stays stable while new entries
arrive: the cursor is ``"<ts in µs>.<source rank>.<id>"`` of the last entry shown, and every source
returns only rows that sort strictly after it in (ts, rank, id) descending order.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import PurePath
from typing import Any, Literal

from sqlalchemy import and_, func, or_, select, true
from sqlalchemy.orm import Session

from schema_app.api_models import HistoryEntry, HistoryPage, ItemStatus, JobStatus
from schema_app.db import UTCDateTime
from schema_app.models import Job, JobItem, Operation
from schema_app.services.home import ITEM_COLUMNS, ItemFacts, friendly_error, item_facts, newest_rows
from schema_app.services.jobs import ApiError, search_clause

HistoryFilter = Literal["all", "moved", "skipped", "problems"]
HISTORY_MAX = 200
RANK_OP, RANK_ITEM = 0, 1
ACTIVE_JOBS = (JobStatus.QUEUED.value, JobStatus.MATCHING.value, JobStatus.EXECUTING.value)


def _cursor(ts: datetime, rank: int, ident: int) -> str:
    return f"{int(ts.timestamp() * 1_000_000)}.{rank}.{ident}"


def _parse_cursor(raw: str) -> tuple[datetime, int, int]:
    try:
        micros, rank, ident = (int(part) for part in raw.split("."))
        return datetime.fromtimestamp(micros / 1_000_000, tz=UTC), rank, ident
    except (ValueError, OverflowError, OSError) as exc:
        raise ApiError(400, "invalid 'before' cursor") from exc


def _older(ts_col: Any, id_col: Any, rank: int, cursor: tuple[datetime, int, int] | None) -> Any:
    """Rows of a source with ``rank`` that sort after ``cursor`` in (ts, rank, id) descending order.
    Always bounded by ``ts <= cursor``, so the newest-first index scan starts at the cursor."""
    if cursor is None:
        return true()
    cts, crank, cid = cursor
    if rank < crank:
        return ts_col <= cts
    if rank > crank:
        return ts_col < cts
    return and_(ts_col <= cts, or_(ts_col < cts, id_col < cid))


def _kind_expr() -> Any:
    """The card kind of an item: its match's kind, else the parsed one (a column of ``job_items``)."""
    return JobItem.kind


def history_page(session: Session, *, filter: HistoryFilter = "all", q: str | None = None, kind: str | None = None,
                 before: str | None = None, limit: int = 50) -> HistoryPage:  # fmt: skip
    limit = max(1, min(HISTORY_MAX, limit))
    cursor = _parse_cursor(before) if before else None
    text = (q or "").strip()
    rows: list[tuple[datetime, int, int, HistoryEntry]] = []
    active = set(session.scalars(select(Job.id).where(Job.status.in_(ACTIVE_JOBS))))

    # ---- operations (moved / undone / failed)
    op_statuses: list[str] = []
    if filter in {"all", "moved"}:
        op_statuses += ["done", "undo_failed"]
    if filter == "all":
        op_statuses.append("undone")
    if filter in {"all", "problems"}:
        op_statuses.append("failed")
    if op_statuses:
        # Exactly the expression of the ix_operations_history index: read newest-first, no sort.
        ts_col = func.coalesce(Operation.undone_at, Operation.created_at, type_=UTCDateTime)
        clauses: list[Any] = [
            Operation.mode != "test",
            # "|| ''" keeps the planner off the status index: the journal is read newest-first from
            # ix_operations_history and the scan stops after a page, instead of sorting every entry.
            (Operation.status + "").in_(op_statuses),
            # A failed move whose item is still in error shows once, as the item.
            or_(Operation.status != "failed", JobItem.id.is_(None), JobItem.status != ItemStatus.ERROR.value),
            _older(ts_col, Operation.id, RANK_OP, cursor),
        ]
        if text:
            clauses.append(or_(Operation.source.icontains(text, autoescape=True),
                               Operation.destination.icontains(text, autoescape=True)))  # fmt: skip
        if kind:
            clauses.append(_kind_expr() == kind)
        ops = session.execute(
            select(Operation, ts_col).outerjoin(JobItem, JobItem.id == Operation.item_id).where(*clauses)
            .order_by(ts_col.desc(), Operation.id.desc()).limit(limit + 1)
        ).all()  # fmt: skip
        item_ids = [op.item_id for op, _ in ops if op.item_id is not None]
        facts: dict[int, ItemFacts] = {}
        if item_ids:
            facts = {f.id: f for f in item_facts(session, session.execute(
                select(*ITEM_COLUMNS).where(JobItem.id.in_(item_ids))).all())}  # fmt: skip
        for op, ts in ops:
            rows.append((ts, RANK_OP, op.id, _op_entry(op, ts, facts.get(op.item_id or -1), active)))

    # ---- items (skipped / error)
    item_statuses: list[str] = []
    if filter in {"all", "skipped"}:
        item_statuses.append(ItemStatus.SKIPPED.value)
    if filter in {"all", "problems"}:
        item_statuses.append(ItemStatus.ERROR.value)
    if item_statuses:
        clauses = [_older(JobItem.updated_at, JobItem.id, RANK_ITEM, cursor)]
        search = search_clause(text)
        if search is not None:
            clauses.append(search)
        if kind:
            clauses.append(_kind_expr() == kind)
        items = newest_rows(session, ITEM_COLUMNS, clauses, item_statuses, limit + 1)
        for fact in item_facts(session, items):
            rows.append((fact.updated_at, RANK_ITEM, fact.id, _item_entry(fact, active)))

    if not op_statuses and not item_statuses:  # pragma: no cover - the Literal covers every filter
        return HistoryPage(entries=[], next_before=None)
    rows.sort(key=lambda r: (r[0], r[1], r[2]), reverse=True)
    page = rows[:limit]
    more = len(rows) > limit
    next_before = _cursor(page[-1][0], page[-1][1], page[-1][2]) if more and page else None
    return HistoryPage(entries=[entry for *_, entry in page], next_before=next_before)


def _op_entry(op: Operation, ts: datetime, fact: ItemFacts | None, active: set[int]) -> HistoryEntry:
    title = (fact.full_title if fact else None) or PurePath(op.destination or op.source).stem
    kind = fact.kind if fact else "unknown"
    if op.status == "failed":
        kind_type, detail = "problem", friendly_error(op.error)
    elif op.status == "undone":
        kind_type, detail = "undone", f"Moved back to {os.path.dirname(op.source) or op.source}"
    else:
        kind_type = "moved"
        detail = f"Matched {fact.candidate_title}" if fact and fact.candidate_title else None
        if op.status == "undo_failed":
            detail = f"Undo failed: {op.error}" if op.error else "Undo failed"
    can_undo = kind_type == "moved" and bool(op.result) and op.job_id not in active
    return HistoryEntry(
        key=f"op:{op.id}", type=kind_type, ts=ts, title=title, kind=kind,  # type: ignore[arg-type]
        source=op.source, destination=op.destination or None, detail=detail, bytes=op.bytes or 0,
        mode=op.mode, operation_id=op.id, item_id=op.item_id, job_id=op.job_id, can_undo=can_undo,
        can_fix=kind_type == "problem" and op.item_id is not None and op.job_id is not None,
    )  # fmt: skip


def _item_entry(fact: ItemFacts, active: set[int]) -> HistoryEntry:
    if fact.status == ItemStatus.ERROR.value:
        entry_type, detail = "problem", friendly_error(fact.error)
    else:
        entry_type = "skipped"
        if "sample" in fact.warnings:
            detail = "Sample clip"
        elif "extra" in fact.warnings:
            detail = f"Bonus clip of {fact.candidate_title}" if fact.candidate_title else "Bonus clip"
        else:
            detail = "Skipped"
    return HistoryEntry(
        key=f"item:{fact.id}", type=entry_type, ts=fact.updated_at, title=fact.title, kind=fact.kind,  # type: ignore[arg-type]
        source=fact.source_path, destination=fact.proposed_path, detail=detail, bytes=0, mode=None,
        operation_id=None, item_id=fact.id, job_id=fact.job_id, can_undo=False,
        can_fix=entry_type == "problem" and fact.job_id not in active,
    )  # fmt: skip


__all__ = ["HistoryFilter", "history_page"]
