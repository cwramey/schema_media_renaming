"""``/api/webhooks/{source}``: download-client notifications (SPEC §7.7). API key, no cookie.

A webhook job executes on its own, so its paths are held to the download area (watch folders and
path-mapping targets) by ``downloaders.confine_paths``; "inside the allowed roots" alone would let
one call rename the whole library.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request, status
from sqlalchemy import select

from schema_app.api_models import JobCreate, JobSource, WebhookAccepted
from schema_app.deps import AppContext, CtxDep, pipeline_of
from schema_app.models import WatchFolder
from schema_app.services import downloaders
from schema_app.services.jobs import ApiError

router = APIRouter()

SOURCES = ("sabnzbd", "qbittorrent", "generic")


async def _payload(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    payload: dict[str, Any] = {}
    if content_type in {"application/x-www-form-urlencoded", "multipart/form-data"}:
        form = await request.form()
        for key in form:
            values = form.getlist(key)
            payload[key] = values if len(values) > 1 else values[0]
    else:
        body = await request.body()
        if body.strip():
            try:
                decoded = json.loads(body)
            except ValueError as exc:
                raise ApiError(400, "body must be JSON or form data") from exc
            if not isinstance(decoded, dict):
                raise ApiError(400, "JSON body must be an object")
            payload = decoded
    for key, value in request.query_params.items():
        if key.lower() != "apikey":
            payload.setdefault(key, value)
    return payload


@router.post("/webhooks/{source}", response_model=WebhookAccepted, status_code=status.HTTP_202_ACCEPTED)
async def webhook(source: str, request: Request, ctx: CtxDep) -> WebhookAccepted:
    source = source.lower()
    if source not in SOURCES:
        raise ApiError(404, f"unknown webhook source {source!r} (use {', '.join(SOURCES)})")
    payload = await _payload(request)
    settings = ctx.store.get_app()
    try:
        ingest = downloaders.parse_webhook(
            source, payload, category_kinds=settings.category_kinds, path_mappings=settings.path_mappings
        )
    except downloaders.WebhookPayloadError as exc:
        await ctx.bus.alog("warning", "webhook", f"{source}: ignored ({exc})")
        raise ApiError(400, str(exc)) from exc
    try:
        paths = downloaders.confine_paths(
            ingest.paths,
            download_areas=[*_watch_folders(ctx), *(m.target for m in settings.path_mappings)],
            allowed_roots=settings.allowed_roots,
            library_roots=settings.library_roots.values(),
        )
    except downloaders.WebhookPathRefused as exc:
        await ctx.bus.alog("warning", "webhook", f"{source}: refused: {exc}")
        raise ApiError(403, str(exc)) from exc
    req = JobCreate(paths=paths, kind=ingest.kind_hint, auto_execute=True, label=ingest.label)
    job = await pipeline_of(ctx).create_job(req, source=JobSource.WEBHOOK)
    await ctx.bus.alog(
        "info", "webhook", f"{source}: queued job #{job.id} for {len(paths)} path(s)", job_id=job.id,
        category_name=ingest.category,
    )
    return WebhookAccepted(job_id=job.id, paths=paths)


def _watch_folders(ctx: AppContext) -> list[str]:
    with ctx.db.session() as session:
        return list(session.scalars(select(WatchFolder.path)))
