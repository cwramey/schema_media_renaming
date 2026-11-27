"""Release folders: detection below category folders, ownership by name, inspection, signatures,
holding and pruning (never through a link)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from schema_app.services import release

# Linux / macOS run both the directory-handle code and the path fallback Windows uses.
MODES = [False, True] if release._FD_MODE else [False]


@pytest.fixture(params=MODES, ids=lambda fd: "fd" if fd else "path")
def mode(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> bool:
    monkeypatch.setattr(release, "_FD_MODE", request.param)
    return request.param


def _tree(base: Path, files: dict[str, bytes]) -> Path:
    for rel, content in files.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return base


def _link_dir(link: Path, target: Path) -> None:
    """A directory symlink, or on Windows a junction (needs no privilege)."""
    if os.name == "nt":
        import _winapi  # type: ignore[import-not-found]

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def _held_files(base: Path) -> list[Path]:
    return [p for p in base.rglob("*") if p.is_file()] if base.exists() else []


def test_release_folder_skips_category_folders(tmp_path: Path) -> None:
    root = tmp_path / "downloads"
    names = release.container_names(["tv-hd"])
    pack = root / "sonarr" / "Show.S01.1080p"
    assert release.release_folder(pack / "Show.S01E01.mkv", [root], names) == pack
    assert release.release_folder(pack / "Featurettes" / "x.mkv", [root], names) == pack
    assert release.release_folder(root / "complete" / "tv-hd" / "Rel" / "a.mkv", [root], names) == root / "complete" / "tv-hd" / "Rel"
    assert release.release_folder(root / "radarr" / "Movie.mkv", [root], names) is None  # loose file in a category
    assert release.release_folder(root / "Movie.mkv", [root], names) is None
    assert release.release_folder(tmp_path / "elsewhere" / "Rel" / "a.mkv", [root], names) is None
    # the deepest root wins
    inner = root / "sonarr"
    assert release.release_folder(inner / "Rel" / "a.mkv", [root, inner], names) == inner / "Rel"


# --------------------------------------------------------------------------- ownership


@pytest.mark.parametrize(
    ("folder", "file", "expected"),
    [
        ("War.Dogs.2016.1080p", "War.Dogs.2016.1080p.BluRay.x264-[YTS.MX].mp4", True),
        ("War Dogs (2016) [1080p] [BluRay] [5.1] [YTS.MX]", "War.Dogs.2016.1080p.mkv", True),
        ("The.Mandalorian.S02.1080p.WEBRip.x265-RARBG", "The.Mandalorian.S02E01.Chapter.9.1080p.mp4", True),
        ("Show.Name.2019.S01.COMPLETE.1080p", "Show.Name.S01E01.mkv", True),  # year on one side only
        ("Show.Name.S01-S03.1080p", "Show.Name.S02E05.mkv", True),  # a multi-season pack
        ("Show.Name.S01-S03.1080p", "Show.Name.S04E01.mkv", False),
        ("Show.S01.1080p", "Show.S02E01.mkv", False),  # another season
        ("War.Dogs.2016.1080p", "War.Dogs.2017.mkv", False),  # another year
        ("4k", "Movie.Name.2019.2160p.mkv", False),  # an rdt-client category
        ("Kids", "Kids.1995.mkv", False),  # a title alone proves nothing
        ("Movie.2160p", "Movie.2160p.mkv", False),  # ... even the same name without year / season
        ("Pack.2001", "Movie.One.2001.mkv", False),  # same year, other title
        ("Home Videos", "IMG_1234.mp4", False),
    ],
)
def test_names_match(folder: str, file: str, expected: bool) -> None:
    assert release.names_match(folder, file) is expected


def test_owned_release_searches_down_from_the_top(tmp_path: Path) -> None:
    cat = tmp_path / "downloads" / "4k"
    movie = cat / "Movie.Name.2019.2160p" / "Movie.Name.2019.2160p.mkv"
    assert release.owned_release(movie, cat) == movie.parent  # the category is skipped
    assert release.owned_release(cat / "Movie.Name.2019.2160p.mkv", cat) is None  # a loose file in it
    nested = cat / "Movie.Name.2019.2160p" / "Movie.Name.2019.2160p" / "x" / "Movie.Name.2019.mkv"
    assert release.owned_release(nested, cat) == cat / "Movie.Name.2019.2160p"  # the outermost match
    assert release.owned_release(tmp_path / "elsewhere" / "a.mkv", cat) is None


def test_clutter_dir_names() -> None:
    for name in ("Featurettes", "Behind.The.Scenes", "deleted_scenes", "Sample", "Subs", "Screens", "Proof"):
        assert release.dir_is_clutter(name), name
    for name in ("Season 1", "Holiday Photos", "Other.Movie.2019", "@eaDir"):
        assert not release.dir_is_clutter(name), name


# --------------------------------------------------------------------------- inspection

CLUTTER = {
    "War.Dogs.2016.1080p.nfo": b"nfo",
    "WWW.YTS.MX.jpg": b"img",
    "YTSProxies.com.txt": b"txt",
    "Subs/2_English.srt": b"srt",
    "Subs/War.Dogs.Ep/3_French.sup": b"sup",  # anything below an allowed folder is fine
    "Sample/War.Dogs.sample.mkv": b"video",
    "Featurettes/Trailer.mkv": b"video",
    "Thumbs.db": b"os",
}


def test_inspect_accepts_one_downloads_clutter(tmp_path: Path) -> None:
    rel = _tree(tmp_path / "War.Dogs.2016.1080p", CLUTTER)
    contents = release.inspect(rel, video_extensions=["mkv"])
    assert contents.problem is None
    assert contents.files == len(CLUTTER)
    assert contents.bytes == sum(len(v) for v in CLUTTER.values())
    assert sorted(Path(v).name for v in contents.videos) == ["Trailer.mkv", "War.Dogs.sample.mkv"]
    assert [s[0] for s in contents.stamps][0] == ""  # the folder itself comes first
    assert len(contents.stamps) == 5  # it and its four sub-folders


@pytest.mark.parametrize(
    "extra",
    ["Other.Download.part01.rar", "Other.Download.r00", "pack.zip", "disc.7z", "DVD.iso", "01 - Track.flac",
     "song.mp3", "manual.pdf", "notes.docx", "setup.exe", "README"],
)  # fmt: skip
def test_inspect_keeps_anything_but_clutter(tmp_path: Path, extra: str) -> None:
    rel = _tree(tmp_path / "War.Dogs.2016.1080p", {**CLUTTER, extra: b"data"})
    problem = release.inspect(rel, video_extensions=["mkv"]).problem
    assert problem is not None and extra in problem and "not download clutter" in problem


def test_inspect_sub_folders(tmp_path: Path) -> None:
    rel = _tree(tmp_path / "Show.S01.1080p", {"Holiday Photos/img.jpg": b"img", "Show.S01E01.1080p/x.nfo": b"n"})
    problem = release.inspect(rel, video_extensions=["mkv"]).problem
    assert problem is not None and "Holiday Photos" in problem
    (rel / "Holiday Photos" / "img.jpg").unlink()
    (rel / "Holiday Photos").rmdir()
    assert "Show.S01E01.1080p" in (release.inspect(rel, video_extensions=["mkv"]).problem or "")
    # ... unless named after the release (per-episode folders of a season pack)
    ok = release.inspect(rel, video_extensions=["mkv"], subdir_ok=lambda sub: release.names_match(rel.name, sub))
    assert ok.problem is None


def test_inspect_busy_budget_trash_and_size_limits(tmp_path: Path) -> None:
    rel = _tree(tmp_path / "War.Dogs.2016.1080p", {**CLUTTER, "Featurettes/Making.Of.mkv.part": b"x"})
    assert release.inspect(rel, video_extensions=["mkv"]).problem == "Making.Of.mkv.part is still downloading"
    (rel / "Featurettes" / "Making.Of.mkv.part").unlink()
    problem = release.inspect(rel, video_extensions=["mkv"], budget=10).problem
    assert problem is not None and "MB" in problem  # more leftovers than the budget
    assert release.inspect(rel, video_extensions=["mkv"], max_files=3).problem == "it holds more than 3 files"
    (rel / ".schema-trash").mkdir()
    assert ".schema-trash" in (release.inspect(rel, video_extensions=["mkv"]).problem or "")


def test_inspect_refuses_links(tmp_path: Path) -> None:
    outside = _tree(tmp_path / "library" / "Movies", {"keep.mkv": b"x"})
    rel = _tree(tmp_path / "War.Dogs.2016.1080p", CLUTTER)
    _link_dir(rel / "Extras", outside)
    problem = release.inspect(rel, video_extensions=["mkv"]).problem
    assert problem is not None and "link" in problem


# --------------------------------------------------------------------------- watcher signature


def test_signature_sees_changes_and_temp_files(tmp_path: Path) -> None:
    rel = tmp_path / "Rel"
    rel.mkdir()
    (rel / "a.mkv").write_bytes(b"x" * 10)
    first = release.signature(rel)
    assert first is not None and not first.busy and first.files == 1
    (rel / "extra.mkv.aria2").write_bytes(b"ctl")
    second = release.signature(rel)
    assert second is not None and second.busy and second != first
    (rel / "extra.mkv.aria2").unlink()
    (rel / "b.mkv.!qB").write_bytes(b"x")
    assert release.signature(rel, ["*.!qB"]).busy  # type: ignore[union-attr]
    assert release.signature(tmp_path / "gone") is None


def test_signature_walk_is_capped(tmp_path: Path) -> None:
    rel = _tree(tmp_path / "Dump", {f"{n:02}.jpg": b"x" for n in range(10)})
    capped = release.signature(rel, max_files=4)
    assert capped is not None and capped.truncated and capped.files == 4
    full = release.signature(rel)
    assert full is not None and not full.truncated and full.files == 10


# --------------------------------------------------------------------------- holding and pruning


def test_hold_and_prune(tmp_path: Path, mode: bool) -> None:
    downloads = tmp_path / "downloads"
    rel = downloads / "War.Dogs.2016"
    (rel / "Featurettes").mkdir(parents=True)
    (rel / "WWW.YTS.MX.jpg").write_bytes(b"img")
    (rel / "Featurettes" / "Deleted.mkv").write_bytes(b"x" * 5)
    base = release.holding_base(downloads)
    then = datetime(2026, 11, 20, 15, 0, 0)
    contents = release.inspect(rel, video_extensions=["mkv"])
    held = release.hold_release(rel, downloads, contents=contents, now=then)
    assert not rel.exists()
    assert held.files == 2 and held.bytes == 8 and held.base == str(base)
    assert Path(held.held).is_dir() and Path(held.held).name == "War.Dogs.2016"
    assert (Path(held.held) / "Featurettes" / "Deleted.mkv").exists()
    manifest = json.loads((Path(held.held).parent / release.HOLDING_MANIFEST).read_text())
    assert manifest["original"] == str(rel) and manifest["held"] == held.held

    # Something Schema didn't put there (no manifest) is never deleted.
    stranger = base / "2026-11-01" / "mine"
    stranger.mkdir(parents=True)
    assert release.prune_holding(base, now=then + timedelta(hours=23)) == []
    assert Path(held.held).exists()
    removed = release.prune_holding(base, root=downloads, now=then + timedelta(hours=25))
    assert removed == [str(Path(held.held).parent)]
    assert not Path(held.held).exists() and stranger.exists()
    assert release.prune_holding(downloads) == []  # not a holding folder: refused
    assert release.prune_holding(tmp_path / "nowhere" / ".schema-trash" / "leftovers") == []


def test_hold_falls_back_to_the_parent_folder(tmp_path: Path, mode: bool) -> None:
    downloads = tmp_path / "downloads"
    rel = _tree(downloads / "4k" / "Movie.2019", {"x.nfo": b"n"})
    (downloads / ".schema-trash").write_bytes(b"not a folder")  # the root's holding folder is unusable
    held = release.hold_release(rel, downloads)
    assert held.base == str(release.holding_base(downloads / "4k")) and not rel.exists()
    assert release.nested_holding_bases(downloads) == [release.holding_base(downloads / "4k")]


def test_hold_refuses_a_link_on_the_way(tmp_path: Path, mode: bool) -> None:
    downloads = tmp_path / "downloads"
    library = _tree(tmp_path / "library" / "Movies" / "War.Dogs.2016", {"War.Dogs.2016.mkv": b"x"}).parent
    downloads.mkdir()
    _link_dir(downloads / "4k", library)  # a category folder that is a link into the library
    with pytest.raises(OSError):
        release.hold_release(downloads / "4k" / "War.Dogs.2016", downloads)
    assert (library / "War.Dogs.2016" / "War.Dogs.2016.mkv").exists()
    assert _held_files(release.holding_base(downloads)) == []


def test_hold_refuses_a_folder_swapped_after_the_check(tmp_path: Path, mode: bool) -> None:
    downloads = tmp_path / "downloads"
    rel = _tree(downloads / "4k" / "War.Dogs.2016", {"x.nfo": b"n"})
    contents = release.inspect(rel, video_extensions=["mkv"])
    library = _tree(tmp_path / "library" / "War.Dogs.2016", {"War.Dogs.2016.mkv": b"x"}).parent
    (downloads / "4k").rename(downloads / "4k-old")
    _link_dir(downloads / "4k", library)  # swapped between the check and the move
    with pytest.raises(OSError):
        release.hold_release(downloads / "4k" / "War.Dogs.2016", downloads, contents=contents)
    assert (library / "War.Dogs.2016" / "War.Dogs.2016.mkv").exists()
    assert _held_files(release.holding_base(downloads)) == []


def test_hold_refuses_a_folder_that_changed_after_the_check(tmp_path: Path, mode: bool) -> None:
    downloads = tmp_path / "downloads"
    rel = _tree(downloads / "War.Dogs.2016", {"Subs/en.srt": b"s"})
    os.utime(rel / "Subs", (1_000_000_000, 1_000_000_000))  # coarse folder timestamps can't hide the change
    contents = release.inspect(rel, video_extensions=["mkv"])
    (rel / "Subs" / "Other.Download.rar").write_bytes(b"new")  # a download client started writing
    with pytest.raises(OSError):
        release.hold_release(rel, downloads, contents=contents)
    assert (rel / "Subs" / "Other.Download.rar").exists()


def test_prune_never_follows_links(tmp_path: Path, mode: bool) -> None:
    downloads = tmp_path / "downloads"
    base = release.holding_base(downloads)
    base.mkdir(parents=True)
    old = datetime(2026, 1, 1)
    manifest = json.dumps({"version": 1, "created": old.isoformat()})

    def slot(folder: Path) -> Path:
        _tree(folder, {"Movie/Movie.mkv": b"x", release.HOLDING_MANIFEST: manifest.encode()})
        return folder / "Movie" / "Movie.mkv"

    victim_day = slot(tmp_path / "library" / "day" / "120000-aaaaaa")
    _link_dir(base / "2026-01-01", tmp_path / "library" / "day")  # a day folder that is a link
    victim_slot = slot(tmp_path / "library" / "slot")
    (base / "2026-01-02").mkdir()
    _link_dir(base / "2026-01-02" / "120000-bbbbbb", victim_slot.parent.parent)  # a slot that is a link
    genuine = slot(base / "2026-01-03" / "120000-cccccc")
    removed = release.prune_holding(base, root=downloads, now=old + timedelta(days=30))
    assert removed == [str(base / "2026-01-03" / "120000-cccccc")]
    assert victim_day.exists() and victim_slot.exists() and not genuine.exists()

    # A holding folder reached through a link is refused as a whole.
    other = tmp_path / "other"
    other.mkdir()
    _link_dir(other / ".schema-trash", downloads / ".schema-trash")
    again = slot(base / "2026-01-04" / "120000-dddddd")
    assert release.prune_holding(release.holding_base(other), now=old + timedelta(days=30)) == []
    assert again.exists()
