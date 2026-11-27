"""Folder watcher: settle detection, partial files, batching, missing folders, hot reload.

Short settle times (0.2 s) keep these fast. ``WatchFolderSpec.settle_seconds`` is an int in the
contract, so the specs are built with ``model_construct`` to allow fractions.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path

import pytest

from schema_app.domain import MediaKind, WatchFolderSpec
from schema_app.services import release as release_mod
from schema_app.services.watcher import FolderWatcher

SETTLE = 0.2


def spec(folder_id: int, path: Path, *, settle: float = SETTLE, recursive: bool = True,
         enabled: bool = True) -> WatchFolderSpec:  # fmt: skip
    return WatchFolderSpec.model_construct(
        id=folder_id, path=str(path), recursive=recursive, enabled=enabled, settle_seconds=settle,
        kind_hint=MediaKind.SERIES,
    )  # fmt: skip


class Recorder:
    def __init__(self) -> None:
        self.batches: list[tuple[int, list[Path]]] = []
        self.statuses: list[tuple[int, str, str | None]] = []
        self.fail_next = False

    async def on_ready(self, folder: WatchFolderSpec, files: list[Path]) -> None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("pipeline is busy")
        self.batches.append((folder.id, list(files)))

    async def on_status(self, folder_id: int, status: str, error: str | None) -> None:
        self.statuses.append((folder_id, status, error))

    @property
    def names(self) -> list[list[str]]:
        return [sorted(p.name for p in files) for _, files in self.batches]


async def wait_for(predicate, timeout: float = 6.0) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


@pytest.fixture
async def recorder_watcher():  # type: ignore[no-untyped-def]
    rec = Recorder()
    watcher = FolderWatcher(rec.on_ready, rec.on_status, rescan_interval=0.25,
                            ignore_patterns=["*.part", "*.!qB"])  # fmt: skip
    await watcher.start()
    try:
        yield rec, watcher
    finally:
        await watcher.stop()


async def test_season_pack_becomes_one_batch(recorder_watcher, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    rec, watcher = recorder_watcher
    inbox = tmp_path / "watch"
    inbox.mkdir()
    await watcher.sync([spec(1, inbox)])
    assert watcher.status() == {1: "watching"}
    pack = inbox / "Show.S01.1080p"
    pack.mkdir()
    for n in range(1, 4):
        (pack / f"Show.S01E0{n}.mkv").write_bytes(b"x" * 100)
    (pack / "Show.S01E01.en.srt").write_bytes(b"sub")  # not a video: travels as a sidecar later
    await wait_for(lambda: rec.batches)
    await asyncio.sleep(0.5)
    assert rec.names == [["Show.S01E01.mkv", "Show.S01E02.mkv", "Show.S01E03.mkv"]]
    assert rec.batches[0][0] == 1


async def test_partial_and_ignored_files(recorder_watcher, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    rec, watcher = recorder_watcher
    inbox = tmp_path / "watch"
    inbox.mkdir()
    await watcher.sync([spec(1, inbox)])
    (inbox / "Movie.2019.mkv.part").write_bytes(b"x" * 10)
    (inbox / "Movie.2019.mkv.!qB").write_bytes(b"x" * 10)
    (inbox / ".hidden.mkv").write_bytes(b"x" * 10)
    (inbox / ".Movie.mkv.schema-tmp").write_bytes(b"x" * 10)
    (inbox / "notes.txt").write_bytes(b"x" * 10)
    (inbox / "empty.mkv").write_bytes(b"")  # zero bytes: never ready
    (inbox / "@eaDir").mkdir()
    (inbox / "@eaDir" / "thumb.mkv").write_bytes(b"x" * 10)
    await asyncio.sleep(1.0)
    assert rec.batches == []
    # The download finishes: the client renames the partial file into place.
    (inbox / "Movie.2019.mkv.part").rename(inbox / "Movie.2019.mkv")
    await wait_for(lambda: rec.batches)
    assert rec.names == [["Movie.2019.mkv"]]


async def test_growing_file_waits_until_stable(recorder_watcher, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    rec, watcher = recorder_watcher
    inbox = tmp_path / "watch"
    inbox.mkdir()
    await watcher.sync([spec(1, inbox, settle=0.3)])
    target = inbox / "Big.mkv"
    started = time.monotonic()
    with open(target, "wb") as handle:
        for _ in range(8):
            handle.write(b"x" * 1000)
            handle.flush()
            await asyncio.sleep(0.12)
            assert rec.batches == [], "reported while still being written"
    finished = time.monotonic()
    await wait_for(lambda: rec.batches)
    assert rec.names == [["Big.mkv"]]
    assert time.monotonic() - finished >= 0.25
    assert finished - started >= 0.9


async def test_each_file_reported_once(recorder_watcher, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    rec, watcher = recorder_watcher
    inbox = tmp_path / "watch"
    inbox.mkdir()
    await watcher.sync([spec(1, inbox)])
    (inbox / "A.mkv").write_bytes(b"a" * 10)
    await wait_for(lambda: rec.batches)
    await asyncio.sleep(1.0)  # several rescans later
    assert rec.names == [["A.mkv"]]
    # Replaced by a different file with the same name -> reported again.
    (inbox / "A.mkv").write_bytes(b"b" * 20)
    await wait_for(lambda: len(rec.batches) == 2)
    assert rec.names == [["A.mkv"], ["A.mkv"]]


async def test_separate_arrivals_make_separate_batches(recorder_watcher, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    rec, watcher = recorder_watcher
    inbox = tmp_path / "watch"
    inbox.mkdir()
    await watcher.sync([spec(1, inbox)])
    (inbox / "first.mkv").write_bytes(b"x" * 10)
    await wait_for(lambda: rec.batches)
    (inbox / "second.mkv").write_bytes(b"x" * 10)
    await wait_for(lambda: len(rec.batches) == 2)
    assert rec.names == [["first.mkv"], ["second.mkv"]]


async def test_non_recursive_ignores_subfolders(recorder_watcher, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    rec, watcher = recorder_watcher
    inbox = tmp_path / "watch"
    (inbox / "sub").mkdir(parents=True)
    await watcher.sync([spec(1, inbox, recursive=False)])
    (inbox / "sub" / "deep.mkv").write_bytes(b"x" * 10)
    (inbox / "top.mkv").write_bytes(b"x" * 10)
    await wait_for(lambda: rec.batches)
    await asyncio.sleep(0.6)
    assert rec.names == [["top.mkv"]]


async def test_existing_files_found_on_start(tmp_path: Path) -> None:
    inbox = tmp_path / "watch"
    inbox.mkdir()
    (inbox / "waiting.mkv").write_bytes(b"x" * 10)
    rec = Recorder()
    watcher = FolderWatcher(rec.on_ready, rec.on_status, rescan_interval=30)
    await watcher.sync([spec(7, inbox)])
    assert watcher.status() == {7: "stopped"}
    await watcher.start()
    try:
        await wait_for(lambda: rec.batches)
        assert rec.names == [["waiting.mkv"]]
        assert (7, "watching", None) in rec.statuses
    finally:
        await watcher.stop()
    assert watcher.status() == {7: "stopped"}


async def test_missing_folder_is_retried(recorder_watcher, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    rec, watcher = recorder_watcher
    inbox = tmp_path / "not-mounted-yet"
    await watcher.sync([spec(3, inbox)])
    assert watcher.status() == {3: "missing"}
    assert watcher.errors()[3] and "not found" in watcher.errors()[3]
    inbox.mkdir()
    await wait_for(lambda: watcher.status()[3] == "watching")
    (inbox / "late.mkv").write_bytes(b"x" * 10)
    await wait_for(lambda: rec.batches)
    assert rec.names == [["late.mkv"]]
    statuses = [s for fid, s, _ in rec.statuses if fid == 3]
    assert statuses[:2] == ["missing", "watching"]


async def test_folder_removed_while_watching(recorder_watcher, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    rec, watcher = recorder_watcher
    inbox = tmp_path / "usb"
    inbox.mkdir()
    await watcher.sync([spec(4, inbox)])
    assert watcher.status() == {4: "watching"}
    try:
        shutil.rmtree(inbox)
    except PermissionError:
        pytest.skip("the OS keeps a watched folder busy")
    await wait_for(lambda: watcher.status()[4] == "missing")


async def test_sync_adds_updates_and_removes(recorder_watcher, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    rec, watcher = recorder_watcher
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    await watcher.sync([spec(1, a), spec(2, b, enabled=False)])
    assert watcher.status() == {1: "watching", 2: "stopped"}
    (b / "ignored-while-disabled.mkv").write_bytes(b"x" * 10)
    await asyncio.sleep(0.6)
    assert rec.batches == []
    await watcher.sync([spec(2, b)])  # remove 1, enable 2
    assert watcher.status() == {2: "watching"}
    await wait_for(lambda: rec.batches)
    assert rec.batches[0][0] == 2 and rec.names == [["ignored-while-disabled.mkv"]]
    (a / "after-removal.mkv").write_bytes(b"x" * 10)
    await asyncio.sleep(0.8)
    assert len(rec.batches) == 1


async def test_failed_delivery_is_offered_again(recorder_watcher, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    rec, watcher = recorder_watcher
    inbox = tmp_path / "watch"
    inbox.mkdir()
    rec.fail_next = True
    await watcher.sync([spec(1, inbox)])
    (inbox / "retry.mkv").write_bytes(b"x" * 10)
    await wait_for(lambda: rec.batches)
    assert rec.names == [["retry.mkv"]]


async def test_custom_video_extensions(tmp_path: Path) -> None:
    rec = Recorder()
    watcher = FolderWatcher(rec.on_ready, rescan_interval=0.25, video_extensions=[".MKV"])
    inbox = tmp_path / "watch"
    inbox.mkdir()
    await watcher.start()
    try:
        await watcher.sync([spec(1, inbox)])
        (inbox / "a.mp4").write_bytes(b"x" * 10)
        (inbox / "b.mkv").write_bytes(b"x" * 10)
        await wait_for(lambda: rec.batches)
        await asyncio.sleep(0.4)
        assert rec.names == [["b.mkv"]]
    finally:
        await watcher.stop()


async def test_stop_is_idempotent_and_restartable(tmp_path: Path) -> None:
    rec = Recorder()
    watcher = FolderWatcher(rec.on_ready, rescan_interval=0.25)
    await watcher.stop()  # never started
    inbox = tmp_path / "watch"
    inbox.mkdir()
    await watcher.sync([spec(1, inbox)])
    await watcher.start()
    await watcher.stop()
    await watcher.stop()
    await watcher.start()
    try:
        (inbox / "x.mkv").write_bytes(b"x" * 10)
        await wait_for(lambda: rec.batches)
    finally:
        await watcher.stop()


# --------------------------------------------------------------------------- incoming snapshot / start now


async def test_incoming_snapshot_shows_settling_and_partial_files(recorder_watcher, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    rec, watcher = recorder_watcher
    inbox = tmp_path / "watch"
    inbox.mkdir()
    await watcher.sync([spec(1, inbox, settle=30)])
    (inbox / "Movie.2019.mkv").write_bytes(b"x" * 100)
    (inbox / "Other.2020.mkv.part").write_bytes(b"x" * 50)
    (inbox / "notes.txt.part").write_bytes(b"x")  # not a video download: not shown

    def states() -> dict[str, str]:
        return {Path(f.path).name: f.state for f in watcher.incoming()}

    await wait_for(lambda: states() == {"Movie.2019.mkv": "settling", "Other.2020.mkv.part": "partial"})
    await wait_for(lambda: any(f.size == 100 for f in watcher.incoming() if f.state == "settling"))
    by_name = {Path(f.path).name: f for f in watcher.incoming()}
    settling, partial = by_name["Movie.2019.mkv"], by_name["Other.2020.mkv.part"]
    assert settling.folder_id == 1 and settling.settle_seconds == 30
    assert settling.stable_since is not None and settling.settles_at is not None
    assert 29 <= (settling.settles_at - settling.stable_since).total_seconds() <= 31
    assert partial.stable_since is None and partial.settles_at is None and partial.size == 50
    assert rec.batches == []
    # The download completes: the partial disappears, the finished file starts settling.
    (inbox / "Other.2020.mkv.part").rename(inbox / "Other.2020.mkv")
    await wait_for(lambda: states() == {"Movie.2019.mkv": "settling", "Other.2020.mkv": "settling"})


async def test_start_now_skips_the_settle_wait(recorder_watcher, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    rec, watcher = recorder_watcher
    inbox = tmp_path / "watch"
    inbox.mkdir()
    await watcher.sync([spec(1, inbox, settle=600)])
    (inbox / "A.2019.mkv").write_bytes(b"a" * 10)
    (inbox / "B.2019.mkv").write_bytes(b"b" * 10)
    (inbox / "C.2019.mkv.part").write_bytes(b"c" * 10)
    await wait_for(lambda: len(watcher.incoming()) == 3)
    await asyncio.sleep(0.6)
    assert rec.batches == []
    assert await watcher.start_now(99) == 0  # unknown folder
    assert await watcher.start_now(1, str(inbox / "C.2019.mkv.part")) == 0  # partial files never start
    assert await watcher.start_now(1, str(inbox / "A.2019.mkv")) == 1
    await wait_for(lambda: rec.batches)
    assert rec.names == [["A.2019.mkv"]]
    assert sorted(Path(f.path).name for f in watcher.incoming()) == ["B.2019.mkv", "C.2019.mkv.part"]
    assert await watcher.start_now(1) == 1  # every settling file of the folder
    await wait_for(lambda: len(rec.batches) == 2)
    assert rec.names[1] == ["B.2019.mkv"]


async def test_scan_now_finds_and_starts_finished_files(recorder_watcher, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    rec, watcher = recorder_watcher
    inbox = tmp_path / "watch"
    inbox.mkdir()
    await watcher.sync([spec(1, inbox, settle=600)])
    (inbox / "A.2019.mkv").write_bytes(b"a" * 10)
    (inbox / "B.2019.mkv").write_bytes(b"b" * 10)
    (inbox / "C.2019.mkv.part").write_bytes(b"c" * 10)
    # No waiting for the watcher to notice: the scan walks the folder itself. Partials never start.
    assert await watcher.scan_now() == 2
    await wait_for(lambda: rec.batches)
    assert sorted(sum(rec.names, [])) == ["A.2019.mkv", "B.2019.mkv"]
    assert [Path(f.path).name for f in watcher.incoming()] == ["C.2019.mkv.part"]
    assert await watcher.scan_now() == 0  # already handed over: not offered again


async def test_release_folder_waits_for_the_whole_download(tmp_path: Path) -> None:
    """The movie is not handed over while its featurettes still download (War Dogs, Nov 2026)."""
    rec = Recorder()
    watcher = FolderWatcher(rec.on_ready, rec.on_status, rescan_interval=0.25, ignore_patterns=["*.part"],
                            release_quiet=0.6)  # fmt: skip
    await watcher.start()
    try:
        inbox = tmp_path / "watch"
        release = inbox / "radarr" / "War.Dogs.2016.1080p"
        (release / "Featurettes").mkdir(parents=True)
        await watcher.sync([spec(1, inbox)])
        (release / "War.Dogs.2016.1080p.mkv").write_bytes(b"x" * 100)
        (release / "Featurettes" / "Making.Of.mkv.part").write_bytes(b"x")
        # The main file is long settled, but the release still has a download in progress.
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            with (release / "Featurettes" / "Making.Of.mkv.part").open("ab") as fh:
                fh.write(b"x")
            await asyncio.sleep(0.1)
        assert rec.batches == []
        assert any(f.state == "settling" for f in watcher.incoming())
        (release / "Featurettes" / "Making.Of.mkv.part").rename(release / "Featurettes" / "Making.Of.mkv")
        # Home "Refresh" skips the quiet period once the folder is steady, for the whole release.
        await watcher.scan_now()
        await wait_for(lambda: rec.batches)
        await asyncio.sleep(0.3)
        assert rec.names == [["Making.Of.mkv", "War.Dogs.2016.1080p.mkv"]]

        # A loose file right in a category folder has no release folder: only its own settle applies.
        (inbox / "radarr" / "Loose.2010.mkv").write_bytes(b"x" * 10)
        await wait_for(lambda: len(rec.batches) == 2)
        assert rec.names[1] == ["Loose.2010.mkv"]
    finally:
        await watcher.stop()


async def test_known_files_never_become_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """After a restart, files a job already handled (seeding in copy / link mode) are not offered again
    and their release folders are never walked; the question is asked once per rescan, batched."""
    inbox = tmp_path / "watch"
    seeding = inbox / "Seeding.Movie.2019.1080p"
    seeding.mkdir(parents=True)
    (seeding / "Seeding.Movie.2019.1080p.mkv").write_bytes(b"x" * 10)
    (inbox / "New.Movie.2020.mkv").write_bytes(b"x" * 10)
    asked: list[dict[str, float | None]] = []

    async def is_known(files: dict[str, float | None]) -> set[str]:
        asked.append(dict(files))
        return {os.path.normcase(os.path.normpath(p)) for p in files if "Seeding" in p}

    walked: list[str] = []
    real_signature = release_mod.signature

    def signature(folder, *args, **kwargs):  # type: ignore[no-untyped-def]
        walked.append(str(folder))
        return real_signature(folder, *args, **kwargs)

    monkeypatch.setattr(release_mod, "signature", signature)
    rec = Recorder()
    watcher = FolderWatcher(rec.on_ready, rec.on_status, rescan_interval=0.25, release_quiet=0.2, is_known=is_known)
    await watcher.sync([spec(1, inbox)])
    await watcher.start()
    try:
        await wait_for(lambda: rec.batches)
        await asyncio.sleep(0.8)  # several rescans later
        assert rec.names == [["New.Movie.2020.mkv"]]
        assert len(asked) == 1 and sorted(Path(p).name for p in asked[0]) == [
            "New.Movie.2020.mkv", "Seeding.Movie.2019.1080p.mkv",
        ]  # fmt: skip
        assert all("Seeding" not in w for w in walked)
        assert all("Seeding" not in f.path for f in watcher.incoming())
        # A file the OS events bring in is asked about too, before its folder is walked.
        (seeding / "Seeding.Movie.2019.1080p.Extras.mkv").write_bytes(b"x" * 10)
        await wait_for(lambda: len(asked) >= 2)
        await asyncio.sleep(0.6)
        assert rec.names == [["New.Movie.2020.mkv"]]
        assert all("Seeding" not in w for w in walked)
    finally:
        await watcher.stop()


async def test_is_known_failure_falls_back_to_offering_files(tmp_path: Path) -> None:
    async def broken(files: dict[str, float | None]) -> set[str]:
        raise RuntimeError("database is locked")

    inbox = tmp_path / "watch"
    inbox.mkdir()
    (inbox / "A.2019.mkv").write_bytes(b"x" * 10)
    rec = Recorder()
    watcher = FolderWatcher(rec.on_ready, rescan_interval=0.25, is_known=broken)
    await watcher.sync([spec(1, inbox)])
    await watcher.start()
    try:
        await wait_for(lambda: rec.batches)
        assert rec.names == [["A.2019.mkv"]]
    finally:
        await watcher.stop()


async def test_live_folders_are_rescanned_rarely(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    live, polled = tmp_path / "live", tmp_path / "smb"
    live.mkdir()
    polled.mkdir()
    rec = Recorder()
    watcher = FolderWatcher(rec.on_ready, rec.on_status, rescan_interval=0.1, live_rescan_interval=60)
    walks: dict[int, int] = {1: 0, 2: 0}
    real_walk = watcher._walk

    def counting(folder, start, partials=None):  # type: ignore[no-untyped-def]
        walks[folder.spec.id] += start is None
        return real_walk(folder, start, partials)

    monkeypatch.setattr(watcher, "_walk", counting)
    await watcher.start()
    try:
        await watcher.sync([spec(1, live)])

        def no_events(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise OSError("inotify watch limit reached")

        monkeypatch.setattr(watcher._observer, "schedule", no_events)
        await watcher.sync([spec(1, live), spec(2, polled)])
        assert "Live file events unavailable" in (watcher.errors()[2] or "")
        await asyncio.sleep(1.0)
        assert walks[1] == 1  # the scan when it started; events do the rest
        assert walks[2] >= 4  # no events: polled every rescan_interval
        (polled / "late.mkv").write_bytes(b"x" * 10)
        await wait_for(lambda: rec.batches)
        assert rec.names == [["late.mkv"]]
    finally:
        await watcher.stop()


async def test_events_for_tracked_files_skip_the_filters(recorder_watcher, tmp_path: Path,  # type: ignore[no-untyped-def]
                                                         monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    rec, watcher = recorder_watcher
    inbox = tmp_path / "watch"
    inbox.mkdir()
    await watcher.sync([spec(1, inbox, settle=600)])
    movie, partial = inbox / "Movie.2019.mkv", inbox / "Other.2020.mkv.part"
    movie.write_bytes(b"x" * 10)
    partial.write_bytes(b"x")
    await wait_for(lambda: len(watcher.incoming()) == 2)
    calls: list[object] = []
    monkeypatch.setattr(watcher, "_relevant", lambda *a: calls.append(a) or True)
    monkeypatch.setattr(watcher, "_partial", lambda *a: calls.append(a) or True)
    for _ in range(500):  # a download writing in small chunks
        watcher._on_event(1, "modified", str(movie), "", False)
        watcher._on_event(1, "modified", str(partial), "", False)
    assert calls == []


async def test_late_event_for_a_handed_over_file_is_not_offered_again(recorder_watcher, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    rec, watcher = recorder_watcher
    inbox = tmp_path / "watch"
    inbox.mkdir()
    await watcher.sync([spec(1, inbox)])
    (inbox / "A.2019.mkv").write_bytes(b"a" * 10)
    await wait_for(lambda: rec.batches)
    watcher._on_event(1, "modified", str(inbox / "A.2019.mkv"), "", False)  # e.g. an attribute change
    await asyncio.sleep(1.6)  # an idle tick (0.5 s) + settle + batch window, with room to spare
    assert rec.names == [["A.2019.mkv"]]


async def test_release_folder_is_looked_up_once_per_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lookups: list[str] = []
    real_release_folder = release_mod.release_folder

    def release_folder(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        lookups.append(str(path))
        return real_release_folder(path, *args, **kwargs)

    monkeypatch.setattr(release_mod, "release_folder", release_folder)
    rec = Recorder()
    watcher = FolderWatcher(rec.on_ready, rescan_interval=0.25, ignore_patterns=["*.part"], release_quiet=0.4)
    inbox = tmp_path / "watch"
    pack = inbox / "Show.S01"
    pack.mkdir(parents=True)
    await watcher.sync([spec(1, inbox, settle=0.1)])
    await watcher.start()
    try:
        for n in (1, 2, 3):
            (pack / f"Show.S01E0{n}.mkv").write_bytes(b"x" * 10)
        (pack / "Show.S01E04.mkv.part").write_bytes(b"x")  # keeps the release busy: dozens of ticks
        await asyncio.sleep(1.2)
        assert rec.batches == []
        (pack / "Show.S01E04.mkv.part").rename(pack / "Show.S01E04.mkv")
        await wait_for(lambda: rec.batches)
        assert len(lookups) <= 10  # once per file (event or rescan), not per file and tick
    finally:
        await watcher.stop()


async def test_snapshot_is_rebuilt_only_when_something_changes(tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    rec = Recorder()
    watcher = FolderWatcher(rec.on_ready, rescan_interval=30, release_quiet=40)
    inbox = tmp_path / "watch"
    pack = inbox / "Show.S01"
    pack.mkdir(parents=True)
    (pack / "Show.S01E01.mkv").write_bytes(b"x" * 10)
    (pack / "Show.S01E02.mkv.part").write_bytes(b"x")  # still downloading: E01 keeps waiting
    await watcher.sync([spec(1, inbox, settle=0.2)])
    await watcher.start()
    try:
        await wait_for(lambda: any(f.state == "settling" and f.size == 10 for f in watcher.incoming()))
        await asyncio.sleep(0.5)
        builds = 0
        real_rebuild = watcher._rebuild_snapshot

        def counting() -> None:
            nonlocal builds
            builds += 1
            real_rebuild()

        monkeypatch.setattr(watcher, "_rebuild_snapshot", counting)
        await asyncio.sleep(1.0)  # ~20 ticks, nothing changes
        assert builds <= 1
        assert rec.batches == [] and len(watcher.incoming()) == 2
    finally:
        await watcher.stop()


async def test_release_quiet_period_without_start_now(tmp_path: Path) -> None:
    rec = Recorder()
    watcher = FolderWatcher(rec.on_ready, rec.on_status, rescan_interval=0.25, release_quiet=0.8)
    await watcher.start()
    try:
        inbox = tmp_path / "watch"
        release = inbox / "Show.S01"
        release.mkdir(parents=True)
        await watcher.sync([spec(1, inbox)])
        (release / "Show.S01E01.mkv").write_bytes(b"x" * 10)
        started = time.monotonic()
        for n in range(2, 5):  # the next episodes keep arriving, a little apart
            await asyncio.sleep(0.3)
            (release / f"Show.S01E0{n}.mkv").write_bytes(b"x" * 10)
        await wait_for(lambda: rec.batches)
        assert time.monotonic() - started >= 0.9 + 0.8 - 0.1
        await asyncio.sleep(0.3)
        assert rec.names == [[f"Show.S01E0{n}.mkv" for n in range(1, 5)]]
    finally:
        await watcher.stop()
