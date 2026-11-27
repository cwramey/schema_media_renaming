"""``/api/settings``, ``/api/settings/api-key``, ``/api/providers*`` and ``/api/mediaservers/test``."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from schema_app.api_models import (
    SECRET_MASK,
    AppSettings,
    MediaServerTest,
    ProvidersIn,
    ProvidersOut,
    RestoreOut,
    SettingsBackup,
)
from schema_app.deps import AppContext, CtxDep
from schema_app.domain import MediaServerConfig, ProviderStatus
from schema_app.services import backup as backup_mod
from schema_app.services import mediaservers
from schema_app.services.jobs import ApiError
from schema_app.services.settings_store import SettingsError, same_server_url

router = APIRouter()


def _watcher_inputs(s: AppSettings) -> tuple[object, ...]:
    """Settings the folder watcher is built with: a change restarts it."""
    return (s.ignore_patterns, s.video_extensions, s.release_quiet_seconds, sorted(s.category_kinds))


@router.get("/settings", response_model=AppSettings)
def settings_get(ctx: CtxDep) -> AppSettings:
    return ctx.store.get_app_masked()


@router.put("/settings", response_model=AppSettings)
async def settings_put(body: AppSettings, ctx: CtxDep) -> AppSettings:
    before = ctx.store.get_app()
    try:
        saved = await asyncio.to_thread(ctx.store.put_app, body)
    except SettingsError as exc:
        raise ApiError(400, str(exc)) from exc
    after = ctx.store.get_app()
    watcher_inputs_changed = _watcher_inputs(before) != _watcher_inputs(after)
    if ctx.watch is not None and watcher_inputs_changed:
        await ctx.watch.restart()
    await ctx.bus.alog("info", "system", "Settings updated")
    return saved


@router.get("/settings/backup", response_model=SettingsBackup)
def settings_backup(ctx: CtxDep, include_secrets: bool = False) -> JSONResponse:
    """Download app + provider settings, watch folders and the user's formats as one JSON file."""
    backup = backup_mod.build_backup(ctx.db, ctx.store, include_secrets=include_secrets)
    stamp = (backup.created_at.strftime("%Y%m%d") if backup.created_at else "backup")
    if include_secrets:
        ctx.bus.log("warning", "system", "Settings backup downloaded with secrets included")
    return JSONResponse(
        backup.model_dump(mode="json"),
        headers={"Content-Disposition": f'attachment; filename="schema-settings-{stamp}.json"',
                 "Cache-Control": "no-store"},  # fmt: skip
    )


@router.post("/settings/restore", response_model=RestoreOut)
async def settings_restore(body: Annotated[dict[str, Any], Body()], ctx: CtxDep) -> RestoreOut:
    """Restore a backup. Masked secrets keep the stored value; watch folders are matched by path."""
    if body.get("schema_backup") != 1:
        raise ApiError(400, "not a Schema settings backup (schema_backup must be 1)")
    try:
        parsed = SettingsBackup.model_validate(body)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(part) for part in first.get("loc", ()))
        raise ApiError(400, f"invalid backup: {where}: {first.get('msg', 'invalid value')}") from exc
    before = ctx.store.get_app()
    plan = await asyncio.to_thread(backup_mod.plan_restore, ctx.store, parsed)
    counts = await asyncio.to_thread(backup_mod.write_restore, ctx.db, ctx.store, plan)
    if plan.providers is not None and ctx.matcher is not None:
        try:
            await ctx.matcher.reconfigure(ctx.store.get_providers())
        except Exception as exc:
            await ctx.bus.alog("error", "provider", f"Could not apply restored provider settings: {exc}")
    after = ctx.store.get_app()
    if ctx.watch is not None:
        if _watcher_inputs(before) != _watcher_inputs(after):
            await ctx.watch.restart()
        else:
            await ctx.watch.sync()
    for note in plan.notes:
        await ctx.bus.alog("warning", "system", f"Settings restore: {note}")
    await ctx.bus.alog("info", "system", "Settings restored from a backup")
    return RestoreOut(**counts)


@router.get("/settings/api-key")
def api_key_get(ctx: CtxDep) -> dict[str, str]:
    return {"api_key": ctx.store.get_api_key()}


@router.post("/settings/api-key")
def api_key_rotate(ctx: CtxDep) -> dict[str, str]:
    key = ctx.store.rotate_api_key()
    ctx.bus.log("info", "system", "Webhook API key rotated")
    return {"api_key": key}


# --------------------------------------------------------------------------- providers


def providers_out(ctx: AppContext) -> ProvidersOut:
    settings = ctx.store.get_providers()
    try:
        statuses = list(ctx.matcher.provider_statuses()) if ctx.matcher is not None else []
    except Exception:
        statuses = []
    return ProvidersOut(
        statuses=statuses,
        priority=settings.priority,
        disabled=settings.disabled,
        language=settings.language,
        credentials=ctx.store.masked_credentials(),
    )


@router.get("/providers", response_model=ProvidersOut)
def providers_get(ctx: CtxDep) -> ProvidersOut:
    return providers_out(ctx)


@router.put("/providers", response_model=ProvidersOut)
async def providers_put(body: ProvidersIn, ctx: CtxDep) -> ProvidersOut:
    try:
        settings = await asyncio.to_thread(ctx.store.put_providers, body)
    except SettingsError as exc:
        raise ApiError(400, str(exc)) from exc
    try:
        await ctx.matcher.reconfigure(settings)
    except Exception as exc:
        await ctx.bus.alog("error", "provider", f"Could not apply provider settings: {exc}")
        raise ApiError(500, f"provider settings saved but could not be applied: {exc}") from exc
    out = providers_out(ctx)
    for provider_status in out.statuses:
        ctx.bus.publish("provider.updated", provider_status)
    await ctx.bus.alog("info", "provider", "Provider settings updated")
    return out


@router.post("/providers/{name}/test", response_model=ProviderStatus)
async def providers_test(name: str, ctx: CtxDep) -> ProviderStatus:
    known = {status.name for status in providers_out(ctx).statuses}
    if known and name not in known:
        raise ApiError(404, f"unknown provider {name!r}")
    try:
        result = await ctx.matcher.test_provider(name)
    except ApiError:
        raise
    except KeyError as exc:
        raise ApiError(404, f"unknown provider {name!r}") from exc
    except Exception as exc:
        raise ApiError(502, f"provider test failed: {exc}") from exc
    ctx.bus.publish("provider.updated", result)
    level = "info" if result.healthy else "warning"
    await ctx.bus.alog(level, "provider", f"{result.label}: {'OK' if result.healthy else result.last_error or 'failed'}")
    return result


# --------------------------------------------------------------------------- media servers


@router.post("/mediaservers/test", response_model=MediaServerTest)
async def mediaservers_test(body: MediaServerConfig, ctx: CtxDep) -> MediaServerTest:
    config = body
    if body.token == SECRET_MASK:
        stored = ctx.store.media_server(body.id)
        if stored is None:
            raise ApiError(400, "token is required for a media server that hasn't been saved yet")
        if not same_server_url(stored.url, body.url):
            raise ApiError(400, "re-enter the token after changing the media server URL")
        config = body.model_copy(update={"token": stored.token})
    try:
        ok, message = await mediaservers.test_server(config)
    except Exception as exc:
        ok, message = False, str(exc) or exc.__class__.__name__
    return MediaServerTest(ok=ok, message=message)
