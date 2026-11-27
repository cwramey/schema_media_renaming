"""Folders renamed in place on move, empty source folders removed afterwards, and undo of both."""

from __future__ import annotations

import errno
import os
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from schema_app.domain import ConflictPolicy, FileOpPlan, FileOpResult, FolderRename, RenameMode
from schema_app.services import fileops
from schema_app.services.fileops import execute, folder_rename_target, plan_operation, undo

SUBS = ["srt", "ass", "nfo"]


@pytest.fixture
def media(tmp_path: Path) -> SimpleNamespace:
    """The NAS layout: allowed root /media with the libraries and the downloads folder inside it."""
    root = tmp_path / "media"
    ns = SimpleNamespace(
        root=root,
        movies=root / "movies",
        tv=root / "tv",
        downloads=root / "downloads",
        trash=tmp_path / "config" / "trash",
    )
    for folder in (ns.movies, ns.tv, ns.downloads, ns.trash):
        folder.mkdir(parents=True)
    ns.roots = [str(root)]
    ns.protected = [str(root), str(ns.movies), str(ns.tv)]  # allowed + library roots
    return ns


def _run(media: SimpleNamespace, source: Path, destination: Path, *, mode: RenameMode = RenameMode.MOVE,
         policy: ConflictPolicy = ConflictPolicy.SKIP, rename: bool = True,
         remove: bool = True) -> tuple[FileOpPlan, FileOpResult]:  # fmt: skip
    plan = plan_operation(source, destination, mode, policy, sidecar_extensions=SUBS, rename_folder=rename,
                          remove_empty_dirs=remove, protected_dirs=media.protected)  # fmt: skip
    return plan, execute(plan, allowed_roots=media.roots, trash_dir=media.trash)


def _alien(media: SimpleNamespace, make_file: Callable[..., Path]) -> tuple[Path, Path, Path]:
    old_dir = media.movies / "Alien.1979"
    src = make_file(old_dir / "Alien (1979) 4K.mkv", size=4096)
    make_file(old_dir / "Alien (1979) 4K.en.srt", b"subs")
    return old_dir, src, media.movies / "Alien (1979)" / "Alien (1979).mkv"


# --------------------------------------------------------------------------- acceptance


def test_alien_folder_is_renamed_not_recreated(media, make_file) -> None:
    old_dir, src, dst = _alien(media, make_file)
    inode = os.stat(old_dir).st_ino
    plan, result = _run(media, src, dst)
    assert plan.folder_rename == FolderRename(source=str(old_dir), destination=str(dst.parent))
    assert plan.conflict == "none"
    assert result.ok and not result.skipped, result.error
    assert result.source == str(src) and result.destination == str(dst)
    assert result.folder_rename == plan.folder_rename
    assert result.removed_dirs == []
    assert not old_dir.exists()
    assert sorted(p.name for p in dst.parent.iterdir()) == ["Alien (1979).en.srt", "Alien (1979).mkv"]
    assert os.stat(dst.parent).st_ino == inode  # the same folder, renamed
    assert [(s.source, s.destination) for s in result.sidecars] == [
        (str(old_dir / "Alien (1979) 4K.en.srt"), str(dst.parent / "Alien (1979).en.srt"))
    ]


def test_extra_files_travel_with_the_folder(media, make_file) -> None:
    old_dir, src, dst = _alien(media, make_file)
    make_file(old_dir / "poster.jpg", b"img")
    make_file(old_dir / "extras" / "Making of.mkv")  # Jellyfin extras folder: doesn't count
    make_file(old_dir / "Sample" / "alien-sample.mkv")
    _, result = _run(media, src, dst)
    assert result.ok and result.folder_rename is not None
    assert (dst.parent / "poster.jpg").read_bytes() == b"img"
    assert (dst.parent / "extras" / "Making of.mkv").exists()
    assert (dst.parent / "Sample" / "alien-sample.mkv").exists()
    assert not old_dir.exists()


def test_collection_folder_is_never_renamed(media, make_file) -> None:
    collection = media.movies / "Bad Boys Quadrilogy 1995-2024"
    first = make_file(collection / "Bad Boys (1995).mkv")
    make_file(collection / "Bad Boys II (2003).mkv")
    dst = media.movies / "Bad Boys (1995)" / "Bad Boys (1995).mkv"
    plan, result = _run(media, first, dst)
    assert plan.folder_rename is None
    assert result.ok and result.folder_rename is None
    assert dst.exists() and not first.exists()
    assert (collection / "Bad Boys II (2003).mkv").exists()  # the folder stays: it isn't empty
    assert result.removed_dirs == []


def test_video_in_another_subfolder_blocks_the_rename(media, make_file) -> None:
    folder = media.movies / "Pack"
    src = make_file(folder / "Movie.2001.mkv")
    make_file(folder / "Disc 2" / "Other.2002.mkv")
    assert folder_rename_target(src, media.movies / "Movie (2001)" / "Movie (2001).mkv", media.protected) is None


def test_download_folder_is_removed_after_the_move(media, make_file) -> None:
    folder = media.downloads / "complete" / "X.2000.1080p"
    src = make_file(folder / "X.2000.1080p.mkv")
    make_file(folder / "X.2000.1080p.en.srt", b"subs")
    dst = media.movies / "X (2000)" / "X (2000).mkv"
    plan, result = _run(media, src, dst)
    assert plan.folder_rename is None  # different parent: a move into the library
    assert result.ok and dst.exists() and dst.with_name("X (2000).en.srt").exists()
    assert result.removed_dirs == [str(folder), str(folder.parent)]
    assert not folder.exists() and not folder.parent.exists()
    assert media.downloads.is_dir()  # share-level folder next to the libraries: never removed


def test_folder_with_leftovers_is_kept(media, make_file) -> None:
    folder = media.downloads / "X.2000"
    src = make_file(folder / "X.2000.mkv")
    make_file(folder / "release.txt", b"info")
    _, result = _run(media, src, media.movies / "X (2000)" / "X (2000).mkv")
    assert result.ok and result.removed_dirs == []
    assert (folder / "release.txt").exists()


def test_existing_destination_folder_means_a_normal_move(media, make_file) -> None:
    old_dir, src, dst = _alien(media, make_file)
    make_file(dst.parent / "folder.jpg", b"img")  # the target folder already exists
    plan, result = _run(media, src, dst)
    assert plan.folder_rename is None
    assert result.ok and result.folder_rename is None and dst.exists()
    assert result.removed_dirs == [str(old_dir)]  # its only video and sidecar left, so it went
    assert (dst.parent / "folder.jpg").exists()


def test_existing_destination_file_applies_the_conflict_policy(media, make_file) -> None:
    old_dir, src, dst = _alien(media, make_file)
    make_file(dst, b"older copy")
    plan, result = _run(media, src, dst, policy=ConflictPolicy.SKIP)
    assert plan.folder_rename is None and plan.conflict == "exists"
    assert not result.ok and result.skipped
    assert src.exists() and old_dir.is_dir() and dst.read_bytes() == b"older copy"
    _, indexed = _run(media, src, dst, policy=ConflictPolicy.INDEX)
    assert indexed.ok and Path(indexed.destination).name == "Alien (1979) (1).mkv"


@pytest.mark.parametrize("mode", [RenameMode.COPY, RenameMode.HARDLINK, RenameMode.SYMLINK, RenameMode.KEEPLINK])
def test_other_modes_behave_as_before(media, make_file, mode, request) -> None:
    if mode in {RenameMode.SYMLINK, RenameMode.KEEPLINK}:
        request.getfixturevalue("need_symlinks")
    old_dir, src, dst = _alien(media, make_file)
    plan, result = _run(media, src, dst, mode=mode)
    assert plan.folder_rename is None
    assert result.ok and result.folder_rename is None and result.removed_dirs == []
    assert old_dir.is_dir() and os.path.lexists(src)
    assert dst.exists()


def test_settings_off_means_a_normal_move(media, make_file) -> None:
    old_dir, src, dst = _alien(media, make_file)
    make_file(old_dir / "poster.jpg", b"img")
    plan, result = _run(media, src, dst, rename=False, remove=True)
    assert plan.folder_rename is None
    assert result.ok and dst.exists()
    assert (old_dir / "poster.jpg").exists() and result.removed_dirs == []
    plan2, result2 = _run(media, dst, media.movies / "Alien 2" / "Alien (1979).mkv", rename=False, remove=False)
    assert result2.ok and dst.parent.is_dir()  # left empty, but removal is off


def test_protected_folders_are_never_renamed_or_removed(media, make_file) -> None:
    inbox = media.movies / "Inbox"
    media.protected.append(str(inbox))  # a watch folder
    src = make_file(inbox / "Alien.1979.mkv")
    plan, result = _run(media, src, media.movies / "Alien (1979)" / "Alien (1979).mkv")
    assert plan.folder_rename is None
    assert result.ok and result.removed_dirs == []
    assert inbox.is_dir()
    loose = make_file(media.movies / "Blade.1998.mkv")  # directly in the library root
    plan, result = _run(media, loose, media.movies / "Blade (1998)" / "Blade (1998).mkv")
    assert plan.folder_rename is None and result.ok and media.movies.is_dir()


def test_a_download_root_of_its_own_keeps_its_children_removable(tmp_path, make_file) -> None:
    downloads, movies = tmp_path / "downloads", tmp_path / "movies"
    src = make_file(downloads / "X.2000" / "X.2000.mkv")
    movies.mkdir()
    plan = plan_operation(src, movies / "X (2000)" / "X (2000).mkv", RenameMode.MOVE, ConflictPolicy.SKIP,
                          remove_empty_dirs=True, protected_dirs=[str(movies)])  # fmt: skip
    result = execute(plan, allowed_roots=[str(downloads), str(movies)])
    assert result.ok and result.removed_dirs == [str(downloads / "X.2000")]
    assert downloads.is_dir()


# --------------------------------------------------------------------------- undo


def test_undo_restores_the_folder_and_the_file(media, make_file) -> None:
    old_dir, src, dst = _alien(media, make_file)
    make_file(old_dir / "poster.jpg", b"img")
    _, result = _run(media, src, dst)
    back = undo(result, allowed_roots=media.roots)
    assert back.ok, back.error
    assert src.exists() and (old_dir / "Alien (1979) 4K.en.srt").exists() and (old_dir / "poster.jpg").exists()
    assert not dst.parent.exists()
    assert [(s.source, s.destination) for s in back.sidecars] == [
        (str(dst.parent / "Alien (1979).en.srt"), str(old_dir / "Alien (1979) 4K.en.srt"))
    ]


def test_folder_only_rename_and_its_undo(media, make_file) -> None:
    old_dir = media.movies / "Alien.1979"
    src = make_file(old_dir / "Alien (1979).mkv")
    dst = media.movies / "Alien (1979)" / "Alien (1979).mkv"
    _, result = _run(media, src, dst)
    assert result.ok and not result.skipped and result.folder_rename is not None
    assert dst.exists() and not old_dir.exists()
    assert undo(result, allowed_roots=media.roots).ok
    assert src.exists() and not dst.parent.exists()


def test_undo_refuses_when_the_original_folder_is_taken(media, make_file) -> None:
    old_dir, src, dst = _alien(media, make_file)
    _, result = _run(media, src, dst)
    old_dir.mkdir()
    back = undo(result, allowed_roots=media.roots)
    assert not back.ok and "original folder" in (back.error or "")
    assert dst.exists()  # nothing moved


def test_undo_recreates_removed_folders(media, make_file) -> None:
    folder = media.downloads / "X.2000"
    src = make_file(folder / "X.2000.mkv")
    _, result = _run(media, src, media.movies / "X (2000)" / "X (2000).mkv")
    assert result.removed_dirs == [str(folder)]
    assert undo(result, allowed_roots=media.roots).ok
    assert src.exists()


def test_undo_puts_a_replaced_sidecar_back_in_the_original_folder(media, make_file) -> None:
    old_dir, src, dst = _alien(media, make_file)
    make_file(old_dir / "Alien (1979).en.srt", b"old subs")  # displaced by OVERWRITE
    _, result = _run(media, src, dst, policy=ConflictPolicy.OVERWRITE)
    assert result.ok and result.folder_rename is not None and result.replaced
    assert (dst.parent / "Alien (1979).en.srt").read_bytes() == b"subs"
    back = undo(result, allowed_roots=media.roots)
    assert back.ok and not back.warnings, back.warnings
    assert (old_dir / "Alien (1979).en.srt").read_bytes() == b"old subs"
    assert (old_dir / "Alien (1979) 4K.en.srt").read_bytes() == b"subs"


# --------------------------------------------------------------------------- dry run and failures


def test_dry_run_reports_the_folder_rename_and_touches_nothing(media, make_file) -> None:
    old_dir, src, dst = _alien(media, make_file)
    plan, result = _run(media, src, dst, mode=RenameMode.TEST)
    assert plan.folder_rename is not None
    assert result.ok and result.folder_rename == plan.folder_rename
    assert result.destination == str(dst)
    assert [s.destination for s in result.sidecars] == [str(dst.parent / "Alien (1979).en.srt")]
    assert sorted(p.name for p in old_dir.iterdir()) == ["Alien (1979) 4K.en.srt", "Alien (1979) 4K.mkv"]
    assert not dst.parent.exists()
    assert undo(result, allowed_roots=media.roots).skipped


def test_failed_folder_rename_falls_back_to_a_move(media, make_file, monkeypatch) -> None:
    old_dir, src, dst = _alien(media, make_file)

    def busy(src: Path, dst: Path) -> None:
        raise PermissionError(errno.EACCES, "in use", str(src))

    monkeypatch.setattr(fileops, "_rename_dir_noclobber", busy)
    _, result = _run(media, src, dst)
    assert result.ok and result.folder_rename is None
    assert dst.exists() and result.removed_dirs == [str(old_dir)]
    assert any("Couldn't rename the folder Alien.1979" in w for w in result.warnings)


def test_failure_inside_the_folder_renames_it_back(media, make_file) -> None:
    old_dir, src, dst = _alien(media, make_file)
    make_file(old_dir / "Alien (1979).en.srt", b"old subs")
    plan, result = _run(media, src, dst, policy=ConflictPolicy.FAIL)
    assert plan.folder_rename is not None
    assert not result.ok and "Sidecar destination already exists" in (result.error or "")
    assert src.exists() and old_dir.is_dir() and not dst.parent.exists()


def test_changed_folder_since_planning_falls_back(media, make_file) -> None:
    old_dir, src, dst = _alien(media, make_file)
    plan = plan_operation(src, dst, RenameMode.MOVE, ConflictPolicy.SKIP, rename_folder=True,
                          remove_empty_dirs=True, protected_dirs=media.protected)  # fmt: skip
    make_file(old_dir / "Aliens (1986).mkv")  # a second movie arrived
    result = execute(plan, allowed_roots=media.roots, trash_dir=media.trash)
    assert result.ok and result.folder_rename is None and dst.exists()
    assert (old_dir / "Aliens (1986).mkv").exists()
    assert any("can no longer be renamed in place" in w for w in result.warnings)
