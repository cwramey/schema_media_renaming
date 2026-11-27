"""``pipeline.expand_paths``: what a manual / webhook folder job picks up before anything is read."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from schema_app.api_models import AppSettings
from schema_app.core.pipeline import expand_paths


def _settings(*roots: Path) -> AppSettings:
    return AppSettings(allowed_roots=[str(r) for r in roots])


def test_regular_videos_are_found(lib: dict[str, Path], make_file: Callable[..., Path]) -> None:
    d = lib["downloads"] / "Show"
    a = make_file(d / "Show.S01E01.mkv")
    b = make_file(d / "Sub" / "Show.S01E02.mkv")
    make_file(d / "notes.txt")
    make_file(d / ".hidden.mkv")
    assert expand_paths([str(d)], _settings(lib["downloads"])) == [str(a), str(b)]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX-only")
def test_fifo_named_like_a_video_is_skipped(lib: dict[str, Path], make_file: Callable[..., Path]) -> None:
    d = lib["downloads"] / "Drop"
    real = make_file(d / "Real.mkv")
    os.mkfifo(d / "Trap.mkv")
    assert expand_paths([str(d)], _settings(lib["downloads"])) == [str(real)]
    assert expand_paths([str(d / "Trap.mkv")], _settings(lib["downloads"])) == []


def test_symlinks_outside_the_roots_or_to_devices_are_skipped(
    lib: dict[str, Path], make_file: Callable[..., Path], need_symlinks: None
) -> None:
    d = lib["downloads"] / "Drop"
    real = make_file(d / "Real.mkv")
    secret = make_file(lib["outside"] / "secret.key", b"do-not-read")
    os.symlink(secret, d / "Escape.mkv")  # resolves outside the allowed roots
    os.symlink(os.devnull, d / "Zero.mkv")  # a device behind a video name
    os.symlink(d / "missing.bin", d / "Dangling.mkv")
    inside = make_file(lib["downloads"] / "Elsewhere" / "Linked.bin")
    os.symlink(inside, d / "Linked.mkv")  # a link that stays inside the roots is fine
    found = expand_paths([str(d)], _settings(lib["downloads"]))
    assert found == [str(d / "Linked.mkv"), str(real)]
    assert expand_paths([str(d / "Escape.mkv")], _settings(lib["downloads"])) == []
