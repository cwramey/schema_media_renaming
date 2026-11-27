"""``/api/watch-folders``: CRUD, live status and "scan now"."""

from __future__ import annotations

import asyncio
import os
from typing import Annotated

from fastapi import APIRouter, Body, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from schema_app.api_models import JobOut, StartNowIn, StartNowOut, WatchFolderIn, WatchFolderOut
from schema_app.deps import AppContext, CtxDep
from schema_app.models import Format, WatchFolder
from schema_app.services import paths as paths_mod
from schema_app.services.jobs import ApiError, conflict, not_found, watch_folder_out

router = APIRouter()


def _status(ctx: AppContext, row: WatchFolder) -> WatchFolderOut:
    return watch_folder_out(row, ctx.watch.status(row.id) if ctx.watch else None)


def _validate(ctx: AppContext, body: WatchFolderIn) -> str:
    roots = ctx.store.get_app().allowed_roots
    path = body.path.strip()
    if not path or not os.path.isabs(path):
        raise ApiError(400, "watch folder path must be absolute")
    path = os.path.normpath(path)
    # "/" or Schema's own data folder (or a folder holding it) is never watched or written to,
    # even when an old allowed root still covers it.
    if problem := paths_mod.root_problem(path):
        raise ApiError(403, f"this folder can't be watched: {problem}")
    if not paths_mod.is_within(path, roots):
        raise ApiError(403, "watch folder is outside the allowed roots")
    if body.dest_root:
        if not os.path.isabs(body.dest_root):
            raise ApiError(400, "dest_root must be an absolute path")
        if problem := paths_mod.root_problem(body.dest_root):
            raise ApiError(403, f"dest_root can't be used: {problem}")
        if not paths_mod.is_within(body.dest_root, roots):
            raise ApiError(403, "dest_root is outside the allowed roots")
    return path


def _apply(row: WatchFolder, body: WatchFolderIn, path: str) -> None:
    row.path = path
    row.enabled = body.enabled
    row.recursive = body.recursive
    row.kind = body.kind.value if body.kind else None
    row.auto_execute = body.auto_execute
    row.settle_seconds = body.settle_seconds
    row.format_id = body.format_id
    row.dest_root = body.dest_root
    row.mode = body.mode.value if body.mode else None


async def _sync_and_publish(ctx: AppContext, out: WatchFolderOut | None) -> WatchFolderOut | None:
    if out is not None:  # a new or moved watch folder: warn if moves from it will be full copies
        await asyncio.to_thread(ctx.store.warn_cross_mounts)
    if ctx.watch is not None:
        await ctx.watch.sync()
    if out is not None:
        # Status may have changed once the watcher picked the folder up.
        refreshed = out.model_copy(update={"status": ctx.watch.status(out.id) if ctx.watch else out.status})
        if not refreshed.enabled:
            refreshed.status = "stopped"
        ctx.bus.publish("watcher.updated", refreshed)
        return refreshed
    return None


@router.get("/watch-folders", response_model=list[WatchFolderOut])
def watch_list(ctx: CtxDep) -> list[WatchFolderOut]:
    with ctx.db.session() as session:
        return [_status(ctx, row) for row in session.scalars(select(WatchFolder).order_by(WatchFolder.id))]


@router.post("/watch-folders", response_model=WatchFolderOut, status_code=status.HTTP_201_CREATED)
async def watch_create(body: WatchFolderIn, ctx: CtxDep) -> WatchFolderOut:
    def run() -> WatchFolderOut:
        path = _validate(ctx, body)
        try:
            with ctx.db.session() as session:
                if body.format_id is not None and session.get(Format, body.format_id) is None:
                    raise ApiError(400, f"format {body.format_id} does not exist")
                row = WatchFolder(path=path)
                _apply(row, body, path)
                session.add(row)
                session.flush()
                return _status(ctx, row)
        except IntegrityError as exc:
            raise conflict("this folder is already watched") from exc

    out = await asyncio.to_thread(run)
    ctx.bus.log("info", "watcher", f"Watching {out.path}")
    return await _sync_and_publish(ctx, out) or out


@router.put("/watch-folders/{folder_id}", response_model=WatchFolderOut)
async def watch_update(folder_id: int, body: WatchFolderIn, ctx: CtxDep) -> WatchFolderOut:
    def run() -> WatchFolderOut:
        path = _validate(ctx, body)
        try:
            with ctx.db.session() as session:
                row = session.get(WatchFolder, folder_id)
                if row is None:
                    raise not_found("watch folder")
                if body.format_id is not None and session.get(Format, body.format_id) is None:
                    raise ApiError(400, f"format {body.format_id} does not exist")
                _apply(row, body, path)
                session.flush()
                return _status(ctx, row)
        except IntegrityError as exc:
            raise conflict("this folder is already watched") from exc

    out = await asyncio.to_thread(run)
    return await _sync_and_publish(ctx, out) or out


@router.delete("/watch-folders/{folder_id}", status_code=status.HTTP_204_NO_CONTENT)
async def watch_delete(folder_id: int, ctx: CtxDep) -> Response:
    def run() -> str:
        with ctx.db.session() as session:
            row = session.get(WatchFolder, folder_id)
            if row is None:
                raise not_found("watch folder")
            path = row.path
            session.delete(row)
            return path

    path = await asyncio.to_thread(run)
    await _sync_and_publish(ctx, None)
    ctx.bus.log("info", "watcher", f"Stopped watching {path}")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/watch-folders/{folder_id}/scan", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
async def watch_scan(folder_id: int, ctx: CtxDep) -> JobOut:
    if ctx.watch is None:  # pragma: no cover
        raise ApiError(503, "watcher not running")
    return await ctx.watch.scan(folder_id)


@router.post("/watch-folders/{folder_id}/start-now", response_model=StartNowOut)
async def watch_start_now(folder_id: int, ctx: CtxDep,
                          body: Annotated[StartNowIn | None, Body()] = None) -> StartNowOut:  # fmt: skip
    """Skip the rest of the settle wait for one file (``path``) or every settling file in the folder.
    Partial (still downloading) files are never started."""
    if ctx.watch is None:  # pragma: no cover
        raise ApiError(503, "watcher not running")
    path = (body.path or "").strip() if body else ""
    if path and not os.path.isabs(path):
        raise ApiError(400, "path must be absolute")
    started = await ctx.watch.start_now(folder_id, path or None)
    return StartNowOut(started=started)
