"""``/api/operations``: the file-operation journal and undo."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select

from schema_app.api_models import OperationOut
from schema_app.deps import CtxDep, SessionDep, pipeline_of
from schema_app.models import Operation
from schema_app.services.jobs import operation_out

router = APIRouter()


@router.get("/operations", response_model=list[OperationOut])
def operations_list(
    session: SessionDep,
    job_id: int | None = None,
    status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[OperationOut]:
    query = select(Operation).order_by(Operation.id.desc()).limit(limit).offset(offset)
    if job_id is not None:
        query = query.where(Operation.job_id == job_id)
    if status:
        query = query.where(Operation.status.in_([s.strip() for s in status.split(",") if s.strip()]))
    return [operation_out(op) for op in session.scalars(query)]


@router.post("/operations/{op_id}/undo", response_model=OperationOut)
async def operations_undo(op_id: int, ctx: CtxDep) -> OperationOut:
    return await pipeline_of(ctx).undo_operation(op_id)
