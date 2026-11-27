"""Small helpers shared by the format-engine tests."""

from __future__ import annotations

from typing import Any

from schema_app.core.format import Bindings, compile_format
from schema_app.core.format.samples import SAMPLES, Sample
from schema_app.domain import MediaKind


def render(expr: str, *, kind: MediaKind = MediaKind.UNKNOWN, ext: str = "", strict: bool = False, **values: Any) -> str:
    """Render ``expr`` against plain values (no extension unless ``ext`` is given)."""
    bindings = Bindings.from_dict(values, kind=kind, extension_suffix=ext)
    return compile_format(expr).render(bindings, strict=strict)


def render_raw(expr: str, **values: Any) -> str:
    return compile_format(expr).render_raw(Bindings.from_dict(values))


def sample(note: str) -> Sample:
    for item in SAMPLES:
        if item.note.startswith(note):
            return item
    raise KeyError(note)


def render_sample(expr: str, note: str) -> str:
    return compile_format(expr).render(sample(note).bindings())
