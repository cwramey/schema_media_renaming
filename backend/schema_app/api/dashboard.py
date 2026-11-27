"""``/api/dashboard`` and ``/api/events``."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from schema_app.api_models import DashboardOut, DashboardTotals, EventOut, ItemStatus, JobStatus
from schema_app.deps import AppContext, CtxDep, SessionDep
from schema_app.domain import ProviderStatus
from schema_app.models import Event, Job, JobItem, Operation, WatchFolder
from schema_app.services.events import event_out
from schema_app.services.jobs import item_counts, job_out, operation_out, watch_folder_out

router = APIRouter()

ACTIVE_FOR_DASHBOARD = (
    JobStatus.QUEUED,
    JobStatus.MATCHING,
    JobStatus.REVIEW,
    JobStatus.READY,
    JobStatus.EXECUTING,
)


def provider_statuses(ctx: AppContext) -> list[ProviderStatus]:
    try:
        return list(ctx.matcher.provider_statuses()) if ctx.matcher is not None else []
    except Exception:
        return []


def _local_midnight_utc() -> datetime:
    now = datetime.now().astimezone()
    return now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


@router.get("/dashboard", response_model=DashboardOut)
def dashboard(session: SessionDep, ctx: CtxDep) -> DashboardOut:
    queue = {status.value: 0 for status in JobStatus}
    for status, count in session.execute(select(Job.status, func.count(Job.id)).group_by(Job.status)):
        queue[status] = int(count)

    renamed = (Operation.status == "done") & (Operation.mode != "test")
    totals = DashboardTotals(
        files_renamed_today=int(
            session.scalar(select(func.count(Operation.id)).where(renamed, Operation.created_at >= _local_midnight_utc()))
            or 0
        ),
        files_renamed_total=int(session.scalar(select(func.count(Operation.id)).where(renamed)) or 0),
        bytes_moved_total=int(session.scalar(select(func.coalesce(func.sum(Operation.bytes), 0)).where(renamed)) or 0),
        items_pending_review=int(
            session.scalar(select(func.count(JobItem.id)).where(JobItem.status == ItemStatus.REVIEW.value)) or 0
        ),
    )

    jobs = session.scalars(
        select(Job).where(Job.status.in_([s.value for s in ACTIVE_FOR_DASHBOARD])).order_by(Job.id.desc()).limit(20)
    ).all()
    counts = item_counts(session, [job.id for job in jobs])
    operations = session.scalars(select(Operation).order_by(Operation.id.desc()).limit(10)).all()
    errors = session.scalars(select(Event).where(Event.level == "error").order_by(Event.id.desc()).limit(10)).all()
    folders = session.scalars(select(WatchFolder).order_by(WatchFolder.id)).all()
    watch = ctx.watch
    return DashboardOut(
        queue=queue,
        totals=totals,
        active_jobs=[job_out(job, counts[job.id]) for job in jobs],
        recent_operations=[operation_out(op) for op in operations],
        recent_errors=[event_out(row) for row in errors],
        watchers=[watch_folder_out(row, watch.status(row.id) if watch else None) for row in folders],
        providers=provider_statuses(ctx),
    )


@router.get("/events", response_model=list[EventOut])
def list_events(
    session: SessionDep,
    level: str | None = None,
    category: str | None = None,
    job_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[EventOut]:
    query = select(Event).order_by(Event.id.desc()).limit(limit).offset(offset)
    if level:
        query = query.where(Event.level.in_([v.strip() for v in level.split(",") if v.strip()]))
    if category:
        query = query.where(Event.category.in_([v.strip() for v in category.split(",") if v.strip()]))
    if job_id is not None:
        query = query.where(Event.job_id == job_id)
    return [event_out(row) for row in session.scalars(query)]
