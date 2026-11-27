"""Jobs, items, preview, parse and search routes."""

from __future__ import annotations

import asyncio
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Query, Response, status
from pydantic import BaseModel, Field

from schema_app.api_models import (
    BulkItemAction,
    ExecuteRequest,
    ItemRematch,
    ItemSearch,
    ItemUpdate,
    JobCreate,
    JobDetail,
    JobItemOut,
    JobItemPage,
    JobOut,
    JobStatus,
    PreviewItem,
    PreviewRequest,
)
from schema_app.core import parser as parser_mod
from schema_app.deps import CtxDep, SessionDep, pipeline_of
from schema_app.domain import Candidate, MediaKind, ParsedMedia
from schema_app.services.jobs import (
    ITEM_PAGE_DEFAULT,
    ITEM_PAGE_MAX,
    JOB_DETAIL_DEFAULT_LIMIT,
    RUNNABLE_ITEM_STATUSES,
    ApiError,
    full_item_out,
    get_item,
    get_job,
    item_page,
    job_detail,
    list_jobs,
    matching_item_ids,
    parse_statuses,
)

router = APIRouter()


class ParseRequest(BaseModel):
    names: list[str] = Field(min_length=1, max_length=500)


# --------------------------------------------------------------------------- jobs


@router.get("/jobs", response_model=list[JobOut])
def jobs_list(
    session: SessionDep,
    status_filter: Annotated[list[JobStatus] | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[JobOut]:
    return list_jobs(session, status_filter, limit=limit, offset=offset)


@router.post("/jobs", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
async def jobs_create(req: JobCreate, ctx: CtxDep) -> JobOut:
    return await pipeline_of(ctx).create_job(req)


StatusQuery = Annotated[
    list[str] | None, Query(alias="status", description="item statuses; repeat or comma-separate")
]
TextQuery = Annotated[str | None, Query(max_length=500, description="filename / path / parsed title contains")]
AfterQuery = Annotated[int | None, Query(ge=0, description="keyset cursor: only items with a larger id")]
ViewQuery = Annotated[Literal["full", "slim"] | None, Query(description="slim drops heavy candidate data")]


@router.get("/jobs/{job_id}", response_model=JobDetail)
def jobs_get(
    job_id: int,
    session: SessionDep,
    status_filter: StatusQuery = None,
    q: TextQuery = None,
    limit: Annotated[int, Query(ge=0, le=ITEM_PAGE_MAX)] = JOB_DETAIL_DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
    after: AfterQuery = None,
    view: ViewQuery = None,
) -> JobDetail:
    """The job plus one page of items (full items by default, for compatibility; ``limit=0`` for the
    header only). Large jobs should page through ``GET /jobs/{id}/items`` instead."""
    return job_detail(session, job_id, statuses=parse_statuses(status_filter), q=q, limit=limit, offset=offset,
                      after=after, slim=view == "slim")  # fmt: skip


@router.get("/jobs/{job_id}/items", response_model=JobItemPage)
def jobs_items(
    job_id: int,
    session: SessionDep,
    status_filter: StatusQuery = None,
    q: TextQuery = None,
    limit: Annotated[int, Query(ge=1, le=ITEM_PAGE_MAX)] = ITEM_PAGE_DEFAULT,
    offset: Annotated[int, Query(ge=0)] = 0,
    after: AfterQuery = None,
    view: ViewQuery = None,
) -> JobItemPage:
    """Page through a job's items (slim by default), with total and per-status counts."""
    return item_page(session, job_id, statuses=parse_statuses(status_filter), q=q, limit=limit, offset=offset,
                     after=after, slim=view != "full")  # fmt: skip


@router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def jobs_delete(job_id: int, ctx: CtxDep) -> Response:
    await pipeline_of(ctx).delete_job(job_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/jobs/{job_id}/execute", response_model=JobOut)
async def jobs_execute(job_id: int, ctx: CtxDep, req: Annotated[ExecuteRequest | None, Body()] = None) -> JobOut:
    req = req or ExecuteRequest()
    if req.q and not req.item_ids:
        # "Execute what the search box shows": resolve the matching runnable items server-side.
        def resolve() -> list[int]:
            with ctx.db.session() as session:
                get_job(session, job_id)
                return matching_item_ids(session, job_id, RUNNABLE_ITEM_STATUSES, req.q)

        ids = await asyncio.to_thread(resolve)
        if not ids:
            raise ApiError(409, "nothing to execute: no matched or approved items match the filter")
        req = ExecuteRequest(item_ids=ids, mode=req.mode)
    return await pipeline_of(ctx).execute_job(job_id, req)


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
async def jobs_cancel(job_id: int, ctx: CtxDep) -> JobOut:
    return await pipeline_of(ctx).cancel_job(job_id)


@router.post("/jobs/{job_id}/rematch", response_model=JobOut)
async def jobs_rematch(job_id: int, ctx: CtxDep) -> JobOut:
    return await pipeline_of(ctx).rematch_job(job_id)


# --------------------------------------------------------------------------- items


BULK_FULL_MAX = 500  # explicit ids up to this many get full items back; more get slim ones
BULK_RESPONSE_MAX = 1000  # the filter form answers with slim items up to this many, else [] (refetch)


@router.post("/items/bulk", response_model=list[JobItemOut])
async def items_bulk(action: BulkItemAction, ctx: CtxDep) -> list[JobItemOut]:
    """Bulk approve/skip/review/rematch by ``item_ids`` (full items back, as before; slim above
    BULK_FULL_MAX) or by ``job_id`` + ``statuses``/``q`` filter (slim items back; works on jobs of any
    size: above BULK_RESPONSE_MAX matching items the answer is an empty list and the client refetches
    what it shows, as the ``job.items_changed`` push says)."""
    pipeline = pipeline_of(ctx)
    if action.item_ids is not None:
        ids = action.item_ids
        slim, respond = len(ids) > BULK_FULL_MAX, True
    else:
        job_id = action.job_id
        assert job_id is not None  # guaranteed by the model validator

        def resolve() -> list[int]:
            with ctx.db.session() as session:
                get_job(session, job_id)
                return matching_item_ids(session, job_id, action.statuses, action.q)

        ids = await asyncio.to_thread(resolve)
        slim, respond = True, len(ids) <= BULK_RESPONSE_MAX
        if not ids:
            return []
    # One pipeline call: it works through the ids in chunks but reads each job's context only once.
    return await pipeline.bulk(BulkItemAction(item_ids=ids, action=action.action), slim=slim, respond=respond)


@router.get("/items/{item_id}", response_model=JobItemOut)
def items_get(item_id: int, session: SessionDep) -> JobItemOut:
    """The full item (every stored candidate with overview, aliases, episodes ...) for the inspector."""
    return full_item_out(session, get_item(session, item_id))


@router.patch("/items/{item_id}", response_model=JobItemOut)
async def items_update(item_id: int, update: ItemUpdate, ctx: CtxDep) -> JobItemOut:
    return await pipeline_of(ctx).update_item(item_id, update)


@router.post("/items/{item_id}/rematch", response_model=JobItemOut)
async def items_rematch(item_id: int, req: ItemRematch, ctx: CtxDep) -> JobItemOut:
    return await pipeline_of(ctx).rematch_item(item_id, req.hint)


@router.post("/items/{item_id}/search", response_model=list[Candidate])
async def items_search(item_id: int, req: ItemSearch, ctx: CtxDep) -> list[Candidate]:
    return await pipeline_of(ctx).search_item(item_id, req)


# --------------------------------------------------------------------------- stateless helpers


@router.post("/preview", response_model=list[PreviewItem])
async def preview(req: PreviewRequest, ctx: CtxDep) -> list[PreviewItem]:
    return await pipeline_of(ctx).preview(req)


@router.post("/parse", response_model=list[ParsedMedia])
async def parse_names(req: ParseRequest) -> list[ParsedMedia]:
    def run() -> list[ParsedMedia]:
        out: list[ParsedMedia] = []
        for name in req.names:
            try:
                out.append(parser_mod.parse(name))
            except Exception as exc:
                raise ApiError(500, f"parser failed on {name!r}: {exc}") from exc
        return out

    return await asyncio.to_thread(run)


@router.get("/search", response_model=list[Candidate])
async def search(
    ctx: CtxDep,
    q: Annotated[str, Query(min_length=1)],
    kind: MediaKind = MediaKind.MOVIE,
    year: int | None = None,
    provider: str | None = None,
) -> list[Candidate]:
    try:
        return await ctx.matcher.search(q, kind, year, provider)
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(502, f"search failed: {exc}") from exc
