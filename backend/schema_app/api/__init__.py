"""HTTP API routers (SPEC §5). Everything lives under ``/api``."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from schema_app.api import (
    auth,
    dashboard,
    demo,
    formats,
    fs,
    history,
    home,
    jobs,
    operations,
    settings,
    watch,
    webhooks,
    ws,
)
from schema_app.deps import require_api_key, require_user


def build_api_router() -> APIRouter:
    api = APIRouter(prefix="/api")
    # Public: health + auth (setup/login handle their own CSRF check).
    api.include_router(auth.router, tags=["auth"])
    # Cookie-authenticated (or SCHEMA_AUTH=disabled); mutations need X-Schema-Client or JSON.
    protected = [Depends(require_user)]
    api.include_router(dashboard.router, tags=["dashboard"], dependencies=protected)
    api.include_router(home.router, tags=["home"], dependencies=protected)
    api.include_router(history.router, tags=["history"], dependencies=protected)
    api.include_router(jobs.router, tags=["jobs"], dependencies=protected)
    api.include_router(formats.router, tags=["formats"], dependencies=protected)
    api.include_router(operations.router, tags=["operations"], dependencies=protected)
    api.include_router(watch.router, tags=["watch"], dependencies=protected)
    api.include_router(settings.router, tags=["settings"], dependencies=protected)
    api.include_router(fs.router, tags=["fs"], dependencies=protected)
    api.include_router(demo.router, tags=["demo"], dependencies=protected)
    # API key instead of the cookie.
    api.include_router(webhooks.router, tags=["webhooks"], dependencies=[Depends(require_api_key)])
    # Authenticates inside the handler (cookie on the upgrade request).
    api.include_router(ws.router)
    return api


__all__ = ["build_api_router"]
