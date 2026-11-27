from __future__ import annotations

import os
from pathlib import Path

import pytest

from schema_app.domain import PathMapping
from schema_app.services import paths
from schema_app.services.paths import (
    fit_component,
    is_reserved_name,
    is_within,
    is_within_nofollow,
    map_path,
    safe_filename,
    sanitize_component,
    sanitize_relative_path,
    split_extension,
)

# --------------------------------------------------------------------------- sanitize_component


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Star Wars: Episode IV", "Star Wars - Episode IV"),
        ("Title : Subtitle", "Title - Subtitle"),
        ("10:30", "10 - 30"),
        ("AC/DC", "AC-DC"),
        ("Face\\Off", "Face-Off"),
        ("What If...?", "What If"),
        ("M*A*S*H", "M-A-S-H"),
        ('The "Best" Show', "The 'Best' Show"),
        ("<Tag> Name", "Tag Name"),
        ("Pipe | Name", "Pipe - Name"),
        ("  lots   of\tspace\n", "lots of space"),
        ("bell\x07char\x1f", "bellchar"),
        ("trailing dots...", "trailing dots"),
        ("trailing dot and space. . ", "trailing dot and space"),
        (".", ""),
        ("..", ""),
        ("...", ""),
        ("", ""),
        ("Amélie", "Amélie"),
        ("風の翼", "風の翼"),
        (".hack//Sign", ".hack--Sign"),
    ],
)
def test_sanitize_component(raw: str, expected: str) -> None:
    assert sanitize_component(raw) == expected


def test_sanitize_component_custom_colon() -> None:
    assert sanitize_component("A: B", colon="꞉") == "A꞉ B"
    assert sanitize_component("A:B", colon="") == "AB"
    assert sanitize_component("A:B", colon="/") == "A-B"  # a separator substitute is itself sanitized


def test_sanitize_component_none_and_numbers() -> None:
    assert sanitize_component(None) == ""  # type: ignore[arg-type]
    assert sanitize_component(2019) == "2019"  # type: ignore[arg-type]


def test_sanitize_component_never_contains_illegal_characters() -> None:
    nasty = "".join(chr(c) for c in range(0, 128))
    out = sanitize_component(nasty)
    assert not set('<>:"/\\|?*') & set(out)
    assert all(ord(ch) >= 32 for ch in out)
    assert out == out.strip() and not out.endswith(".")


# --------------------------------------------------------------------------- reserved names


@pytest.mark.parametrize("name", ["CON", "con", "Nul", "PRN.txt", "aux.mkv", "COM1", "lpt9.srt", "CON .mkv"])
def test_reserved_names_detected(name: str) -> None:
    assert is_reserved_name(name)


@pytest.mark.parametrize("name", ["CONAN", "Console", "COM10", "LPT0", "NULL.mkv", "con-air.mkv"])
def test_not_reserved(name: str) -> None:
    assert not is_reserved_name(name)


def test_relative_path_renames_reserved_components() -> None:
    assert sanitize_relative_path("CON/nul.mkv") == "CON_/nul_.mkv"
    assert sanitize_relative_path("Movies/Con (2010)/Con.mkv") == "Movies/Con (2010)/Con_.mkv"


def test_safe_filename() -> None:
    assert safe_filename("aux.srt") == "aux_.srt"
    assert safe_filename("Movie: Part 2.mkv") == "Movie - Part 2.mkv"
    with pytest.raises(ValueError):
        safe_filename("...")


# --------------------------------------------------------------------------- relative paths


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "Show (2016)/Season 01/Show - S01E01 - Pilot.mkv",
            "Show (2016)/Season 01/Show - S01E01 - Pilot.mkv",
        ),
        ("A//B/./C.mkv", "A/B/C.mkv"),
        ("A\\B\\C.mkv", "A/B/C.mkv"),
        ("Show/Season 1 /Ep. .mkv", "Show/Season 1/Ep. .mkv"),
        ("Show: Origins/E01.mkv", "Show - Origins/E01.mkv"),
        ("Trailing dots.../file.mkv", "Trailing dots/file.mkv"),
        ("  spaced  / name.mkv ", "spaced/name.mkv"),
    ],
)
def test_sanitize_relative_path(raw: str, expected: str) -> None:
    assert sanitize_relative_path(raw) == expected


@pytest.mark.parametrize(
    "raw", ["/abs/path.mkv", "\\abs\\path.mkv", "C:/Movies/x.mkv", "c:x.mkv", "\\\\server\\share\\x"]
)
def test_absolute_paths_rejected(raw: str) -> None:
    with pytest.raises(ValueError, match="absolute"):
        sanitize_relative_path(raw)


@pytest.mark.parametrize("raw", ["../escape.mkv", "a/../../b.mkv", "a/ .. /b.mkv", "a\\..\\b"])
def test_traversal_rejected(raw: str) -> None:
    with pytest.raises(ValueError, match=r"\.\."):
        sanitize_relative_path(raw)


@pytest.mark.parametrize("raw", ["", "   ", "./.", "...", "/" * 0 + "./"])
def test_empty_results_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        sanitize_relative_path(raw)


def test_dotted_names_are_not_traversal() -> None:
    assert (
        sanitize_relative_path("...And Justice for All (1979)/x.mkv") == "...And Justice for All (1979)/x.mkv"
    )


# --------------------------------------------------------------------------- 255-byte limit


def test_long_component_keeps_extension_and_suffixes() -> None:
    long_title = "A" * 300
    out = sanitize_relative_path(f"Show/{long_title}.eng.forced.srt")
    name = out.split("/")[-1]
    assert len(name.encode("utf-8")) <= 255
    assert name.endswith(".eng.forced.srt")
    assert name.startswith("AAAA")


def test_long_multibyte_component_is_cut_on_character_boundary() -> None:
    title = "風" * 200  # 600 bytes in UTF-8
    name = sanitize_relative_path(f"{title}.mkv")
    assert len(name.encode("utf-8")) <= 255
    assert name.endswith(".mkv")
    name.encode("utf-8").decode("utf-8")  # still valid UTF-8
    assert set(name[:-4]) == {"風"}


def test_long_folder_component_trimmed() -> None:
    out = sanitize_relative_path(("B" * 400) + "/file.mkv")
    folder, _ = out.split("/")
    assert len(folder.encode()) <= 255


def test_fit_component_with_reserve() -> None:
    name = ("x" * 250) + ".mkv"
    fitted = fit_component(name, reserve=10)
    assert len(fitted.encode()) <= 245 and fitted.endswith(".mkv")
    assert fit_component("short.mkv") == "short.mkv"


@pytest.mark.parametrize(
    ("name", "stem", "suffix"),
    [
        ("Show - S01E01.eng.forced.ass", "Show - S01E01", ".eng.forced.ass"),
        ("Movie (2019).mkv", "Movie (2019)", ".mkv"),
        ("Mr. Robot - S01E01.mkv", "Mr. Robot - S01E01", ".mkv"),
        ("noext", "noext", ""),
        (".hidden", ".hidden", ""),
        ("Movie.pt-BR.srt", "Movie", ".pt-BR.srt"),
    ],
)
def test_split_extension(name: str, stem: str, suffix: str) -> None:
    assert split_extension(name) == (stem, suffix)


# --------------------------------------------------------------------------- is_within


def test_is_within_basic(tmp_path: Path) -> None:
    root = tmp_path / "media"
    (root / "Movies").mkdir(parents=True)
    assert is_within(root / "Movies" / "x.mkv", [str(root)])
    assert is_within(root, [str(root)])
    assert not is_within(tmp_path / "other" / "x.mkv", [str(root)])
    assert not is_within(tmp_path / "media-evil" / "x.mkv", [str(root)])  # prefix trick
    assert not is_within(root / "x.mkv", [])
    assert not is_within(root / "x.mkv", ["", "  "])


def test_is_within_rejects_dotdot(tmp_path: Path) -> None:
    root = tmp_path / "media"
    root.mkdir()
    assert not is_within(str(root) + os.sep + ".." + os.sep + "secret.txt", [str(root)])


def test_is_within_multiple_roots(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert is_within(b / "f", [str(a), str(b)])


@pytest.mark.skipif(os.name != "nt", reason="case-insensitive comparison is Windows-only")
def test_is_within_case_insensitive_on_windows(tmp_path: Path) -> None:
    root = tmp_path / "Media"
    root.mkdir()
    assert is_within(str(root / "x.mkv").upper(), [str(root).lower()])


def test_is_within_resolves_symlinks(tmp_path: Path, need_symlinks: None) -> None:
    root = tmp_path / "media"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.mkv").write_text("x")
    os.symlink(outside, root / "escape", target_is_directory=True)
    assert not is_within(root / "escape" / "secret.mkv", [str(root)])
    # The link entry itself lives inside the root.
    assert is_within_nofollow(root / "escape", [str(root)])
    # A root that is itself a symlink still contains its real children.
    link_root = tmp_path / "link-root"
    os.symlink(outside, link_root, target_is_directory=True)
    assert is_within(link_root / "secret.mkv", [str(link_root)])
    assert is_within(outside / "secret.mkv", [str(link_root)])


def test_containing_root_prefers_deepest(tmp_path: Path) -> None:
    outer = tmp_path / "storage"
    inner = outer / "media"
    inner.mkdir(parents=True)
    found = paths.containing_root(inner / "Movies" / "x.mkv", [str(outer), str(inner)])
    assert found is not None and os.path.normcase(found) == os.path.normcase(os.path.realpath(inner))


# --------------------------------------------------------------------------- map_path


def test_map_path_longest_prefix() -> None:
    mappings = [
        PathMapping(source="/downloads", target="/storage/downloads"),
        PathMapping(source="/downloads/tv", target="/storage/tv-in"),
    ]
    assert map_path("/downloads/movies/x.mkv", mappings) == "/storage/downloads/movies/x.mkv"
    assert map_path("/downloads/tv/show/e1.mkv", mappings) == "/storage/tv-in/show/e1.mkv"
    assert map_path("/downloads", mappings) == "/storage/downloads"
    assert map_path("/downloadsX/x.mkv", mappings) == "/downloadsX/x.mkv"  # component-wise only
    assert map_path("/other/x.mkv", mappings) == "/other/x.mkv"


def test_map_path_windows_client_to_posix() -> None:
    mappings = [PathMapping(source="D:\\Downloads", target="/storage/downloads")]
    assert map_path("d:\\downloads\\Show\\e1.mkv", mappings) == "/storage/downloads/Show/e1.mkv"


def test_map_path_posix_to_windows_server_and_reverse() -> None:
    mappings = [PathMapping(source="/storage/media", target="\\\\nas\\media")]
    assert map_path("/storage/media/TV/Show/e1.mkv", mappings) == "\\\\nas\\media\\TV\\Show\\e1.mkv"
    assert map_path("\\\\nas\\media\\TV\\e1.mkv", mappings, reverse=True) == "/storage/media/TV/e1.mkv"


def test_map_path_trailing_slashes_and_empty() -> None:
    mappings = [PathMapping(source="/downloads/", target="/storage/dl/")]
    assert map_path("/downloads/a.mkv", mappings) == "/storage/dl/a.mkv"
    assert map_path("", mappings) == ""
    assert map_path("/x", []) == "/x"


# --------------------------------------------------------------------------- case / Unicode variants

NFD_TITLE = "Amélie (2001)"  # decomposed "e" + combining acute, as macOS writes it
NFC_TITLE = "Amélie (2001)"


def test_fold_name_ignores_case_and_unicode_normalisation() -> None:
    assert paths.fold_name("Doctor Who (2005)") == paths.fold_name("Doctor who (2005)")
    assert paths.fold_name(NFD_TITLE) == paths.fold_name(NFC_TITLE) == paths.fold_name(NFD_TITLE.upper())
    assert paths.fold_name("Doctor Who (2005)") != paths.fold_name("Doctor Who (2006)")


def test_sanitized_names_are_nfc() -> None:
    assert NFD_TITLE != NFC_TITLE
    assert sanitize_component(NFD_TITLE) == NFC_TITLE
    assert safe_filename(f"{NFD_TITLE}.mkv") == f"{NFC_TITLE}.mkv"
    assert sanitize_relative_path(f"{NFD_TITLE}/{NFD_TITLE}.mkv") == f"{NFC_TITLE}/{NFC_TITLE}.mkv"


# --------------------------------------------------------------------------- Schema's data folder


@pytest.fixture
def data_dir(tmp_path: Path):
    """tmp_path/config protected as Schema's data folder (with a demo/ exemption); undone afterwards."""
    data = tmp_path / "config"
    (data / "demo" / "library").mkdir(parents=True)
    (data / "secret.key").write_text("k")
    paths.protect([data], exempt=[data / "demo"])
    yield data
    paths.protect([])


def test_data_dir_is_never_within_the_roots(tmp_path: Path, data_dir: Path) -> None:
    roots = [str(tmp_path), os.path.abspath(os.sep)]  # an old config with "/" and the folder above /config
    assert is_within(tmp_path / "media" / "x.mkv", roots)
    assert not is_within(data_dir, roots)
    assert not is_within(data_dir / "secret.key", roots)
    assert not is_within(data_dir / "trash" / "x.mkv", roots)
    assert not is_within_nofollow(data_dir, roots)  # the folder itself, seen from its parent
    assert not is_within_nofollow(data_dir / "secret.key", roots)
    assert is_within(data_dir / "demo" / "library" / "x.mkv", roots)  # demo-mode exemption
    assert paths.is_protected_path(data_dir / "schema.db")
    assert not paths.is_protected_path(data_dir / "demo" / "x.mkv")
    assert paths.protected_paths() == (os.path.normcase(os.path.realpath(data_dir)),)


def test_data_dir_reached_through_a_symlink(tmp_path: Path, data_dir: Path, need_symlinks: None) -> None:
    media = tmp_path / "media"
    media.mkdir()
    os.symlink(data_dir, media / "config-link", target_is_directory=True)
    os.symlink(data_dir / "secret.key", media / "key.mkv")
    assert not is_within(media / "config-link" / "secret.key", [str(media)])
    assert not is_within(media / "key.mkv", [str(media)])
    assert is_within_nofollow(media / "key.mkv", [str(media)])  # the link entry itself is in the library
    assert paths.root_problem(media / "config-link") is not None


def test_root_problem(tmp_path: Path, data_dir: Path) -> None:
    assert paths.root_problem(os.path.abspath(os.sep)) is not None  # "/" (or "C:\")
    assert "contains" in (paths.root_problem(tmp_path) or "")
    assert "contains" in (paths.root_problem(data_dir) or "")
    assert "inside" in (paths.root_problem(data_dir / "trash") or "")
    assert paths.root_problem(tmp_path / "media") is None
    assert paths.root_problem(data_dir / "demo" / "library") is None


def test_nothing_protected_by_default(tmp_path: Path) -> None:
    paths.protect([])
    assert not paths.is_protected_path(tmp_path / "anything")
    assert paths.root_problem(tmp_path) is None
