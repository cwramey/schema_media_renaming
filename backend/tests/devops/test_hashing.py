from __future__ import annotations

import os
import threading
import zlib
from pathlib import Path

import pytest

from schema_app.services import hashing
from schema_app.services.hashing import crc32_file, normalize_crc32, open_regular, verify_crc32


def test_crc32_matches_zlib(tmp_path: Path) -> None:
    data = bytes(range(256)) * 5000
    path = tmp_path / "f.bin"
    path.write_bytes(data)
    expected = f"{zlib.crc32(data) & 0xFFFFFFFF:08X}"
    assert crc32_file(path) == expected
    assert crc32_file(path, chunk_size=7) == expected  # chunking doesn't change the result


def test_crc32_known_value_and_padding(tmp_path: Path) -> None:
    path = tmp_path / "f.txt"
    path.write_bytes(b"123456789")
    assert crc32_file(path) == "CBF43926"  # the standard CRC-32 check value
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    assert crc32_file(empty) == "00000000"


def test_verify_crc32(tmp_path: Path) -> None:
    path = tmp_path / "[Group] Show - 01 [CBF43926].mkv"
    path.write_bytes(b"123456789")
    assert verify_crc32(path, "CBF43926")
    assert verify_crc32(path, "[cbf43926]")
    assert not verify_crc32(path, "DEADBEEF")


@pytest.mark.parametrize("value", ["", "XYZ", "123456789", "[ABCDEFGH]"])
def test_malformed_expected(tmp_path: Path, value: str) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"x")
    with pytest.raises(ValueError):
        verify_crc32(path, value)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        verify_crc32(tmp_path / "missing.mkv", "00000000")


def test_normalize() -> None:
    assert normalize_crc32("0xabc") == "00000ABC"
    assert normalize_crc32(" (A1B2C3D4) ") == "A1B2C3D4"


# --------------------------------------------------------------------------- non-regular files


def test_crc32_refuses_devices_and_folders(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        crc32_file(os.devnull)  # a character device on every OS
    with pytest.raises(OSError):
        crc32_file(tmp_path)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX-only")
def test_crc32_refuses_a_fifo_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "video.mkv"
    os.mkfifo(fifo)
    outcome: list[object] = []

    def run() -> None:
        try:
            crc32_file(fifo)
        except OSError as exc:
            outcome.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(5)
    assert not worker.is_alive(), "crc32_file blocked on a FIFO"
    assert outcome and isinstance(outcome[0], OSError)


def test_crc32_through_a_symlink_to_a_device(tmp_path: Path, need_symlinks: None) -> None:
    link = tmp_path / "zero.mkv"
    os.symlink(os.devnull, link)
    with pytest.raises(OSError):
        crc32_file(link)


def test_open_regular_nofollow(tmp_path: Path, need_symlinks: None) -> None:
    target = tmp_path / "real.mkv"
    target.write_bytes(b"x")
    link = tmp_path / "link.mkv"
    os.symlink(target, link)
    os.close(open_regular(link))  # follows by default
    with pytest.raises(OSError):
        open_regular(link, follow=False)


def test_crc32_drops_read_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int, int]] = []
    monkeypatch.setattr(hashing, "_FADVISE", lambda fd, off, length, advice: calls.append((off, length, advice)))
    monkeypatch.setattr(os, "POSIX_FADV_DONTNEED", 4, raising=False)
    monkeypatch.setattr(os, "POSIX_FADV_SEQUENTIAL", 2, raising=False)
    path = tmp_path / "f.bin"
    path.write_bytes(b"a" * 10)
    assert crc32_file(path, chunk_size=4) == f"{zlib.crc32(b'a' * 10) & 0xFFFFFFFF:08X}"
    assert [(o, n) for o, n, a in calls if a == 4] == [(0, 4), (4, 4), (8, 2)]
