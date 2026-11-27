"""File operations: every mode x conflict policy, sidecars, undo, boundaries, crash safety."""

from __future__ import annotations

import errno
import json
import os
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from schema_app.domain import ConflictPolicy, FileOpPlan, FileOpResult, RenameMode
from schema_app.services import fileops, hashing, paths
from schema_app.services.fileops import execute, find_sidecars, plan_operation, undo

ALL_MODES = list(RenameMode)
LINK_MODES = {RenameMode.SYMLINK, RenameMode.KEEPLINK}
SUBS = ["srt", "ass", "ssa", "sub", "idx", "sup", "vtt", "nfo"]


def _skip_without_symlinks(mode: RenameMode, request: pytest.FixtureRequest) -> None:
    if mode in LINK_MODES:
        request.getfixturevalue("need_symlinks")


def _leftovers(folder: Path) -> list[Path]:
    return [p for p in folder.rglob("*") if p.name.endswith((fileops.TEMP_SUFFIX, fileops.LINK_SUFFIX))]


def _assert_applied(mode: RenameMode, src: Path, dst: Path, content: bytes) -> None:
    if mode is RenameMode.MOVE:
        assert not os.path.lexists(src)
        assert dst.read_bytes() == content and not dst.is_symlink()
    elif mode is RenameMode.COPY:
        assert src.read_bytes() == content
        assert dst.read_bytes() == content and not os.path.samefile(src, dst)
    elif mode is RenameMode.HARDLINK:
        assert os.path.samefile(src, dst)
    elif mode is RenameMode.SYMLINK:
        assert dst.is_symlink() and os.path.samefile(dst, src) and src.read_bytes() == content
    elif mode is RenameMode.KEEPLINK:
        assert dst.read_bytes() == content and not dst.is_symlink()
        assert src.is_symlink() and os.path.samefile(src, dst)
    elif mode is RenameMode.TEST:
        assert src.read_bytes() == content


# --------------------------------------------------------------------------- sidecars


def test_find_sidecars_keeps_suffixes_and_respects_claims(
    lib: dict[str, Path], make_file: Callable[..., Path]
) -> None:
    d = lib["downloads"] / "Movie.2019"
    video = make_file(d / "Movie.2019.1080p.mkv")
    names = [
        "Movie.2019.1080p.en.srt",
        "Movie.2019.1080p.eng.forced.ass",
        "Movie.2019.1080p.idx",
        "Movie.2019.1080p.sub",
        "Movie.2019.1080p.nfo",
        "movie.2019.1080p.EN.SDH.SRT",  # case-insensitive stem + extension
    ]
    for name in names:
        make_file(d / name, b"sub")
    make_file(d / "Movie.2019.1080p.Extended.mkv")  # a second video with a longer stem
    make_file(d / "Movie.2019.1080p.Extended.en.srt", b"sub")  # belongs to the Extended cut
    make_file(d / "Movie.2019.1080p.jpg", b"img")  # not a sidecar extension
    make_file(d / "Other.en.srt", b"sub")
    found = [p.name for p in find_sidecars(video, SUBS)]
    assert sorted(found, key=str.lower) == sorted(names, key=str.lower)
    assert find_sidecars(video, []) == []
    assert {p.name for p in find_sidecars(video, [".SRT"])} == {
        "Movie.2019.1080p.en.srt",
        "movie.2019.1080p.EN.SDH.SRT",
    }


def test_plan_computes_sidecar_destinations(lib: dict[str, Path], make_file: Callable[..., Path]) -> None:
    src = make_file(lib["downloads"] / "Show.S01E01.720p.mkv")
    make_file(lib["downloads"] / "Show.S01E01.720p.en.srt", b"s")
    make_file(lib["downloads"] / "Show.S01E01.720p.eng.forced.ass", b"s")
    dst = lib["library"] / "Show" / "Season 01" / "Show - S01E01 - Pilot.mkv"
    plan = plan_operation(src, dst, RenameMode.MOVE, ConflictPolicy.SKIP, sidecar_extensions=SUBS)
    assert plan.conflict == "none"
    assert {Path(s.destination).name for s in plan.sidecars} == {
        "Show - S01E01 - Pilot.en.srt",
        "Show - S01E01 - Pilot.eng.forced.ass",
    }
    assert plan_operation(src, dst, RenameMode.MOVE, ConflictPolicy.SKIP).sidecars == []


def test_sidecars_travel_and_come_back(
    lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    src = make_file(lib["downloads"] / "Movie.2019.mkv", b"movie")
    subs = {
        "Movie.2019.en.srt": b"en",
        "Movie.2019.eng.forced.ass": b"forced",
        "Movie.2019.idx": b"idx",
        "Movie.2019.sub": b"sub",
    }
    for name, data in subs.items():
        make_file(lib["downloads"] / name, data)
    dst = lib["library"] / "Movie (2019)" / "Movie (2019).mkv"
    plan = plan_operation(src, dst, RenameMode.MOVE, ConflictPolicy.SKIP, sidecar_extensions=SUBS)
    result = execute(plan, allowed_roots=roots)
    assert result.ok and not result.skipped and result.verified
    moved = {Path(s.destination).name: Path(s.destination) for s in result.sidecars}
    assert set(moved) == {
        "Movie (2019).en.srt",
        "Movie (2019).eng.forced.ass",
        "Movie (2019).idx",
        "Movie (2019).sub",
    }
    assert moved["Movie (2019).eng.forced.ass"].read_bytes() == b"forced"
    assert not any((lib["downloads"] / n).exists() for n in subs)

    back = undo(result, allowed_roots=roots)
    assert back.ok, back.error
    assert src.read_bytes() == b"movie"
    for name, data in subs.items():
        assert (lib["downloads"] / name).read_bytes() == data
    assert not dst.exists() and not any(p.exists() for p in moved.values())


def test_sidecar_conflict_under_skip_leaves_that_sidecar(
    lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    src = make_file(lib["downloads"] / "M.mkv", b"movie")
    make_file(lib["downloads"] / "M.en.srt", b"new-sub")
    make_file(lib["downloads"] / "M.de.srt", b"de-sub")
    dst = lib["library"] / "M (2000).mkv"
    make_file(lib["library"] / "M (2000).en.srt", b"old-sub")  # orphan subtitle already there
    plan = plan_operation(src, dst, RenameMode.MOVE, ConflictPolicy.SKIP, sidecar_extensions=SUBS)
    result = execute(plan, allowed_roots=roots)
    assert result.ok
    assert (lib["library"] / "M (2000).en.srt").read_bytes() == b"old-sub"
    assert (lib["downloads"] / "M.en.srt").read_bytes() == b"new-sub"  # stayed behind
    assert (lib["library"] / "M (2000).de.srt").read_bytes() == b"de-sub"
    assert "M.en.srt" in " ".join(result.warnings)  # note on a successful result


# --------------------------------------------------------------------------- mode x policy matrix


@pytest.mark.parametrize("mode", ALL_MODES)
def test_no_conflict_each_mode(
    mode: RenameMode, lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path],
    request: pytest.FixtureRequest,
) -> None:  # fmt: skip
    _skip_without_symlinks(mode, request)
    src = make_file(lib["downloads"] / "a.mkv", b"incoming")
    dst = lib["library"] / "Show" / "Season 01" / "A.mkv"
    plan = plan_operation(src, dst, mode, ConflictPolicy.SKIP)
    result = execute(plan, allowed_roots=roots, trash_dir=lib["trash"])
    assert result.ok, result.error
    assert result.destination == str(dst)
    assert result.bytes == len(b"incoming")
    _assert_applied(mode, src, dst, b"incoming")
    if mode is RenameMode.TEST:
        assert not dst.exists() and not dst.parent.exists()
    else:
        assert result.verified
    assert _leftovers(lib["downloads"]) == [] and _leftovers(lib["library"]) == []


EXPECTED = {
    ConflictPolicy.SKIP: "skipped",
    ConflictPolicy.FAIL: "failed",
    ConflictPolicy.OVERWRITE: "replaced",
    ConflictPolicy.INDEX: "indexed",
}


@pytest.mark.parametrize("policy", list(EXPECTED))
@pytest.mark.parametrize("mode", ALL_MODES)
def test_conflict_matrix(
    mode: RenameMode, policy: ConflictPolicy, lib: dict[str, Path], roots: list[str],
    make_file: Callable[..., Path], request: pytest.FixtureRequest,
) -> None:  # fmt: skip
    _skip_without_symlinks(mode, request)
    src = make_file(lib["downloads"] / "a.mkv", b"incoming-file")
    dst = make_file(lib["library"] / "A.mkv", b"existing")
    plan = plan_operation(src, dst, mode, policy)
    assert plan.conflict == "exists"
    result = execute(plan, allowed_roots=roots, trash_dir=lib["trash"])
    outcome = EXPECTED[policy]

    if outcome == "skipped":
        assert not result.ok and result.skipped and "already exists" in (result.error or "")
        assert src.read_bytes() == b"incoming-file" and dst.read_bytes() == b"existing"
    elif outcome == "failed":
        assert not result.ok and not result.skipped and "already exists" in (result.error or "")
        assert src.read_bytes() == b"incoming-file" and dst.read_bytes() == b"existing"
    elif outcome == "replaced":
        assert result.ok, result.error
        if mode is RenameMode.TEST:
            assert dst.read_bytes() == b"existing" and result.replaced is None
        else:
            _assert_applied(mode, src, dst, b"incoming-file")
            assert result.replaced and Path(result.replaced).read_bytes() == b"existing"
            assert Path(result.replaced).is_relative_to(lib["trash"])
            manifest = json.loads((Path(result.replaced).parent / fileops.TRASH_MANIFEST).read_text())
            assert manifest["items"][0]["original"] == str(dst)
    else:  # indexed
        assert result.ok, result.error
        assert result.destination == str(lib["library"] / "A (1).mkv")
        assert dst.read_bytes() == b"existing"
        _assert_applied(mode, src, Path(result.destination), b"incoming-file")
        if mode is RenameMode.TEST:
            assert not Path(result.destination).exists()
    assert _leftovers(lib["library"]) == []


@pytest.mark.parametrize("mode", ALL_MODES)
@pytest.mark.parametrize(
    ("incoming", "existing", "replace"),
    [(4000, 3000, True), (3000, 4000, False), (None, 3000, False), (4000, None, False)],
)
def test_upgrade_policy(
    mode: RenameMode, incoming: int | None, existing: int | None, replace: bool, lib: dict[str, Path],
    roots: list[str], make_file: Callable[..., Path], request: pytest.FixtureRequest,
) -> None:  # fmt: skip
    _skip_without_symlinks(mode, request)
    src = make_file(lib["downloads"] / "a.mkv", b"incoming-2160p")
    dst = make_file(lib["library"] / "A.mkv", b"existing-1080p")
    plan = plan_operation(
        src, dst, mode, ConflictPolicy.UPGRADE, incoming_rank=incoming, existing_rank=existing
    )
    result = execute(plan, allowed_roots=roots, trash_dir=lib["trash"])
    if replace:
        assert result.ok, result.error
        if mode is not RenameMode.TEST:
            _assert_applied(mode, src, dst, b"incoming-2160p")
            assert Path(result.replaced or "").read_bytes() == b"existing-1080p"
    else:
        assert not result.ok and result.skipped
        assert dst.read_bytes() == b"existing-1080p" and src.read_bytes() == b"incoming-2160p"


def test_upgrade_tie_breaks_on_size(
    lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    src = make_file(lib["downloads"] / "a.mkv", b"bigger-incoming")
    dst = make_file(lib["library"] / "A.mkv", b"small")
    plan = plan_operation(
        src, dst, RenameMode.MOVE, ConflictPolicy.UPGRADE, incoming_rank=4200, existing_rank=4200
    )
    result = execute(plan, allowed_roots=roots, trash_dir=lib["trash"])
    assert result.ok and dst.read_bytes() == b"bigger-incoming"
    assert "larger" in " ".join(result.warnings)  # the decision is explained on the result

    src2 = make_file(lib["downloads"] / "b.mkv", b"tiny")
    dst2 = make_file(lib["library"] / "B.mkv", b"existing-bigger")
    plan2 = plan_operation(
        src2, dst2, RenameMode.MOVE, ConflictPolicy.UPGRADE, incoming_rank=1, existing_rank=1
    )
    result2 = execute(plan2, allowed_roots=roots, trash_dir=lib["trash"])
    assert not result2.ok and result2.skipped and "same quality rank" in (result2.error or "")
    assert dst2.read_bytes() == b"existing-bigger"


def test_index_skips_taken_numbers_and_moves_sidecars(
    lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    src = make_file(lib["downloads"] / "x.mkv", b"new")
    make_file(lib["downloads"] / "x.en.srt", b"sub")
    dst = make_file(lib["library"] / "Movie (2019).mkv", b"v0")
    make_file(lib["library"] / "Movie (2019) (1).mkv", b"v1")
    make_file(lib["library"] / "Movie (2019) (2).en.srt", b"orphan")  # makes (2) unusable for the sidecar
    plan = plan_operation(src, dst, RenameMode.MOVE, ConflictPolicy.INDEX, sidecar_extensions=SUBS)
    result = execute(plan, allowed_roots=roots)
    assert result.ok
    assert Path(result.destination).name == "Movie (2019) (3).mkv"
    assert [Path(s.destination).name for s in result.sidecars] == ["Movie (2019) (3).en.srt"]
    back = undo(result, allowed_roots=roots)
    assert back.ok and src.read_bytes() == b"new" and (lib["downloads"] / "x.en.srt").exists()


# --------------------------------------------------------------------------- same file / case-only


def test_same_file_hardlink_counts_as_done(
    lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    src = make_file(lib["downloads"] / "a.mkv", b"data")
    dst = lib["library"] / "A.mkv"
    os.link(src, dst)
    plan = plan_operation(src, dst, RenameMode.MOVE, ConflictPolicy.FAIL)
    assert plan.conflict == "same_file"
    result = execute(plan, allowed_roots=roots)
    assert result.ok and result.skipped and result.error is None
    assert src.exists() and os.path.samefile(src, dst)
    assert undo(result, allowed_roots=roots).skipped  # nothing to undo


def test_identical_content_counts_as_done(
    lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    src = make_file(lib["downloads"] / "a.mkv", b"same-bytes")
    dst = make_file(lib["library"] / "A.mkv", b"same-bytes")
    plan = plan_operation(src, dst, RenameMode.MOVE, ConflictPolicy.OVERWRITE)
    assert plan.conflict == "same_file"
    result = execute(plan, allowed_roots=roots, trash_dir=lib["trash"])
    assert result.ok and result.skipped and result.replaced is None
    assert src.exists() and dst.read_bytes() == b"same-bytes"
    assert "identical copy" in " ".join(result.warnings)
    assert not any(lib["trash"].iterdir())


def test_same_size_different_bytes_is_a_conflict(
    lib: dict[str, Path], make_file: Callable[..., Path]
) -> None:
    src = make_file(lib["downloads"] / "a.mkv", b"AAAA")
    dst = make_file(lib["library"] / "A.mkv", b"AAAB")
    assert plan_operation(src, dst, RenameMode.MOVE, ConflictPolicy.SKIP).conflict == "exists"


def test_same_path_is_done(lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]) -> None:
    src = make_file(lib["library"] / "A.mkv")
    result = execute(plan_operation(src, src, RenameMode.MOVE, ConflictPolicy.FAIL), allowed_roots=roots)
    assert result.ok and result.skipped and src.exists()


def test_case_only_rename(lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]) -> None:
    src = make_file(lib["library"] / "the office - s01e01.mkv", b"ep")
    dst = lib["library"] / "The Office - S01E01.mkv"
    plan = plan_operation(src, dst, RenameMode.MOVE, ConflictPolicy.FAIL)
    assert plan.conflict == "none"
    result = execute(plan, allowed_roots=roots)
    assert result.ok and not result.skipped, result.error
    assert os.listdir(lib["library"]) == ["The Office - S01E01.mkv"]
    back = undo(result, allowed_roots=roots)
    assert back.ok, back.error
    assert os.listdir(lib["library"]) == ["the office - s01e01.mkv"]


# --------------------------------------------------------------------------- boundaries


def test_refuses_destination_outside_roots(
    lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    src = make_file(lib["downloads"] / "a.mkv")
    dst = lib["outside"] / "A.mkv"
    result = execute(plan_operation(src, dst, RenameMode.MOVE, ConflictPolicy.SKIP), allowed_roots=roots)
    assert not result.ok and "outside the allowed roots" in (result.error or "")
    assert src.exists() and not dst.exists()


def test_refuses_source_outside_roots(
    lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    src = make_file(lib["outside"] / "a.mkv")
    result = execute(plan_operation(src, lib["library"] / "A.mkv", RenameMode.COPY, ConflictPolicy.SKIP),
                     allowed_roots=roots)  # fmt: skip
    assert not result.ok and "outside the allowed roots" in (result.error or "")


def test_refuses_traversal_and_relative(
    lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    src = make_file(lib["downloads"] / "a.mkv")
    sneaky = FileOpPlan(source=str(src), destination=str(lib["library"]) + "/../outside/A.mkv",
                        mode=RenameMode.MOVE, policy=ConflictPolicy.SKIP)  # fmt: skip
    assert not execute(sneaky, allowed_roots=roots).ok
    relative = FileOpPlan(
        source=str(src), destination="A.mkv", mode=RenameMode.MOVE, policy=ConflictPolicy.SKIP
    )
    assert "absolute" in (execute(relative, allowed_roots=roots).error or "")
    assert src.exists()


def test_refuses_without_roots(lib: dict[str, Path], make_file: Callable[..., Path]) -> None:
    src = make_file(lib["downloads"] / "a.mkv")
    result = execute(plan_operation(src, lib["library"] / "A.mkv", RenameMode.MOVE, ConflictPolicy.SKIP),
                     allowed_roots=[])  # fmt: skip
    assert not result.ok and "No allowed roots" in (result.error or "")


def test_refuses_symlink_escape(lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path],
                                need_symlinks: None) -> None:  # fmt: skip
    src = make_file(lib["downloads"] / "a.mkv")
    os.symlink(lib["outside"], lib["library"] / "escape", target_is_directory=True)
    result = execute(plan_operation(src, lib["library"] / "escape" / "A.mkv", RenameMode.MOVE, ConflictPolicy.SKIP),
                     allowed_roots=roots)  # fmt: skip
    assert not result.ok and "outside the allowed roots" in (result.error or "")
    assert src.exists() and not (lib["outside"] / "A.mkv").exists()


def test_source_missing_and_folder(lib: dict[str, Path], roots: list[str]) -> None:
    gone = execute(plan_operation(lib["downloads"] / "nope.mkv", lib["library"] / "x.mkv",
                                  RenameMode.MOVE, ConflictPolicy.SKIP), allowed_roots=roots)  # fmt: skip
    assert not gone.ok and "no longer exists" in (gone.error or "")
    assert gone.error_code == "source_missing"  # callers map by this, never by the text ("exists")
    folder = execute(plan_operation(lib["downloads"], lib["library"] / "x", RenameMode.MOVE, ConflictPolicy.SKIP),
                     allowed_roots=roots)  # fmt: skip
    assert not folder.ok and "folder" in (folder.error or "")
    assert folder.error_code == "error"


def test_destination_conflicts_carry_a_typed_code(
    lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    src = make_file(lib["downloads"] / "a.mkv")
    make_file(lib["library"] / "A.mkv", b"other")
    for policy in (ConflictPolicy.FAIL, ConflictPolicy.SKIP):
        result = execute(plan_operation(src, lib["library"] / "A.mkv", RenameMode.MOVE, policy), allowed_roots=roots)
        assert not result.ok and result.error_code == "destination_exists"


def test_destination_is_a_folder(
    lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    src = make_file(lib["downloads"] / "a.mkv")
    (lib["library"] / "A.mkv").mkdir()
    result = execute(plan_operation(src, lib["library"] / "A.mkv", RenameMode.MOVE, ConflictPolicy.OVERWRITE),
                     allowed_roots=roots)  # fmt: skip
    assert not result.ok and "folder" in (result.error or "") and src.exists()


# --------------------------------------------------------------------------- cross-device


@pytest.fixture
def fake_devices(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Pretend every top-level folder under tmp_path is its own filesystem."""

    def device(path: object) -> str:
        p = Path(os.path.abspath(os.fspath(path)))  # type: ignore[arg-type]
        try:
            return p.relative_to(tmp_path).parts[0]
        except (ValueError, IndexError):
            return "?"

    def guard(fn: Callable[..., object]) -> Callable[..., object]:
        def wrapper(a: object, b: object, *args: object, **kwargs: object) -> object:
            if device(a) != device(b):
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return fn(a, b, *args, **kwargs)

        return wrapper

    for name in ("rename", "replace", "link"):
        monkeypatch.setattr(os, name, guard(getattr(os, name)))


@pytest.mark.parametrize("detect_upfront", [False, True])
def test_cross_device_move_and_undo(
    detect_upfront: bool, fake_devices: None, monkeypatch: pytest.MonkeyPatch, lib: dict[str, Path],
    roots: list[str], make_file: Callable[..., Path],
) -> None:  # fmt: skip
    if detect_upfront:  # st_dev differs (the common case) vs. rename discovering EXDEV (mergerfs)
        monkeypatch.setattr(fileops, "_same_device", lambda a, b: False)
    payload = os.urandom(300_000)
    src = make_file(lib["downloads"] / "big.mkv", payload, mtime=1_600_000_000)
    make_file(lib["downloads"] / "big.en.srt", b"sub")
    dst = lib["library"] / "Big (2020)" / "Big (2020).mkv"
    plan = plan_operation(src, dst, RenameMode.MOVE, ConflictPolicy.SKIP, sidecar_extensions=SUBS)
    result = execute(plan, allowed_roots=roots)
    assert result.ok and result.verified, result.error
    assert not src.exists() and dst.read_bytes() == payload
    assert int(dst.stat().st_mtime) == 1_600_000_000  # timestamps survive the copy
    assert (dst.parent / "Big (2020).en.srt").read_bytes() == b"sub"
    assert _leftovers(lib["library"]) == []

    back = undo(result, allowed_roots=roots)
    assert back.ok, back.error
    assert src.read_bytes() == payload and (lib["downloads"] / "big.en.srt").exists()
    assert not dst.exists()


def test_cross_device_overwrite_uses_same_disk_trash(
    fake_devices: None, lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    src = make_file(lib["downloads"] / "a.mkv", b"new")
    dst = make_file(lib["library"] / "Movies" / "A.mkv", b"old")
    result = execute(plan_operation(src, dst, RenameMode.MOVE, ConflictPolicy.OVERWRITE),
                     allowed_roots=roots, trash_dir=lib["trash"])  # fmt: skip
    assert result.ok, result.error
    trashed = Path(result.replaced or "")
    # /config/trash is another "device", so the displaced file stays on the library's disk.
    assert trashed.is_relative_to(lib["library"] / fileops.TRASH_DIRNAME)
    assert trashed.read_bytes() == b"old" and dst.read_bytes() == b"new"
    assert not any(lib["trash"].rglob("*.mkv"))

    back = undo(result, allowed_roots=roots)
    assert back.ok, back.error
    assert dst.read_bytes() == b"old" and src.read_bytes() == b"new"
    assert not trashed.exists()


def test_hardlink_across_devices_fails_cleanly(
    fake_devices: None, lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    src = make_file(lib["downloads"] / "a.mkv")
    dst = lib["library"] / "Show" / "A.mkv"
    result = execute(plan_operation(src, dst, RenameMode.HARDLINK, ConflictPolicy.SKIP), allowed_roots=roots)
    assert not result.ok and "across filesystems" in (result.error or "")
    assert not dst.exists() and not dst.parent.exists()  # created folders rolled back


# --------------------------------------------------------------------------- crash safety


def test_failed_fsync_leaves_no_partial_destination(
    fake_devices: None, monkeypatch: pytest.MonkeyPatch, lib: dict[str, Path], roots: list[str],
    make_file: Callable[..., Path],
) -> None:  # fmt: skip
    src = make_file(lib["downloads"] / "a.mkv", os.urandom(50_000))
    dst = lib["library"] / "A.mkv"

    def boom(fd: int) -> None:
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(os, "fsync", boom)
    result = execute(plan_operation(src, dst, RenameMode.MOVE, ConflictPolicy.SKIP), allowed_roots=roots)
    assert not result.ok and "I/O error" in (result.error or "")
    assert not dst.exists() and _leftovers(lib["library"]) == []
    assert src.stat().st_size == 50_000


@pytest.mark.parametrize("mode", [RenameMode.COPY, RenameMode.MOVE])
def test_short_copy_is_discarded(
    mode: RenameMode, fake_devices: None, monkeypatch: pytest.MonkeyPatch, lib: dict[str, Path],
    roots: list[str], make_file: Callable[..., Path],
) -> None:  # fmt: skip
    src = make_file(lib["downloads"] / "a.mkv", b"0123456789" * 1000)
    dst = lib["library"] / "A.mkv"

    def short_copy(fd: int, tmp: Path, *, want_crc: bool, clone: bool = False) -> str | None:
        tmp.write_bytes(b"0123")
        return "00000000" if want_crc else None

    monkeypatch.setattr(fileops, "_copy_data", short_copy)
    result = execute(plan_operation(src, dst, mode, ConflictPolicy.SKIP), allowed_roots=roots)
    assert not result.ok and "incomplete" in (result.error or "")
    assert not dst.exists() and _leftovers(lib["library"]) == [] and src.exists()


def test_checksum_mismatch_is_discarded(
    fake_devices: None, monkeypatch: pytest.MonkeyPatch, lib: dict[str, Path], roots: list[str],
    make_file: Callable[..., Path],
) -> None:  # fmt: skip
    src = make_file(lib["downloads"] / "a.mkv", b"payload")
    monkeypatch.setattr(fileops, "_crc_of", lambda p: "DEADBEEF")
    result = execute(plan_operation(src, lib["library"] / "A.mkv", RenameMode.MOVE, ConflictPolicy.SKIP),
                     allowed_roots=roots)  # fmt: skip
    assert not result.ok and "Checksum mismatch" in (result.error or "")
    assert src.read_bytes() == b"payload" and not (lib["library"] / "A.mkv").exists()


def test_sidecar_failure_rolls_back_main_and_displaced(
    monkeypatch: pytest.MonkeyPatch, lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    src = make_file(lib["downloads"] / "M.mkv", b"new-movie")
    make_file(lib["downloads"] / "M.en.srt", b"sub")
    dst = make_file(lib["library"] / "Movie" / "Movie.mkv", b"old-movie")
    plan = plan_operation(src, dst, RenameMode.MOVE, ConflictPolicy.OVERWRITE, sidecar_extensions=SUBS)
    real = fileops._rename_noclobber

    def flaky(a: object, b: object) -> None:
        if str(a).endswith(".srt"):
            raise PermissionError(errno.EACCES, "Permission denied", str(a))
        real(a, b)  # type: ignore[arg-type]

    monkeypatch.setattr(fileops, "_rename_noclobber", flaky)
    result = execute(plan, allowed_roots=roots, trash_dir=lib["trash"])
    assert not result.ok and "Permission denied" in (result.error or "")
    assert src.read_bytes() == b"new-movie" and (lib["downloads"] / "M.en.srt").exists()
    assert dst.read_bytes() == b"old-movie"  # displaced file restored
    assert not list(lib["trash"].rglob("*.mkv"))


def test_keeplink_symlink_failure_moves_nothing(
    monkeypatch: pytest.MonkeyPatch, lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    src = make_file(lib["downloads"] / "seed.mkv", b"seeding")
    dst = lib["library"] / "Seed.mkv"

    def no_symlinks(*_a: object, **_k: object) -> None:
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "symlink", no_symlinks)
    result = execute(plan_operation(src, dst, RenameMode.KEEPLINK, ConflictPolicy.SKIP), allowed_roots=roots)
    assert not result.ok and "symlink" in (result.error or "")
    assert src.read_bytes() == b"seeding" and not dst.exists()


def test_cross_device_source_delete_failure_removes_copy(
    fake_devices: None, monkeypatch: pytest.MonkeyPatch, lib: dict[str, Path], roots: list[str],
    make_file: Callable[..., Path],
) -> None:  # fmt: skip
    src = make_file(lib["downloads"] / "a.mkv", b"payload")
    dst = lib["library"] / "A.mkv"
    real_unlink = os.unlink

    def guarded_unlink(path: object, *args: object, **kwargs: object) -> None:
        if os.path.abspath(os.fspath(path)) == str(src):  # type: ignore[arg-type]
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        real_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "unlink", guarded_unlink)
    result = execute(plan_operation(src, dst, RenameMode.MOVE, ConflictPolicy.SKIP), allowed_roots=roots)
    assert not result.ok and "couldn't remove the original" in (result.error or "")
    assert src.read_bytes() == b"payload" and not dst.exists()


def test_not_enough_free_space(
    monkeypatch: pytest.MonkeyPatch, lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    src = make_file(lib["downloads"] / "a.mkv", b"x" * 1000)
    monkeypatch.setattr(shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(10**9, 10**9, 1000))  # type: ignore[attr-defined]
    result = execute(plan_operation(src, lib["library"] / "A.mkv", RenameMode.COPY, ConflictPolicy.SKIP),
                     allowed_roots=roots)  # fmt: skip
    assert not result.ok and "Not enough free space" in (result.error or "")
    assert not (lib["library"] / "A.mkv").exists() and _leftovers(lib["library"]) == []
    test_mode = execute(plan_operation(src, lib["library"] / "A.mkv", RenameMode.TEST, ConflictPolicy.SKIP),
                        allowed_roots=roots)  # fmt: skip
    assert test_mode.ok  # a dry run copies nothing, so it needs no space


# --------------------------------------------------------------------------- no-clobber rename


@pytest.fixture(params=["native", "posix-branch"])
def rename_branch(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run the POSIX link+unlink branch on Windows too (NTFS supports hardlinks)."""
    if request.param == "posix-branch":
        monkeypatch.setattr(fileops, "IS_WINDOWS", False)
    return str(request.param)


def test_rename_noclobber_never_replaces(rename_branch: str, tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    with pytest.raises(FileExistsError):
        fileops._rename_noclobber(a, b)
    assert a.read_bytes() == b"a" and b.read_bytes() == b"b"
    fileops._rename_noclobber(a, tmp_path / "c")
    assert not a.exists() and (tmp_path / "c").read_bytes() == b"a"


def test_rename_noclobber_without_hardlink_support(
    rename_branch: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_links(*_a: object, **_k: object) -> None:
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "link", no_links)
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    with pytest.raises(FileExistsError):
        fileops._rename_noclobber(a, b)
    fileops._rename_noclobber(a, tmp_path / "c")
    assert (tmp_path / "c").read_bytes() == b"a"


def test_full_move_with_posix_branch(
    rename_branch: str, lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    src = make_file(lib["downloads"] / "a.mkv", b"new")
    make_file(lib["downloads"] / "a.en.srt", b"sub")
    dst = make_file(lib["library"] / "A.mkv", b"old")
    plan = plan_operation(src, dst, RenameMode.MOVE, ConflictPolicy.OVERWRITE, sidecar_extensions=SUBS)
    result = execute(plan, allowed_roots=roots, trash_dir=lib["trash"])
    assert result.ok and dst.read_bytes() == b"new" and (lib["library"] / "A.en.srt").exists()
    back = undo(result, allowed_roots=roots)
    assert back.ok and dst.read_bytes() == b"old" and src.read_bytes() == b"new"


def test_stale_temp_file_is_replaced(
    lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    src = make_file(lib["downloads"] / "a.mkv", b"fresh")
    dst = lib["library"] / "A.mkv"
    stale = make_file(lib["library"] / f".A.mkv{fileops.TEMP_SUFFIX}", b"half-written-from-a-crash")
    result = execute(plan_operation(src, dst, RenameMode.COPY, ConflictPolicy.SKIP), allowed_roots=roots)
    assert result.ok and dst.read_bytes() == b"fresh" and not stale.exists()


# --------------------------------------------------------------------------- undo


@pytest.mark.parametrize("mode", [m for m in ALL_MODES if m is not RenameMode.TEST])
def test_undo_each_mode(
    mode: RenameMode, lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path],
    request: pytest.FixtureRequest,
) -> None:  # fmt: skip
    _skip_without_symlinks(mode, request)
    src = make_file(lib["downloads"] / "a.mkv", b"incoming")
    make_file(lib["downloads"] / "a.en.srt", b"sub")
    dst = make_file(lib["library"] / "A.mkv", b"previous")
    plan = plan_operation(src, dst, mode, ConflictPolicy.OVERWRITE, sidecar_extensions=SUBS)
    result = execute(plan, allowed_roots=roots, trash_dir=lib["trash"])
    assert result.ok, result.error
    # The journal stores the result as JSON; undo must work from the round-tripped model.
    stored = FileOpResult.model_validate_json(result.model_dump_json())
    back = undo(stored, allowed_roots=roots)
    assert back.ok, back.error
    assert back.source == str(dst) and back.destination == str(src)
    assert src.read_bytes() == b"incoming" and not src.is_symlink()
    assert (lib["downloads"] / "a.en.srt").read_bytes() == b"sub"
    assert dst.read_bytes() == b"previous" and not dst.is_symlink()
    assert not (lib["library"] / "A.en.srt").exists()
    assert not list(lib["trash"].rglob("*.mkv"))


def test_undo_test_mode_and_failed_results() -> None:
    test_result = FileOpResult(ok=True, mode=RenameMode.TEST, source="/a", destination="/b")
    assert undo(test_result, allowed_roots=["/"]).skipped
    failed = FileOpResult(ok=False, mode=RenameMode.MOVE, source="/a", destination="/b", error="x")
    out = undo(failed, allowed_roots=["/"])
    assert not out.ok and "nothing to undo" in (out.error or "")


def test_undo_refused_when_destination_changed(
    lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    src = make_file(lib["downloads"] / "a.mkv", b"original")
    dst = lib["library"] / "A.mkv"
    result = execute(plan_operation(src, dst, RenameMode.MOVE, ConflictPolicy.SKIP), allowed_roots=roots)
    dst.write_bytes(b"re-encoded and longer")
    back = undo(result, allowed_roots=roots)
    assert not back.ok and "changed since" in (back.error or "")
    assert dst.exists() and not src.exists()


def test_undo_refused_when_source_occupied(
    lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    src = make_file(lib["downloads"] / "a.mkv", b"original")
    dst = lib["library"] / "A.mkv"
    result = execute(plan_operation(src, dst, RenameMode.MOVE, ConflictPolicy.SKIP), allowed_roots=roots)
    make_file(src, b"a new download with the same name")
    back = undo(result, allowed_roots=roots)
    assert not back.ok and "already exists at the original location" in (back.error or "")
    assert dst.read_bytes() == b"original" and src.read_bytes().startswith(b"a new")


def test_undo_refused_when_destination_gone(
    lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    src = make_file(lib["downloads"] / "a.mkv", b"x")
    dst = lib["library"] / "A.mkv"
    result = execute(plan_operation(src, dst, RenameMode.MOVE, ConflictPolicy.SKIP), allowed_roots=roots)
    dst.unlink()
    back = undo(result, allowed_roots=roots)
    assert not back.ok and "no longer at" in (back.error or "")


def test_undo_copy_refuses_to_delete_only_copy(
    lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    src = make_file(lib["downloads"] / "a.mkv", b"x")
    dst = lib["library"] / "A.mkv"
    result = execute(plan_operation(src, dst, RenameMode.COPY, ConflictPolicy.SKIP), allowed_roots=roots)
    src.unlink()
    back = undo(result, allowed_roots=roots)
    assert not back.ok and "only remaining copy" in (back.error or "")
    assert dst.exists()


def test_undo_hardlink_refuses_when_replaced(
    lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    src = make_file(lib["downloads"] / "a.mkv", b"x")
    dst = lib["library"] / "A.mkv"
    result = execute(plan_operation(src, dst, RenameMode.HARDLINK, ConflictPolicy.SKIP), allowed_roots=roots)
    dst.unlink()
    make_file(dst, b"y")
    back = undo(result, allowed_roots=roots)
    assert not back.ok and "no longer a hardlink" in (back.error or "")
    assert dst.read_bytes() == b"y"


def test_undo_refuses_outside_roots(
    lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    src = make_file(lib["downloads"] / "a.mkv", b"x")
    dst = lib["library"] / "A.mkv"
    result = execute(plan_operation(src, dst, RenameMode.MOVE, ConflictPolicy.SKIP), allowed_roots=roots)
    back = undo(result, allowed_roots=[str(lib["library"])])
    assert not back.ok and "outside the allowed roots" in (back.error or "")
    assert dst.exists()


def test_undo_with_trash_already_emptied(
    lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    src = make_file(lib["downloads"] / "a.mkv", b"new")
    dst = make_file(lib["library"] / "A.mkv", b"old")
    result = execute(plan_operation(src, dst, RenameMode.MOVE, ConflictPolicy.OVERWRITE),
                     allowed_roots=roots, trash_dir=lib["trash"])  # fmt: skip
    shutil.rmtree(lib["trash"])
    back = undo(result, allowed_roots=roots)
    assert back.ok and src.read_bytes() == b"new" and not dst.exists()
    assert "no longer in the trash" in " ".join(back.warnings)


def test_undo_ignores_tampered_trash_manifest(
    lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    src = make_file(lib["downloads"] / "a.mkv", b"new")
    dst = make_file(lib["library"] / "A.mkv", b"old")
    secret = make_file(lib["downloads"] / "elsewhere" / "secret.key", b"do-not-move")
    result = execute(plan_operation(src, dst, RenameMode.MOVE, ConflictPolicy.OVERWRITE),
                     allowed_roots=roots, trash_dir=lib["trash"])  # fmt: skip
    manifest_path = Path(result.replaced).parent / fileops.TRASH_MANIFEST
    manifest = json.loads(manifest_path.read_text())
    manifest["items"].append({"original": str(lib["library"] / "leak.key"), "trashed": str(secret)})
    manifest_path.write_text(json.dumps(manifest))
    back = undo(result, allowed_roots=roots)
    assert back.ok and dst.read_bytes() == b"old"
    assert secret.read_bytes() == b"do-not-move" and not (lib["library"] / "leak.key").exists()
    assert "not a file inside the trash folder" in " ".join(back.warnings)


def test_find_sidecars_sees_files_added_after_a_cached_listing(
    lib: dict[str, Path], make_file: Callable[..., Path]
) -> None:
    d = lib["downloads"] / "Flat"
    video = make_file(d / "Show.S01E01.mkv")
    make_file(d / "Show.S01E02.mkv")
    old = os.stat(d).st_mtime - 60
    os.utime(d, (old, old))  # old enough to be cached
    assert find_sidecars(video, SUBS) == []
    make_file(d / "Show.S01E01.en.srt", b"sub")  # bumps the folder mtime
    assert [p.name for p in find_sidecars(video, SUBS)] == ["Show.S01E01.en.srt"]


def test_undo_keeplink_cross_device(
    fake_devices: None,
    lib: dict[str, Path],
    roots: list[str],
    make_file: Callable[..., Path],
    need_symlinks: None,
) -> None:
    src = make_file(lib["downloads"] / "seed.mkv", b"seeding-data")
    dst = lib["library"] / "Seed.mkv"
    result = execute(plan_operation(src, dst, RenameMode.KEEPLINK, ConflictPolicy.SKIP), allowed_roots=roots)
    assert result.ok, result.error
    assert src.is_symlink() and dst.read_bytes() == b"seeding-data"
    back = undo(result, allowed_roots=roots)
    assert back.ok, back.error
    assert not src.is_symlink() and src.read_bytes() == b"seeding-data" and not dst.exists()


def test_test_mode_reports_index_and_unwritable(
    lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    src = make_file(lib["downloads"] / "a.mkv")
    dst = make_file(lib["library"] / "A.mkv", b"existing")
    result = execute(plan_operation(src, dst, RenameMode.TEST, ConflictPolicy.INDEX), allowed_roots=roots)
    assert result.ok and result.destination.endswith("A (1).mkv")
    assert sorted(os.listdir(lib["library"])) == ["A.mkv"]


# --------------------------------------------------------------------------- case / Unicode variants
# The NAS filesystems (ext4/btrfs/ZFS) are case- and normalisation-sensitive; NTFS is only
# case-insensitive. NFD/NFC variants therefore exercise the listing-based lookups on every OS;
# plain case variants of folders only can on a case-sensitive disk.

NFD_TITLE = "Amélie (2001)"  # decomposed "e" + combining acute, as macOS writes it
NFC_TITLE = "Amélie (2001)"


def _distinct_names(folder: Path, a: str, b: str) -> bool:
    """True when the filesystem under ``folder`` keeps ``a`` and ``b`` apart."""
    probe = folder / a
    probe.write_bytes(b"")
    try:
        return not os.path.exists(folder / b)
    finally:
        probe.unlink()


def test_dir_variant_lookup_returns_the_existing_spelling(lib: dict[str, Path], make_file: Callable[..., Path]) -> None:
    (lib["library"] / "Doctor Who (2005)").mkdir()
    (lib["library"] / NFD_TITLE).mkdir()
    make_file(lib["library"] / "Torchwood")  # a file is never reused as a folder
    assert fileops._dir_variant(lib["library"], "Doctor who (2005)") == "Doctor Who (2005)"
    assert fileops._dir_variant(lib["library"], "Doctor Who (2005)") == "Doctor Who (2005)"
    assert fileops._dir_variant(lib["library"], NFC_TITLE) == NFD_TITLE
    assert fileops._dir_variant(lib["library"], "torchwood") is None
    assert fileops._dir_variant(lib["library"], "Doctor Who (2006)") is None


def test_plan_reuses_case_variant_series_folder(lib: dict[str, Path], make_file: Callable[..., Path]) -> None:
    if not _distinct_names(lib["library"], "case-probe", "CASE-PROBE"):
        pytest.skip("case-insensitive filesystem: the OS itself resolves 'Doctor who' to the existing folder")
    season = lib["library"] / "Doctor Who (2005)" / "Season 01"
    season.mkdir(parents=True)
    src = make_file(lib["downloads"] / "dw.s01e01.mkv")
    wanted = lib["library"] / "Doctor who (2005)" / "Season 01" / "Doctor Who - S01E01.mkv"
    plan = plan_operation(src, wanted, RenameMode.MOVE, ConflictPolicy.SKIP)
    assert plan.destination == str(season / "Doctor Who - S01E01.mkv")
    assert plan.conflict == "none"


def test_plan_and_execute_reuse_nfd_series_folder(
    lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    if not _distinct_names(lib["library"], NFD_TITLE, NFC_TITLE):
        pytest.skip("normalisation-insensitive filesystem (APFS): NFD and NFC already name one folder")
    series = lib["library"] / NFD_TITLE
    series.mkdir()
    src = make_file(lib["downloads"] / "amelie.s01e01.mkv", b"episode")
    wanted = lib["library"] / NFC_TITLE / "Season 01" / "E01.mkv"  # "Season 01" is new
    plan = plan_operation(src, wanted, RenameMode.MOVE, ConflictPolicy.SKIP)
    expected = series / "Season 01" / "E01.mkv"
    assert plan.destination == str(expected) and plan.conflict == "none"
    result = execute(plan, allowed_roots=roots)
    assert result.ok, result.error
    assert result.destination == str(expected) and expected.read_bytes() == b"episode"
    assert os.listdir(lib["library"]) == [NFD_TITLE]  # no second, NFC series folder
    assert fileops.existing_destination(wanted) == str(expected)


def test_case_variant_file_is_a_conflict(lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]) -> None:
    folder = lib["library"] / "Show"
    make_file(folder / "show - s01e01.mkv", b"existing")
    src = make_file(lib["downloads"] / "new.mkv", b"incoming")
    plan = plan_operation(src, folder / "Show - S01E01.mkv", RenameMode.MOVE, ConflictPolicy.SKIP)
    assert plan.conflict == "exists"
    result = execute(plan, allowed_roots=roots)
    assert result.skipped and not result.ok
    assert os.listdir(folder) == ["show - s01e01.mkv"] and src.read_bytes() == b"incoming"


@pytest.mark.parametrize("policy", [ConflictPolicy.OVERWRITE, ConflictPolicy.INDEX])
def test_nfd_variant_file_conflict_follows_the_policy(
    policy: ConflictPolicy, lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    folder = lib["library"] / "Movies"
    if not _distinct_names(lib["library"], NFD_TITLE, NFC_TITLE):
        pytest.skip("normalisation-insensitive filesystem (APFS): the exact-name check already sees it")
    make_file(folder / f"{NFD_TITLE}.mkv", b"old")
    src = make_file(lib["downloads"] / "amelie.mkv", b"new-version")
    dst = folder / f"{NFC_TITLE}.mkv"
    plan = plan_operation(src, dst, RenameMode.MOVE, policy)
    assert plan.conflict == "exists"
    result = execute(plan, allowed_roots=roots, trash_dir=lib["trash"])
    assert result.ok, result.error
    if policy is ConflictPolicy.OVERWRITE:
        assert os.listdir(folder) == [f"{NFC_TITLE}.mkv"]  # the variant went to the trash
        assert result.replaced and Path(result.replaced).read_bytes() == b"old"
    else:
        assert Path(result.destination).name == f"{NFC_TITLE} (1).mkv"
        assert sorted(os.listdir(folder)) == sorted([f"{NFD_TITLE}.mkv", f"{NFC_TITLE} (1).mkv"])


def test_case_only_rename_of_the_source_still_works(
    lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    folder = lib["library"] / "Movies"
    src = make_file(folder / "the matrix (1999).mkv", b"neo")
    dst = folder / "The Matrix (1999).mkv"
    assert plan_operation(src, dst, RenameMode.COPY, ConflictPolicy.SKIP).conflict == "same_file"
    plan = plan_operation(src, dst, RenameMode.MOVE, ConflictPolicy.SKIP)
    assert plan.conflict == "none" and plan.destination == str(dst)
    result = execute(plan, allowed_roots=roots)
    assert result.ok and not result.skipped, result.error
    assert os.listdir(folder) == ["The Matrix (1999).mkv"]
    back = undo(result, allowed_roots=roots)
    assert back.ok, back.error
    assert os.listdir(folder) == ["the matrix (1999).mkv"]


def test_normalisation_only_rename_of_the_source(
    lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    # On NTFS/ext4 the NFD and NFC names are different entries, so this takes the same path as a
    # case-only rename on the NAS: the only "variant" is the source itself.
    folder = lib["library"] / "Movies"
    src = make_file(folder / f"{NFD_TITLE}.mkv", b"x")
    dst = folder / f"{NFC_TITLE}.mkv"
    assert plan_operation(src, dst, RenameMode.COPY, ConflictPolicy.SKIP).conflict == "same_file"
    plan = plan_operation(src, dst, RenameMode.MOVE, ConflictPolicy.SKIP)
    assert plan.conflict == "none"
    result = execute(plan, allowed_roots=roots)
    assert result.ok and not result.skipped, result.error
    assert os.listdir(folder) == [f"{NFC_TITLE}.mkv"]


# --------------------------------------------------------------------------- Schema's data folder


@pytest.fixture
def protected_config(lib: dict[str, Path]):  # type: ignore[no-untyped-def]
    """tmp/config (which holds the trash) protected the way SettingsStore protects /config."""
    data = lib["trash"].parent
    (data / "secret.key").write_bytes(b"signing-key")
    paths.protect([data])
    yield data
    paths.protect([])


def test_data_dir_is_off_limits_even_inside_the_roots(
    protected_config: Path, lib: dict[str, Path], make_file: Callable[..., Path], tmp_path: Path
) -> None:
    roots = [str(tmp_path)]  # an old setting whose root holds /config
    secret = protected_config / "secret.key"
    stolen = lib["library"] / "key.mkv"
    for mode in (RenameMode.COPY, RenameMode.MOVE, RenameMode.HARDLINK, RenameMode.TEST):
        result = execute(plan_operation(secret, stolen, mode, ConflictPolicy.SKIP), allowed_roots=roots)
        assert not result.ok and "data folder" in (result.error or ""), mode
    assert not stolen.exists() and secret.read_bytes() == b"signing-key"
    src = make_file(lib["downloads"] / "a.mkv")
    result = execute(plan_operation(src, protected_config / "schema.db", RenameMode.MOVE, ConflictPolicy.SKIP),
                     allowed_roots=roots)  # fmt: skip
    assert not result.ok and "data folder" in (result.error or "") and src.exists()
    forged = FileOpResult(ok=True, mode=RenameMode.MOVE, source=str(stolen), destination=str(secret), bytes=11)
    back = undo(forged, allowed_roots=roots)
    assert not back.ok and "data folder" in (back.error or "")
    assert secret.exists() and not stolen.exists()


def test_trash_inside_the_data_dir_still_works(
    protected_config: Path, lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    src = make_file(lib["downloads"] / "a.mkv", b"new")
    dst = make_file(lib["library"] / "A.mkv", b"old")
    result = execute(plan_operation(src, dst, RenameMode.MOVE, ConflictPolicy.OVERWRITE),
                     allowed_roots=roots, trash_dir=lib["trash"])  # fmt: skip
    assert result.ok, result.error
    assert Path(result.replaced or "").is_relative_to(lib["trash"])
    back = undo(result, allowed_roots=roots)
    assert back.ok, back.error
    assert dst.read_bytes() == b"old" and src.read_bytes() == b"new"


# --------------------------------------------------------------------------- source pinned for copies


def test_copy_refuses_a_source_replaced_after_the_checks(
    monkeypatch: pytest.MonkeyPatch, lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    src = make_file(lib["downloads"] / "a.mkv", b"original")
    other = make_file(lib["downloads"] / "other.bin", b"swapped-in")
    monkeypatch.setattr(fileops, "_check_free_space", lambda folder, needed: os.replace(other, src))
    dst = lib["library"] / "A.mkv"
    result = execute(plan_operation(src, dst, RenameMode.COPY, ConflictPolicy.SKIP), allowed_roots=roots)
    assert not result.ok and "replaced" in (result.error or "")
    assert not dst.exists() and _leftovers(lib["library"]) == []


def test_move_refuses_a_source_swapped_for_a_symlink(
    monkeypatch: pytest.MonkeyPatch, lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path],
    need_symlinks: None,
) -> None:  # fmt: skip
    src = make_file(lib["downloads"] / "a.mkv", b"original")
    secret = make_file(lib["outside"] / "secret.key", b"secret")

    def swap(folder: Path, needed: int) -> None:
        os.unlink(src)
        os.symlink(secret, src)

    monkeypatch.setattr(fileops, "_same_device", lambda a, b: False)  # a copy, opened after the checks
    monkeypatch.setattr(fileops, "_check_free_space", swap)
    dst = lib["library"] / "A.mkv"
    result = execute(plan_operation(src, dst, RenameMode.MOVE, ConflictPolicy.SKIP), allowed_roots=roots)
    assert not result.ok and "Refused to copy" in (result.error or "")
    assert not dst.exists() and secret.read_bytes() == b"secret" and src.is_symlink()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX-only")
def test_copy_refuses_a_source_swapped_for_a_fifo(
    monkeypatch: pytest.MonkeyPatch, lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    src = make_file(lib["downloads"] / "a.mkv", b"original")

    def swap(folder: Path, needed: int) -> None:
        os.unlink(src)
        os.mkfifo(src)

    monkeypatch.setattr(fileops, "_check_free_space", swap)
    results: list[FileOpResult] = []
    plan = plan_operation(src, lib["library"] / "A.mkv", RenameMode.COPY, ConflictPolicy.SKIP)
    worker = threading.Thread(target=lambda: results.append(execute(plan, allowed_roots=roots)), daemon=True)
    worker.start()
    worker.join(10)
    assert not worker.is_alive(), "the copy blocked on a FIFO"
    assert results and not results[0].ok


# --------------------------------------------------------------------------- one full compare per item


def test_full_compare_runs_once_per_item(
    monkeypatch: pytest.MonkeyPatch, lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    payload = os.urandom(3 * 1024 * 1024)
    src = make_file(lib["downloads"] / "a.mkv", payload)
    dst = make_file(lib["library"] / "A.mkv", payload)
    read: list[int] = []
    real_compare = fileops._compare_fds

    def counting(fa: int, fb: int, ranges: list[tuple[int, int]]) -> bool:
        read.append(sum(n for _, n in ranges))
        return real_compare(fa, fb, ranges)

    monkeypatch.setattr(fileops, "_compare_fds", counting)
    assert plan_operation(src, dst, RenameMode.MOVE, ConflictPolicy.SKIP).conflict == "same_file"  # match time
    plan = plan_operation(src, dst, RenameMode.MOVE, ConflictPolicy.SKIP)  # execute time re-plans ...
    result = execute(plan, allowed_roots=roots)  # ... then executes
    assert result.ok and result.skipped
    # first + last MiB once (the re-plan hits the cache), then the one full compare (to EOF)
    assert len(read) == 2 and read[0] == 2 * 1024 * 1024 and read[1] >= len(payload)


def test_quick_check_only_guides_planning(
    lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    size = 3 * 1024 * 1024
    original = bytearray(os.urandom(size))
    corrupt = bytearray(original)
    corrupt[size // 2] ^= 0xFF  # same size, same head and tail, different middle
    src = make_file(lib["downloads"] / "a.mkv", bytes(original))
    dst = make_file(lib["library"] / "A.mkv", bytes(corrupt))
    plan = plan_operation(src, dst, RenameMode.MOVE, ConflictPolicy.SKIP)
    assert plan.conflict == "same_file"
    result = execute(plan, allowed_roots=roots)
    assert not result.ok and result.skipped  # the full compare saw the difference: policy "skip"
    assert src.read_bytes() == bytes(original) and dst.read_bytes() == bytes(corrupt)


# --------------------------------------------------------------------------- folder fsyncs


@pytest.fixture
def dir_syncs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    synced: list[str] = []
    monkeypatch.setattr(fileops, "IS_WINDOWS", False)  # the POSIX path, on every OS
    monkeypatch.setattr(fileops, "_fsync_dir", lambda p: synced.append(os.fspath(p)))
    monkeypatch.setattr(fileops, "_dirty_dirs", {})
    monkeypatch.setattr(fileops, "_dir_sync_last", float("-inf"))
    monkeypatch.setattr(fileops, "_dir_sync_timer", None)
    return synced


def test_folder_syncs_are_coalesced(
    dir_syncs: list[str], monkeypatch: pytest.MonkeyPatch, lib: dict[str, Path], roots: list[str],
    make_file: Callable[..., Path],
) -> None:  # fmt: skip
    monkeypatch.setattr(fileops, "DIR_SYNC_INTERVAL", 3600.0)
    season = lib["library"] / "Show" / "Season 01"
    for i in range(5):
        src = make_file(lib["downloads"] / f"e{i}.mkv", str(i).encode())
        plan = plan_operation(src, season / f"E{i}.mkv", RenameMode.MOVE, ConflictPolicy.SKIP)
        assert execute(plan, allowed_roots=roots).ok
    assert dir_syncs == [str(season)]  # the first right away, the other four wait
    fileops.flush_dirs()
    assert dir_syncs == [str(season), str(season)]
    assert fileops._dir_sync_timer is None


def test_pending_folder_syncs_run_without_another_call(
    dir_syncs: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(fileops, "DIR_SYNC_INTERVAL", 0.05)
    fileops.sync_dirs_later([tmp_path / "a"])
    fileops.sync_dirs_later([tmp_path / "b"])
    assert dir_syncs == [str(tmp_path / "a")]
    deadline = time.monotonic() + 5
    while len(dir_syncs) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert dir_syncs == [str(tmp_path / "a"), str(tmp_path / "b")]


def test_cross_device_move_syncs_the_folder_before_deleting_the_source(
    fake_devices: None, monkeypatch: pytest.MonkeyPatch, lib: dict[str, Path], roots: list[str],
    make_file: Callable[..., Path],
) -> None:  # fmt: skip
    events: list[tuple[str, str]] = []
    real_unlink = fileops._unlink
    monkeypatch.setattr(fileops, "_fsync_dir", lambda p: events.append(("sync", os.fspath(p))))

    def unlink(p: Path) -> None:
        events.append(("unlink", os.fspath(p)))
        real_unlink(p)

    monkeypatch.setattr(fileops, "_unlink", unlink)
    src = make_file(lib["downloads"] / "a.mkv", b"x" * 1000)
    dst = lib["library"] / "A" / "A.mkv"
    assert execute(plan_operation(src, dst, RenameMode.MOVE, ConflictPolicy.SKIP), allowed_roots=roots).ok
    assert events.index(("sync", str(dst.parent))) < events.index(("unlink", str(src)))


# --------------------------------------------------------------------------- listing cache


def _age(folder: Path) -> None:
    old = time.time() - 60
    os.utime(folder, (old, old))


def _count_scans(monkeypatch: pytest.MonkeyPatch, folder: Path) -> list[str]:
    scans: list[str] = []
    real = os.scandir
    key = os.path.normcase(os.fspath(folder))

    def counting(path: object = ".") -> object:
        if isinstance(path, (str, os.PathLike)) and os.path.normcase(os.fspath(path)) == key:
            scans.append(os.fspath(path))
        return real(path)  # type: ignore[call-overload]

    monkeypatch.setattr(os, "scandir", counting)
    return scans


def test_listing_patch_keeps_order_and_variants() -> None:
    base = fileops._Listing({"b.srt": (True, False), "A.mkv": (True, False), "Dir": (False, True)})
    assert base.variants("dir") == [("Dir", True)]  # builds the folded index
    patched = base.changed("a.MKV", (True, False)).changed("b.srt", None).changed("dir2", (False, True))
    assert patched.names == ["A.mkv", "a.MKV"] and patched.lowers == ["a.mkv", "a.mkv"]
    assert patched.variants("A.MKV") == [("A.mkv", False), ("a.MKV", False)]
    assert patched.variants("DIR2") == [("dir2", True)] and patched.variants("b.srt") == []
    assert base.names == ["A.mkv", "b.srt"] and base.variants("a.mkv") == [("A.mkv", False)]  # untouched


def test_new_folders_keep_the_root_listing_cached(
    monkeypatch: pytest.MonkeyPatch, lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    root = lib["library"] / "Movies"
    for i in range(5):
        (root / f"Movie {i}").mkdir(parents=True)
    _age(root)
    scans = _count_scans(monkeypatch, root)
    for i in range(3):
        src = make_file(lib["downloads"] / f"m{i}.mkv", str(i).encode())
        dst = root / f"New {i} (2020)" / f"New {i} (2020).mkv"
        assert execute(plan_operation(src, dst, RenameMode.MOVE, ConflictPolicy.SKIP), allowed_roots=roots).ok
    assert len(scans) == 1  # listed by the first plan, then patched in place by each mkdir
    listing = fileops._listing(root)
    assert listing is not None and listing.variants("new 2 (2020)") == [("New 2 (2020)", True)]
    assert len(scans) == 1


def test_moves_out_of_a_flat_folder_list_it_once(
    monkeypatch: pytest.MonkeyPatch, lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    flat = lib["downloads"] / "Flat"
    for i in range(4):
        make_file(flat / f"Show.S01E0{i}.mkv", str(i).encode())
        make_file(flat / f"Show.S01E0{i}.en.srt", b"sub")
    _age(flat)
    scans = _count_scans(monkeypatch, flat)
    for i in range(4):
        src = flat / f"Show.S01E0{i}.mkv"
        dst = lib["library"] / "Show" / f"Show - S01E0{i}.mkv"
        plan = plan_operation(src, dst, RenameMode.MOVE, ConflictPolicy.SKIP, sidecar_extensions=SUBS)
        assert [Path(s.source).name for s in plan.sidecars] == [f"Show.S01E0{i}.en.srt"]
        assert execute(plan, allowed_roots=roots).ok
    assert len(scans) == 1
    assert sorted(p.name for p in (lib["library"] / "Show").iterdir()) == sorted(
        [f"Show - S01E0{i}.mkv" for i in range(4)] + [f"Show - S01E0{i}.en.srt" for i in range(4)]
    )


def test_outside_changes_after_a_patch_are_still_seen(tmp_path: Path) -> None:
    d = tmp_path / "d"
    d.mkdir()
    (d / "a.mkv").write_bytes(b"a")
    _age(d)
    assert fileops._sorted_listing(d) == (["a.mkv"], ["a.mkv"])
    with fileops._editing(d / "b.mkv"):
        (d / "b.mkv").write_bytes(b"b")
    assert fileops._sorted_listing(d) == (["a.mkv", "b.mkv"], ["a.mkv", "b.mkv"])
    time.sleep(0.05)  # past the timestamp tick of Schema's own change
    (d / "c.mkv").write_bytes(b"c")
    assert fileops._sorted_listing(d) == (["a.mkv", "b.mkv", "c.mkv"], ["a.mkv", "b.mkv", "c.mkv"])


def test_overlapping_changes_drop_the_cached_listing(tmp_path: Path) -> None:
    d = tmp_path / "d"
    d.mkdir()
    (d / "a").write_bytes(b"a")
    _age(d)
    fileops._listing(d)
    assert os.fspath(d) in fileops._listing_cache
    with fileops._editing(d / "b"), fileops._editing(d / "c"):
        (d / "b").write_bytes(b"b")
        (d / "c").write_bytes(b"c")
    assert os.fspath(d) not in fileops._listing_cache
    assert fileops._sorted_listing(d) == (["a", "b", "c"], ["a", "b", "c"])


# --------------------------------------------------------------------------- copy path


def _fake_copy_file_range(calls: list[int], fail: int | None = None) -> Callable[..., int]:
    def fake(fd_in: int, fd_out: int, count: int, offset_src: int = 0, offset_dst: int = 0) -> int:
        calls.append(count)
        if fail is not None:
            raise OSError(fail, os.strerror(fail))
        os.lseek(fd_in, offset_src, os.SEEK_SET)
        data = os.read(fd_in, count)
        os.lseek(fd_out, offset_dst, os.SEEK_SET)
        return os.write(fd_out, data)

    return fake


def test_copy_mode_uses_copy_file_range_on_one_filesystem(
    monkeypatch: pytest.MonkeyPatch, lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(os, "copy_file_range", _fake_copy_file_range(calls), raising=False)
    payload = os.urandom(100_000)
    src = make_file(lib["downloads"] / "a.mkv", payload, mtime=1_600_000_000)
    dst = lib["library"] / "A.mkv"
    result = execute(plan_operation(src, dst, RenameMode.COPY, ConflictPolicy.SKIP), allowed_roots=roots)
    assert result.ok and result.verified, result.error
    assert calls and dst.read_bytes() == payload and src.read_bytes() == payload
    assert int(dst.stat().st_mtime) == 1_600_000_000


def test_copy_falls_back_when_copy_file_range_cant(
    monkeypatch: pytest.MonkeyPatch, lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(os, "copy_file_range", _fake_copy_file_range(calls, errno.EXDEV), raising=False)
    payload = os.urandom(50_000)
    src = make_file(lib["downloads"] / "a.mkv", payload)
    dst = lib["library"] / "A.mkv"
    assert execute(plan_operation(src, dst, RenameMode.COPY, ConflictPolicy.SKIP), allowed_roots=roots).ok
    assert calls == [len(payload)] and dst.read_bytes() == payload


def test_cross_device_move_reads_once_and_drops_pages(
    monkeypatch: pytest.MonkeyPatch, lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path]
) -> None:
    clone_calls: list[int] = []
    monkeypatch.setattr(os, "copy_file_range", _fake_copy_file_range(clone_calls), raising=False)
    advice: list[tuple[int, int, int]] = []
    monkeypatch.setattr(hashing, "_FADVISE", lambda fd, off, n, a: advice.append((a, off, n)))
    monkeypatch.setattr(os, "POSIX_FADV_DONTNEED", 4, raising=False)
    monkeypatch.setattr(os, "POSIX_FADV_SEQUENTIAL", 2, raising=False)
    monkeypatch.setattr(fileops, "COPY_CHUNK", 4096)
    monkeypatch.setattr(fileops, "_WRITEBACK_LAG", 8192)
    monkeypatch.setattr(fileops, "_same_device", lambda a, b: False)
    read_backs: list[Path] = []
    real_crc = fileops._crc_of

    def crc_of(p: Path) -> str:
        read_backs.append(p)
        return real_crc(p)

    monkeypatch.setattr(fileops, "_crc_of", crc_of)
    payload = os.urandom(20_000)
    src = make_file(lib["downloads"] / "a.mkv", payload)
    dst = lib["library"] / "A.mkv"
    result = execute(plan_operation(src, dst, RenameMode.MOVE, ConflictPolicy.SKIP), allowed_roots=roots)
    assert result.ok and result.verified, result.error
    assert dst.read_bytes() == payload and not src.exists()
    assert clone_calls == []  # a move needs the CRC of the data, so it streams
    assert len(read_backs) == 1  # the copy is read back once to verify it
    dropped = [(off, n) for a, off, n in advice if a == 4]
    assert (0, 0) in dropped  # the whole copy, after its fsync
    # source chunks while copying, destination chunks while writing, then the read-back
    assert sum(n for _, n in dropped) >= 3 * len(payload)


# --------------------------------------------------------------------------- cross-mount moves


def test_mount_of_prefers_the_deepest_and_last_mount() -> None:
    mounts = [("1", "/"), ("2", "/media"), ("3", "/media/movies"), ("4", "/media/movies")]
    assert fileops._mount_of("/media/movies/A/a.mkv", mounts) == ("4", "/media/movies")
    assert fileops._mount_of("/media/moviesX", mounts) == ("2", "/media")
    assert fileops._mount_of("/config", mounts) == ("1", "/")
    assert fileops._unescape_mount("/mnt/my\\040disk") == "/mnt/my disk"


def test_cross_mount_sees_separate_mounts_of_one_filesystem(
    monkeypatch: pytest.MonkeyPatch, lib: dict[str, Path]
) -> None:
    downloads, library = os.path.realpath(lib["downloads"]), os.path.realpath(lib["library"])
    monkeypatch.setattr(fileops, "_mounts", lambda: [("1", "/"), ("20", downloads), ("21", library)])
    reason = fileops.cross_mount(lib["downloads"] / "tv", lib["library"] / "TV")  # not created yet
    assert reason is not None and "separate mounts" in reason
    monkeypatch.setattr(fileops, "_mounts", lambda: [("1", "/")])
    assert fileops.cross_mount(lib["downloads"], lib["library"]) is None
    monkeypatch.setattr(fileops, "_mounts", lambda: None)
    assert fileops.cross_mount(lib["downloads"], lib["library"]) is None


def test_exdev_on_one_device_is_reported_once(
    fake_devices: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    lib: dict[str, Path], roots: list[str], make_file: Callable[..., Path],
) -> None:  # fmt: skip
    monkeypatch.setattr(fileops, "_exdev_warned", set())
    for i in range(2):
        src = make_file(lib["downloads"] / f"{i}.mkv", b"x")
        result = execute(plan_operation(src, lib["library"] / f"{i}.mkv", RenameMode.MOVE, ConflictPolicy.SKIP),
                         allowed_roots=roots)  # fmt: skip
        assert result.ok, result.error
    assert len([r for r in caplog.records if "full copies" in r.getMessage()]) == 1
