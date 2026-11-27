"""Schema's format language: a sandboxed, FileBot-compatible expression engine (SPEC §8).

Typical use::

    template = compile_format("{n} - {s00e00}{' - ' + t}")
    relative = template.render(build_binding(parsed, candidate, probe))   # "Show - S01E02 - Title.mkv"
"""

from schema_app.core.format.bindings import Bindings, build_binding
from schema_app.core.format.engine import RenderResult, Template, catalog, compile_format, validate
from schema_app.core.format.errors import (
    BindingMissing,
    FormatError,
    FormatEvalError,
    FormatSyntaxError,
)
from schema_app.core.format.presets import DEFAULT_FORMATS, PRESETS, get_preset
from schema_app.core.format.samples import Sample, render_samples, samples_for

__all__ = [
    "DEFAULT_FORMATS",
    "PRESETS",
    "BindingMissing",
    "Bindings",
    "FormatError",
    "FormatEvalError",
    "FormatSyntaxError",
    "RenderResult",
    "Sample",
    "Template",
    "build_binding",
    "catalog",
    "compile_format",
    "get_preset",
    "render_samples",
    "samples_for",
    "validate",
]
