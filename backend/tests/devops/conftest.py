"""Fixtures for DevOps service tests. Everything lives under ``tmp_path``; no network."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest


@pytest.fixture
def make_file() -> Callable[..., Path]:
    """make_file(path, content=b"...", size=None, mtime=None) -> Path (parents created)."""

    def _make(path: Path, content: bytes | str = b"video-bytes", *, size: int | None = None,
              mtime: float | None = None) -> Path:  # fmt: skip
        path.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode() if isinstance(content, str) else content
        if size is not None:
            data = (data * (size // max(1, len(data)) + 1))[:size]
        path.write_bytes(data)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    return _make


@pytest.fixture
def lib(tmp_path: Path) -> dict[str, Path]:
    """Two allowed roots (downloads + library), a trash dir outside them, and an outside dir."""
    layout = {
        "downloads": tmp_path / "downloads",
        "library": tmp_path / "library",
        "trash": tmp_path / "config" / "trash",
        "outside": tmp_path / "outside",
    }
    for path in layout.values():
        path.mkdir(parents=True, exist_ok=True)
    return layout


@pytest.fixture
def roots(lib: dict[str, Path]) -> list[str]:
    return [str(lib["downloads"]), str(lib["library"])]


def _can_symlink(tmp: Path) -> bool:
    target = tmp / "symlink-probe-target"
    link = tmp / "symlink-probe-link"
    target.write_text("x")
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError, AttributeError):
        return False
    finally:
        target.unlink(missing_ok=True)
    link.unlink(missing_ok=True)
    return True


@pytest.fixture
def need_symlinks(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Skip when the OS user can't create symlinks (Windows without Developer Mode / admin)."""
    if not _can_symlink(tmp_path_factory.mktemp("symlink-probe")):
        pytest.skip("symlinks need Developer Mode or admin rights on this Windows host")
