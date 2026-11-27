"""Release folders: the folder a download client creates for one download.


A torrent or NZB usually lands as one folder ("War.Dogs.2016.1080p.BluRay/") holding the main video,
extras, samples and junk (.nfo, .jpg, .txt). Two things need to know that folder:

* the watcher (:mod:`schema_app.services.watcher`) waits until the *whole* release folder has been
  quiet, so a movie is not moved while its featurettes are still downloading (download clients
  re-download a file that disappears mid-download);
* after a move job, :func:`hold_release` moves a release folder that holds nothing Schema still
  needs into a holding folder (``.schema-trash/leftovers/<day>/<slot>/``, same disk) where it stays
  for :data:`LEFTOVER_KEEP_HOURS` before :func:`prune_holding` deletes it. Undo still works: undoing
  a move recreates the source folder.

Category folders ("sonarr", "radarr", "tv", "complete" ...) between the watch folder and the
release are skipped, so ``/downloads/sonarr/Show.S01/`` is the release, never ``/downloads/sonarr``.

Cleanup only ever holds a folder that provably belongs to one download. A folder the user made
(``/downloads/4k`` as an rdt-client category, a folder of photos) can hold other downloads still
unpacking from RARs, documents, music; so a folder is held only when

* its name matches the videos moved out of it (:func:`names_match`: the same title and the same year
  or season; a title alone proves nothing), searching from the top folder down (:func:`owned_release`);
* one walk (:func:`inspect`) finds nothing but one download's clutter in it: no sub-folders other than
  extras / samples / subtitles / screens / proof (or one named after the release), no file types
  beyond :data:`LEFTOVER_EXTENSIONS` besides videos, at most :data:`LEFTOVER_BUDGET` bytes of them,
  no links, no temp file of a download client;
* right before the move, nothing from the watch / allowed root down to it is a link and the folder is
  the one that was inspected, unchanged. On Linux the rename and the pruning go through directory
  handles opened one component at a time below that root without following links, so a folder
  swapped for a symlink mid-way is never followed; elsewhere every component is re-checked first.
"""

from __future__ import annotations

import contextlib
import errno
import fnmatch
import functools
import json
import logging
import os
import re
import secrets
import shutil
import stat
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from schema_app.core import parser as parser_mod
from schema_app.core.normalize import normalize_title

log = logging.getLogger(__name__)

TRASH_DIRNAME = ".schema-trash"  # same as fileops.TRASH_DIRNAME
HOLDING_DIRNAME = "leftovers"  # below .schema-trash
HOLDING_MANIFEST = "schema-leftovers.json"
LEFTOVER_KEEP_HOURS = 24

# Folders download clients and the *arr apps put between the download root and a release.
CONTAINER_NAMES = frozenset(
    {
        "complete", "completed", "done", "finished", "downloads", "download", "incoming", "intermediate",
        "tv", "tv-sonarr", "sonarr", "series", "shows", "movies", "movie", "radarr", "films", "anime",
        "lidarr", "readarr", "whisparr", "music", "books", "rdt", "rdtclient", "rdt-client",
        "qbittorrent", "sabnzbd", "nzbget", "transmission", "deluge", "torrents", "usenet", "manual",
    }
)  # fmt: skip

# Files that mean "a download client is still working on this folder", whatever the ignore patterns.
BUSY_MARKERS = ("*.aria2", "*.part", "*.partial", "*.!qb", "*.!ut", "*.crdownload", "*.download")

# What a release may leave behind once its videos are in the library. Anything else (archives still
# to unpack, disc images, music, documents) means the folder holds more than one download's clutter.
LEFTOVER_EXTENSIONS = frozenset(
    {
        "nfo", "jpg", "jpeg", "png", "gif", "webp", "txt", "sfv", "md5", "url",
        "srt", "ass", "ssa", "sub", "idx", "sup", "vtt", "nzb",
    }
)  # fmt: skip
LEFTOVER_NAMES = frozenset({"thumbs.db", "desktop.ini", ".ds_store"})  # OS metadata
# Sub-folders releases ship with (Plex extras folders, samples, subtitles, screenshots, proof).
CLUTTER_DIRS = frozenset(
    {
        "extras", "extra", "featurettes", "featurette", "behind the scenes", "deleted scenes", "interviews",
        "interview", "scenes", "shorts", "trailers", "trailer", "other", "others", "bonus",
        "bonus features", "special features", "sample", "samples", "subs", "sub", "subtitles", "subtitle",
        "screens", "screenshots", "screenshot", "proof", "proofs", "covers", "cover", "artwork", "art",
        "scans", "posters", "extrafanart", "extrathumbs",
    }
)  # fmt: skip
LEFTOVER_BUDGET = 50 * 1024 * 1024  # bytes of non-video leftovers; more is somebody's data, not clutter
MAX_INSPECT_FILES = 2000  # a release with more files than this is not one download's clutter
MAX_SIGNATURE_FILES = 2000  # files stat'ed per watcher walk of one release folder

_SEASON_RANGE = re.compile(r"(?i)(?<![a-z0-9])s(\d{1,3})\s*-\s*s?(\d{1,3})(?![0-9])")
_DIR_SEPARATORS = re.compile(r"[\s._-]+")


def container_names(extra: Iterable[str] = ()) -> frozenset[str]:
    """:data:`CONTAINER_NAMES` plus the user's download categories (``category_kinds`` keys)."""
    names = {str(n).strip().casefold() for n in extra if n and str(n).strip()}
    return CONTAINER_NAMES | frozenset(names)


def _key(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))


def _relative_parts(path: str | Path, root: str | Path) -> tuple[str, ...] | None:
    p, r = _key(path), _key(root)
    if p == r or not p.startswith(r.rstrip(os.sep) + os.sep):
        return None
    return Path(os.path.relpath(os.path.abspath(path), os.path.abspath(root))).parts


def owning_root(path: str | Path, roots: Iterable[str | Path]) -> Path | None:
    """The deepest of ``roots`` that ``path`` lies below (compared as written, links not resolved)."""
    best: Path | None = None
    for root in roots:
        inside = bool(root) and _relative_parts(path, root) is not None
        if inside and (best is None or len(_key(root)) > len(_key(best))):
            best = Path(os.path.abspath(root))
    return best


def release_folder(path: str | Path, roots: Iterable[str | Path],
                   containers: frozenset[str] = CONTAINER_NAMES) -> Path | None:  # fmt: skip
    """The release folder of the file ``path``: below the deepest of ``roots`` holding it, the first
    folder whose name isn't a category / container name. None when the file sits directly in the
    root (or only in container folders), or no root holds it."""
    root = owning_root(path, roots)
    if root is None:
        return None
    dirs = (_relative_parts(path, root) or ())[:-1]
    for index, name in enumerate(dirs):
        if name.casefold() not in containers:
            return root.joinpath(*dirs[: index + 1])
    return None


# --------------------------------------------------------------------------- ownership


@dataclass(frozen=True)
class _Identity:
    titles: frozenset[str]
    year: int | None
    seasons: tuple[int, int] | None  # inclusive; "S01-S03" packs cover a range


def _identity(name: str) -> _Identity:
    parsed = parser_mod.parse(name)
    titles = frozenset(t for t in (normalize_title(x) for x in (parsed.title, *parsed.alt_titles)) if t)
    seasons: tuple[int, int] | None = None
    if (span := _SEASON_RANGE.search(name)) is not None:
        low, high = sorted((int(span.group(1)), int(span.group(2))))
        seasons = (low, high)
    elif parsed.season is not None:
        seasons = (parsed.season, parsed.season)
    return _Identity(titles=titles, year=parsed.year, seasons=seasons)


def names_match(folder_name: str, file_name: str) -> bool:
    """The folder ``folder_name`` is named after the release ``file_name`` comes from: the same
    normalized title, and the same year or season ("S01-S03" covers the seasons in between).

    A title alone proves nothing: the category folder "Kids" is not the release of "Kids.1995.mkv"."""
    folder, item = _identity(folder_name), _identity(file_name)
    if not folder.titles & item.titles:
        return False
    if folder.year is not None and item.year is not None and folder.year != item.year:
        return False
    if folder.seasons is not None and item.seasons is not None:
        return folder.seasons[0] <= item.seasons[0] and item.seasons[1] <= folder.seasons[1]
    return folder.year is not None and folder.year == item.year


def owned_release(source: str | Path, top: str | Path) -> Path | None:
    """The folder from ``top`` down to the one holding the file ``source`` (both included) that is
    named after it (:func:`names_match`), the one nearest to ``top`` first; None when none is (an
    unknown category folder such as ``/downloads/4k``, a folder of home videos)."""
    parts = _relative_parts(source, top)
    if not parts:
        return None
    name = Path(source).name
    top = Path(os.path.abspath(top))
    for depth in range(len(parts)):
        folder = top.joinpath(*parts[:depth])
        if names_match(folder.name, name):
            return folder
    return None


def dir_is_clutter(name: str) -> bool:
    """A sub-folder name releases ship with (Featurettes, Sample, Subs, Screens, Proof ...)."""
    return _DIR_SEPARATORS.sub(" ", name.casefold()).strip() in CLUTTER_DIRS


# --------------------------------------------------------------------------- name patterns


def glob_regex(patterns: Iterable[str]) -> re.Pattern[str] | None:
    """One compiled regex for shell patterns (``fnmatch`` semantics), matched against lower-case
    names; None when there are none."""
    unique = list(dict.fromkeys(p.lower() for p in patterns if p))
    if not unique:
        return None
    return re.compile("|".join(f"(?:{fnmatch.translate(p)})" for p in unique))


@functools.lru_cache(maxsize=32)
def _busy_regex(patterns: tuple[str, ...]) -> re.Pattern[str]:
    regex = glob_regex((*BUSY_MARKERS, *patterns))
    assert regex is not None  # BUSY_MARKERS is never empty
    return regex


def is_busy_name(name: str, patterns: Iterable[str] = ()) -> bool:
    """A download client's temp / control file (``Movie.mkv.part``, ``Movie.mkv.aria2`` ...)."""
    return _busy_regex(tuple(patterns)).match(name.lower()) is not None


# --------------------------------------------------------------------------- watcher signature


@dataclass(frozen=True)
class Signature:
    """What a release folder looks like right now; any change restarts its quiet period."""

    files: int
    size: int
    newest_ns: int
    names: int  # hash of the file names: a rename ("x.mkv.tmp" -> "x.mkv") is a change too
    busy: bool  # a temp / control file of a download client is present
    truncated: bool = False  # more than ``max_files`` files: only the first ones were looked at


def signature(folder: str | Path, patterns: Iterable[str] = (), *,
              max_files: int = MAX_SIGNATURE_FILES) -> Signature | None:  # fmt: skip
    """Blocking. None when ``folder`` is gone. Schema's own ``.schema-trash`` is not looked at, and at
    most ``max_files`` files are stat'ed (a folder that big is no single download)."""
    if not os.path.isdir(folder):
        return None
    busy_re = _busy_regex(tuple(patterns))
    files = size = newest = 0
    busy = truncated = False
    seen: list[str] = []
    for current, dirs, names in os.walk(folder, onerror=lambda _e: None):
        dirs[:] = [d for d in dirs if d.casefold() != TRASH_DIRNAME]
        for name in names:
            if files >= max_files:
                truncated = True
                break
            busy = busy or busy_re.match(name.lower()) is not None
            path = os.path.join(current, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            files += 1
            size += st.st_size
            newest = max(newest, st.st_mtime_ns)
            seen.append(path)
        if truncated:
            break
    return Signature(files=files, size=size, newest_ns=newest, names=hash(tuple(sorted(seen))), busy=busy,
                     truncated=truncated)  # fmt: skip


# --------------------------------------------------------------------------- inspection


@dataclass(frozen=True)
class Contents:
    """One walk of a release folder (:func:`inspect`)."""

    files: int
    bytes: int
    videos: tuple[str, ...]  # every video file, at any depth (the database decides whether they're done)
    problem: str | None  # why the folder stays whatever the database says; None: only clutter
    stamps: tuple[tuple[str, int, int, int], ...] = ()  # (relative folder, st_dev, st_ino, st_mtime_ns)


def _is_link_entry(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_symlink() or entry.is_junction()
    except OSError:
        return True


def inspect(folder: str | Path, *, video_extensions: Iterable[str], busy_patterns: Iterable[str] = (),
            subdir_ok: Callable[[str], bool] | None = None, budget: int = LEFTOVER_BUDGET,
            max_files: int = MAX_INSPECT_FILES) -> Contents:  # fmt: skip
    """Blocking. Walk ``folder`` once (links are never followed) and tell whether it holds nothing but
    one download's clutter. ``subdir_ok`` admits a top-level sub-folder besides the clutter names of
    :func:`dir_is_clutter` (one named after the release)."""
    videos_ext = {e.lower().lstrip(".") for e in video_extensions if e}
    busy_re = _busy_regex(tuple(busy_patterns))
    files = total = other = 0
    videos: list[str] = []
    stamps: list[tuple[str, int, int, int]] = []
    busy = link = foreign_dir = foreign_file = unreadable = None
    trash = False
    stack: list[tuple[str, str]] = [("", os.fspath(folder))]
    while stack:
        rel, path = stack.pop()
        try:
            st = os.lstat(path)
            with os.scandir(path) as listing:
                entries = sorted(listing, key=lambda e: e.name)
        except OSError as exc:
            unreadable = unreadable or f"{rel or os.path.basename(path)} could not be read ({exc.strerror or exc})"
            continue
        stamps.append((rel, st.st_dev, st.st_ino, st.st_mtime_ns))
        for entry in entries:
            name = entry.name
            if _is_link_entry(entry):
                link = link or os.path.join(rel, name)
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            if is_dir:
                if name.casefold() == TRASH_DIRNAME:
                    trash = True
                elif not rel and not dir_is_clutter(name) and not (subdir_ok is not None and subdir_ok(name)):
                    foreign_dir = foreign_dir or name  # not walked: the folder stays anyway
                else:
                    stack.append((os.path.join(rel, name), entry.path))
                continue
            files += 1
            if files > max_files:
                return Contents(files=files, bytes=total, videos=tuple(videos), stamps=tuple(stamps),
                                problem=f"it holds more than {max_files} files")  # fmt: skip
            try:
                size = entry.stat(follow_symlinks=False).st_size
            except OSError:
                size = 0
            total += size
            lower = name.lower()
            if busy_re.match(lower):
                busy = busy or name
            ext = lower.rsplit(".", 1)[1] if "." in lower else ""
            if ext in videos_ext:
                videos.append(entry.path)
                continue
            other += size
            if ext not in LEFTOVER_EXTENSIONS and lower not in LEFTOVER_NAMES:
                foreign_file = foreign_file or os.path.join(rel, name)
    problem: str | None = None
    if busy:
        problem = f"{busy} is still downloading"
    elif unreadable:
        problem = unreadable
    elif link:
        problem = f"it holds a link ({link})"
    elif trash:
        problem = f"it holds Schema's own {TRASH_DIRNAME} folder"
    elif foreign_dir:
        problem = f"it holds the folder {foreign_dir}, which may belong to another download"
    elif foreign_file:
        problem = f"it holds {foreign_file}, which is not download clutter"
    elif other > budget:
        problem = f"its other files take {other / 1048576:.0f} MB (more than {budget / 1048576:.0f} MB of clutter)"
    return Contents(files=files, bytes=total, videos=tuple(videos), problem=problem, stamps=tuple(stamps))


# --------------------------------------------------------------------------- link-safe folder access

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _CLOEXEC
# Linux / macOS: act through directory handles opened below the root without following links.
# Windows has no *at() calls: every component is checked right before acting instead.
_FD_MODE = bool(
    _NOFOLLOW
    and {os.open, os.mkdir, os.rename, os.rmdir, os.stat} <= os.supports_dir_fd
    and os.scandir in os.supports_fd
    and shutil.rmtree.avoids_symlink_attacks
)


def _check_part(part: str) -> None:
    if part in ("", ".", "..") or "/" in part or "\\" in part:
        raise OSError(errno.EINVAL, f"unexpected path component {part!r}")


def _open_below(root: str | Path, parts: Sequence[str], *, create: bool = False) -> int:
    """POSIX: a handle on ``root/parts...``, each component below ``root`` opened with O_NOFOLLOW, so a
    link anywhere below ``root`` fails (ELOOP / ENOTDIR); ``create`` makes missing folders. The caller
    closes it. ``root`` itself is trusted (a watch folder or allowed root the admin configured)."""
    fd = os.open(os.path.realpath(root), _DIR_FLAGS)
    try:
        for part in parts:
            _check_part(part)
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, 0o777, dir_fd=fd)
            child = os.open(part, _DIR_FLAGS | _NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def _is_link(path: str | Path) -> bool:
    return os.path.islink(path) or os.path.isjunction(path)


def _check_below(root: str | Path, parts: Sequence[str], *, create: bool = False) -> Path:
    """Path fallback (Windows): ``root/parts...`` once no component below ``root`` is a link or
    junction and the whole path resolves where it says; ``create`` makes missing folders."""
    path, real = Path(os.path.abspath(root)), os.path.realpath(root)
    for part in parts:
        _check_part(part)
        path, real = path / part, os.path.join(real, part)
        if create and not os.path.lexists(path):
            with contextlib.suppress(FileExistsError):
                path.mkdir()
        if _is_link(path):
            raise OSError(errno.ELOOP, "a folder on the way is a link", str(path))
    if os.path.normcase(os.path.realpath(path)) != os.path.normcase(real):
        raise OSError(errno.ELOOP, "the folder does not resolve where it should", str(path))
    return path


def _same_folder(st: os.stat_result, contents: Contents, where: str) -> None:
    if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
        raise OSError(errno.ELOOP, "no longer a plain folder", where)
    if contents.stamps:
        _rel, dev, ino, mtime = contents.stamps[0]
        if (st.st_dev, st.st_ino, st.st_mtime_ns) != (dev, ino, mtime):
            raise OSError(errno.EAGAIN, "the folder changed since it was checked", where)


def _unchanged_below(folder: str | Path, contents: Contents) -> None:
    """Every sub-folder still as :func:`inspect` saw it: a file added, removed or renamed anywhere
    changes the modification time of the folder holding it."""
    for rel, dev, ino, mtime in contents.stamps[1:]:
        try:
            st = os.lstat(os.path.join(folder, rel))
        except OSError as exc:
            raise OSError(errno.EAGAIN, "the folder changed since it was checked", str(folder)) from exc
        if not stat.S_ISDIR(st.st_mode) or (st.st_dev, st.st_ino, st.st_mtime_ns) != (dev, ino, mtime):
            raise OSError(errno.EAGAIN, "the folder changed since it was checked", str(folder))


# --------------------------------------------------------------------------- holding


@dataclass(frozen=True)
class Held:
    original: str
    held: str
    files: int
    bytes: int
    base: str = ""  # the holding folder (``.../.schema-trash/leftovers``) it went to


def holding_base(root: str | Path) -> Path:
    return Path(root) / TRASH_DIRNAME / HOLDING_DIRNAME


def nested_holding_bases(root: str | Path, depth: int = 2) -> list[Path]:
    """Blocking. Holding folders up to ``depth`` levels below ``root`` (where :func:`hold_release`
    falls back to when the root's own is on another disk). Links are not followed."""
    found: list[Path] = []
    level = [Path(os.path.abspath(root))]
    for _ in range(depth):
        below: list[Path] = []
        for folder in level:
            try:
                with os.scandir(folder) as listing:
                    subdirs = [Path(e.path) for e in listing
                               if e.name.casefold() != TRASH_DIRNAME and not _is_link_entry(e)
                               and e.is_dir(follow_symlinks=False)]  # fmt: skip
            except OSError:
                continue
            for sub in subdirs:
                base = holding_base(sub)
                if os.path.isdir(base) and not _is_link(base) and not _is_link(base.parent):
                    found.append(base)
            below.extend(subdirs)
        level = below
    return found


def hold_release(folder: str | Path, root: str | Path, *, contents: Contents | None = None,
                 now: datetime | None = None) -> Held:  # fmt: skip
    """Move ``folder`` (below the watch / allowed folder ``root``) into a holding folder on the same
    disk, ``<base>/.schema-trash/leftovers/<day>/<slot>/<name>`` with a manifest, where ``<base>`` is
    ``root`` or else the folder's parent (a rename, never a copy).

    ``contents`` is the :func:`inspect` walk the decision was made on (walked here when None): the folder
    must still be that folder, unchanged, with no link from ``root`` down to it, or nothing is moved.
    Raises OSError when it can't be held."""
    folder = Path(os.path.abspath(folder))
    parts = _relative_parts(folder, root)
    if not parts:
        raise OSError(errno.EINVAL, "the folder is not below its watch folder", str(folder))
    if contents is None:
        contents = inspect(folder, video_extensions=())
    now = now or datetime.now()
    day = now.strftime("%Y-%m-%d")
    slot = f"{now.strftime('%H%M%S')}-{secrets.token_hex(3)}"
    bases = list(dict.fromkeys([(), tuple(parts[:-1])]))
    manifest = {
        "version": 1,
        "created": now.isoformat(timespec="seconds"),
        "original": str(folder),
        "files": contents.files,
        "bytes": contents.bytes,
    }
    hold = _hold_fd if _FD_MODE else _hold_path
    base = hold(folder, Path(root), tuple(parts), bases, day, slot, contents, manifest)
    base_path = holding_base(Path(os.path.abspath(root)).joinpath(*base))
    target = base_path / day / slot / folder.name
    log.info("moved leftover download folder %s -> %s", folder, target)
    return Held(original=str(folder), held=str(target), files=contents.files, bytes=contents.bytes,
                base=str(base_path))  # fmt: skip


def _manifest_text(manifest: dict[str, object], held: Path) -> str:
    return json.dumps({**manifest, "held": str(held)}, indent=2)


def _hold_fd(folder: Path, root: Path, parts: tuple[str, ...], bases: list[tuple[str, ...]], day: str,
             slot: str, contents: Contents, manifest: dict[str, object]) -> tuple[str, ...]:  # fmt: skip
    name = parts[-1]
    last: OSError | None = None
    parent_fd = _open_below(root, parts[:-1])
    try:
        _same_folder(os.stat(name, dir_fd=parent_fd, follow_symlinks=False), contents, str(folder))
        _unchanged_below(folder, contents)
        for base in bases:
            try:
                day_fd = _open_below(root, (*base, TRASH_DIRNAME, HOLDING_DIRNAME, day), create=True)
            except OSError as exc:
                last = exc
                continue
            try:
                os.mkdir(slot, 0o777, dir_fd=day_fd)
                slot_fd = os.open(slot, _DIR_FLAGS | _NOFOLLOW, dir_fd=day_fd)
            except OSError as exc:
                last = exc
                os.close(day_fd)
                continue
            try:
                # The source name is resolved inside the pinned parent and rename never follows its last
                # component, so whatever is moved is what sits in that very folder.
                os.rename(name, name, src_dir_fd=parent_fd, dst_dir_fd=slot_fd)
            except OSError as exc:
                last = exc
                os.close(slot_fd)
                with contextlib.suppress(OSError):
                    os.rmdir(slot, dir_fd=day_fd)
                os.close(day_fd)
                continue
            held = holding_base(Path(os.path.abspath(root)).joinpath(*base)) / day / slot / name
            try:
                fd = os.open(HOLDING_MANIFEST, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, 0o644,
                             dir_fd=slot_fd)  # fmt: skip
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(_manifest_text(manifest, held))
            except OSError as exc:  # the folder is held anyway; prune never deletes a slot without one
                log.warning("could not write the leftovers manifest for %s: %s", held, exc)
            finally:
                os.close(slot_fd)
                os.close(day_fd)
            return base
    finally:
        os.close(parent_fd)
    raise last or OSError(errno.ENOENT, "no holding folder available", str(folder))


def _hold_path(folder: Path, root: Path, parts: tuple[str, ...], bases: list[tuple[str, ...]], day: str,
               slot: str, contents: Contents, manifest: dict[str, object]) -> tuple[str, ...]:  # fmt: skip
    last: OSError | None = None
    for base in bases:
        try:
            slot_dir = _check_below(root, (*base, TRASH_DIRNAME, HOLDING_DIRNAME, day), create=True) / slot
            slot_dir.mkdir()
        except OSError as exc:
            last = exc
            continue
        try:
            source = _check_below(root, parts)  # re-checked right before acting
            _same_folder(os.lstat(source), contents, str(folder))
            _unchanged_below(source, contents)
            os.rename(source, slot_dir / source.name)
        except OSError as exc:
            last = exc
            with contextlib.suppress(OSError):
                slot_dir.rmdir()
            if exc.errno in (errno.ELOOP, errno.EAGAIN):
                break  # the folder itself is the problem, not this holding folder
            continue
        held = slot_dir / source.name
        try:
            (slot_dir / HOLDING_MANIFEST).write_text(_manifest_text(manifest, held), encoding="utf-8")
        except OSError as exc:  # the folder is held anyway; prune never deletes a slot without one
            log.warning("could not write the leftovers manifest for %s: %s", held, exc)
        return base
    raise last or OSError(errno.ENOENT, "no holding folder available", str(folder))


# --------------------------------------------------------------------------- pruning


def _parse_created(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(json.loads(raw)["created"]))
    except (ValueError, KeyError, TypeError):
        return None


def _created(slot: Path) -> datetime | None:
    try:
        with open(slot / HOLDING_MANIFEST, encoding="utf-8") as fh:
            return _parse_created(fh.read(65536))
    except OSError:
        return None


def _created_fd(day_fd: int, slot: str) -> datetime | None:
    try:
        if not stat.S_ISDIR(os.stat(slot, dir_fd=day_fd, follow_symlinks=False).st_mode):
            return None
        slot_fd = os.open(slot, _DIR_FLAGS | _NOFOLLOW, dir_fd=day_fd)
        try:
            fd = os.open(HOLDING_MANIFEST, os.O_RDONLY | _NOFOLLOW | _CLOEXEC, dir_fd=slot_fd)
        finally:
            os.close(slot_fd)
        with os.fdopen(fd, encoding="utf-8") as fh:
            return _parse_created(fh.read(65536))
    except OSError:
        return None


def prune_holding(base: str | Path, *, root: str | Path | None = None,
                  keep: timedelta = timedelta(hours=LEFTOVER_KEEP_HOURS),
                  now: datetime | None = None) -> list[str]:  # fmt: skip
    """Delete held folders older than ``keep`` from the holding folder ``base``
    (``<dir>/.schema-trash/leftovers``). Only slots carrying Schema's manifest are touched, so nothing
    Schema didn't put there is ever deleted, and no link below ``root`` (the trusted watch / allowed
    folder; default ``<dir>``) is followed. Returns the removed slot paths."""
    base = Path(os.path.abspath(base))
    if base.name != HOLDING_DIRNAME or base.parent.name.casefold() != TRASH_DIRNAME:
        return []
    root = Path(os.path.abspath(root)) if root is not None else base.parent.parent
    parts = _relative_parts(base, root)
    if not parts:
        return []
    now = now or datetime.now()
    try:
        return (_prune_fd if _FD_MODE else _prune_path)(base, root, parts, keep, now)
    except FileNotFoundError:
        return []
    except OSError as exc:
        log.warning("could not prune held leftovers in %s: %s", base, exc)
        return []


def _prune_fd(base: Path, root: Path, parts: tuple[str, ...], keep: timedelta, now: datetime) -> list[str]:
    removed: list[str] = []
    base_fd = _open_below(root, parts)
    try:
        for day in sorted(os.listdir(base_fd)):
            try:
                if not stat.S_ISDIR(os.stat(day, dir_fd=base_fd, follow_symlinks=False).st_mode):
                    continue
                day_fd = os.open(day, _DIR_FLAGS | _NOFOLLOW, dir_fd=base_fd)
            except OSError:
                continue
            try:
                for slot in sorted(os.listdir(day_fd)):
                    created = _created_fd(day_fd, slot)
                    if created is None or now - created < keep:
                        continue
                    try:
                        shutil.rmtree(slot, dir_fd=day_fd)  # refuses a slot swapped for a link meanwhile
                    except OSError as exc:
                        log.warning("could not delete held leftovers %s: %s", base / day / slot, exc)
                        continue
                    removed.append(str(base / day / slot))
                    log.info("deleted held leftovers %s", base / day / slot)
            finally:
                os.close(day_fd)
            with contextlib.suppress(OSError):
                os.rmdir(day, dir_fd=base_fd)  # only when empty
    finally:
        os.close(base_fd)
    return removed


def _prune_path(base: Path, root: Path, parts: tuple[str, ...], keep: timedelta, now: datetime) -> list[str]:
    removed: list[str] = []
    holding = _check_below(root, parts)
    for day in sorted(os.listdir(holding)):
        day_dir = holding / day
        if _is_link(day_dir) or not day_dir.is_dir():
            continue
        for slot in sorted(os.listdir(day_dir)):
            slot_dir = day_dir / slot
            if _is_link(slot_dir) or not slot_dir.is_dir():
                continue
            created = _created(slot_dir)
            if created is None or now - created < keep:
                continue
            try:
                _check_below(root, (*parts, day, slot))  # re-checked right before deleting
                shutil.rmtree(slot_dir)  # never descends into links / junctions
            except OSError as exc:
                log.warning("could not delete held leftovers %s: %s", slot_dir, exc)
                continue
            removed.append(str(base / day / slot))
            log.info("deleted held leftovers %s", slot_dir)
        with contextlib.suppress(OSError):
            day_dir.rmdir()  # only when empty
    return removed
