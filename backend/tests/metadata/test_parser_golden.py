"""Golden snapshot of the filename parser: any change to any parsed field fails here.

``parser_golden.jsonl`` holds one line per path: the path and the full ``model_dump(mode="json")``
captured from the parser. The comparison is on canonical JSON text, so a type change
(``1`` vs ``1.0``), a reordered list or a new default all show up.

When a parser change is *meant* to change results, regenerate and review the diff:

    SCHEMA_UPDATE_PARSER_GOLDEN=1 python -m pytest tests/metadata/test_parser_golden.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from schema_app.core.parser import parse
from schema_app.domain import ParsedMedia

GOLDEN = Path(__file__).with_name("parser_golden.jsonl")


def _canon(data: object) -> str:
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _dump(path: str) -> dict:
    return parse(path).model_dump(mode="json")


def _load() -> list[tuple[str, dict]]:
    rows = []
    for line in GOLDEN.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            rows.append((row["path"], row["parsed"]))
    return rows


if os.environ.get("SCHEMA_UPDATE_PARSER_GOLDEN") == "1":  # pragma: no cover - maintenance path
    from .parser_corpus import golden_corpus

    with GOLDEN.open("w", encoding="utf-8", newline="\n") as fh:
        for _path in golden_corpus():
            fh.write(_canon({"path": _path, "parsed": _dump(_path)}) + "\n")


def test_golden_snapshot_matches() -> None:
    rows = _load()
    assert len(rows) >= 500
    mismatches = []
    for path, expected in rows:
        got = _dump(path)
        if _canon(got) != _canon(expected):
            changed = sorted(k for k in set(got) | set(expected) if got.get(k) != expected.get(k))
            mismatches.append(f"{path!r}: {changed}")
    assert not mismatches, f"{len(mismatches)} parse results changed:\n" + "\n".join(mismatches[:40])


def test_golden_parse_is_repeatable_and_valid() -> None:
    """Parsing twice gives equal, independent objects that survive re-validation unchanged."""
    for path, _expected in _load()[:300]:
        first, second = parse(path), parse(path)
        assert first == second
        assert first is not second and first.quality is not second.quality
        assert first.episodes is not second.episodes and first.alt_titles is not second.alt_titles
        revalidated = ParsedMedia.model_validate(first.model_dump())
        assert _canon(revalidated.model_dump(mode="json")) == _canon(first.model_dump(mode="json"))


@pytest.mark.parametrize("path", ["", "/", "a/b/c/", "Show/Season 1/", "x" * 400])
def test_golden_odd_paths_still_parse(path: str) -> None:
    assert isinstance(parse(path), ParsedMedia)
