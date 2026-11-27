"""Demo mode (``SCHEMA_DEMO=1``): a generated library, preset roots and a seeded "Demo import" job."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from sqlalchemy import delete, select

from schema_app.api_models import JobCreate, JobSource
from schema_app.demo.builder import build, is_built, layout, read_manifest
from schema_app.models import Job, Operation
from schema_app.services.settings_store import KEY_DEMO_SEEDED

if TYPE_CHECKING:
    from schema_app.deps import AppContext

DEMO_JOB_LABEL = "Demo import"

__all__ = ["DEMO_JOB_LABEL", "bootstrap_demo", "build", "layout", "read_manifest", "reset_demo", "seed_demo_job"]


def _apply_roots(ctx: AppContext) -> None:
    """Point library/allowed roots at the demo folders, but only where the user hasn't set any."""
    demo = layout(ctx.config.demo_dir)
    settings = ctx.store.get_app()
    changes: dict[str, object] = {}
    if not settings.library_roots:
        changes["library_roots"] = {kind: str(path) for kind, path in demo.library_roots.items()}
    if not settings.allowed_roots:
        changes["allowed_roots"] = [str(demo.incoming), str(demo.library)]
    if changes:
        ctx.store.update_app(**changes)


async def bootstrap_demo(ctx: AppContext) -> None:
    """Build the demo files on first boot (idempotent) and set the roots."""
    root = ctx.config.demo_dir
    if not is_built(root):
        _, created = await asyncio.to_thread(build, root)
        await ctx.bus.alog("info", "system", f"Demo library created with {len(created)} files", path=str(root))
    await asyncio.to_thread(_apply_roots, ctx)


async def seed_demo_job(ctx: AppContext, *, force: bool = False) -> int | None:
    """Queue the "Demo import" job once (not auto-executed). Returns the job id if created."""
    if not force and ctx.store.get_value(KEY_DEMO_SEEDED):
        return None
    if ctx.pipeline is None:
        return None
    incoming = layout(ctx.config.demo_dir).incoming
    req = JobCreate(paths=[str(incoming)], auto_execute=False, label=DEMO_JOB_LABEL)
    job = await ctx.pipeline.create_job(req, source=JobSource.MANUAL, validate=False)
    ctx.store.set_value(KEY_DEMO_SEEDED, True)
    return job.id


async def reset_demo(ctx: AppContext) -> None:
    """``POST /api/demo/reset``: stop the workers, forget jobs and history, rebuild the files and
    re-seed the demo job."""
    pipeline = ctx.pipeline
    if pipeline is not None:
        await pipeline.stop()

    def wipe_db() -> list[int]:
        with ctx.db.session() as session:
            job_ids = list(session.scalars(select(Job.id)))
            session.execute(delete(Operation))
            session.execute(delete(Job))
            return job_ids

    deleted = await asyncio.to_thread(wipe_db)
    await asyncio.to_thread(build, ctx.config.demo_dir, wipe=True)
    await asyncio.to_thread(_apply_roots, ctx)
    if pipeline is not None:
        await pipeline.start()
    for job_id in deleted:
        ctx.bus.publish("job.deleted", {"id": job_id})
    await ctx.bus.alog("info", "system", "Demo library reset")
    await seed_demo_job(ctx, force=True)
