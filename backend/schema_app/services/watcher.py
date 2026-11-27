"""Folder watcher with settle detection.

Contract: docs/SPEC.md §7.3.

How it works
------------
* One watchdog ``Observer`` (inotify / ReadDirectoryChangesW) delivers file events from its
  threads; they are handed to the event loop with ``loop.call_soon_threadsafe``.
* A periodic full rescan catches what inotify can't see on SMB/NFS mounts, files that arrived while
  Schema was down, and folders that come back after being ``missing``: every ``rescan_interval``
  (default 60 s) for a folder without live events (degraded, missing), every
  ``live_rescan_interval`` for one whose events work (a full rescan costs ~40 µs of Python per file).
* Candidates are video files (``video_extensions``) that are not hidden, not inside ignored folders
  and don't match ``ignore_patterns`` (``*.part``, ``*.!qB`` ...). A candidate becomes *ready* once
  its size and mtime have been unchanged for the folder's ``settle_seconds``, it is not empty, and
  (on Windows) it can be opened exclusively, i.e. no other process still has it open.
* A file inside a release folder (the folder a download client made for one download, below any
  category folders, see :mod:`schema_app.services.release`) also waits until that whole folder has
  been unchanged for ``release_quiet`` seconds with no temp file (``*.part``, ``*.aria2`` ...) left in
  it, so a movie is never moved while its extras still download (clients re-download a file that
  vanishes mid-download).
* Ready files are batched per folder: the batch is flushed after a quiet period of up to 2 s
  without new ready files, so a season pack becomes one ``on_ready`` call (one job).
* Each file is reported once per (size, mtime). If ``on_ready`` raises, its files settle again
  and are offered again. After a restart, files still sitting in a watch folder are offered again
  unless the optional ``is_known`` callback says a job already handled them (seeding files in copy /
  link mode): those never become pending, so their release folders are never walked either.
* :meth:`FolderWatcher.incoming` is a snapshot of what is waiting (settling files with their
  countdown, partial downloads matching the ignore patterns); it is rebuilt on the event loop when
  something changed (and every few seconds for the countdowns) and safe to read from any thread.
  :meth:`FolderWatcher.start_now` skips the rest of a settle wait.

Unlike FileBot's folder watch (a single 2 s debounce with no stability check), a file that is
still being written is never handed over.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from schema_app.domain import WatchFolderSpec
from schema_app.services import release as release_mod

log = logging.getLogger(__name__)

OnReady = Callable[[WatchFolderSpec, list[Path]], Awaitable[None]]
OnStatus = Callable[[int, str, str | None], Awaitable[None]]  # (folder_id, status, error)
# {path: mtime in seconds} -> the normalized paths (normcase(normpath)) a job already handled.
IsKnown = Callable[[dict[str, float | None]], Awaitable[set[str]]]

DEFAULT_IGNORE_PATTERNS = [
    "*.part",
    "*.partial",
    "*.!qB",
    "*.!ut",
    "*.tmp",
    "*.temp",
    "*.crdownload",
    "*.download",
]
# Always ignored, whatever the user configures (Schema's own temp files, NAS metadata folders).
_INTERNAL_IGNORE = ["*.schema-tmp", "*.schema-link"]
_IGNORED_DIRS = {"@eadir", "#recycle", "$recycle.bin", "system volume information", "lost+found", "#snapshot"}
DEFAULT_VIDEO_EXTENSIONS = [
    "mkv", "mp4", "m4v", "avi", "mov", "wmv", "ts", "m2ts", "webm", "mpg", "mpeg", "flv", "ogm",
]  # fmt: skip
BATCH_WINDOW = 2.0
OBSERVER_TIMEOUT = 10.0  # seconds to wait for watchdog to start/stop an emitter thread
PARTIAL_STAT_INTERVAL = 2.0  # seconds between size checks of still-downloading files
MAX_PARTIAL = 500  # per folder: a dump of thousands of temp files must not grow without bound
RELEASE_CHECK_INTERVAL = 5.0  # seconds between walks of one release folder
SNAPSHOT_REFRESH = 10.0  # seconds: incoming() countdowns are refreshed this often even when nothing changed


@dataclass
class _Pending:
    size: int
    mtime_ns: int
    stable_since: float
    forced: bool = False  # start_now(): ready as soon as one stat pass sees it unchanged
    release: str | None = None  # its release folder (fixed by its path: computed once)
    vetted: bool = True  # False: from a file event, not yet checked against reported / is_known


@dataclass(frozen=True)
class WatchedFile:
    """One file waiting in a watch folder (see :meth:`FolderWatcher.incoming`)."""

    folder_id: int
    path: str
    size: int
    mtime_ns: int
    state: str  # "settling" | "partial"
    settle_seconds: float
    stable_since: datetime | None  # None for partial files
    settles_at: datetime | None


@dataclass
class _Release:
    """Quiet-period tracking of one release folder (see :mod:`schema_app.services.release`)."""

    sig: release_mod.Signature | None
    stable_since: float
    checked: float
    steady: bool = False  # two walks in a row saw the same signature


@dataclass
class _Folder:
    spec: WatchFolderSpec
    status: str = "stopped"
    error: str | None = None
    watch: Any = None  # watchdog ObservedWatch
    pending: dict[str, _Pending] = field(default_factory=dict)
    reported: dict[str, tuple[int, int]] = field(default_factory=dict)
    batch: list[Path] = field(default_factory=list)
    batch_last: float = 0.0
    batch_forced: bool = False  # the batch holds a start_now() file: flush without the batch window
    partial: dict[str, tuple[int, int]] = field(default_factory=dict)  # still downloading: (size, mtime)
    partial_checked: float = 0.0
    scanning: bool = False  # a full rescan is running in a worker thread
    checking: bool = False  # a settle (stat) pass is running in a worker thread
    releases: dict[str, _Release] = field(default_factory=dict)  # release folder -> quiet tracking
    last_scan: float = float("-inf")  # monotonic start of the last full rescan

    @property
    def settle(self) -> float:
        return max(0.0, float(self.spec.settle_seconds))


class _Handler(FileSystemEventHandler):
    """Runs on watchdog threads; forwards to the event loop and does nothing else."""

    def __init__(self, watcher: FolderWatcher, folder_id: int) -> None:
        self._watcher = watcher
        self._folder_id = folder_id

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type in ("opened", "closed_no_write"):
            return  # reads (Plex scans, our own hashing) are not changes
        src = os.fsdecode(event.src_path)
        dest = os.fsdecode(event.dest_path) if getattr(event, "dest_path", "") else ""
        self._watcher._from_thread(self._folder_id, event.event_type, src, dest, event.is_directory)


def _make_observer() -> Any:
    """watchdog's default observer, except on Windows (see :func:`_windows_observer`)."""
    return _windows_observer() if os.name == "nt" else Observer()


def _windows_observer() -> Any:
    """Observer whose emitters can always be stopped.

    watchdog's ``WindowsApiEmitter`` closes the directory handle from the *stopping* thread while
    the emitter thread may be about to call ``ReadDirectoryChangesW`` again; that call can then block
    forever on a closed (or reused) handle, which hangs ``unschedule``/``stop`` and can crash the
    interpreter at exit. Here only the emitter thread closes its handle, and stopping merely cancels
    the pending read (repeatedly) until the thread has left. Linux (inotify) is unaffected.

    watchdog 7 rewrote the emitter around a ``DirectoryChangeReader`` that owns (and stops) its
    handle itself; there the default observer is used.
    """
    from watchdog.observers import read_directory_changes, winapi
    from watchdog.observers.api import DEFAULT_OBSERVER_TIMEOUT, BaseObserver
    from watchdog.observers.read_directory_changes import WindowsApiEmitter

    if hasattr(read_directory_changes, "DirectoryChangeReader"):
        return Observer()

    class _StoppableWindowsEmitter(WindowsApiEmitter):
        def on_thread_stop(self) -> None:
            if threading.current_thread() is self:
                return  # the watched folder vanished; run() exits on its own
            for _ in range(100):
                handle = self._whandle
                if handle:
                    with contextlib.suppress(OSError):
                        winapi.CancelIoEx(handle, None)
                if not self.is_alive():
                    return
                self.join(0.05)

        def run(self) -> None:
            try:
                super().run()
            finally:
                with self._lock:
                    handle, self._whandle = self._whandle, None
                if handle:
                    with contextlib.suppress(OSError):
                        winapi.CloseHandle(handle)

    return BaseObserver(_StoppableWindowsEmitter, timeout=DEFAULT_OBSERVER_TIMEOUT)


def _is_unlocked(path: str) -> bool:
    """Best effort "nobody has this open": an exclusive open on Windows; always True elsewhere."""
    if os.name != "nt":
        return True
    import _winapi  # type: ignore[import-not-found]

    try:
        handle = _winapi.CreateFile(
            path, _winapi.GENERIC_READ, 0, _winapi.NULL, _winapi.OPEN_EXISTING, 0, _winapi.NULL
        )
    except OSError:
        return False  # sharing violation (still being written) or gone
    _winapi.CloseHandle(handle)
    return True


class FolderWatcher:
    """Watches folders (watchdog + periodic rescan fallback for SMB/NFS where inotify is blind)
    and calls ``on_ready`` with batches of files whose size and mtime have been stable for
    ``settle_seconds`` and that are not locked / partial (.part, .!qB, ...)."""

    def __init__(self, on_ready: OnReady, on_status: OnStatus | None = None, *,
                 rescan_interval: float = 60.0, live_rescan_interval: float | None = None,
                 ignore_patterns: list[str] | None = None, video_extensions: list[str] | None = None,
                 release_quiet: float = 0.0, container_names: Iterable[str] = (),
                 is_known: IsKnown | None = None) -> None:  # fmt: skip
        self._on_ready = on_ready
        self._on_status = on_status
        self._is_known = is_known
        # Full rescans: ``rescan_interval`` for folders without live events (degraded, missing),
        # ``live_rescan_interval`` (default: the same) for folders whose events work.
        self.rescan_interval = max(0.05, float(rescan_interval))
        live = self.rescan_interval if live_rescan_interval is None else float(live_rescan_interval)
        self.live_rescan_interval = max(self.rescan_interval, live)
        patterns = DEFAULT_IGNORE_PATTERNS if ignore_patterns is None else ignore_patterns
        self.ignore_patterns = [p.lower() for p in [*patterns, *_INTERNAL_IGNORE] if p]
        self._ignore_re = release_mod.glob_regex(self.ignore_patterns)
        self._internal_re = release_mod.glob_regex(_INTERNAL_IGNORE)
        self._busy_patterns = [p.lower() for p in patterns if p]
        # A file inside a release folder is handed over only once the whole folder (every file in it,
        # videos or not) has been unchanged this long and holds no temp file of a download client.
        self.release_quiet = max(0.0, float(release_quiet))
        self.release_check_interval = min(RELEASE_CHECK_INTERVAL, max(0.05, self.release_quiet / 4))
        self.container_names = release_mod.container_names(container_names)
        exts = video_extensions if video_extensions else DEFAULT_VIDEO_EXTENSIONS
        self.video_extensions = {e.lower().lstrip(".") for e in exts if e}
        self.batch_window = BATCH_WINDOW
        self._folders: dict[int, _Folder] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._observer: Any = None
        self._tasks: list[asyncio.Task[None]] = []
        self._callbacks: set[asyncio.Task[None]] = set()
        self._running = False
        self._lock = asyncio.Lock()
        # Rebuilt (never mutated) on the event loop; readers in other threads just take the reference.
        self._snapshot: tuple[WatchedFile, ...] = ()
        self._dirty = False  # something the snapshot shows changed since it was built
        self._snapshot_at = 0.0

    # ----------------------------------------------------------------------------- public API

    async def start(self) -> None:
        if self._running:
            return
        self._loop = asyncio.get_running_loop()
        observer = _make_observer()
        observer.daemon = True
        observer.start()
        self._observer = observer
        self._running = True
        async with self._lock:
            for folder in list(self._folders.values()):
                await self._activate(folder)
        self._tasks = [
            asyncio.create_task(self._tick_loop(), name="schema-watcher-tick"),
            asyncio.create_task(self._rescan_loop(), name="schema-watcher-rescan"),
        ]

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        if self._callbacks:
            _, still_running = await asyncio.wait(set(self._callbacks), timeout=5)
            for task in still_running:
                task.cancel()
        for folder in self._folders.values():
            await self._unschedule(folder)
            folder.status, folder.error = "stopped", None
            folder.pending.clear()
            folder.partial.clear()
            folder.releases.clear()
            folder.batch.clear()
        self._snapshot = ()
        observer, self._observer = self._observer, None
        if observer is not None:
            observer.stop()
            await asyncio.to_thread(observer.join, 5)

    async def sync(self, specs: list[WatchFolderSpec]) -> None:
        """Replace the watched set (add/remove/update folders) without a restart."""
        async with self._lock:
            wanted = {spec.id: spec for spec in specs}
            for folder_id in [fid for fid in self._folders if fid not in wanted]:
                folder = self._folders.pop(folder_id)
                await self._unschedule(folder)
            for folder_id, spec in wanted.items():
                folder = self._folders.get(folder_id)
                if folder is None:
                    folder = _Folder(spec=spec)
                    self._folders[folder_id] = folder
                    if self._running:
                        await self._activate(folder)
                    continue
                old = folder.spec
                folder.spec = spec
                if (old.path, old.recursive, old.enabled) != (spec.path, spec.recursive, spec.enabled):
                    await self._unschedule(folder)
                    folder.pending.clear()
                    folder.partial.clear()
                    folder.releases.clear()
                    folder.batch.clear()
                    if old.path != spec.path:
                        folder.reported.clear()
                    if self._running:
                        await self._activate(folder)
                    else:
                        folder.status, folder.error = "stopped", None
            self._rebuild_snapshot()

    def status(self) -> dict[int, str]:
        """folder_id -> "watching" | "stopped" | "error" | "missing"."""
        return {folder_id: folder.status for folder_id, folder in self._folders.items()}

    def errors(self) -> dict[int, str | None]:
        """folder_id -> last error / degraded-mode note (None when healthy)."""
        return {folder_id: folder.error for folder_id, folder in self._folders.items()}

    def incoming(self) -> list[WatchedFile]:
        """Files waiting in the watch folders: settling (with their countdown) and partial downloads.

        Safe from any thread: it returns the last snapshot the event loop built (at most one tick,
        about a second, old)."""
        return list(self._snapshot)

    async def start_now(self, folder_id: int, path: str | None = None) -> int:
        """Skip the rest of the settle wait for ``path`` (or every settling file of the folder).

        Event loop only. Partial downloads are never started. Returns how many files were marked;
        each is handed over as soon as one stat pass sees it unchanged (and, on Windows, unlocked)."""
        folder = self._folders.get(folder_id)
        if folder is None or folder.status != "watching":
            return 0
        wanted = _norm(path) if path else None
        started = 0
        for key, entry in folder.pending.items():
            if wanted is not None and _norm(key) != wanted:
                continue
            entry.forced = True
            started += 1
        if started:
            self._rebuild_snapshot()
        return started

    async def scan_now(self) -> int:
        """Re-walk every watched folder now and skip the settle wait for every finished file found.

        Event loop only. Catches files the OS events and the periodic rescan have not reported yet.
        Partial downloads are never started, and files already handed over are not offered again.
        Returns how many files were marked to start."""
        started = 0
        for folder in list(self._folders.values()):
            if folder.status != "watching":
                continue
            await self._rescan(folder)
            started += await self.start_now(folder.spec.id)
        return started

    # ----------------------------------------------------------------------------- lifecycle

    async def _activate(self, folder: _Folder) -> None:
        spec = folder.spec
        if not spec.enabled:
            self._set_status(folder, "stopped", None)
            return
        exists = await asyncio.to_thread(os.path.isdir, spec.path)
        if not exists:
            self._set_status(folder, "missing", f"Folder not found: {spec.path}")
            return
        note: str | None = None
        observer = self._observer
        try:
            # watchdog starts/joins emitter threads here; keep that off the event loop.
            folder.watch = await asyncio.wait_for(
                asyncio.to_thread(
                    observer.schedule, _Handler(self, spec.id), spec.path, recursive=spec.recursive
                ),
                timeout=OBSERVER_TIMEOUT,
            )
        except Exception as exc:  # inotify limits, permissions, exotic filesystems, a stuck emitter
            folder.watch = None
            reason = str(exc) or exc.__class__.__name__
            note = (
                f"Live file events unavailable ({reason}); checking every {self.rescan_interval:g} s instead"
            )
            log.warning("watch folder %s: %s", spec.path, note)
        self._set_status(folder, "watching", note)
        self._spawn(self._rescan(folder))

    async def _unschedule(self, folder: _Folder) -> None:
        watch, folder.watch = folder.watch, None
        observer = self._observer
        if watch is None or observer is None:
            return
        try:
            await asyncio.wait_for(asyncio.to_thread(observer.unschedule, watch), timeout=OBSERVER_TIMEOUT)
        except Exception as exc:  # already gone, or an emitter that won't stop: never block the loop
            log.debug("unschedule %s: %r", folder.spec.path, exc)

    def _set_status(self, folder: _Folder, status: str, error: str | None) -> None:
        if (folder.status, folder.error) == (status, error):
            return
        folder.status, folder.error = status, error
        if self._on_status is not None and self._running:
            self._spawn(self._call_status(folder.spec.id, status, error))

    async def _call_status(self, folder_id: int, status: str, error: str | None) -> None:
        try:
            await self._on_status(folder_id, status, error)  # type: ignore[misc]
        except Exception:
            log.exception("watcher on_status callback failed")

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._callbacks.add(task)
        task.add_done_callback(self._callbacks.discard)

    # ----------------------------------------------------------------------------- events

    def _from_thread(self, folder_id: int, kind: str, src: str, dest: str, is_dir: bool) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(self._on_event, folder_id, kind, src, dest, is_dir)

    def _on_event(self, folder_id: int, kind: str, src: str, dest: str, is_dir: bool) -> None:
        folder = self._folders.get(folder_id)
        if folder is None or folder.status != "watching" or not self._running:
            return
        if kind in ("deleted", "moved"):
            self._forget(folder, src, is_dir)
        target = dest if kind == "moved" else src
        if kind == "deleted" or not target:
            if kind == "deleted" and os.path.normcase(src) == os.path.normcase(
                os.path.abspath(folder.spec.path)
            ):
                self._spawn(self._rescan(folder))  # the watched folder itself went away
            return
        if is_dir:
            if kind in ("created", "moved") and self._dir_allowed(folder, Path(target)):
                self._spawn(self._rescan(folder, Path(target)))
            return
        # A download writes in many small chunks, each a "modified" event: a file already tracked needs
        # nothing (every stat pass sees its changes), so that is checked before anything costlier.
        if target in folder.pending or target in folder.partial:
            return
        path = Path(target)
        key = str(path)
        if key in folder.pending or key in folder.partial:
            return
        if self._relevant(folder, path):
            folder.pending[key] = _Pending(size=-1, mtime_ns=-1, stable_since=time.monotonic(),
                                           release=self._release_of(folder, key), vetted=False)  # fmt: skip
            self._dirty = True
        elif len(folder.partial) < MAX_PARTIAL and self._partial(folder, path):
            folder.partial[key] = (-1, -1)
            self._dirty = True

    def _forget(self, folder: _Folder, path: str, is_dir: bool) -> None:
        if is_dir:
            prefix = path.rstrip("\\/") + os.sep
            for table in (folder.pending, folder.reported, folder.partial):
                for key in [k for k in table if k.startswith(prefix)]:
                    del table[key]
        else:
            folder.pending.pop(path, None)
            folder.reported.pop(path, None)
            folder.partial.pop(path, None)
        self._dirty = True

    # ----------------------------------------------------------------------------- filters

    def _is_video_name(self, name: str) -> bool:
        dot = name.rfind(".")
        return dot >= 0 and name[dot + 1 :].lower() in self.video_extensions

    def _ignored_name(self, name: str) -> bool:
        lower = name.lower()
        return lower.startswith(".") or (self._ignore_re is not None and self._ignore_re.match(lower) is not None)

    def _relevant_name(self, name: str) -> bool:
        """A candidate by its name alone: a video (checked first: the cheapest test that rejects most
        files) that is not hidden and matches no ignore pattern."""
        return self._is_video_name(name) and not self._ignored_name(name)

    def _dir_ignored(self, name: str) -> bool:
        lower = name.lower()
        return lower.startswith(".") or lower in _IGNORED_DIRS

    def _dir_allowed(self, folder: _Folder, directory: Path) -> bool:
        if not folder.spec.recursive:
            return False
        try:
            rel = directory.relative_to(folder.spec.path)
        except ValueError:
            return False
        return not any(self._dir_ignored(part) for part in rel.parts)

    def _in_watched_dirs(self, folder: _Folder, path: Path) -> bool:
        """``path`` lies in the folder (directly, or below it when recursive) outside ignored folders."""
        try:
            rel = path.relative_to(folder.spec.path)
        except ValueError:
            return False
        dirs = rel.parts[:-1]
        if dirs and not folder.spec.recursive:
            return False
        return not any(self._dir_ignored(part) for part in dirs)

    def _relevant(self, folder: _Folder, path: Path) -> bool:
        return self._relevant_name(path.name) and self._in_watched_dirs(folder, path)

    def _partial_name(self, name: str) -> bool:
        """A download in progress: matches an ignore pattern and is a video once the temp suffix is
        dropped ("Movie.mkv.part", "Movie.mkv.!qB")."""
        lower = name.lower()
        stem = lower.rsplit(".", 1)[0]
        if "." not in stem or stem.rsplit(".", 1)[1] not in self.video_extensions:
            return False
        if lower.startswith(".") or (self._internal_re is not None and self._internal_re.match(lower)):
            return False
        return self._ignore_re is not None and self._ignore_re.match(lower) is not None

    def _partial(self, folder: _Folder, path: Path) -> bool:
        return self._partial_name(path.name) and self._in_watched_dirs(folder, path)

    def _walk(self, folder: _Folder, start: Path | None,
              partials: dict[str, tuple[int, int]] | None = None) -> dict[str, tuple[int, int]] | None:  # fmt: skip
        """Blocking: relevant files under ``start`` (default: the folder) with (size, mtime_ns).
        Still-downloading files go to ``partials``. Returns None when the watched folder itself is
        missing.

        Ignored and (non-recursive) sub-folders are pruned while walking and ``start`` is an allowed
        folder (:meth:`_dir_allowed`), so each file only needs the checks on its name."""
        root = Path(folder.spec.path)
        if not root.is_dir():
            return None
        found: dict[str, tuple[int, int]] = {}
        top = str(start or root)  # normalized, so the keys equal str(Path(...)) of event paths
        recursive = folder.spec.recursive
        for current, dirs, files in os.walk(top, onerror=lambda _e: None):
            dirs[:] = [d for d in dirs if recursive and not self._dir_ignored(d)]
            for name in files:
                relevant = self._relevant_name(name)
                if not relevant and (partials is None or not self._partial_name(name)):
                    continue
                path = os.path.join(current, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                if relevant:
                    found[path] = (st.st_size, st.st_mtime_ns)
                elif partials is not None:
                    partials[path] = (st.st_size, st.st_mtime_ns)
        return found

    # ----------------------------------------------------------------------------- loops

    def _tick_interval(self) -> float:
        values = [
            min(f.settle / 4, self._batch_window(f) / 2)
            for f in self._folders.values()
            if f.pending or f.batch
        ]
        return min(1.0, max(0.02, min(values))) if values else 0.5

    def _batch_window(self, folder: _Folder) -> float:
        return max(0.05, min(self.batch_window, folder.settle))

    async def _tick_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._tick_interval())
            for folder in list(self._folders.values()):
                try:
                    if folder.pending and not folder.checking and folder.status == "watching":
                        await self._check_pending(folder)
                    if folder.partial and not folder.checking and folder.status == "watching":
                        await self._check_partial(folder)
                    self._maybe_flush(folder)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("watcher tick failed for %s", folder.spec.path)
            if self._dirty or time.monotonic() - self._snapshot_at >= SNAPSHOT_REFRESH:
                self._rebuild_snapshot()

    def _rescan_every(self, folder: _Folder) -> float:
        """Seconds between full rescans: rare while live events work, every ``rescan_interval``
        without them (degraded mode, a missing folder waiting to come back)."""
        return self.live_rescan_interval if folder.watch is not None else self.rescan_interval

    async def _rescan_loop(self) -> None:
        while self._running:
            await asyncio.sleep(min(self.rescan_interval, 5.0))
            now = time.monotonic()
            for folder in list(self._folders.values()):
                if now - folder.last_scan < self._rescan_every(folder):
                    continue
                try:
                    await self._rescan(folder)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("watcher rescan failed for %s", folder.spec.path)

    async def _rescan(self, folder: _Folder, start: Path | None = None) -> None:
        if not self._running or not folder.spec.enabled:
            return
        if self._folders.get(folder.spec.id) is not folder:
            return
        full = start is None
        if full and folder.scanning:
            return
        if full:
            folder.scanning = True
            folder.last_scan = time.monotonic()
        partials: dict[str, tuple[int, int]] = {}
        try:
            found = await asyncio.to_thread(self._walk, folder, start, partials)
        finally:
            if full:
                folder.scanning = False
        if self._folders.get(folder.spec.id) is not folder or not self._running:
            return
        if found is None:
            if folder.status != "missing":
                await self._unschedule(folder)
                folder.pending.clear()
                folder.partial.clear()
                folder.releases.clear()
                folder.batch.clear()
                self._set_status(folder, "missing", f"Folder not found: {folder.spec.path}")
                self._dirty = True
            return
        if folder.status == "missing":
            async with self._lock:
                if folder.status == "missing":
                    await self._activate(folder)
            return  # _activate schedules a fresh full rescan
        new = {
            key: st for key, st in found.items()
            if folder.reported.get(key) != st and key not in folder.pending
        }  # fmt: skip
        known = await self._known(folder, new)
        if self._folders.get(folder.spec.id) is not folder or not self._running:
            return
        now = time.monotonic()
        releases: dict[str, str | None] = {}  # parent folder -> release folder: one lookup per folder
        for key, (size, mtime) in new.items():
            if key in known:
                folder.reported[key] = (size, mtime)  # handled earlier: never pending, never walked
                continue
            if key in folder.pending:
                continue  # an event brought it in while is_known ran
            parent = os.path.dirname(key)
            if parent not in releases:
                releases[parent] = self._release_of(folder, key)
            folder.pending[key] = _Pending(size=size, mtime_ns=mtime, stable_since=now, release=releases[parent])
        if start is None:
            for key in [k for k in folder.reported if k not in found]:
                del folder.reported[key]
            folder.partial = dict(list(partials.items())[:MAX_PARTIAL])
        else:
            for key, value in partials.items():
                if len(folder.partial) < MAX_PARTIAL or key in folder.partial:
                    folder.partial[key] = value
        self._rebuild_snapshot()

    async def _known(self, folder: _Folder, files: dict[str, tuple[int, int]]) -> set[str]:
        """The keys of ``files`` ((size, mtime_ns) each) that ``is_known`` says a job already handled,
        in one call. Never raises: on error nothing is known (the pipeline de-duplicates again)."""
        if self._is_known is None or not files:
            return set()
        try:
            handled = await self._is_known({key: mtime / 1e9 for key, (_size, mtime) in files.items()})
        except Exception:
            log.exception("watcher is_known callback failed for %s", folder.spec.path)
            return set()
        return {key for key in files if _norm(key) in handled}

    async def _vet(self, folder: _Folder, stats: dict[str, tuple[int, int] | None]) -> None:
        """Files the OS events brought in, now that their size and mtime are known: one already handed
        over and unchanged since is dropped, and so is one a job already handled (``is_known``),
        before their release folders are ever walked."""
        fresh: dict[str, tuple[int, int]] = {}
        for key, st in stats.items():
            entry = folder.pending.get(key)
            if entry is None or entry.vetted or st is None:
                continue
            if folder.reported.get(key) == st:
                del folder.pending[key]  # a late event for a file already handed over
                self._dirty = True
            else:
                fresh[key] = st
        if not fresh:
            return
        known = await self._known(folder, fresh)
        for key, st in fresh.items():
            entry = folder.pending.get(key)
            if entry is None:
                continue
            if key in known:
                del folder.pending[key]
                folder.reported[key] = st
                self._dirty = True
            else:
                entry.vetted = True

    async def _check_pending(self, folder: _Folder) -> None:
        keys = list(folder.pending)
        folder.checking = True
        try:
            stats = await asyncio.to_thread(_stat_many, keys)
            await self._vet(folder, stats)
            await self._update_releases(folder)
            now = time.monotonic()
            ready: list[str] = []
            for key, st in stats.items():
                entry = folder.pending.get(key)
                if entry is None or not entry.vetted:
                    continue
                if st is None:
                    del folder.pending[key]
                    self._dirty = True
                    continue
                if st != (entry.size, entry.mtime_ns):
                    entry.size, entry.mtime_ns, entry.stable_since = st[0], st[1], now
                    self._dirty = True
                    continue
                if (
                    entry.size > 0
                    and (entry.forced or now - entry.stable_since >= folder.settle)
                    and self._release_ready(folder, entry, now)
                ):
                    ready.append(key)
            if ready:
                unlocked = await asyncio.to_thread(lambda: [k for k in ready if _is_unlocked(k)])
                now = time.monotonic()
                for key in unlocked:
                    entry = folder.pending.pop(key, None)
                    if entry is None:
                        continue
                    self._dirty = True
                    folder.reported[key] = (entry.size, entry.mtime_ns)
                    folder.batch.append(Path(key))
                    folder.batch_last = now
                    folder.batch_forced = folder.batch_forced or entry.forced
        finally:
            folder.checking = False

    def _release_of(self, folder: _Folder, key: str) -> str | None:
        """The release folder of the file ``key`` (fixed by its path, so computed once per file)."""
        if self.release_quiet <= 0:
            return None
        release = release_mod.release_folder(key, [folder.spec.path], self.container_names)
        return None if release is None else str(release)

    async def _update_releases(self, folder: _Folder) -> None:
        """Walk the release folders of the pending files, each at most every
        ``release_check_interval``; any change to a release folder restarts its quiet period."""
        if self.release_quiet <= 0:
            return
        wanted = {entry.release for entry in folder.pending.values() if entry.release is not None and entry.vetted}
        for stale in [k for k in folder.releases if k not in wanted]:
            del folder.releases[stale]
        now = time.monotonic()
        due = [
            k for k in wanted
            if k not in folder.releases or now - folder.releases[k].checked >= self.release_check_interval
        ]  # fmt: skip
        if not due:
            return
        patterns = self._busy_patterns
        sigs = await asyncio.to_thread(lambda: {k: release_mod.signature(k, patterns) for k in due})
        now = time.monotonic()
        for key, sig in sigs.items():
            state = folder.releases.get(key)
            if state is None:
                folder.releases[key] = _Release(sig=sig, stable_since=now, checked=now)
                self._dirty = True
            elif sig != state.sig:
                state.sig, state.stable_since, state.checked, state.steady = sig, now, now, False
                self._dirty = True
            else:
                state.checked, state.steady = now, True

    def _release_ready(self, folder: _Folder, entry: _Pending, now: float) -> bool:
        """The release folder of ``entry`` (if any) has been quiet long enough and no download client
        is still writing in it. "Start now" skips the quiet period but still needs two identical walks
        and no temp files, so Refresh never takes a movie whose extras are still downloading."""
        if entry.release is None:
            return True
        state = folder.releases.get(entry.release)
        if state is None or state.sig is None or state.sig.busy:
            return False
        return (entry.forced and state.steady) or now - state.stable_since >= self.release_quiet

    def _release_wait_until(self, folder: _Folder, entry: _Pending, mono: float, wall: datetime) -> datetime | None:
        state = folder.releases.get(entry.release) if entry.release is not None else None
        if state is None:
            return None
        start = mono if state.sig is None or state.sig.busy else state.stable_since
        return wall + timedelta(seconds=max(0.0, start + self.release_quiet - mono))

    async def _check_partial(self, folder: _Folder) -> None:
        """Refresh sizes of still-downloading files (shown on Home) and drop the ones that are gone."""
        now = time.monotonic()
        if now - folder.partial_checked < PARTIAL_STAT_INTERVAL:
            return
        folder.partial_checked = now
        folder.checking = True
        try:
            stats = await asyncio.to_thread(_stat_many, list(folder.partial))
            for key, st in stats.items():
                if key not in folder.partial or folder.partial[key] == st:
                    continue
                if st is None:
                    del folder.partial[key]
                else:
                    folder.partial[key] = st
                self._dirty = True
        finally:
            folder.checking = False

    def _rebuild_snapshot(self) -> None:
        """Publish an immutable view of the waiting files for :meth:`incoming` (event loop only)."""
        mono, wall = time.monotonic(), datetime.now(UTC)
        self._dirty, self._snapshot_at = False, mono
        files: list[WatchedFile] = []
        for folder in self._folders.values():
            if folder.status != "watching" or not folder.spec.enabled:
                continue
            settle = folder.settle
            for key, entry in folder.pending.items():
                since = wall - timedelta(seconds=max(0.0, mono - entry.stable_since))
                settles = wall if entry.forced else since + timedelta(seconds=settle)
                release_at = None if entry.forced else self._release_wait_until(folder, entry, mono, wall)
                if release_at is not None and release_at > settles:
                    settles = release_at
                files.append(WatchedFile(folder_id=folder.spec.id, path=key, size=max(0, entry.size),
                                         mtime_ns=entry.mtime_ns, state="settling", settle_seconds=settle,
                                         stable_since=since, settles_at=settles))  # fmt: skip
            for key, (size, mtime) in folder.partial.items():
                files.append(WatchedFile(folder_id=folder.spec.id, path=key, size=max(0, size), mtime_ns=mtime,
                                         state="partial", settle_seconds=settle, stable_since=None,
                                         settles_at=None))  # fmt: skip
        files.sort(key=lambda f: (f.state != "settling", f.settles_at or wall, f.path.lower()))
        self._snapshot = tuple(files)

    def _maybe_flush(self, folder: _Folder) -> None:
        if not folder.batch:
            return
        if not folder.batch_forced and time.monotonic() - folder.batch_last < self._batch_window(folder):
            return
        files = sorted(dict.fromkeys(folder.batch), key=lambda p: str(p).lower())
        folder.batch = []
        folder.batch_forced = False
        self._spawn(self._deliver(folder, files))

    async def _deliver(self, folder: _Folder, files: list[Path]) -> None:
        try:
            await self._on_ready(folder.spec, files)
        except Exception as exc:
            log.exception("watcher on_ready failed for %s", folder.spec.path)
            # Offer them again once they have settled again (a full rescan may be many minutes away).
            now = time.monotonic()
            for path in files:
                key = str(path)
                folder.reported.pop(key, None)
                if key not in folder.pending:
                    folder.pending[key] = _Pending(size=-1, mtime_ns=-1, stable_since=now,
                                                   release=self._release_of(folder, key))  # fmt: skip
            self._dirty = True
            self._set_status(folder, folder.status, f"Could not queue new files: {exc}")


def _norm(path: str) -> str:
    """The comparison key ``is_known`` answers in (``services.jobs.already_handled``)."""
    return os.path.normcase(os.path.normpath(path))


def _stat_many(keys: list[str]) -> dict[str, tuple[int, int] | None]:
    out: dict[str, tuple[int, int] | None] = {}
    for key in keys:
        try:
            st = os.stat(key)
        except OSError:
            out[key] = None
        else:
            out[key] = (st.st_size, st.st_mtime_ns)
    return out
