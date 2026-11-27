"""``/api/home`` (the flow board), ``/api/home/actions`` (its card buttons), ``/home/refresh`` and ``/home/dismiss-all``."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from fastapi import APIRouter
from sqlalchemy import select

from schema_app.api.dashboard import provider_statuses
from schema_app.api_models import (
    ExecuteRequest,
    HomeActionIn,
    HomeClearedOut,
    HomeOut,
    HomeRefreshOut,
    ItemStatus,
    ItemUpdate,
    OkOut,
)
from schema_app.deps import CtxDep, SessionDep, pipeline_of
from schema_app.domain import MatchHint
from schema_app.models import Event, JobItem
from schema_app.services.home import (
    build_home,
    clear_vanished,
    dismiss,
    dismiss_all,
    dismiss_many,
    open_item_clauses,
    source_missing_clause,
)
from schema_app.services.jobs import RUNNABLE_ITEM_STATUSES, ApiError, conflict

router = APIRouter()

_RETRYABLE = {ItemStatus.ERROR.value, ItemStatus.REVIEW.value}
_SKIPPABLE = {ItemStatus.REVIEW.value, ItemStatus.ERROR.value, ItemStatus.MATCHED.value, ItemStatus.APPROVED.value}
_APPROVABLE = {ItemStatus.REVIEW.value, ItemStatus.MATCHED.value, ItemStatus.APPROVED.value}
# Review warnings that mean "the match itself is the problem": retrying re-runs matching.
_REMATCH_WARNINGS = ("no_match", "match_error", "matcher_unavailable", "provider_error", "parse_error")

# Every open tab polls the board, and refetches it on pushes: one build serves them all for a moment.
# An entry is reused only while nothing was published since (bus version) and the settings are the
# same; the TTL bounds what pushes don't cover (files settling in watch folders).
HOME_TTL = 1.0
_home_cache: dict[int, tuple[float, tuple[Any, ...], HomeOut]] = {}
_home_lock = threading.Lock()


def _cached_home(ctx: Any, key: tuple[Any, ...]) -> HomeOut | None:
    entry = _home_cache.get(id(ctx))
    if entry is not None and entry[1] == key and time.monotonic() - entry[0] < HOME_TTL:
        return entry[2]
    return None


def forget_home(ctx: Any) -> None:
    """Drop the cached board (after a Home action: the next poll must show its effect)."""
    _home_cache.pop(id(ctx), None)


@router.get("/home", response_model=HomeOut)
def home(session: SessionDep, ctx: CtxDep) -> HomeOut:
    settings = ctx.store.get_app()
    key = (getattr(ctx.bus, "version", None), settings.model_dump_json())
    hit = _cached_home(ctx, key)
    if hit is not None:
        return hit
    with _home_lock:  # concurrent polls wait for one build instead of each building the board
        hit = _cached_home(ctx, key)
        if hit is not None:
            return hit
        out = build_home(session, settings, watch=ctx.watch, providers=provider_statuses(ctx))
        if len(_home_cache) > 8:  # one entry per app (tests start many)
            _home_cache.clear()
        _home_cache[id(ctx)] = (time.monotonic(), key, out)
        return out


@router.post("/home/refresh", response_model=HomeRefreshOut)
async def home_refresh(ctx: CtxDep) -> HomeRefreshOut:
    """Re-check the disk: cards for files that are no longer there (cleaned out by hand) clear, and
    finished files sitting in the watch (download) folders start now instead of after the settle wait."""

    def run() -> int:
        with ctx.db.session() as session:
            return clear_vanished(session)

    cleared = await asyncio.to_thread(run)
    started = await ctx.watch.scan_now() if ctx.watch is not None else 0
    forget_home(ctx)
    return HomeRefreshOut(cleared=cleared, started=started)


@router.post("/home/dismiss-all", response_model=HomeClearedOut)
async def home_dismiss_all(ctx: CtxDep) -> HomeClearedOut:
    """Clear the whole Needs review list (the files themselves are left alone)."""

    def run() -> int:
        with ctx.db.session() as session:
            return dismiss_all(session)

    cleared = await asyncio.to_thread(run)
    forget_home(ctx)
    return HomeClearedOut(cleared=cleared)


def _parse_key(key: str) -> tuple[str, int]:
    kind, _, raw = key.partition(":")
    if kind not in {"item", "event", "group"} or not raw.isdigit():
        raise ApiError(404, f"unknown card {key!r}")
    return kind, int(raw)


@router.post("/home/actions", response_model=OkOut)
async def home_action(body: HomeActionIn, ctx: CtxDep) -> OkOut:
    try:
        return await _home_action(body, ctx)
    finally:
        forget_home(ctx)


async def _home_action(body: HomeActionIn, ctx: Any) -> OkOut:
    kind, ref = _parse_key(body.key)
    if kind == "group":
        if body.action != "dismiss":
            raise conflict("a grouped card can only be dismissed")

        def dismiss_group() -> None:
            with ctx.db.session() as session:
                ids = session.scalars(
                    select(JobItem.id).where(JobItem.job_id == ref, *open_item_clauses(), source_missing_clause())
                ).all()  # fmt: skip
                if not ids:
                    raise ApiError(404, "nothing to dismiss for this job")
                dismiss_many(session, "item", ids)  # one insert + one prune, not one per file

        await asyncio.to_thread(dismiss_group)
        return OkOut()
    if kind == "event":
        if body.action != "dismiss":
            raise conflict("an event card can only be dismissed")

        def dismiss_event() -> None:
            with ctx.db.session() as session:
                if session.get(Event, ref) is None:
                    raise ApiError(404, "event not found")
                dismiss(session, "event", ref)

        await asyncio.to_thread(dismiss_event)
        return OkOut()

    def load() -> tuple[str, int, bool, list[str]]:
        with ctx.db.session() as session:
            item = session.get(JobItem, ref)
            if item is None:
                raise ApiError(404, "item not found")
            runnable = bool(item.proposed_path and (item.selected_candidate_id or item.path_overridden))
            return item.status, item.job_id, runnable, list(item.warnings or [])

    status, job_id, runnable, warnings = await asyncio.to_thread(load)
    pipeline = pipeline_of(ctx)
    if body.action == "dismiss":

        def dismiss_item() -> None:
            with ctx.db.session() as session:
                dismiss(session, "item", ref)

        await asyncio.to_thread(dismiss_item)
    elif body.action == "skip":
        if status not in _SKIPPABLE:
            raise conflict(f"item is {status}; only items waiting for you can be skipped")
        await pipeline.update_item(ref, ItemUpdate(status="skipped"))
    elif body.action == "approve":
        if status not in _APPROVABLE:
            raise conflict(f"item is {status}; only matched items or items in review can be approved")
        if status != ItemStatus.APPROVED.value:
            await pipeline.update_item(ref, ItemUpdate(status="approved"))
        await pipeline.execute_job(job_id, ExecuteRequest(item_ids=[ref]))
    elif body.action == "retry":
        if status not in _RETRYABLE:
            raise conflict(f"item is {status}; only failed or waiting items can be retried")
        rematch = not runnable or any(w.startswith(_REMATCH_WARNINGS) for w in warnings)
        if rematch:
            out = await pipeline.rematch_item(ref, MatchHint())
            if out.status in RUNNABLE_ITEM_STATUSES:
                await pipeline.execute_job(job_id, ExecuteRequest(item_ids=[ref]))
        else:
            await pipeline.update_item(ref, ItemUpdate(status="approved"))
            await pipeline.execute_job(job_id, ExecuteRequest(item_ids=[ref]))
    return OkOut()
