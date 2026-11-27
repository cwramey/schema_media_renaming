"""Public format-engine API: compile, render, validate, catalog (SPEC §8).

Rendering follows FileBot's per-block isolation: every top-level ``{…}`` block is evaluated on its
own; a block whose binding is undefined renders ``""`` silently, a block that fails for another
reason also renders ``""`` but the error is reported (``RenderResult.issues``, severity
"warning"). Only an empty (or absolute) final path is an error.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from schema_app.api_models import FilterSpec, FormatCatalog, FormatIssue
from schema_app.core.format.bindings import (
    BINDINGS,
    OBJECT_PROPERTIES,
    ROOT_RELATIVE_BINDINGS,
    UNSUPPORTED_FILEBOT_BINDINGS,
    Bindings,
    binding_specs,
)
from schema_app.core.format.errors import BindingMissing, FormatEvalError, FormatSyntaxError
from schema_app.core.format.evaluator import Evaluator
from schema_app.core.format.lexer import BlockSegment, scan_template
from schema_app.core.format.methods import METHODS, UNSUPPORTED_FILEBOT_METHODS
from schema_app.core.format.parser import (
    Binary,
    Call,
    Closures,
    Elvis,
    GStr,
    Ident,
    Index,
    ListLit,
    Member,
    Node,
    Ternary,
    Unary,
    iter_nodes,
    parse_tokens,
)
from schema_app.core.format.presets import PRESETS
from schema_app.core.format.values import to_text
from schema_app.domain import MediaKind
from schema_app.services import paths as _paths

MAX_FORMAT_LENGTH = 10_000
_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


@dataclass(slots=True)
class _Block:
    node: Node
    start: int
    end: int
    source: str


@dataclass
class RenderResult:
    path: str | None  # sanitized relative path incl. extension suffix; None when rendering failed
    raw: str  # unsanitized concatenation (no extension)
    issues: list[FormatIssue] = field(default_factory=list)


def _as_bindings(bindings: Bindings | Mapping[str, Any]) -> Bindings:
    if isinstance(bindings, Bindings):
        return bindings
    return Bindings.from_dict(bindings)


class Template:
    """A compiled format. Immutable and thread-safe; cached by :func:`compile_format`."""

    __slots__ = ("expression", "segments", "identifiers")

    def __init__(self, expression: str, segments: list[str | _Block], identifiers: frozenset[str]) -> None:
        self.expression = expression
        self.segments = segments
        self.identifiers = identifiers

    def __repr__(self) -> str:
        return f"Template({self.expression!r})"

    @property
    def blocks(self) -> list[_Block]:
        return [segment for segment in self.segments if isinstance(segment, _Block)]

    # ------------------------------------------------------------------ evaluation
    def _evaluate(self, bindings: Bindings, *, strict: bool) -> tuple[str, list[FormatIssue]]:
        evaluator = Evaluator(bindings)
        out: list[str] = []
        issues: list[FormatIssue] = []
        for segment in self.segments:
            if isinstance(segment, str):
                out.append(segment)
                continue
            try:
                try:
                    value = evaluator.eval(segment.node)
                except RecursionError:
                    raise FormatEvalError(
                        "Expression is too complex (nested too deeply)", segment.start, segment.end
                    ) from None
            except BindingMissing:
                continue
            except FormatEvalError as exc:
                if strict:
                    raise
                issues.append(FormatIssue(message=exc.message, start=exc.start, end=exc.end, severity="warning"))
                continue
            out.append(to_text(value))
        return "".join(out), issues

    def _finalize(self, raw: str, suffix: str) -> str:
        text = raw.replace("\\", "/")
        stripped = text.strip()
        if stripped.startswith("/") or _DRIVE_RE.match(stripped):
            raise FormatEvalError(
                "Formats are relative to the library root; remove the leading '/' or drive letter",
                0,
                len(self.expression),
            )
        components: list[str] = []
        for part in text.split("/"):
            if not part.strip():
                continue
            cleaned = _paths.sanitize_component(part)
            if cleaned:
                components.append(cleaned)
        if not components:
            raise FormatEvalError("Format produced an empty path", 0, len(self.expression))
        try:
            return _paths.sanitize_relative_path("/".join(components) + suffix)
        except ValueError as exc:
            raise FormatEvalError(f"Invalid destination: {exc}", 0, len(self.expression)) from None

    def render_detailed(self, bindings: Bindings | Mapping[str, Any]) -> RenderResult:
        """Render without raising for per-block problems; see :class:`RenderResult`."""
        resolved = _as_bindings(bindings)
        raw, issues = self._evaluate(resolved, strict=False)
        try:
            path: str | None = self._finalize(raw, resolved.extension_suffix)
        except FormatEvalError as exc:
            issues.append(FormatIssue(message=exc.message, start=exc.start, end=exc.end, severity="error"))
            path = None
        return RenderResult(path=path, raw=raw, issues=issues)

    def render(self, bindings: Bindings | Mapping[str, Any], *, strict: bool = False) -> str:
        """Sanitized relative path with the extension suffix appended.

        Blocks with undefined bindings render empty. Other block errors render empty too, unless
        ``strict`` is set, in which case the first one is raised. An empty or absolute result
        always raises :class:`FormatEvalError`.
        """
        resolved = _as_bindings(bindings)
        if strict:
            raw, _ = self._evaluate(resolved, strict=True)
            return self._finalize(raw, resolved.extension_suffix)
        result = self.render_detailed(resolved)
        if result.path is None:
            error = next(issue for issue in result.issues if issue.severity == "error")
            raise FormatEvalError(error.message, error.start, error.end)
        return result.path

    def render_raw(self, bindings: Bindings | Mapping[str, Any]) -> str:
        """Unsanitized concatenation without the extension (used by preset bindings)."""
        raw, _ = self._evaluate(_as_bindings(bindings), strict=False)
        return raw


@lru_cache(maxsize=512)
def compile_format(expr: str) -> Template:
    """Parse a format. Raises :class:`FormatSyntaxError` with character offsets."""
    if not isinstance(expr, str):
        raise TypeError("format expression must be a string")
    if not expr.strip():
        raise FormatSyntaxError("Format is empty", 0, len(expr))
    if len(expr) > MAX_FORMAT_LENGTH:
        raise FormatSyntaxError(f"Format is too long (max {MAX_FORMAT_LENGTH} characters)", 0, len(expr))
    segments: list[str | _Block] = []
    identifiers: set[str] = set()
    for segment in scan_template(expr):
        if isinstance(segment, BlockSegment):
            try:
                node = parse_tokens(segment.tokens)
            except RecursionError:
                raise FormatSyntaxError("Expression is nested too deeply", segment.start, segment.end) from None
            identifiers.update(n.name for n in iter_nodes(node) if isinstance(n, Ident))
            segments.append(_Block(node, segment.start, segment.end, segment.source))
        else:
            segments.append(segment.text)
    return Template(expr, segments, frozenset(identifiers))


# --------------------------------------------------------------------------- validation

_KIND_WORD = {MediaKind.MOVIE: "movie", MediaKind.SERIES: "series", MediaKind.ANIME: "anime"}


def _suggest(name: str, choices: list[str]) -> str:
    match = difflib.get_close_matches(name, choices, n=1, cutoff=0.75)
    return f"; did you mean '{match[0]}'?" if match else ""


def _walk(node: Node, guarded: bool = False):
    """Yield (node, guarded) where guarded means "inside an intentional fallback"
    (any{}/allOf{} closures, the left side of ?:, a safe-navigation receiver)."""
    yield node, guarded
    if isinstance(node, Closures):
        for body in node.bodies:
            yield from _walk(body, True)
    elif isinstance(node, Elvis):
        yield from _walk(node.left, True)
        yield from _walk(node.right, guarded)
    elif isinstance(node, Member | Call):
        yield from _walk(node.obj, guarded or node.safe)
        if isinstance(node, Call):
            for arg in node.args:
                yield from _walk(arg, guarded)
    elif isinstance(node, Index):
        yield from _walk(node.obj, guarded)
        yield from _walk(node.index, guarded)
    elif isinstance(node, ListLit):
        for item in node.items:
            yield from _walk(item, True)  # missing list entries are dropped by design
    elif isinstance(node, Unary):
        yield from _walk(node.operand, guarded)
    elif isinstance(node, Binary):
        yield from _walk(node.left, guarded)
        yield from _walk(node.right, guarded)
    elif isinstance(node, Ternary):
        yield from _walk(node.cond, guarded)
        yield from _walk(node.then, guarded)
        yield from _walk(node.other, guarded)
    elif isinstance(node, GStr):
        for part in node.parts:
            if isinstance(part, Node):
                yield from _walk(part, guarded)


def _static_issues(template: Template, kind: MediaKind) -> list[FormatIssue]:
    issues: list[FormatIssue] = []
    method_names = sorted(METHODS)
    binding_names = sorted(BINDINGS)
    for block in template.blocks:
        for node, guarded in _walk(block.node):
            if isinstance(node, Call):
                spec = METHODS.get(node.name)
                span = (node.name_start, node.end)
                if spec is not None:
                    if not spec.accepts(len(node.args)):
                        issues.append(FormatIssue(
                            message=f"{node.name}() takes {spec.arity_text()} argument(s), got {len(node.args)}",
                            start=span[0], end=span[1], severity="error"))
                elif node.name in UNSUPPORTED_FILEBOT_METHODS:
                    issues.append(FormatIssue(
                        message=f"Unsupported FileBot method '{node.name}()'; this block will render empty",
                        start=span[0], end=span[1], severity="warning"))
                else:
                    issues.append(FormatIssue(
                        message=f"Unknown method '{node.name}()'{_suggest(node.name, method_names)}",
                        start=span[0], end=span[1], severity="error"))
            elif isinstance(node, Ident):
                definition = BINDINGS.get(node.name)
                if definition is not None:
                    if (
                        not guarded
                        and kind in _KIND_WORD
                        and kind not in definition.spec.kinds
                    ):
                        issues.append(FormatIssue(
                            message=f"'{node.name}' is not available for {_KIND_WORD[kind]} formats; the block will be empty",
                            start=node.start, end=node.end, severity="warning"))
                elif node.name in UNSUPPORTED_FILEBOT_BINDINGS:
                    hint = (
                        "; Schema formats are relative to the library root"
                        if node.name in ROOT_RELATIVE_BINDINGS
                        else ""
                    )
                    issues.append(FormatIssue(
                        message=f"Unsupported FileBot binding '{node.name}'{hint}",
                        start=node.start, end=node.end, severity="warning"))
                else:
                    issues.append(FormatIssue(
                        message=f"Unknown binding '{node.name}'{_suggest(node.name, binding_names)}",
                        start=node.start, end=node.end, severity="warning"))
            elif isinstance(node, Member) and isinstance(node.obj, Ident):
                props = OBJECT_PROPERTIES.get(node.obj.name)
                if props is not None and node.name not in props:
                    issues.append(FormatIssue(
                        message=f"'{node.obj.name}' has no property '{node.name}'{_suggest(node.name, sorted(props))}",
                        start=node.obj.end, end=node.end, severity="warning"))
    expr = template.expression
    stripped = expr.lstrip()
    if template.segments and isinstance(template.segments[0], str):
        lead = len(expr) - len(stripped)
        if stripped.startswith(("/", "\\")) or _DRIVE_RE.match(stripped):
            issues.append(FormatIssue(
                message="Formats are relative to the library root; remove the leading '/' or drive letter",
                start=lead, end=lead + (3 if _DRIVE_RE.match(stripped) else 1), severity="error"))
    if template.segments and isinstance(template.segments[-1], str):
        tail = expr.rstrip()
        if tail.endswith(("/", "\\")):
            issues.append(FormatIssue(
                message="Format ends with a folder separator; it must end with a file name",
                start=len(tail) - 1, end=len(tail), severity="error"))
    return issues


def validate(expr: str, kind: MediaKind) -> list[FormatIssue]:
    """Syntax errors, static checks (methods, bindings, kind availability, absolute paths) and the
    swallowed block errors of the built-in samples for ``kind`` (as warnings)."""
    from schema_app.core.format.samples import samples_for

    try:
        template = compile_format(expr)
    except FormatSyntaxError as exc:
        return [FormatIssue(message=exc.message, start=exc.start, end=exc.end, severity="error")]
    issues = _static_issues(template, kind)
    seen = {(issue.message, issue.start, issue.end) for issue in issues}
    empty_samples: list[str] = []
    if not any(issue.severity == "error" for issue in issues):
        for sample in samples_for(kind):
            result = template.render_detailed(sample.bindings())
            for issue in result.issues:
                if issue.severity != "warning":
                    continue
                key = (issue.message, issue.start, issue.end)
                if key not in seen:
                    seen.add(key)
                    issues.append(issue)
            if result.path is None:
                empty_samples.append(sample.source)
    if empty_samples:
        issues.append(FormatIssue(
            message=f"Renders an empty path for {len(empty_samples)} sample(s), e.g. {empty_samples[0]}",
            start=0, end=len(expr), severity="warning"))
    issues.sort(key=lambda issue: (issue.start, issue.end))
    return issues


def catalog() -> FormatCatalog:
    """Bindings, methods ("filters") and presets for the format builder UI."""
    filters = [
        FilterSpec(name=spec.name, signature=spec.signature, description=spec.description, example=spec.example)
        for spec in METHODS.values()
    ]
    return FormatCatalog(bindings=binding_specs(), filters=filters, presets=list(PRESETS))
