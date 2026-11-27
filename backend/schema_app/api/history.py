"""``/api/history``: the file timeline (moved / skipped / problems / undone), newest first."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query

from schema_app.api_models import HistoryPage
from schema_app.deps import SessionDep
from schema_app.services.history import history_page

router = APIRouter()


@router.get("/history", response_model=HistoryPage)
def history(
    session: SessionDep,
    filter: Literal["all", "moved", "skipped", "problems"] = "all",
    q: Annotated[str | None, Query(max_length=500)] = None,
    kind: Literal["movie", "series", "anime"] | None = None,
    before: Annotated[str | None, Query(max_length=64, description="next_before of the previous page")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> HistoryPage:
    return history_page(session, filter=filter, q=q, kind=kind, before=before, limit=limit)
