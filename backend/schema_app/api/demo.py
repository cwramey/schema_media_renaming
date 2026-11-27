"""``/api/demo/reset`` (demo mode only)."""

from __future__ import annotations

from fastapi import APIRouter

from schema_app.demo import reset_demo
from schema_app.deps import CtxDep
from schema_app.services.jobs import ApiError

router = APIRouter()


@router.post("/demo/reset")
async def demo_reset(ctx: CtxDep) -> dict[str, bool]:
    if not ctx.config.demo:
        raise ApiError(404, "demo mode is off (set SCHEMA_DEMO=1)")
    await reset_demo(ctx)
    return {"ok": True}
