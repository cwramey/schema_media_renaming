"""Safe rename / move / copy / link engine with undo.

Contract: docs/SPEC.md §7.2.
All functions are synchronous and blocking; callers wrap them in ``asyncio.to_thread``.

Guarantees
----------
* **No partial destinations.** Data is copied to ``.<name>.schema-tmp`` inside the destination
  folder, flushed with ``fsync``, verified (size; size + CRC32 for cross-device moves: the CRC of
  the source is taken while copying, the copy is read back from disk) and only then renamed into
  place. Any failure removes the temp file. A cross-device source is deleted only after that, and
  after the destination folder's entry is fsynced.
* **The checked file is the copied file.** A copy opens its source without following a final
  symlink (O_NOFOLLOW) or blocking on a FIFO, and insists on the device/inode seen by the checks.
  Copies drop the pages they read and write from the page cache (``posix_fadvise``); copy mode on
  one filesystem uses ``copy_file_range`` (a reflink on btrfs).
* **No silent clobbering.** Renames never replace an existing entry (Windows ``rename`` semantics;
  ``link``+``unlink`` or an explicit check on POSIX). Files displaced by ``overwrite``/``upgrade``
  are moved to a trash folder, never deleted.
* **All or nothing per item.** Every step registers its inverse; if a later step (a sidecar, the
  symlink of ``keeplink`` ...) fails, completed steps are rolled back. The one exception is after a
  cross-device source has been deleted (the point of no return): later sidecar failures then leave
  that sidecar where it was instead of undoing the main file.
* **Boundaries.** Sources and destinations must be inside ``allowed_roots`` (symlinks resolved), and
  never inside Schema's own data folder (``paths.protect``), whatever the roots say.
* **Durability, batched.** New folder entries are fsynced per folder at most once a second
  (:func:`sync_dirs_later`), so a 50k-file batch doesn't pay 10-30 ms of HDD seek per file.
* **Planning reads little.** An equal-sized file at the destination is compared by its first and
  last MiB while planning (cached); execution does the one full byte compare.
* **Undo.** Each :class:`FileOpResult` carries what :func:`undo` needs. Undo refuses when the
  destination changed (size, and mtime when the contract carries it) or the original location is
  occupied, and restores displaced files from the trash.

Trash
-----
Displaced files go to ``<trash_dir>/<YYYY-MM-DD>/<HHMMSS>-<token>/`` with a ``schema-trash.json``
manifest. When ``trash_dir`` is on a different filesystem than the displaced file (the usual
``/config`` vs ``/storage`` split), copying a 40 GB movie into the app-data volume would be slow and
could fill it, so the file goes to ``<allowed root>/.schema-trash/...`` (same filesystem) instead,
falling back to ``<folder>/.schema-trash/...``. ``FileOpResult.replaced`` is the trashed path.

Folders
-------
A ``move`` whose source sits alone in its own folder ("/media/movies/Alien.1979/Alien (1979) 4K.mkv"
-> "/media/movies/Alien (1979)/Alien (1979).mkv") can rename that folder in place instead of creating
a new one and leaving the old one behind (:func:`folder_rename_target` lists the conditions): one
atomic folder rename, then the file and its sidecars are renamed inside it, and posters or extras in
the folder simply stay with it. Undo renames the file back first, then the folder. After a plain
move, ``FileOpPlan.remove_empty_dirs`` removes the source folder if the move left it empty, then each
empty parent, up to the first protected folder (:func:`_is_protected`); undo recreates them.
"""

from __future__ import annotations

import atexit
import bisect
import contextlib
import errno
import io
import json
import logging
import os
import re
import secrets
import shutil
import stat
import threading
import time
import zlib
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from schema_app.domain import ConflictPolicy, FileOpPlan, FileOpResult, FolderRename, RenameMode, SidecarMove
from schema_app.services import hashing
from schema_app.services import paths as pathsvc

log = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"
TEMP_SUFFIX = ".schema-tmp"
LINK_SUFFIX = ".schema-link"
TRASH_DIRNAME = ".schema-trash"
TRASH_MANIFEST = "schema-trash.json"
COPY_CHUNK = 8 * 1024 * 1024
FREE_SPACE_MARGIN = 16 * 1024 * 1024
MAX_INDEX = 999

# Used only to decide which sibling "owns" a sidecar ("Movie.Extended.en.srt" belongs to
# "Movie.Extended.mkv", not "Movie.mkv").
VIDEO_EXTENSIONS = frozenset(
    [
        "mkv",
        "mp4",
        "m4v",
        "avi",
        "mov",
        "wmv",
        "ts",
        "m2ts",
        "mts",
        "webm",
        "mpg",
        "mpeg",
        "flv",
        "ogm",
        "ogv",
        "vob",
        "divx",
        "rmvb",
        "3gp",
        "iso",
    ]
)

# Sub-folders whose videos belong to the movie next to them (Jellyfin / Plex extras folders, scene
# release samples): they don't stop the movie's folder from being renamed in place.
EXTRAS_DIRS = frozenset(
    [
        "extras",
        "featurettes",
        "behind the scenes",
        "deleted scenes",
        "interviews",
        "scenes",
        "shorts",
        "trailers",
        "clips",
        "other",
        "sample",
        "samples",
    ]
)

_MOVE_MODES = (RenameMode.MOVE, RenameMode.KEEPLINK)
_FOLDER_MODES = (RenameMode.MOVE, RenameMode.TEST)  # a dry run reports the folder rename a move would do
_NO_ROOTS = (
    "No allowed roots are configured, so Schema refuses to touch files. "
    "Add your media folders to Settings → Allowed roots."
)


class FileOpError(Exception):
    """An expected failure with a human-readable message (becomes ``FileOpResult.error``); ``code``
    becomes ``FileOpResult.error_code`` so callers map failures by type, never by message text."""

    code = "error"


class SourceMissingError(FileOpError):
    """The source was moved or deleted after the scan."""

    code = "source_missing"


class DestinationExistsError(FileOpError):
    """Something already occupies the destination (conflict policy ``fail``)."""

    code = "destination_exists"


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, FileOpError):
        return exc.code
    if isinstance(exc, FileExistsError):
        return DestinationExistsError.code
    return FileOpError.code


# --------------------------------------------------------------------------- sidecars


def _norm_exts(extensions: Iterable[str] | None) -> set[str]:
    return {e.strip().lower().lstrip(".") for e in (extensions or []) if e and e.strip().lstrip(".")}


# Sorted directory listings shared by the find_sidecars() calls of a batch: one scandir per folder
# instead of one per video (flat download folders with thousands of files were O(n^2)). Entries
# are keyed on the folder's mtime and only cached once that mtime is a few seconds old, so a file
# created right after a listing can't hide behind coarse (SMB/FAT) timestamps.
#
# Schema's own changes (a new movie folder in the library root, a file moved out of a flat download
# folder) would bump those mtimes on every item of a batch and force a full re-scan (and re-fold) per
# item: :func:`_editing` brackets each change and patches the cached listing in place instead.
_LISTING_CACHE_SIZE = 64
_LISTING_MIN_AGE_NS = 3_000_000_000
_COARSE_MTIME_NS = 1_000_000_000  # an mtime that is a whole second: FAT/SMB-style timestamps


class _Listing:
    """One scandir of a folder: its regular files sorted by lower-cased name (find_sidecars), plus a
    lazily built casefold+NFC index of every entry (case/normalisation-variant lookups). Never
    mutated once shared: :meth:`changed` returns an updated copy."""

    __slots__ = ("lowers", "names", "_entries", "_folded")

    def __init__(
        self,
        entries: dict[str, tuple[bool, bool]],  # name -> (is_file, is_dir)
        *,
        lowers: list[str] | None = None,
        names: list[str] | None = None,
        folded: dict[str, list[tuple[str, bool]]] | None = None,
    ) -> None:
        if lowers is None or names is None:
            pairs = sorted((name.lower(), name) for name, (is_file, _) in entries.items() if is_file)
            lowers = [lower for lower, _ in pairs]
            names = [name for _, name in pairs]
        self.lowers = lowers
        self.names = names
        self._entries = entries
        self._folded = folded

    def variants(self, name: str) -> list[tuple[str, bool]]:
        """(name, is_dir) of the entries equal to ``name`` ignoring case and Unicode normalisation
        (the exact name included), sorted by name."""
        folded = self._folded
        if folded is None:  # built once per listing; a racing duplicate build is harmless
            folded = {}
            for entry_name in sorted(self._entries):
                folded.setdefault(pathsvc.fold_name(entry_name), []).append((entry_name, self._entries[entry_name][1]))
            self._folded = folded
        return folded.get(pathsvc.fold_name(name), [])

    def changed(self, name: str, kind: tuple[bool, bool] | None) -> _Listing:
        """A copy where ``name`` is ``kind`` ((is_file, is_dir)), or absent when ``kind`` is None.
        Copies references only (no re-sort, no re-fold), so it stays cheap on a 5000-entry root."""
        entries = dict(self._entries)
        old = entries.pop(name, None)
        if kind is not None:
            entries[name] = kind
        lowers, names = self.lowers, self.names
        was_file, is_file = old is not None and old[0], kind is not None and kind[0]
        if was_file or is_file:
            lowers, names = list(lowers), list(names)
            low = name.lower()
            if was_file:
                i = bisect.bisect_left(lowers, low)
                while i < len(lowers) and lowers[i] == low:
                    if names[i] == name:
                        del lowers[i], names[i]
                        break
                    i += 1
            if is_file:
                lo = bisect.bisect_left(lowers, low)
                i = bisect.bisect_left(names, name, lo, bisect.bisect_right(lowers, low, lo))
                lowers.insert(i, low)
                names.insert(i, name)
        folded = self._folded
        if folded is not None:
            folded = dict(folded)
            key = pathsvc.fold_name(name)
            group = [v for v in folded.get(key, ()) if v[0] != name]
            if kind is not None:
                group.append((name, kind[1]))
                group.sort()
            if group:
                folded[key] = group
            else:
                folded.pop(key, None)
        return _Listing(entries, lowers=lowers, names=names, folded=folded)


_listing_cache: OrderedDict[str, tuple[int, _Listing]] = OrderedDict()
_listing_lock = threading.Lock()
_listing_edits: dict[str, int] = {}  # folder -> Schema changes in flight there


def _listing(folder: Path) -> _Listing | None:
    try:
        mtime = os.stat(folder).st_mtime_ns
    except OSError:
        return None
    key = os.fspath(folder)
    with _listing_lock:
        hit = _listing_cache.get(key)
        if hit is not None and hit[0] == mtime:
            _listing_cache.move_to_end(key)
            return hit[1]
    try:
        with os.scandir(folder) as it:
            listing = _Listing({entry.name: (_entry_is_file(entry), _entry_is_dir(entry)) for entry in it})
    except OSError:
        return None
    if time.time_ns() - mtime >= _LISTING_MIN_AGE_NS:
        with _listing_lock:
            _cache_listing(key, mtime, listing)
    return listing


def _cache_listing(key: str, mtime: int, listing: _Listing) -> None:
    """Store a listing (caller holds ``_listing_lock``)."""
    _listing_cache[key] = (mtime, listing)
    _listing_cache.move_to_end(key)
    while len(_listing_cache) > _LISTING_CACHE_SIZE:
        _listing_cache.popitem(last=False)


def _entry_kind(path: Path) -> tuple[bool, bool] | None:
    """(is_file, is_dir) the way scandir reports ``path`` (symlinks followed), None when absent."""
    try:
        st = os.stat(path)
    except OSError:
        return (False, False) if os.path.lexists(path) else None
    return stat.S_ISREG(st.st_mode), stat.S_ISDIR(st.st_mode)


def _forget_listings(folder: Path) -> None:
    """Drop cached listings of ``folder`` and everything below it (it was renamed away)."""
    key = os.fspath(folder)
    prefix = key.rstrip(os.sep) + os.sep
    with _listing_lock:
        for cached in [k for k in _listing_cache if k == key or k.startswith(prefix)]:
            del _listing_cache[cached]


@contextlib.contextmanager
def _editing(*paths: Path | str) -> Iterator[None]:
    """Bracket one of Schema's own changes to the entries ``paths`` (created, removed or renamed).

    A cached listing of their folder that was current right before the change is patched in place
    afterwards (each name re-checked on disk) and keeps being used at the folder's new mtime. If the
    folder changed meanwhile in a way the patch can't account for (it was stale already, another
    Schema change overlapped, coarse timestamps), the cached listing is dropped instead: the next
    lookup re-scans, exactly as without this. Something else creating an entry in the very same
    folder during the change itself (microseconds) is the one case this can't see."""
    by_folder: dict[str, list[str]] = {}
    for p in paths:
        p = Path(p)
        by_folder.setdefault(os.fspath(p.parent), []).append(p.name)
    validated: dict[str, tuple[int, _Listing]] = {}
    with _listing_lock:
        for key in by_folder:
            _listing_edits[key] = _listing_edits.get(key, 0) + 1
        hits = {key: _listing_cache.get(key) for key in by_folder}
    for key, hit in hits.items():
        if hit is None:
            continue
        try:
            current = os.stat(key).st_mtime_ns
        except OSError:
            current = None
        with _listing_lock:
            if current == hit[0] and _listing_cache.get(key) is hit:
                validated[key] = hit
            elif _listing_cache.get(key) is hit:
                del _listing_cache[key]  # stale already: overlapping patches must not revive it
    try:
        yield
    finally:
        patched: dict[str, tuple[int, _Listing]] = {}
        for key, hit in validated.items():
            try:
                mtime = os.stat(key).st_mtime_ns
            except OSError:
                continue
            if mtime % _COARSE_MTIME_NS == 0:
                continue
            listing = hit[1]
            for name in by_folder[key]:
                listing = listing.changed(name, _entry_kind(Path(key) / name))
            patched[key] = (mtime, listing)
        with _listing_lock:
            for key in by_folder:
                left = _listing_edits.get(key, 1) - 1
                if left:
                    _listing_edits[key] = left
                else:
                    _listing_edits.pop(key, None)
                hit = validated.get(key)
                if hit is not None and _listing_cache.get(key) is hit and not left and key in patched:
                    _cache_listing(key, *patched[key])
                elif hit is not None:
                    _listing_cache.pop(key, None)


def _sorted_listing(folder: Path) -> tuple[list[str], list[str]] | None:
    """(lower-cased names, original names), both sorted by the lower-cased name: regular files only."""
    listing = _listing(folder)
    return None if listing is None else (listing.lowers, listing.names)


def find_sidecars(video: str | Path, extensions: list[str]) -> list[Path]:
    """Companion files sharing the video's stem: "Movie.en.srt", "Movie.forced.ass", "Movie.nfo"."""
    video = Path(video)
    exts = _norm_exts(extensions)
    if not exts:
        return []
    stem = video.stem
    if not stem:
        return []
    listing = _sorted_listing(video.parent)
    if listing is None:
        return []
    lowers, all_names = listing
    prefix = stem.lower() + "."
    # Only names starting with the prefix can match, and they are one contiguous sorted slice.
    lo = bisect.bisect_left(lowers, prefix)
    names = all_names[lo : bisect.bisect_left(lowers, prefix + "\U0010ffff", lo)]
    # Longer-stemmed sibling videos claim sidecars that match them more specifically.
    claimers = [
        Path(n).stem.lower() + "."
        for n in names
        if n != video.name
        and Path(n).suffix.lower().lstrip(".") in VIDEO_EXTENSIONS
        and len(Path(n).stem) > len(stem)
        and Path(n).stem.lower().startswith(prefix)
    ]
    found: list[Path] = []
    for name in names:
        lower = name.lower()
        if name == video.name or not lower.startswith(prefix):
            continue
        if Path(name).suffix.lower().lstrip(".") not in exts:
            continue
        if any(lower.startswith(c) for c in claimers):
            continue
        found.append(video.parent / name)
    return sorted(found, key=lambda p: p.name.lower())


def _entry_is_file(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_file()
    except OSError:
        return False


def _entry_is_dir(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_dir()
    except OSError:
        return False


def sidecar_suffix(video: str | Path, sidecar: str | Path) -> str:
    """The part of the sidecar name after the video stem: ".en.srt", ".eng.forced.ass", ".idx"."""
    stem = Path(video).stem
    name = Path(sidecar).name
    if name.lower().startswith(stem.lower() + "."):
        return name[len(stem) :]
    return Path(name).suffix


def _sidecar_destination(destination: Path, suffix: str) -> Path:
    return destination.parent / pathsvc.fit_component(destination.stem + suffix)


# --------------------------------------------------------------------------- planning


def _abs(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _samefile(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _is_case_only(src: Path, dst: Path) -> bool:
    """dst names the same directory entry as src, differing only in letter case or Unicode
    normalisation (case-insensitive FS such as NTFS/APFS/SMB, or APFS normalisation-insensitivity)."""
    return (
        src.name != dst.name
        and pathsvc.fold_name(src.name) == pathsvc.fold_name(dst.name)
        and _samefile(src.parent, dst.parent)
        and _samefile(src, dst)
    )


# Case-sensitive filesystems (ext4/btrfs/ZFS on the NAS) happily hold "Doctor Who (2005)" next to
# "Doctor who (2005)", or an NFD and an NFC "Amélie": SMB clients then show mangled names and
# Jellyfin two series. Destinations are therefore matched against existing entries with
# pathsvc.fold_name (casefold + NFC), using the cached directory listings above.


def _existing_entry(path: Path) -> Path | None:
    """The entry occupying ``path``: ``path`` itself, else an entry of its folder whose name only
    differs in case / Unicode normalisation (None when the name is free)."""
    if os.path.lexists(path):
        return path
    listing = _listing(path.parent)
    if listing is None:
        return None
    for name, _ in listing.variants(path.name):
        if name != path.name:
            return path.parent / name
    return None


def _dir_variant(folder: Path, name: str) -> str | None:
    """Real name of the sub-folder of ``folder`` matching ``name`` ignoring case / normalisation."""
    listing = _listing(folder)
    if listing is None:
        return None
    exact = [n for n, is_dir in listing.variants(name) if is_dir]
    return name if name in exact else (exact[0] if exact else None)


def _reuse_folder_names(dst: Path) -> Path:
    """Spell the destination's missing folders like existing ones that match them ignoring case /
    normalisation, so "Doctor who (2005)/..." goes into the existing "Doctor Who (2005)" folder.

    Only folders that don't exist under their exact name are looked up (one stat each, walking up
    to the deepest existing folder), then one cached listing per level below it. The common case,
    an existing destination folder, costs a single stat.
    """
    current = dst.parent
    missing: list[str] = []
    while not os.path.isdir(current):
        if current.parent == current:
            return dst
        missing.append(current.name)
        current = current.parent
    if not missing:
        return dst
    missing.reverse()
    for index, name in enumerate(missing):
        real = _dir_variant(current, name)
        if real is None:
            return current.joinpath(*missing[index:], dst.name)
        current = current / real
    return current / dst.name


def _conflict_with(src: Path, dst: Path, mode: RenameMode, *, deep: bool = True) -> tuple[str, Path | None]:
    """(conflict state, entry occupying the destination): see :func:`_detect_conflict`.

    ``deep`` compares an equal-sized occupant byte for byte (:func:`_identical`), which reads both
    files whole; only execution does that, once. Planning uses :func:`_looks_identical`."""
    if str(src) == str(dst):
        return "same_file", dst
    if _is_case_only(src, dst):
        # Only a move can fix letter case; copying/linking a file onto itself is already done.
        return ("none", None) if mode is RenameMode.MOVE else ("same_file", dst)
    existing = _existing_entry(dst)
    if existing is None:
        return "none", None
    if existing != dst and existing.name == src.name and _samefile(existing.parent, src.parent):
        # Case-sensitive FS: the only variant is the source itself ("movie.mkv" -> "Movie.mkv").
        return ("none", None) if mode is RenameMode.MOVE else ("same_file", existing)
    if _samefile(src, existing) or (_identical(src, existing) if deep else _looks_identical(src, existing)):
        return "same_file", existing
    return "exists", existing


def _same_sized_regular(a: Path, b: Path) -> tuple[os.stat_result, os.stat_result] | None:
    """Stats of ``a`` and ``b`` when both are non-empty regular files of the same size."""
    try:
        sa, sb = os.stat(a), os.stat(b)
    except OSError:
        return None
    if not (stat.S_ISREG(sa.st_mode) and stat.S_ISREG(sb.st_mode)):
        return None
    if sa.st_size != sb.st_size or sa.st_size == 0:
        return None
    return sa, sb


def _compare_fds(fa: int, fb: int, ranges: Iterable[tuple[int, int]]) -> bool:
    """Equal bytes in each (offset, length) range of two open files; read pages are dropped from the
    page cache (a full compare of two 40 GB files would otherwise evict everything else)."""
    for offset, length in ranges:
        end = offset + length
        while offset < end:
            want = min(COPY_CHUNK, end - offset)
            ca, cb = _read_at(fa, want, offset), _read_at(fb, want, offset)
            hashing.drop_cache(fa, offset, want)
            hashing.drop_cache(fb, offset, want)
            if ca != cb:
                return False
            if not ca:
                break
            offset += len(ca)
    return True


_TO_EOF = 1 << 62


def _read_at(fd: int, size: int, offset: int) -> bytes:
    """Up to ``size`` bytes at ``offset`` (fewer only at the end of the file)."""
    parts: list[bytes] = []
    got = 0
    while got < size:
        if hasattr(os, "pread"):
            data = os.pread(fd, size - got, offset + got)
        else:  # Windows
            os.lseek(fd, offset + got, os.SEEK_SET)
            data = os.read(fd, size - got)
        if not data:
            break
        parts.append(data)
        got += len(data)
    return parts[0] if len(parts) == 1 else b"".join(parts)


def _identical(a: Path, b: Path) -> bool:
    """Byte-identical regular files (sizes compared first; then a streamed compare that stops at the
    first differing chunk). Empty files never count, like FileBot's exact-copy check."""
    stats = _same_sized_regular(a, b)
    if stats is None:
        return False
    try:
        fa = hashing.open_regular(a)
        try:
            fb = hashing.open_regular(b)
            try:
                hashing.advise_sequential(fa)
                hashing.advise_sequential(fb)
                return _compare_fds(fa, fb, [(0, _TO_EOF)])  # to the end, even if one grew since
            finally:
                os.close(fb)
        finally:
            os.close(fa)
    except OSError:
        return False


# Planning-time stand-in for the full compare: same size plus equal first and last MiB. A video's
# container header and index sit there, so equal-sized files that differ only in between (a
# corrupted download) are the rare case where planning says "already in place" and execution, whose
# full compare decides, then treats the file as a conflict. Results are cached per both files'
# (dev, inode, size, mtime): re-planning an unchanged pair reads nothing.
QUICK_COMPARE_BYTES = 1024 * 1024
_QUICK_CACHE_SIZE = 4096
_quick_cache: OrderedDict[tuple[object, ...], bool] = OrderedDict()
_quick_lock = threading.Lock()


def _looks_identical(a: Path, b: Path) -> bool:
    """Cheap :func:`_identical` for planning (see :data:`QUICK_COMPARE_BYTES`)."""
    stats = _same_sized_regular(a, b)
    if stats is None:
        return False
    sa, sb = stats
    key = (os.fspath(a), os.fspath(b), sa.st_dev, sa.st_ino, sa.st_mtime_ns, sb.st_dev, sb.st_ino, sb.st_mtime_ns,
           sa.st_size)  # fmt: skip
    with _quick_lock:
        hit = _quick_cache.get(key)
        if hit is not None:
            _quick_cache.move_to_end(key)
            return hit
    size = sa.st_size
    head = min(size, QUICK_COMPARE_BYTES)
    tail_start = max(head, size - QUICK_COMPARE_BYTES)
    ranges = [(0, head)] + ([(tail_start, size - tail_start)] if tail_start < size else [])
    try:
        fa = hashing.open_regular(a)
        try:
            fb = hashing.open_regular(b)
            try:
                same = _compare_fds(fa, fb, ranges)
            finally:
                os.close(fb)
        finally:
            os.close(fa)
    except OSError:
        return False
    with _quick_lock:
        _quick_cache[key] = same
        while len(_quick_cache) > _QUICK_CACHE_SIZE:
            _quick_cache.popitem(last=False)
    return same


def _detect_conflict(src: Path, dst: Path, mode: RenameMode, *, deep: bool = True) -> str:
    """Conflict state right now: "none", "exists" or "same_file".

    "same_file" covers the identical path, a hardlink/inode match, and (as FileBot does) an existing
    destination with byte-identical content: the work is already done, nothing gets overwritten.
    An existing entry whose name differs only in case / Unicode normalisation counts as the
    destination ("exists"), except when it is the source itself (a case-only rename).
    ``deep=False`` (planning) only compares size and the first and last MiB.
    """
    return _conflict_with(src, dst, mode, deep=deep)[0]


# --------------------------------------------------------------------------- folders


def _same_path(a: Path, b: Path) -> bool:
    return os.path.normcase(os.fspath(a)) == os.path.normcase(os.fspath(b)) or _samefile(a, b)


def _dir_key(path: str | Path) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))


def _under(child: str, parent: str) -> bool:
    """``child`` lies strictly inside ``parent`` (both :func:`_dir_key` forms)."""
    return child.startswith(parent.rstrip(os.sep) + os.sep) and child != parent


def _is_protected(folder: Path, guarded: Iterable[str]) -> bool:
    """Folders Schema never renames or removes: the guarded roots (allowed roots, library roots,
    watch folders) and every folder holding one; also share-level folders, the direct children of a
    root that holds another root ("/media/downloads" next to the library "/media/movies" in "/media")."""
    key = _dir_key(folder)
    keys = {_dir_key(g) for g in guarded if g and str(g).strip()}
    if any(k == key or _under(k, key) for k in keys):
        return True
    parent = os.path.dirname(key)
    return parent in keys and any(_under(k, parent) for k in keys)


def is_protected(folder: str | Path, guarded: Iterable[str]) -> bool:
    """Public form of :func:`_is_protected`: Schema never renames, removes or cleans up ``folder``."""
    return _is_protected(Path(folder), guarded)


def _is_video_name(name: str) -> bool:
    return "." in name and name.rsplit(".", 1)[1].lower() in VIDEO_EXTENSIONS


def _holds_video(folder: Path) -> bool:
    # os.walk doesn't follow symlinked sub-folders
    return any(_is_video_name(f) for _, _, files in os.walk(folder) for f in files)


def _only_video(folder: Path, video: Path) -> bool:
    """``video`` is the only video in ``folder``, sub-folders included, except :data:`EXTRAS_DIRS`."""
    try:
        with os.scandir(folder) as it:
            entries = [(entry.name, _entry_is_real_dir(entry)) for entry in it]
    except OSError:
        return False
    own = os.path.normcase(video.name)
    for name, is_dir in entries:
        if is_dir:
            if name.casefold() not in EXTRAS_DIRS and _holds_video(folder / name):
                return False
        elif os.path.normcase(name) != own and _is_video_name(name):
            return False
    return True


def _entry_is_real_dir(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


def folder_rename_target(source: str | Path, destination: str | Path, guarded: Iterable[str]) -> FolderRename | None:
    """The in-place rename of ``source``'s folder that puts it where ``destination``'s folder goes, or
    None when a plain move must happen instead. All of these must hold:

    * the folders differ but have the same parent ("/media/movies/Alien.1979" -> "/media/movies/Alien
      (1979)"), so the rename is a single same-filesystem ``rename``;
    * the source is the only video in its folder; videos in extras sub-folders ("trailers",
      "featurettes" ...) don't count, any other video (a collection, a second movie) does;
    * nothing exists at the new folder name yet (case / normalisation variants included);
    * the folder is a real folder (not a symlink) and not protected (:func:`_is_protected`).

    Whether the mode allows it and whether other items of the same job use the folder is the
    caller's decision.
    """
    src, dst = _abs(source), _abs(destination)
    old_dir, new_dir = src.parent, dst.parent
    if _same_path(old_dir, new_dir) or not _same_path(old_dir.parent, new_dir.parent):
        return None
    if os.path.islink(old_dir) or not os.path.isdir(old_dir) or _existing_entry(new_dir) is not None:
        return None
    if _is_protected(old_dir, guarded) or not _only_video(old_dir, src):
        return None
    return FolderRename(source=str(old_dir), destination=str(new_dir))


def _rename_dir_noclobber(src: Path, dst: Path) -> None:
    """Rename a folder without replacing anything (POSIX ``rename`` silently replaces an empty folder)."""
    if os.path.lexists(dst):
        raise FileExistsError(errno.EEXIST, "Destination already exists", os.fspath(dst))
    with _editing(src, dst):
        os.rename(src, dst)
    _forget_listings(src)
    _forget_listings(dst)


def _rebase(path: Path, old: Path, new: Path) -> Path:
    """``path`` moved along with its ancestor folder ``old`` renamed to ``new``."""
    try:
        return new / path.relative_to(old)
    except ValueError:
        return path


# --------------------------------------------------------------------------- planning (cont.)


def plan_operation(
    source: str | Path,
    destination: str | Path,
    mode: RenameMode,
    policy: ConflictPolicy,
    *,
    sidecar_extensions: list[str] | None = None,
    incoming_rank: int | None = None,
    existing_rank: int | None = None,
    rename_folder: bool = False,
    remove_empty_dirs: bool = False,
    protected_dirs: list[str] | None = None,
) -> FileOpPlan:
    """Pure-ish planning: detect conflicts (exists / same file / case-only rename), compute
    sidecar destinations (keeping language/flag suffixes). No mutation. An equal-sized file at the
    destination counts as "same_file" when its first and last MiB match (cheap, cached);
    :func:`execute` decides with a full compare.

    ``sidecar_extensions=None`` (or empty) means "no sidecars" (``settings.move_sidecars`` off).
    Case-only renames ("movie.mkv" -> "Movie.mkv" on a case-insensitive disk) plan as ``"none"``.
    Missing destination folders that match existing ones ignoring case / Unicode normalisation take
    the existing spelling, so ``FileOpPlan.destination`` can differ from ``destination``.

    ``rename_folder`` (move, or a dry run of one) plans renaming the source's folder in place when
    :func:`folder_rename_target` allows it; ``conflict`` then describes the rename inside the folder.
    ``remove_empty_dirs`` and ``protected_dirs`` (library / watch roots; include the allowed roots
    so planning sees them too) are carried over for :func:`execute`.
    """
    mode = RenameMode(mode)
    policy = ConflictPolicy(policy)
    src = _abs(source)
    dst = _reuse_folder_names(_abs(destination))
    protected = [str(p) for p in (protected_dirs or []) if p and str(p).strip()]
    folder = folder_rename_target(src, dst, protected) if rename_folder and mode in _FOLDER_MODES else None
    sidecars: list[SidecarMove] = []
    if sidecar_extensions:
        for sc in find_sidecars(src, sidecar_extensions):
            sc_dst = _sidecar_destination(dst, sidecar_suffix(src, sc))
            if str(sc_dst) != str(sc):
                sidecars.append(SidecarMove(source=str(sc), destination=str(sc_dst)))
    # Planning never reads whole files: an equal-sized destination is compared by its first and last
    # MiB here; execute() does the one full byte compare.
    if folder is None:
        conflict = _detect_conflict(src, dst, mode, deep=False)
    else:
        # The file only changes its name inside the renamed folder, and the folder moves even when
        # the name is already right.
        conflict = _detect_conflict(src, src.parent / dst.name, RenameMode.MOVE, deep=False)
        conflict = "none" if conflict == "same_file" else conflict
    return FileOpPlan(
        source=str(src),
        destination=str(dst),
        mode=mode,
        policy=policy,
        sidecars=sidecars,
        conflict=conflict,
        incoming_rank=incoming_rank,
        existing_rank=existing_rank,
        folder_rename=folder,
        remove_empty_dirs=remove_empty_dirs,
        protected_dirs=protected,
    )


def existing_destination(destination: str | Path) -> str | None:
    """The file already occupying ``destination`` as :func:`plan_operation` sees it (existing folder
    spellings reused, case / normalisation variants of the name included), or None."""
    entry = _existing_entry(_reuse_folder_names(_abs(destination)))
    return None if entry is None else str(entry)


# --------------------------------------------------------------------------- low-level helpers


def _rename_noclobber(src: Path | str, dst: Path | str) -> None:
    """Rename that never replaces an existing ``dst``.

    Raises FileExistsError when ``dst`` exists and OSError(EXDEV) across filesystems.
    """
    with _editing(src, dst):
        _rename_noclobber_raw(os.fspath(src), os.fspath(dst))


def _rename_noclobber_raw(src: str, dst: str) -> None:
    if IS_WINDOWS:
        os.rename(src, dst)  # Windows refuses to replace an existing file
        return
    if not os.path.islink(src):
        try:
            os.link(src, dst)  # atomic "create if absent"
        except FileExistsError:
            raise
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                raise
            # No hardlinks on this filesystem (SMB, FAT, some FUSE): check, then rename.
        else:
            try:
                os.unlink(src)
            except OSError:
                with contextlib.suppress(OSError):
                    os.unlink(dst)
                raise
            return
    if os.path.lexists(dst):
        raise FileExistsError(errno.EEXIST, "Destination already exists", dst)
    os.rename(src, dst)


def _rename_case_only(src: Path, dst: Path) -> None:
    """Case-insensitive filesystems ignore "a" -> "A" renames; go through an intermediate name."""
    tmp = src.parent / f".{secrets.token_hex(6)}{TEMP_SUFFIX}"
    with _editing(src, tmp, dst):
        os.rename(src, tmp)
        try:
            os.rename(tmp, dst)
        except OSError:
            os.rename(tmp, src)
            raise


def _unlink(path: Path | str) -> None:
    """``os.unlink`` that keeps the folder's cached listing (see :func:`_editing`)."""
    with _editing(path):
        os.unlink(path)


def _fsync_dir(path: Path | str) -> None:
    if IS_WINDOWS:
        return
    with contextlib.suppress(OSError):
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


# Folder entries (renames, new folders) are made durable with an fsync of the folder. On a HDD RAID
# each costs 10-30 ms, minutes over a 50k-file batch, so they are coalesced: a folder is synced at
# most once per DIR_SYNC_INTERVAL, by the next call after that or a timer, and at exit. Points of no
# return (deleting a cross-device source) still sync right away (see _Execution._finalize_unit).
DIR_SYNC_INTERVAL = 1.0
_dirty_dirs: dict[str, None] = {}
_dir_sync_lock = threading.Lock()
_dir_sync_last = float("-inf")
_dir_sync_timer: threading.Timer | None = None


def sync_dirs_later(folders: Iterable[Path | str]) -> None:
    """Have ``folders`` fsynced soon: now if no folder sync ran in the last DIR_SYNC_INTERVAL,
    otherwise together with every other folder changed meanwhile, within DIR_SYNC_INTERVAL."""
    global _dir_sync_timer
    if IS_WINDOWS:
        return
    with _dir_sync_lock:
        for folder in folders:
            _dirty_dirs[os.fspath(folder)] = None
        wait = _dir_sync_last + DIR_SYNC_INTERVAL - time.monotonic()
        if wait > 0:
            if _dir_sync_timer is None:
                timer = threading.Timer(wait, flush_dirs)
                timer.daemon = True
                _dir_sync_timer = timer
                timer.start()
            return
    flush_dirs()


def flush_dirs() -> None:
    """fsync every folder with changes not yet synced (registered to run at exit)."""
    global _dir_sync_last, _dir_sync_timer
    with _dir_sync_lock:
        todo = list(_dirty_dirs)
        _dirty_dirs.clear()
        _dir_sync_last = time.monotonic()
        timer, _dir_sync_timer = _dir_sync_timer, None
    if timer is not None and timer is not threading.current_thread():
        timer.cancel()
    for folder in todo:
        _fsync_dir(folder)


atexit.register(flush_dirs)


def _same_device(path: Path, folder: Path) -> bool:
    try:
        return os.stat(path).st_dev == os.stat(_nearest_existing(folder)).st_dev
    except OSError:
        return False


def _nearest_existing(folder: Path) -> Path:
    current = folder
    while not current.exists():
        if current.parent == current:
            break
        current = current.parent
    return current


def _fmt_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{n} B"


def _describe(exc: BaseException) -> str:
    if isinstance(exc, FileOpError):
        return str(exc)
    if isinstance(exc, OSError):
        reason = {
            errno.EACCES: "permission denied",
            errno.EPERM: "operation not permitted",
            errno.ENOSPC: "no space left on device",
            errno.EROFS: "read-only file system",
            errno.ENAMETOOLONG: "file name too long",
            errno.ENOENT: "no such file or folder",
            errno.EXDEV: "source and destination are on different filesystems",
            errno.EEXIST: "already exists",
            errno.EIO: "I/O error",
        }.get(exc.errno or 0, exc.strerror or str(exc))
        where = f": {exc.filename}" if exc.filename else ""
        if exc.filename2:
            where += f" -> {exc.filename2}"
        return f"{reason[:1].upper()}{reason[1:]}{where}"
    return str(exc)


def _temp_name(target: Path, suffix: str) -> Path:
    name = pathsvc.fit_component(f".{target.name}", reserve=len(suffix.encode()))
    return target.parent / f"{name}{suffix}"


def _remove_stale(path: Path) -> None:
    """Remove a leftover temp entry of ours (named *.schema-tmp / *.schema-link) from a crash."""
    if os.path.islink(path) or os.path.isfile(path):
        _unlink(path)


# Write-back lag for copies: each chunk's dirty pages get their write-back started right after the
# write, and are dropped from the page cache this far behind, once on disk. A 40 GB copy then holds
# a few dozen MB of page cache instead of pushing the container (1.5 GB) into reclaim.
_WRITEBACK_LAG = 4 * COPY_CHUNK
_CLONE_FALLBACK_ERRNOS = frozenset(
    e
    for e in (
        errno.EXDEV, errno.ENOSYS, errno.EINVAL, errno.EBADF, errno.EPERM, errno.ETXTBSY,
        getattr(errno, "EOPNOTSUPP", None), getattr(errno, "ENOTSUP", None),
    )
    if e is not None
)  # fmt: skip


def _open_exclusive(path: Path) -> int:
    """Create ``path`` for writing; fails if anything exists there (like open(..., "xb"))."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags, 0o666)


def _clone(fd_in: int, fd_out: int, size: int) -> bool:
    """In-kernel copy with ``copy_file_range`` (a reflink on btrfs/XFS: instant, no extra space).
    False, with nothing written, when the kernel or filesystem can't (the caller streams instead)."""
    copy_range = getattr(os, "copy_file_range", None)
    if copy_range is None:
        return False
    copied = 0
    try:
        while copied < size:
            n = copy_range(fd_in, fd_out, min(size - copied, 1 << 30), copied, copied)
            if n == 0:
                break  # the source shrank: the size check after the copy reports it
            copied += n
    except OSError as exc:
        if copied == 0 and exc.errno in _CLONE_FALLBACK_ERRNOS:
            return False
        raise
    return True


def _stream(fd_in: int, fd_out: int, *, want_crc: bool) -> int:
    """User-space copy from the current position of ``fd_in``; returns the CRC32 of the data when
    ``want_crc`` (computed on the way, so the source is read once)."""
    crc = 0
    offset = 0
    buf = bytearray(COPY_CHUNK)
    view = memoryview(buf)
    hashing.advise_sequential(fd_in)
    with io.FileIO(fd_in, "rb", closefd=False) as fin, io.FileIO(fd_out, "wb", closefd=False) as fout:
        while n := fin.readinto(view):
            chunk = view[:n]
            written = 0
            while written < n:
                written += fout.write(chunk[written:]) or 0
            if want_crc:
                crc = zlib.crc32(chunk, crc)
            hashing.drop_cache(fd_in, offset, n)  # clean pages: gone now
            hashing.drop_cache(fd_out, offset, n)  # dirty pages: start their write-back
            if offset >= _WRITEBACK_LAG:
                hashing.drop_cache(fd_out, offset - _WRITEBACK_LAG, n)  # written by now: drop them
            offset += n
    return crc


def _copy_data(fd_in: int, tmp: Path, *, want_crc: bool, clone: bool = False) -> str | None:
    """Copy the open source ``fd_in`` into a new ``tmp`` (exclusive create), fsync it, keep its
    timestamps. ``clone`` tries an in-kernel copy first (same filesystem; never when a CRC is
    wanted, since that has to see the data). Returns the source CRC32 when ``want_crc``."""
    st = os.fstat(fd_in)
    fd_out = _open_exclusive(tmp)
    try:
        crc = 0
        if not (clone and not want_crc and _clone(fd_in, fd_out, st.st_size)):
            crc = _stream(fd_in, fd_out, want_crc=want_crc)
        os.fsync(fd_out)
        hashing.drop_cache(fd_out)
    finally:
        os.close(fd_out)
    with contextlib.suppress(OSError):
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
    return hashing.format_crc32(crc) if want_crc else None


def _crc_of(path: Path) -> str:
    """CRC32 of a file read back from disk (its pages were dropped after the fsync)."""
    return hashing.crc32_file(path, chunk_size=COPY_CHUNK)


def _fd_path(fd: int) -> str | None:
    """What an open fd really is (Linux /proc), or None where that can't be asked."""
    try:
        return os.readlink(f"/proc/self/fd/{fd}")
    except (OSError, ValueError, NotImplementedError):
        return None


def _open_source(src: Path, expected: os.stat_result, roots: list[str], *, follow: bool) -> int:
    """Open a file Schema is about to copy, making sure it is still the file that was checked.

    The checks before a copy (inside the allowed roots, not a symlink for a move) look at the path;
    the copy reads whatever the path names when it opens it. So: open without following a final
    symlink (unless a copy of a symlink was checked through it), never blocking on a FIFO, then
    require the same device and inode as ``expected`` (the earlier lstat/stat) and, where Linux can
    tell, a real path still inside the allowed roots."""
    try:
        fd = hashing.open_regular(src, follow=follow)
    except OSError as exc:
        raise FileOpError(f"Refused to copy {src}: it is no longer a regular file ({_describe(exc)})") from exc
    try:
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) != (expected.st_dev, expected.st_ino):
            raise FileOpError(f"{src} was replaced while Schema was working on it; nothing was copied")
        real = _fd_path(fd)
        if real is not None and real.startswith("/") and not pathsvc.is_within(real.removesuffix(" (deleted)"), roots):
            raise FileOpError(f"Refused: {src} leads outside the allowed roots ({real})")
    except BaseException:
        os.close(fd)
        raise
    return fd


# --------------------------------------------------------------------------- cross-mount moves


def _unescape_mount(field: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def _mounts() -> list[tuple[str, str]] | None:
    """(mount id, mount point) of every mount in this process's namespace (Linux), in mount order."""
    try:
        text = Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    mounts: list[tuple[str, str]] = []
    for line in text.splitlines():
        fields = line.split(" ")
        if len(fields) > 4:
            mounts.append((fields[0], _unescape_mount(fields[4])))
    return mounts


def _mount_of(path: str, mounts: list[tuple[str, str]]) -> tuple[str, str] | None:
    """The mount ``path`` (resolved) is on: the longest mount point holding it, the last one mounted
    when several stack on one point."""
    best: tuple[str, str] | None = None
    for mount_id, point in mounts:
        holds = path == point or point == "/" or path.startswith(point.rstrip("/") + "/")
        if holds and (best is None or len(point) >= len(best[1])):
            best = (mount_id, point)
    return best


def cross_mount(source: str | Path, destination: str | Path) -> str | None:
    """Why a rename from the folder ``source`` into ``destination`` would fail with EXDEV (so every
    move between them is a full copy), or None when it wouldn't (or can't be told).

    Different filesystems obviously; and on Linux also two mounts of the *same* filesystem, which is
    what two Docker volumes of one disk (``/data/downloads:/downloads`` and ``/data/media:/media``)
    are: the kernel refuses renames across mount points even then. Nothing is written to test it."""
    a = _nearest_existing(Path(os.path.abspath(os.fspath(source))))
    b = _nearest_existing(Path(os.path.abspath(os.fspath(destination))))
    try:
        sa, sb = os.stat(a), os.stat(b)
    except OSError:
        return None
    if sa.st_dev != sb.st_dev:
        return "they are on different filesystems"
    mounts = _mounts()
    if not mounts:
        return None
    ma, mb = _mount_of(os.path.realpath(a), mounts), _mount_of(os.path.realpath(b), mounts)
    if ma is not None and mb is not None and ma[0] != mb[0]:
        return f"they are separate mounts ({ma[1]} and {mb[1]}) of the same filesystem"
    return None


_exdev_warned: set[tuple[str, str]] = set()


def _warn_exdev(src_dir: Path, dst_dir: Path) -> None:
    """A rename just failed with EXDEV although both sides report the same device (bind mounts,
    mergerfs): say so once per pair of mounts, since every such move becomes a full copy."""
    mounts = _mounts() or []
    a = _mount_of(os.path.realpath(src_dir), mounts)
    b = _mount_of(os.path.realpath(dst_dir), mounts)
    key = (a[1] if a else os.fspath(src_dir), b[1] if b else os.fspath(dst_dir))
    if key in _exdev_warned:
        return
    _exdev_warned.add(key)
    log.warning(
        "Moves from %s to %s are full copies: the system refuses to rename across them (separate "
        "mounts of one disk?). Mount one common parent folder into the container so moves are instant.",
        key[0], key[1],
    )


def _check_free_space(folder: Path, needed: int) -> None:
    if needed <= 0:
        return
    target = _nearest_existing(folder)
    try:
        free = shutil.disk_usage(target).free
    except OSError:
        return  # unknown (some network filesystems); the copy itself will fail cleanly if full
    if free < needed + FREE_SPACE_MARGIN:
        raise FileOpError(
            f"Not enough free space on {target}: need {_fmt_bytes(needed)}, {_fmt_bytes(free)} available"
        )


def _symlink_error(exc: OSError, path: Path) -> FileOpError:
    if IS_WINDOWS and getattr(exc, "winerror", None) == 1314:
        return FileOpError(
            f"Can't create a symlink at {path}: Windows needs Developer Mode or admin rights for symlinks"
        )
    return FileOpError(f"Can't create a symlink at {path}: {_describe(exc)}")


def _model_has(field_name: str) -> bool:
    return field_name in FileOpResult.model_fields


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


# --------------------------------------------------------------------------- transaction


class _Txn:
    """Ordered list of compensating actions, run in reverse on failure."""

    def __init__(self) -> None:
        self._actions: list[tuple[str, Callable[[], None]]] = []

    def add(self, label: str, action: Callable[[], None]) -> None:
        self._actions.append((label, action))

    def commit(self) -> None:
        """Point of no return: forget compensations (nothing before this can be reverted safely)."""
        self._actions.clear()

    def rollback(self) -> list[str]:
        failures: list[str] = []
        while self._actions:
            label, action = self._actions.pop()
            try:
                action()
            except Exception as exc:  # keep rolling back the rest
                log.error("rollback step failed: %s: %s", label, exc)
                failures.append(f"{label}: {_describe(exc)}")
        return failures


def _rmdir(folder: Path) -> None:
    """``os.rmdir`` that keeps the parent's cached listing (see :func:`_editing`)."""
    with _editing(folder):
        os.rmdir(folder)
    _forget_listings(folder)


def _remove_dirs_if_empty(dirs: list[Path]) -> None:
    for d in reversed(dirs):
        try:
            _rmdir(d)
        except OSError:
            break


def _makedirs_tracked(folder: Path) -> list[Path]:
    """mkdir -p, returning the directories actually created (outermost first)."""
    missing: list[Path] = []
    current = folder
    while not os.path.isdir(current):
        if os.path.lexists(current):
            raise FileOpError(f"Can't create folder {folder}: {current} exists and is not a folder")
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    missing.reverse()
    created: list[Path] = []
    try:
        for d in missing:
            try:
                with _editing(d):  # a new movie folder must not cost a re-scan of a 5000-folder root
                    os.mkdir(d)
            except FileExistsError:
                if not os.path.isdir(d):
                    raise
                continue
            created.append(d)
    except OSError:
        _remove_dirs_if_empty(created)
        raise
    return created


# --------------------------------------------------------------------------- trash


class _Trash:
    """Moves displaced files aside (same filesystem) and records them in a manifest."""

    def __init__(self, trash_dir: Path | None, roots: list[str]) -> None:
        self.trash_dir = Path(trash_dir) if trash_dir else None
        self.roots = roots
        now = datetime.now()
        self.day = now.strftime("%Y-%m-%d")
        self.slot = f"{now.strftime('%H%M%S')}-{secrets.token_hex(3)}"
        self.items: list[tuple[Path, Path]] = []  # (original, trashed)

    def _bases(self, path: Path) -> list[Path]:
        bases: list[Path] = []
        if self.trash_dir is not None:
            bases.append(self.trash_dir)
        root = pathsvc.containing_root(path.parent, self.roots)
        if root:
            bases.append(Path(root) / TRASH_DIRNAME)
        bases.append(path.parent / TRASH_DIRNAME)
        unique: list[Path] = []
        for b in bases:
            if all(os.path.normcase(str(b)) != os.path.normcase(str(u)) for u in unique):
                unique.append(b)
        return unique

    def displace(self, path: Path, txn: _Txn) -> Path:
        last: BaseException | None = None
        for base in self._bases(path):
            op_dir = base / self.day / self.slot
            try:
                created = _makedirs_tracked(op_dir)
            except (OSError, FileOpError) as exc:
                last = exc
                continue
            target = _unique_child(op_dir, path.name)
            try:
                _rename_noclobber(path, target)
            except OSError as exc:
                _remove_dirs_if_empty(created)
                last = exc
                continue
            self.items.append((path, target))
            txn.add(f"restore {path} from trash", lambda t=target, p=path: _rename_noclobber(t, p))
            log.info("displaced %s -> %s", path, target)
            return target
        raise FileOpError(
            f"Couldn't move the existing file {path} aside into a trash folder on the same disk"
            + (f" ({_describe(last)})" if last else "")
        )

    def commit(self, main_destination: Path) -> str | None:
        if not self.items:
            return None
        by_dir: dict[Path, list[tuple[Path, Path]]] = {}
        for original, trashed in self.items:
            by_dir.setdefault(trashed.parent, []).append((original, trashed))
        for folder, entries in by_dir.items():
            manifest = {
                "version": 1,
                "created": datetime.now().isoformat(timespec="seconds"),
                "items": [{"original": str(o), "trashed": str(t)} for o, t in entries],
            }
            try:
                (folder / TRASH_MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            except OSError as exc:
                log.warning("could not write trash manifest in %s: %s", folder, exc)
        for original, trashed in self.items:
            if os.path.normcase(str(original)) == os.path.normcase(str(main_destination)):
                return str(trashed)
        return str(self.items[0][1])


def _unique_child(folder: Path, name: str) -> Path:
    candidate = folder / name
    n = 1
    stem, suffix = Path(name).stem, Path(name).suffix
    while os.path.lexists(candidate):
        candidate = folder / f"{stem} ({n}){suffix}"
        n += 1
    return candidate


def _restore_from_trash(
    replaced: str, destination: Path, roots: list[str], renamed: tuple[Path, Path] | None = None
) -> list[str]:
    """Put displaced files back. Returns human notes for anything that could not be restored.

    ``renamed`` = (folder, original name): undo renamed that folder back, so files displaced inside
    it return under its original name.
    """
    notes: list[str] = []
    trashed_main = Path(replaced)
    manifest_path = trashed_main.parent / TRASH_MANIFEST
    entries: list[tuple[Path, Path]] = []
    if manifest_path.is_file():
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            entries = [(Path(i["original"]), Path(i["trashed"])) for i in data.get("items", [])]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("unreadable trash manifest %s: %s", manifest_path, exc)
    if not entries:
        entries = [(destination, trashed_main)]
    # The manifest can sit on a user-writable share: only trust entries that are plain files directly
    # inside this trash slot, so an edited manifest (or a symlinked slot) can't make undo move
    # arbitrary files such as /config/schema.db into the library.
    slot = trashed_main.parent
    slot_key = os.path.normcase(os.path.abspath(slot))
    slot_is_link = os.path.islink(slot)
    for original, trashed in entries:
        if renamed is not None:
            original = _rebase(original, *renamed)
        if slot_is_link or os.path.normcase(os.path.abspath(trashed.parent)) != slot_key or os.path.islink(trashed):
            notes.append(f"Not restoring {trashed}: it is not a file inside the trash folder {slot}")
            continue
        if not os.path.lexists(trashed):
            notes.append(f"The replaced file is no longer in the trash: {trashed}")
            continue
        if not pathsvc.is_within_nofollow(original, roots):
            notes.append(f"Not restoring {trashed}: {original} is outside the allowed roots")
            continue
        if os.path.lexists(original):
            notes.append(f"Not restoring {trashed}: {original} is occupied")
            continue
        try:
            _rename_noclobber(trashed, original)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                notes.append(f"Couldn't restore {original} from {trashed}: {_describe(exc)}")
                continue
            try:
                _copy_back(trashed, original)
            except (OSError, FileOpError) as exc2:
                notes.append(f"Couldn't restore {original} from {trashed}: {_describe(exc2)}")
                continue
    if not notes:
        with contextlib.suppress(OSError):
            if manifest_path.is_file():
                manifest_path.unlink()
        with contextlib.suppress(OSError):
            trashed_main.parent.rmdir()
            trashed_main.parent.parent.rmdir()
    return notes


def _copy_back(src: Path, dst: Path) -> None:
    """Cross-device move used only for trash restores: copy, verify, rename, delete."""
    fd = hashing.open_regular(src, follow=False)
    try:
        size = os.fstat(fd).st_size
        _check_free_space(dst.parent, size)
        tmp = _temp_name(dst, TEMP_SUFFIX)
        _remove_stale(tmp)
        try:
            with _editing(tmp):
                crc = _copy_data(fd, tmp, want_crc=True)
        except BaseException:
            with contextlib.suppress(OSError):
                _unlink(tmp)
            raise
    finally:
        os.close(fd)
    try:
        if os.stat(tmp).st_size != size or _crc_of(tmp) != crc:
            raise FileOpError(f"Verification failed while restoring {dst}")
        _rename_noclobber(tmp, dst)
    except BaseException:
        with contextlib.suppress(OSError):
            _unlink(tmp)
        raise
    _fsync_dir(dst.parent)  # the restored entry is on disk before the only other copy goes
    _unlink(src)


# --------------------------------------------------------------------------- execution


@dataclass
class _Unit:
    src: Path
    dst: Path
    main: bool
    size: int = 0
    cross_device: bool = False
    staged: Path | None = None  # verified temp copy waiting in the destination folder
    link_tmp: Path | None = None  # keeplink: symlink prepared next to the source
    dropped: bool = False
    # What a copy must find when it opens the source: the stat taken when it was checked (lstat, or
    # stat for a symlink copied through), and whether to follow a final symlink.
    expected: os.stat_result | None = None
    follow: bool = False


@dataclass
class _Outcome:
    destination: Path
    units: list[_Unit] = field(default_factory=list)
    replaced: str | None = None
    verified: bool = False
    size: int = 0
    dest_mtime_ns: int | None = None
    already_done: bool = False  # destination already held this file (same inode or identical bytes)
    folder_rename: FolderRename | None = None
    removed_dirs: list[str] = field(default_factory=list)


def _refuse_data_dir(path: Path, what: str, *, follow: bool) -> None:
    """Hard deny, whatever the allowed roots say: nothing in Schema's own data folder (secret.key,
    schema.db) is ever a source, destination or undo target (``paths.protect``)."""
    if pathsvc.is_protected_path(path, follow=follow):
        raise FileOpError(f"Refused: the {what} {path} is inside Schema's data folder")


class _Execution:
    """One plan, executed step by step with compensations recorded in a :class:`_Txn`."""

    def __init__(self, plan: FileOpPlan, roots: list[str], trash_dir: Path | None) -> None:
        self.plan = plan
        self.mode = RenameMode(plan.mode)
        self.policy = ConflictPolicy(plan.policy)
        self.src = _abs(plan.source)
        self.dst = _abs(plan.destination)
        self.roots = [r for r in (roots or []) if r and str(r).strip()]
        self.guarded = [*self.roots, *plan.protected_dirs]  # never renamed or removed
        self.trash_dir = trash_dir
        self.trash = _Trash(trash_dir, self.roots)
        self.notes: list[str] = []
        self.irreversible = False

    # -- checks ------------------------------------------------------------------------------

    def _inside(self, path: Path, what: str, *, follow: bool) -> None:
        _refuse_data_dir(path, what, follow=follow)
        ok = pathsvc.is_within(path, self.roots) if follow else pathsvc.is_within_nofollow(path, self.roots)
        if not ok:
            raise FileOpError(f"Refused: the {what} {path} is outside the allowed roots")

    def _sidecar_units(self) -> list[_Unit]:
        units: list[_Unit] = []
        for sc in self.plan.sidecars:
            s, d = _abs(sc.source), _abs(sc.destination)
            if not os.path.isfile(s):
                continue  # already moved, or deleted since planning
            self._inside(s, "sidecar", follow=True)
            units.append(_Unit(src=s, dst=d, main=False))
        return units

    def _index_destination(self, dst: Path, sidecars: list[_Unit]) -> Path:
        stem, ext = dst.stem, dst.suffix
        suffixes = [sidecar_suffix(self.dst, u.dst) for u in sidecars]
        listing = _listing(dst.parent)  # one listing for every candidate: case variants are taken too

        def taken(path: Path) -> bool:
            return os.path.lexists(path) or (listing is not None and bool(listing.variants(path.name)))

        for n in range(1, MAX_INDEX + 1):
            marker = f" ({n})"
            base = pathsvc.fit_component(stem + ext, reserve=len(marker.encode()))
            base_stem = base[: len(base) - len(ext)] if ext and base.endswith(ext) else Path(base).stem
            candidate = dst.parent / f"{base_stem}{marker}{ext}"
            if taken(candidate):
                continue
            if any(taken(_sidecar_destination(candidate, s)) for s in suffixes):
                continue
            return candidate
        raise FileOpError(f"No free index name for {dst} (tried {MAX_INDEX})")

    # -- main flow ---------------------------------------------------------------------------

    def run(self, txn: _Txn) -> _Outcome:
        mode, src = self.mode, self.src
        if not Path(self.plan.source).is_absolute() or not Path(self.plan.destination).is_absolute():
            raise FileOpError("Source and destination must be absolute paths")
        if not self.roots:
            raise FileOpError(_NO_ROOTS)
        self._inside(src, "source", follow=True)
        self._inside(self.dst, "destination", follow=False)
        if not os.path.lexists(src):
            raise SourceMissingError(f"Source file no longer exists: {src}")
        if os.path.isdir(src):
            raise FileOpError(f"Source is a folder, not a file: {src}")
        if os.path.islink(src) and mode is RenameMode.KEEPLINK:
            raise FileOpError(f"Source is already a symlink (linked earlier?): {src}")
        folder = self._folder_rename()
        if folder is not None:
            outcome = self._in_renamed_folder(*folder, txn)
            if outcome is not None:
                return outcome
        outcome = self._transfer(txn)
        if self.plan.remove_empty_dirs and mode is RenameMode.MOVE and not outcome.already_done:
            outcome.removed_dirs = self._remove_empty_dirs()
        return outcome

    def _folder_rename(self) -> tuple[Path, Path] | None:
        """(old, new) folder of the planned in-place rename, re-checked now; None: plain move."""
        planned = self.plan.folder_rename
        if planned is None or self.mode not in _FOLDER_MODES:
            return None
        current = folder_rename_target(self.src, self.dst, self.guarded)
        if current is None or (current.source, current.destination) != (planned.source, planned.destination):
            self.notes.append(
                f"The folder {Path(planned.source).name} can no longer be renamed in place (its contents or "
                "the destination changed since planning); the file was moved instead"
            )
            return None
        old_dir, new_dir = _abs(planned.source), _abs(planned.destination)
        self._inside(old_dir, "folder", follow=True)
        self._inside(new_dir, "destination folder", follow=False)
        return old_dir, new_dir

    def _in_renamed_folder(self, old_dir: Path, new_dir: Path, txn: _Txn) -> _Outcome | None:
        """Rename the source's folder, then the file and its sidecars inside it (a dry run plans the
        inner rename where the files are now). None when the folder can't be renamed: the caller
        falls back to a plain move."""
        if self.mode is RenameMode.TEST:
            if not os.access(old_dir.parent, os.W_OK):
                self.notes.append(
                    f"The folder {old_dir.name} can't be renamed ({old_dir.parent} is not writable); "
                    "the file would be moved instead"
                )
                return None
            inside = old_dir
        else:
            try:
                _rename_dir_noclobber(old_dir, new_dir)
            except OSError as exc:
                self.notes.append(f"Couldn't rename the folder {old_dir.name} ({_describe(exc)}); moved the file instead")
                return None
            txn.add(f"rename folder {new_dir} back", lambda: _rename_dir_noclobber(new_dir, old_dir))
            log.info("renamed folder %s -> %s", old_dir, new_dir)
            inside = new_dir
        inner = _Execution(
            self.plan.model_copy(
                update={
                    "source": str(inside / self.src.name),
                    "destination": str(inside / self.dst.name),
                    "sidecars": [
                        SidecarMove(source=str(inside / Path(s.source).name), destination=str(inside / Path(s.destination).name))
                        for s in self.plan.sidecars
                        if _same_path(Path(s.source).parent, old_dir)
                    ],
                    "folder_rename": None,
                    "remove_empty_dirs": False,
                }
            ),
            self.roots,
            self.trash_dir,
        )
        outcome = inner.run(txn)
        self.notes.extend(inner.notes)
        # Report sources where they were and destinations where they are now (or would be).
        for u in outcome.units:
            u.src = _rebase(u.src, inside, old_dir)
            u.dst = _rebase(u.dst, inside, new_dir)
        outcome.destination = _rebase(outcome.destination, inside, new_dir)
        outcome.folder_rename = FolderRename(source=str(old_dir), destination=str(new_dir))
        if outcome.already_done:  # the file already had its new name: only the folder moved
            outcome.already_done = False
            with contextlib.suppress(OSError):
                outcome.dest_mtime_ns = os.stat(outcome.destination).st_mtime_ns
        return outcome

    def _remove_empty_dirs(self) -> list[str]:
        """After a move: remove the source folder if the move left it empty, then each parent that is
        now empty, stopping at the first protected folder (:func:`_is_protected`). ``rmdir`` never
        removes a folder that still holds anything, hidden files included."""
        removed: list[str] = []
        folder = self.src.parent
        while (
            folder.parent != folder
            and not os.path.islink(folder)
            and pathsvc.is_within(folder, self.roots)
            and not _is_protected(folder, self.guarded)
        ):
            try:
                _rmdir(folder)
            except OSError:
                break
            log.info("removed empty folder %s", folder)
            removed.append(str(folder))
            folder = folder.parent
        return removed

    def _transfer(self, txn: _Txn) -> _Outcome:
        """The plain rename / move / copy / link of the file and its sidecars."""
        mode, src = self.mode, self.src
        size = os.stat(src).st_size
        sidecars = self._sidecar_units()

        dst = self.dst
        displace: list[Path] = []
        conflict, existing = _conflict_with(src, dst, mode)
        if conflict == "same_file":
            # A case / normalisation variant holding this file is where the file already is.
            done = existing or dst
            if str(src) != str(done) and not _samefile(src, done):
                self.notes.append(f"An identical copy already exists at {done}; the source was left in place")
            return _Outcome(destination=done, verified=True, size=size, already_done=True)
        if conflict == "exists":
            occupant = existing or dst
            if os.path.isdir(occupant) and not os.path.islink(occupant):
                raise DestinationExistsError(f"A folder already exists at the destination: {occupant}")
            decision = self._resolve_conflict(occupant)
            if decision == "replace":
                displace.append(occupant)  # the variant's name goes too, not just its content
            elif decision == "index":
                dst = self._index_destination(dst, sidecars)
        if dst != self.dst:
            for u in sidecars:
                u.dst = _sidecar_destination(dst, sidecar_suffix(self.dst, u.dst))
        main = _Unit(src=src, dst=dst, main=True, size=size)
        units = [main, *self._sidecar_conflicts(sidecars, displace, replacing=bool(displace))]
        for u in units:
            self._inside(u.dst, "destination", follow=False)

        if mode is RenameMode.TEST:
            return self._dry_run(units, displace)

        if mode in _MOVE_MODES:
            for folder in {u.src.parent for u in units}:
                if not os.access(folder, os.W_OK):
                    raise FileOpError(
                        f"Schema can't remove files from {folder} (permission denied), so a move would "
                        "leave the original behind. Fix the folder permissions or use copy mode."
                    )
        for folder in sorted({u.dst.parent for u in units}, key=lambda p: len(str(p))):
            created = _makedirs_tracked(folder)
            if created:
                txn.add(f"remove new folders under {created[0]}", lambda c=created: _remove_dirs_if_empty(c))
        for u in units:
            u.size = size if u.main else os.stat(u.src).st_size
            if mode in _MOVE_MODES:
                u.cross_device = not _same_device(u.src, u.dst.parent)
        to_copy = [u for u in units if mode is RenameMode.COPY or (mode in _MOVE_MODES and u.cross_device)]
        for u in to_copy:
            self._pin_source(u)
        _check_free_space(dst.parent, sum(u.size for u in to_copy))

        try:
            for u in to_copy:
                self._stage(u, verify_hash=mode is not RenameMode.COPY)
            if mode is RenameMode.KEEPLINK:
                for u in units:
                    self._prepare_link(u)
            for path in displace:
                self.trash.displace(path, txn)
            for u in units:
                self._place(u, txn)
            self._finalize(units, txn)
        finally:
            self._cleanup(units)

        verified = self._verify(units)
        dest_mtime: int | None = None
        with contextlib.suppress(OSError):
            dest_mtime = os.stat(dst).st_mtime_ns
        sync_dirs_later({u.dst.parent for u in units})
        return _Outcome(
            destination=dst,
            units=[u for u in units if not u.main and not u.dropped],
            replaced=self.trash.commit(dst),
            verified=verified,
            size=size,
            dest_mtime_ns=dest_mtime,
        )

    def _resolve_conflict(self, dst: Path) -> str:
        policy, plan = self.policy, self.plan
        if policy is ConflictPolicy.SKIP:
            raise _Skip(f"Destination already exists: {dst}")
        if policy is ConflictPolicy.FAIL:
            raise DestinationExistsError(f"Destination already exists: {dst} (conflict policy: fail)")
        if policy is ConflictPolicy.UPGRADE:
            if plan.incoming_rank is None or plan.existing_rank is None:
                raise _Skip(f"Destination already exists and its quality can't be compared: {dst}")
            if plan.incoming_rank < plan.existing_rank:
                raise _Skip(
                    f"Destination already exists with better quality "
                    f"(rank {plan.existing_rank} > {plan.incoming_rank}): {dst}"
                )
            if plan.incoming_rank == plan.existing_rank:
                # Same quality rank: like FileBot's last tiebreak, the larger file wins.
                incoming, existing = os.stat(self.src).st_size, os.stat(dst).st_size
                if incoming <= existing:
                    raise _Skip(
                        f"Destination already exists with the same quality rank ({plan.existing_rank}) and is "
                        f"not smaller ({_fmt_bytes(existing)} vs {_fmt_bytes(incoming)}): {dst}"
                    )
                self.notes.append(
                    f"Same quality rank ({plan.existing_rank}); replaced the existing file because the new one "
                    f"is larger ({_fmt_bytes(incoming)} vs {_fmt_bytes(existing)})"
                )
            return "replace"
        if policy is ConflictPolicy.OVERWRITE:
            return "replace"
        return "index"

    def _sidecar_conflicts(
        self, sidecars: list[_Unit], displace: list[Path], *, replacing: bool
    ) -> list[_Unit]:
        kept: list[_Unit] = []
        for u in sidecars:
            if str(u.src) == str(u.dst):
                continue
            conflict, existing = _conflict_with(u.src, u.dst, self.mode)
            if conflict == "same_file":
                continue
            if conflict == "exists":
                occupant = existing or u.dst
                if os.path.isdir(occupant) and not os.path.islink(occupant):
                    self.notes.append(f"Sidecar {u.src.name} stayed: a folder exists at {occupant}")
                    continue
                if self.policy is ConflictPolicy.OVERWRITE or (
                    self.policy is ConflictPolicy.UPGRADE and replacing
                ):
                    displace.append(occupant)
                elif self.policy is ConflictPolicy.FAIL:
                    raise DestinationExistsError(f"Sidecar destination already exists: {u.dst} (conflict policy: fail)")
                else:
                    self.notes.append(f"Sidecar {u.src.name} stayed: {u.dst.name} already exists")
                    continue
            kept.append(u)
        return kept

    def _dry_run(self, units: list[_Unit], displace: list[Path]) -> _Outcome:
        """RenameMode.TEST: boundaries and conflicts were resolved above; check the destination
        folder can be written and report what would happen. Nothing on disk changes."""
        main = units[0]
        anchor = _nearest_existing(main.dst.parent)
        if not anchor.is_dir() or not os.access(anchor, os.W_OK):
            raise FileOpError(f"Destination folder is not writable: {anchor}")
        return _Outcome(destination=main.dst, units=units[1:], verified=False, size=main.size)

    # -- steps ----------------------------------------------------------------------------------

    def _pin_source(self, u: _Unit) -> None:
        """Before copying ``u.src``: it must be a regular file (a symlink only in copy mode, where its
        target was checked against the roots), and the copy will insist on this very inode."""
        st = os.lstat(u.src)
        u.follow = False
        if stat.S_ISLNK(st.st_mode):
            if self.mode is not RenameMode.COPY:
                raise FileOpError(f"Source is a symlink on another filesystem; refusing to move it: {u.src}")
            st = os.stat(u.src)
            u.follow = True
        if not stat.S_ISREG(st.st_mode):
            raise FileOpError(f"Source is not a regular file; refusing to copy it: {u.src}")
        u.expected = st

    def _stage(self, u: _Unit, *, verify_hash: bool) -> None:
        """Copy ``u.src`` to a temp file next to ``u.dst``, fsync it and verify it: size, and for a
        move the CRC of the data read back from disk against the CRC computed while copying."""
        if u.expected is None:
            self._pin_source(u)
        assert u.expected is not None
        fd = _open_source(u.src, u.expected, self.roots, follow=u.follow)
        try:
            tmp = _temp_name(u.dst, TEMP_SUFFIX)
            _remove_stale(tmp)
            u.staged = tmp
            clone = self.mode is RenameMode.COPY and _same_device(u.src, u.dst.parent)
            with _editing(tmp):
                crc = _copy_data(fd, tmp, want_crc=verify_hash, clone=clone)
        finally:
            os.close(fd)
        copied = os.stat(tmp).st_size
        if copied != u.size:
            raise FileOpError(
                f"Copy of {u.src.name} is incomplete ({_fmt_bytes(copied)} of {_fmt_bytes(u.size)})"
            )
        if verify_hash and _crc_of(tmp) != crc:
            raise FileOpError(f"Checksum mismatch after copying {u.src.name}; the copy was discarded")

    def _prepare_link(self, u: _Unit) -> None:
        link_tmp = _temp_name(u.src, LINK_SUFFIX)
        _remove_stale(link_tmp)
        try:
            with _editing(link_tmp):
                os.symlink(os.fspath(u.dst), os.fspath(link_tmp))
        except OSError as exc:
            raise _symlink_error(exc, u.src) from exc
        u.link_tmp = link_tmp

    def _place(self, u: _Unit, txn: _Txn) -> None:
        mode = self.mode
        if u.staged is not None:
            _rename_noclobber(u.staged, u.dst)
            u.staged = None
            txn.add(f"remove copy {u.dst}", lambda d=u.dst: _unlink_quiet(d))
            return
        if mode in _MOVE_MODES:
            if _is_case_only(u.src, u.dst):
                _rename_case_only(u.src, u.dst)
                txn.add(f"rename back {u.dst}", lambda s=u.src, d=u.dst: _rename_case_only(d, s))
                return
            try:
                _rename_noclobber(u.src, u.dst)
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
                # Same st_dev but the kernel says otherwise (mergerfs, bind mounts): copy instead.
                _warn_exdev(u.src.parent, u.dst.parent)
                u.cross_device = True
                self._pin_source(u)
                _check_free_space(u.dst.parent, u.size)
                self._stage(u, verify_hash=True)
                self._place(u, txn)
                return
            txn.add(f"move back {u.dst}", lambda s=u.src, d=u.dst: _rename_noclobber(d, s))
            return
        if mode is RenameMode.HARDLINK:
            try:
                with _editing(u.dst):
                    os.link(u.src, u.dst)
            except OSError as exc:
                if exc.errno == errno.EXDEV:
                    raise FileOpError(
                        f"Can't hardlink across filesystems: {u.src} and {u.dst.parent} are on different "
                        "disks or shares. Use copy or move, or keep downloads and the library on one filesystem."
                    ) from exc
                if exc.errno in {
                    errno.EPERM,
                    getattr(errno, "ENOTSUP", -1),
                    getattr(errno, "EOPNOTSUPP", -1),
                }:
                    raise FileOpError(
                        f"Hardlinks aren't possible here ({_describe(exc)}): the filesystem of {u.dst.parent} "
                        "doesn't support them, or the file belongs to another user (Linux protected_hardlinks)"
                    ) from exc
                raise
            txn.add(f"remove hardlink {u.dst}", lambda d=u.dst: _unlink_quiet(d))
            return
        if mode is RenameMode.SYMLINK:
            try:
                with _editing(u.dst):
                    os.symlink(os.fspath(u.src), os.fspath(u.dst))
            except OSError as exc:
                raise _symlink_error(exc, u.dst) from exc
            txn.add(f"remove symlink {u.dst}", lambda d=u.dst: _unlink_quiet(d))
            return
        raise FileOpError(f"Unsupported mode: {mode}")

    def _finalize(self, units: list[_Unit], txn: _Txn) -> None:
        """Remove cross-device sources (move) or swap sources for symlinks (keeplink)."""
        if self.mode not in _MOVE_MODES:
            return
        for u in units:  # main first
            try:
                self._finalize_unit(u, txn)
            except (OSError, FileOpError) as exc:
                if u.main or not self.irreversible:
                    if u.main and u.cross_device:
                        raise FileOpError(
                            f"Copied to the destination but couldn't remove the original ({_describe(exc)}); "
                            "the copy was removed again"
                        ) from exc
                    raise
                self._drop(u, exc)

    def _finalize_unit(self, u: _Unit, txn: _Txn) -> None:
        # Deleting a cross-device source is the point of no return: the copy's folder entry must be
        # on disk first (the coalesced folder syncs would leave a window where a power cut loses both).
        if self.mode is RenameMode.MOVE:
            if u.cross_device:
                _fsync_dir(u.dst.parent)
                _unlink(u.src)
                self._point_of_no_return(txn)
            return
        # KEEPLINK
        assert u.link_tmp is not None
        if u.cross_device:
            _fsync_dir(u.dst.parent)
            with _editing(u.link_tmp, u.src):
                os.replace(u.link_tmp, u.src)  # atomically swap the original for a symlink
            u.link_tmp = None
            self._point_of_no_return(txn)
        else:
            _rename_noclobber(u.link_tmp, u.src)
            u.link_tmp = None
            txn.add(f"remove symlink {u.src}", lambda s=u.src: _unlink_quiet(s))

    def _point_of_no_return(self, txn: _Txn) -> None:
        if not self.irreversible:
            self.irreversible = True
            txn.commit()

    def _drop(self, u: _Unit, exc: BaseException) -> None:
        """A sidecar failed after the main file passed the point of no return: put it back."""
        try:
            if u.cross_device:
                if os.path.isfile(u.src):
                    _unlink_quiet(u.dst)
            elif os.path.lexists(u.dst) and not os.path.lexists(u.src):
                _rename_noclobber(u.dst, u.src)
        except OSError as exc2:
            self.notes.append(f"Sidecar {u.src.name} may be in both places: {_describe(exc2)}")
        u.dropped = True
        self.notes.append(f"Sidecar {u.src.name} stayed in place: {_describe(exc)}")

    def _cleanup(self, units: list[_Unit]) -> None:
        for u in units:
            for attr in ("staged", "link_tmp"):
                tmp = getattr(u, attr)
                if tmp is not None:
                    with contextlib.suppress(OSError):
                        _unlink(tmp)
                    setattr(u, attr, None)

    def _verify(self, units: list[_Unit]) -> bool:
        ok = True
        for u in units:
            if u.dropped:
                continue
            try:
                if self.mode is RenameMode.HARDLINK:
                    good = _samefile(u.src, u.dst)
                elif self.mode is RenameMode.SYMLINK:
                    good = os.path.islink(u.dst) and _samefile(u.dst, u.src)
                else:
                    good = os.stat(u.dst).st_size == u.size
                    if self.mode is RenameMode.KEEPLINK:
                        good = good and os.path.islink(u.src)
            except OSError:
                good = False
            if not good:
                ok = False
                self.notes.append(f"Post-check failed for {u.dst}")
        return ok


class _Skip(Exception):
    """Conflict handled by leaving everything untouched (policy skip / no upgrade)."""


def _unlink_quiet(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        _unlink(path)


def _result_kwargs(**kwargs: object) -> dict[str, object]:
    """Drop fields the current contract doesn't define (forward-compatible extras)."""
    return {k: v for k, v in kwargs.items() if k in FileOpResult.model_fields}


def _notes_kwargs(notes: list[str]) -> dict[str, object]:
    """Non-fatal notes on a successful result. They go to ``warnings`` once the contract has that
    field; until then they ride in ``error`` (``ok`` stays True, so callers can tell them apart)."""
    if _model_has("warnings"):
        return {"warnings": list(notes)}
    return {"error": "; ".join(notes) if notes else None}


def execute(plan: FileOpPlan, *, allowed_roots: list[str], trash_dir: Path | None = None) -> FileOpResult:
    """Carry out a plan. Guarantees: never leaves a partial destination (copy to temp name in the
    destination folder, verify size, then atomic rename); cross-device moves are copy+verify+delete;
    refuses paths outside ``allowed_roots``; displaced files go to ``trash_dir`` instead of being
    deleted. RenameMode.TEST validates and returns ok without touching disk.

    Outcome flags: ``ok`` and not ``skipped``: done. ``ok`` and ``skipped``: already in place
    (same inode, or byte-identical content at the destination). ``skipped`` and not ``ok``: conflict
    left untouched (policy skip / no upgrade). Neither: failed (``error`` explains; anything done
    so far was rolled back). On success ``error`` may carry non-fatal notes (a sidecar that stayed
    behind, an upgrade decided by file size) until ``FileOpResult`` gains a ``warnings`` field.
    """
    started = time.monotonic()
    mode = RenameMode(plan.mode)
    run = _Execution(plan, allowed_roots, Path(trash_dir) if trash_dir else None)
    txn = _Txn()
    try:
        outcome = run.run(txn)
    except _Skip as skip:
        txn.rollback()  # e.g. a folder renamed in place before the conflict inside it was found
        return FileOpResult(
            ok=False,
            mode=mode,
            source=plan.source,
            destination=plan.destination,
            skipped=True,
            error=str(skip),
            error_code=DestinationExistsError.code,
            duration_ms=_elapsed_ms(started),
        )
    except (FileOpError, OSError) as exc:
        failures = txn.rollback()
        message = _describe(exc)
        if failures:
            message += ". Rollback incomplete: " + "; ".join(failures)
        log.warning("file operation failed: %s -> %s: %s", plan.source, plan.destination, message)
        return FileOpResult(
            ok=False,
            mode=mode,
            source=plan.source,
            destination=plan.destination,
            error=message,
            error_code=_error_code(exc),
            duration_ms=_elapsed_ms(started),
        )
    for note in run.notes:
        log.warning("%s", note)
    return FileOpResult(
        ok=True,
        mode=mode,
        source=str(run.src),
        destination=str(outcome.destination),
        sidecars=[SidecarMove(source=str(u.src), destination=str(u.dst)) for u in outcome.units],
        bytes=outcome.size,
        verified=outcome.verified,
        skipped=outcome.already_done,
        replaced=outcome.replaced,
        folder_rename=outcome.folder_rename,
        removed_dirs=outcome.removed_dirs,
        duration_ms=_elapsed_ms(started),
        **_notes_kwargs(run.notes),
        **_result_kwargs(dest_mtime_ns=outcome.dest_mtime_ns),
    )


# --------------------------------------------------------------------------- undo


def undo(result: FileOpResult, *, allowed_roots: list[str]) -> FileOpResult:
    """Reverse a completed operation (move back / delete copy or link), including sidecars.
    Refuses if the destination changed since (size/mtime mismatch) or the source path is occupied."""
    started = time.monotonic()
    mode = RenameMode(result.mode)
    base = {"mode": mode, "source": result.destination, "destination": result.source}
    if mode is RenameMode.TEST or (result.ok and result.skipped):
        return FileOpResult(ok=True, skipped=True, duration_ms=_elapsed_ms(started), **base)
    if not result.ok:
        return FileOpResult(
            ok=False,
            error="This operation didn't complete, so there is nothing to undo.",
            duration_ms=_elapsed_ms(started),
            **base,
        )
    roots = [r for r in (allowed_roots or []) if r and str(r).strip()]
    txn = _Txn()
    notes: list[str] = []
    try:
        moved, verified = _undo(result, roots, txn, notes)
    except (FileOpError, OSError) as exc:
        failures = txn.rollback()
        message = (
            "Undo refused: " + _describe(exc)
            if isinstance(exc, FileOpError)
            else "Undo failed: " + _describe(exc)
        )
        if failures:
            message += ". Rollback incomplete: " + "; ".join(failures)
        return FileOpResult(ok=False, error=message, duration_ms=_elapsed_ms(started), **base)
    if result.replaced:
        folder = result.folder_rename
        renamed = (_abs(folder.destination), _abs(folder.source)) if folder is not None else None
        notes.extend(_restore_from_trash(result.replaced, _abs(result.destination), roots, renamed))
    for note in notes:
        log.warning("undo: %s", note)
    return FileOpResult(
        ok=True,
        bytes=result.bytes,
        verified=verified,
        sidecars=moved,
        duration_ms=_elapsed_ms(started),
        **base,
        **_notes_kwargs(notes),
    )


def _check_unchanged(path: Path, result: FileOpResult) -> None:
    st = os.stat(path)
    if st.st_size != result.bytes:
        raise FileOpError(
            f"{path} changed since it was renamed (size {_fmt_bytes(st.st_size)}, was {_fmt_bytes(result.bytes)})"
        )
    expected_mtime = getattr(result, "dest_mtime_ns", None)
    if isinstance(expected_mtime, int) and st.st_mtime_ns != expected_mtime:
        raise FileOpError(f"{path} was modified after it was renamed")


def _points_to(link: Path, target: Path) -> bool:
    if not os.path.islink(link):
        return False
    try:
        raw = os.readlink(link)
    except OSError:
        return False
    if os.path.normcase(os.path.abspath(os.path.join(link.parent, raw))) == os.path.normcase(str(target)):
        return True
    return _samefile(link, target)


def _undo(
    result: FileOpResult, roots: list[str], txn: _Txn, notes: list[str]
) -> tuple[list[SidecarMove], bool]:
    mode = RenameMode(result.mode)
    if not roots:
        raise FileOpError(_NO_ROOTS)
    src, dst = _abs(result.source), _abs(result.destination)
    pairs = [(_abs(sc.source), _abs(sc.destination)) for sc in result.sidecars]
    for path in [src, dst, *(p for pair in pairs for p in pair)]:
        _refuse_data_dir(path, "path", follow=False)  # a forged history row can't reach /config
        if not pathsvc.is_within_nofollow(path, roots):
            raise FileOpError(f"{path} is outside the allowed roots")
    if mode is RenameMode.MOVE and result.folder_rename is not None:
        return _undo_folder_rename(result, roots, txn, notes)

    # A move that removed emptied source folders: moving the file back recreates them.
    if mode in _MOVE_MODES:
        if not os.path.lexists(dst):
            raise FileOpError(f"The file is no longer at {dst}; it was moved or deleted after the rename")
        _check_unchanged(dst, result)
        if mode is RenameMode.KEEPLINK:
            if not _points_to(src, dst):
                raise FileOpError(f"{src} is no longer the symlink Schema left behind")
        elif os.path.lexists(src) and not _is_case_only(dst, src):
            raise FileOpError(f"Something already exists at the original location {src}; not overwriting it")
        present: list[SidecarMove] = []
        for s, d in pairs:
            if not os.path.lexists(d):
                notes.append(f"Sidecar {d.name} is gone from the destination; not restored")
                continue
            if mode is RenameMode.KEEPLINK:
                if not _points_to(s, d):
                    raise FileOpError(f"{s} is no longer the symlink Schema left behind")
            elif os.path.lexists(s) and not _is_case_only(d, s):
                raise FileOpError(f"Something already exists at the sidecar's original location {s}")
            present.append(SidecarMove(source=str(d), destination=str(s)))
        if mode is RenameMode.KEEPLINK:
            for link in [src, *(Path(m.destination) for m in present)]:
                target = os.readlink(link)
                _unlink(link)
                txn.add(f"recreate symlink {link}", lambda lk=link, t=target: os.symlink(t, lk))
        back = _Execution(
            FileOpPlan(
                source=str(dst),
                destination=str(src),
                mode=RenameMode.MOVE,
                policy=ConflictPolicy.FAIL,
                sidecars=present,
            ),
            roots,
            None,
        )
        outcome = back.run(txn)
        notes.extend(back.notes)
        return [
            SidecarMove(source=str(u.src), destination=str(u.dst)) for u in outcome.units
        ], outcome.verified

    # copy / hardlink / symlink: the destination is an extra entry; remove it (and sidecars)
    removals: list[Path] = []
    moved: list[SidecarMove] = []
    for s, d, is_main in [(src, dst, True), *((s, d, False) for s, d in pairs)]:
        if not os.path.lexists(d):
            if is_main:
                raise FileOpError(f"{d} no longer exists; nothing to undo")
            notes.append(f"Sidecar {d.name} is already gone")
            continue
        if mode is RenameMode.COPY:
            if not os.path.isfile(s):
                raise FileOpError(f"The original {s} is gone; refusing to delete the only remaining copy {d}")
            if is_main:
                _check_unchanged(d, result)
            elif os.stat(d).st_size != os.stat(s).st_size:
                raise FileOpError(f"The copied sidecar {d} differs from its original; not deleting it")
        elif mode is RenameMode.HARDLINK:
            if not _samefile(s, d):
                raise FileOpError(f"{d} is no longer a hardlink of {s}; not deleting it")
        elif mode is RenameMode.SYMLINK and not _points_to(d, s):
            raise FileOpError(f"{d} is no longer a symlink to {s}; not deleting it")
        removals.append(d)
        if not is_main:
            moved.append(SidecarMove(source=str(d), destination=str(s)))
    for d in removals:
        _unlink(d)
    return moved, True


def _undo_folder_rename(
    result: FileOpResult, roots: list[str], txn: _Txn, notes: list[str]
) -> tuple[list[SidecarMove], bool]:
    """Undo a move that renamed the source's folder, in reverse order: the file and its sidecars get
    their old names back inside the folder, then the folder gets its old name back."""
    assert result.folder_rename is not None
    old_dir, new_dir = _abs(result.folder_rename.source), _abs(result.folder_rename.destination)
    src, dst = _abs(result.source), _abs(result.destination)
    for path in (old_dir, new_dir):
        _refuse_data_dir(path, "folder", follow=False)
        if not pathsvc.is_within_nofollow(path, roots):
            raise FileOpError(f"{path} is outside the allowed roots")
    if not _same_path(dst.parent, new_dir) or not _same_path(src.parent, old_dir):
        raise FileOpError("The record of this folder rename doesn't match its file; not undoing it")
    if os.path.islink(new_dir) or not os.path.isdir(new_dir):
        raise FileOpError(f"The folder is no longer at {new_dir}; it was renamed or deleted after the move")
    if os.path.lexists(old_dir):
        raise FileOpError(f"Something already exists at the original folder {old_dir}; not overwriting it")
    if src.name == dst.name:  # only the folder was renamed
        if not os.path.lexists(dst):
            raise FileOpError(f"The file is no longer at {dst}; it was moved or deleted after the rename")
        _check_unchanged(dst, result)
        moved: list[SidecarMove] = []
        verified = True
    else:
        inner = result.model_copy(
            update={
                "source": str(new_dir / src.name),
                "sidecars": [
                    SidecarMove(source=str(new_dir / Path(s.source).name), destination=s.destination)
                    for s in result.sidecars
                ],
                "folder_rename": None,
            }
        )
        moved, verified = _undo(inner, roots, txn, notes)
    _rename_dir_noclobber(new_dir, old_dir)
    txn.add(f"rename folder {old_dir} back", lambda: _rename_dir_noclobber(old_dir, new_dir))
    return [SidecarMove(source=m.source, destination=str(_rebase(Path(m.destination), new_dir, old_dir))) for m in moved], verified
