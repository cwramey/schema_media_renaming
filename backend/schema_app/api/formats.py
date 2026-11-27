"""``/api/formats*``: saved formats, catalog and live validation (SPEC §8)."""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from schema_app.api_models import (
    FormatCatalog,
    FormatIn,
    FormatIssue,
    FormatOut,
    FormatSample,
    FormatValidateRequest,
    FormatValidateResponse,
)
from schema_app.core import parser as parser_mod
from schema_app.core.format import (
    PRESETS,
    FormatSyntaxError,
    build_binding,
    catalog,
    compile_format,
    render_samples,
    validate,
)
from schema_app.db import Database, utcnow
from schema_app.deps import SessionDep
from schema_app.domain import MediaKind
from schema_app.models import Format
from schema_app.services.jobs import ApiError, conflict, format_out, not_found
from schema_app.services.settings_store import KEY_FORMATS_SEEDED, SettingsStore

router = APIRouter()

MAX_USER_SAMPLES = 50
DEFAULT_FORMAT_NAMES = {MediaKind.MOVIE: "Movies", MediaKind.SERIES: "Series", MediaKind.ANIME: "Anime"}


def seed_formats(db: Database, store: SettingsStore) -> None:
    """Sync builtin presets (read-only rows) and create editable defaults on first run."""
    from schema_app.core.format import DEFAULT_FORMATS

    with db.session() as session:
        builtin = {row.preset_id: row for row in session.scalars(select(Format).where(Format.builtin.is_(True)))}
        wanted = {preset.id for preset in PRESETS}
        for preset in PRESETS:
            row = builtin.get(preset.id)
            if row is None:
                session.add(Format(name=preset.name, kind=preset.kind.value, expression=preset.expression,
                                   builtin=True, is_default=False, preset_id=preset.id))  # fmt: skip
            elif (row.name, row.kind, row.expression) != (preset.name, preset.kind.value, preset.expression):
                row.name, row.kind, row.expression = preset.name, preset.kind.value, preset.expression
        for preset_id, row in builtin.items():
            if preset_id not in wanted:
                session.delete(row)
    if store.get_value(KEY_FORMATS_SEEDED):
        return
    with db.session() as session:
        for kind, name in DEFAULT_FORMAT_NAMES.items():
            has_default = session.scalar(
                select(Format.id).where(Format.kind == kind.value, Format.is_default.is_(True))
            )
            if has_default is None:
                session.add(Format(name=name, kind=kind.value, expression=DEFAULT_FORMATS[kind], is_default=True))
    store.set_value(KEY_FORMATS_SEEDED, True)


def _check_expression(expression: str) -> None:
    try:
        compile_format(expression)
    except FormatSyntaxError as exc:
        raise ApiError(400, f"{exc.message} (at {exc.start})") from exc


def _clear_defaults(session: Session, kind: str, keep_id: int | None) -> None:
    query = update(Format).where(Format.kind == kind, Format.is_default.is_(True))
    if keep_id is not None:
        query = query.where(Format.id != keep_id)
    session.execute(query.values(is_default=False))


@router.get("/formats", response_model=list[FormatOut])
def formats_list(session: SessionDep) -> list[FormatOut]:
    rows = session.scalars(select(Format).order_by(Format.builtin, Format.kind, Format.name, Format.id)).all()
    return [format_out(row) for row in rows]


@router.post("/formats", response_model=FormatOut, status_code=status.HTTP_201_CREATED)
def formats_create(body: FormatIn, session: SessionDep) -> FormatOut:
    _check_expression(body.expression)
    row = Format(name=body.name.strip(), kind=body.kind.value, expression=body.expression, is_default=body.is_default)
    session.add(row)
    session.flush()
    if body.is_default:
        _clear_defaults(session, row.kind, row.id)
    session.flush()
    return format_out(row)


@router.get("/formats/catalog", response_model=FormatCatalog)
def formats_catalog() -> FormatCatalog:
    return catalog()


@router.post("/formats/validate", response_model=FormatValidateResponse)
def formats_validate(req: FormatValidateRequest) -> FormatValidateResponse:
    issues: list[FormatIssue] = validate(req.expression, req.kind)
    ok = not any(issue.severity == "error" for issue in issues)
    try:
        template = compile_format(req.expression)
    except FormatSyntaxError:
        return FormatValidateResponse(ok=False, issues=issues, samples=[])
    except Exception as exc:  # e.g. over-long format
        return FormatValidateResponse(ok=False, issues=[*issues, FormatIssue(message=str(exc), start=0, end=0)])
    if req.samples is None:
        samples = render_samples(template, req.kind)
    else:
        samples = []
        for name in req.samples[:MAX_USER_SAMPLES]:
            try:
                parsed = parser_mod.parse(name)
            except Exception as exc:
                samples.append(FormatSample(source=name, error=f"parse error: {exc}"))
                continue
            result = template.render_detailed(build_binding(parsed, None, None))
            errors = [issue.message for issue in result.issues if issue.severity == "error"]
            samples.append(FormatSample(source=name, output=result.path, error=errors[0] if errors else None))
    return FormatValidateResponse(ok=ok, issues=issues, samples=samples)


@router.put("/formats/{format_id}", response_model=FormatOut)
def formats_update(format_id: int, body: FormatIn, session: SessionDep) -> FormatOut:
    row = session.get(Format, format_id)
    if row is None:
        raise not_found("format")
    if row.builtin:
        raise conflict("builtin presets are read-only; save a copy instead")
    _check_expression(body.expression)
    row.name = body.name.strip()
    row.kind = body.kind.value
    row.expression = body.expression
    row.is_default = body.is_default
    row.updated_at = utcnow()
    session.flush()
    if body.is_default:
        _clear_defaults(session, row.kind, row.id)
    session.flush()
    return format_out(row)


@router.delete("/formats/{format_id}", status_code=status.HTTP_204_NO_CONTENT)
def formats_delete(format_id: int, session: SessionDep) -> Response:
    row = session.get(Format, format_id)
    if row is None:
        raise not_found("format")
    if row.builtin:
        raise conflict("builtin presets can't be deleted")
    session.delete(row)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
