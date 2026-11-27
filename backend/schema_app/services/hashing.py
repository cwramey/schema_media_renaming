"""Checksums. Contract: docs/SPEC.md §7.4.

Also the low-level read helpers file operations share: :func:`open_regular` (never blocks on a FIFO,
never reads a device) and :func:`drop_cache` (keeps a 40 GB read from filling the page cache that
counts against the container's memory limit).
"""

from __future__ import annotations

import contextlib
import errno
import io
import os
import re
import stat
import zlib
from pathlib import Path

_CRC_RE = re.compile(r"^[0-9A-F]{1,8}$")

_FADVISE = getattr(os, "posix_fadvise", None)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_BINARY = getattr(os, "O_BINARY", 0)  # Windows: no newline translation


def open_regular(path: str | Path, *, follow: bool = True) -> int:
    """Open ``path`` read-only and return the fd (the caller closes it), refusing anything that is
    not a regular file with OSError: a FIFO (opened non-blocking, so it can't hang the caller until a
    writer shows up), a device such as /dev/zero, a socket or a folder. ``follow=False`` also refuses
    a symlink as the last component (O_NOFOLLOW where the OS has it). The check is made on the opened
    file (fstat), so swapping the path afterwards changes nothing."""
    target = os.fspath(path)
    flags = os.O_RDONLY | _O_BINARY | _O_NONBLOCK
    if not follow:
        if _O_NOFOLLOW:
            flags |= _O_NOFOLLOW
        elif os.path.islink(target):
            raise OSError(errno.ELOOP, "Refusing to follow a symlink", target)
    fd = os.open(target, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(errno.EINVAL, "Not a regular file", target)
        if _O_NONBLOCK:
            os.set_blocking(fd, True)
    except BaseException:
        os.close(fd)
        raise
    return fd


def drop_cache(fd: int, offset: int = 0, length: int = 0) -> None:
    """``posix_fadvise(DONTNEED)``: forget the cached pages of a range already used (``length`` 0 =
    to the end of the file). On dirty pages Linux starts their write-back and drops them once a later
    call finds them clean. No-op where the OS lacks it (Windows, macOS)."""
    if _FADVISE is not None:
        with contextlib.suppress(OSError, ValueError):
            _FADVISE(fd, offset, length, os.POSIX_FADV_DONTNEED)


def advise_sequential(fd: int) -> None:
    """Tell the kernel the file is read front to back once (bigger read-ahead)."""
    if _FADVISE is not None:
        with contextlib.suppress(OSError, ValueError):
            _FADVISE(fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)


def crc32_fd(fd: int, *, chunk_size: int = 4 * 1024 * 1024) -> str:
    """CRC32 of an open file from its current position to the end; read pages are dropped from the
    page cache as it goes."""
    crc = 0
    offset = os.lseek(fd, 0, os.SEEK_CUR)
    advise_sequential(fd)
    buf = bytearray(max(1, chunk_size))
    view = memoryview(buf)
    with io.FileIO(fd, "rb", closefd=False) as handle:
        while n := handle.readinto(view):
            crc = zlib.crc32(view[:n], crc)
            drop_cache(fd, offset, n)
            offset += n
    return format_crc32(crc)


def crc32_file(path: str | Path, *, chunk_size: int = 4 * 1024 * 1024) -> str:
    """CRC32 of the whole file as 8 uppercase hex chars (blocking; streams in chunks).

    Raises OSError for anything but a regular file (a FIFO or a symlink to /dev/zero would otherwise
    hang the worker forever)."""
    fd = open_regular(path)
    try:
        return crc32_fd(fd, chunk_size=chunk_size)
    finally:
        os.close(fd)


def format_crc32(value: int) -> str:
    """Render an integer CRC the way release names do: 8 uppercase hex digits."""
    return f"{value & 0xFFFFFFFF:08X}"


def normalize_crc32(value: str) -> str:
    """Normalize "[a1b2c3d4]", "a1b2c3d4" or "0x1B2C3D4" to "A1B2C3D4"; ValueError if not a CRC32."""
    text = str(value).strip().strip("[]()").strip().upper()
    if text.startswith("0X"):
        text = text[2:]
    if not _CRC_RE.match(text):
        raise ValueError(f"not a CRC32 value: {value!r}")
    return text.zfill(8)


def verify_crc32(path: str | Path, expected: str) -> bool:
    """True when the file's CRC32 equals ``expected`` (brackets and case are ignored).

    Raises ValueError for a malformed ``expected`` and OSError when the file can't be read; a read
    error is not a mismatch, so callers can tell the two apart.
    """
    return crc32_file(path) == normalize_crc32(expected)
