from __future__ import annotations

from pathlib import Path

import pytest

from schema_app.domain import MediaKind, PathMapping
from schema_app.services.downloaders import (
    IngestRequest,
    WebhookPathRefused,
    WebhookPayloadError,
    confine_paths,
    kind_for_category,
    parse_webhook,
)

CATEGORIES = {
    "tv": MediaKind.SERIES,
    "sonarr": MediaKind.SERIES,
    "movies": MediaKind.MOVIE,
    "radarr": MediaKind.MOVIE,
    "anime": MediaKind.ANIME,
}
MAPPINGS = [
    PathMapping(source="/downloads", target="/storage/downloads"),
    PathMapping(source="/downloads/complete/anime", target="/storage/anime-in"),
]


def parse(source: str, payload: dict[str, object]) -> IngestRequest:
    return parse_webhook(source, payload, category_kinds=CATEGORIES, path_mappings=MAPPINGS)


# --------------------------------------------------------------------------- SABnzbd


def test_sabnzbd_success() -> None:
    req = parse("sabnzbd", {
        "complete_dir": "/downloads/complete/tv/Show.S01E01.1080p",
        "name": "Show.S01E01.1080p",
        "category": "tv",
        "status": "0",
    })  # fmt: skip
    assert req.paths == ["/storage/downloads/complete/tv/Show.S01E01.1080p"]
    assert req.kind_hint is MediaKind.SERIES and req.source == "sabnzbd"
    assert req.label == "Show.S01E01.1080p" and req.category == "tv"


def test_sabnzbd_longest_mapping_wins() -> None:
    req = parse(
        "sab", {"complete_dir": "/downloads/complete/anime/Kaze - 01", "category": "anime", "status": 0}
    )
    assert req.paths == ["/storage/anime-in/Kaze - 01"] and req.kind_hint is MediaKind.ANIME


@pytest.mark.parametrize(("status", "words"), [("1", "failed verification"), ("2", "failed to unpack"),
                                               ("-1", "failed post-processing"), ("7", "status 7")])  # fmt: skip
def test_sabnzbd_failed_download_is_ignored(status: str, words: str) -> None:
    with pytest.raises(WebhookPayloadError, match=words):
        parse("sabnzbd", {"complete_dir": "/downloads/x", "status": status, "fail_msg": "CRC errors"})


def test_sabnzbd_missing_dir_and_default_category() -> None:
    with pytest.raises(WebhookPayloadError, match="complete_dir"):
        parse("sabnzbd", {"name": "x", "status": "0"})
    req = parse("sabnzbd", {"complete_dir": "/elsewhere/x", "category": "*"})
    assert req.paths == ["/elsewhere/x"] and req.category is None and req.kind_hint is None


def test_sabnzbd_env_style_keys() -> None:
    req = parse("sabnzbd", {"SAB_COMPLETE_DIR": "/downloads/m", "SAB_CAT": "movies", "SAB_PP_STATUS": "0",
                            "SAB_FINAL_NAME": "Movie"})  # fmt: skip
    assert req.paths == ["/storage/downloads/m"] and req.kind_hint is MediaKind.MOVIE and req.label == "Movie"


# --------------------------------------------------------------------------- qBittorrent


def test_qbittorrent_content_path_form_fields() -> None:
    req = parse("qbittorrent", {
        "content_path": "/downloads/complete/Movie (2019) [1080p]",
        "name": "Movie (2019) [1080p]",
        "category": "radarr",
        "hash": "abc123",
    })  # fmt: skip
    assert req.paths == ["/storage/downloads/complete/Movie (2019) [1080p]"]
    assert req.kind_hint is MediaKind.MOVIE and req.label == "Movie (2019) [1080p]"


def test_qbittorrent_save_path_plus_name_and_quotes() -> None:
    req = parse("qbit", {"save_path": "/downloads/complete/", "name": "Show.S02", "category": "tv-sonarr"})
    assert req.paths == ["/storage/downloads/complete/Show.S02"] and req.kind_hint is MediaKind.SERIES
    quoted = parse("qbittorrent", {"content_path": '"/downloads/x y.mkv"'})
    assert quoted.paths == ["/storage/downloads/x y.mkv"]


def test_qbittorrent_windows_client_paths() -> None:
    mappings = [PathMapping(source="D:\\Torrents", target="/storage/torrents")]
    req = parse_webhook("qbittorrent", {"save_path": "D:\\Torrents\\done", "name": "Paper Moons (2004)"},
                        category_kinds=CATEGORIES, path_mappings=mappings)  # fmt: skip
    assert req.paths == ["/storage/torrents/done/Paper Moons (2004)"]


def test_qbittorrent_missing_path() -> None:
    with pytest.raises(WebhookPayloadError, match="%F"):
        parse("qbittorrent", {"name": "x", "hash": "y"})


# --------------------------------------------------------------------------- generic


def test_generic_single_and_list() -> None:
    req = parse("generic", {"path": "/downloads/a.mkv", "kind": "movie"})
    assert req.paths == ["/storage/downloads/a.mkv"] and req.kind_hint is MediaKind.MOVIE
    many = parse("generic", {"paths": ["/downloads/a.mkv", "/downloads/b.mkv", "/downloads/a.mkv"],
                             "category": "anime", "label": "batch"})  # fmt: skip
    assert many.paths == ["/storage/downloads/a.mkv", "/storage/downloads/b.mkv"]
    assert many.kind_hint is MediaKind.ANIME and many.label == "batch"


def test_generic_newline_separated_form_field() -> None:
    req = parse("generic", {"paths": "/downloads/a.mkv\n/downloads/b.mkv\r\n", "type": "tv"})
    assert req.paths == ["/storage/downloads/a.mkv", "/storage/downloads/b.mkv"]
    assert req.kind_hint is MediaKind.SERIES


def test_generic_kind_overrides_category_and_unknown_kind() -> None:
    req = parse("generic", {"path": "/x.mkv", "kind": "anime", "category": "movies"})
    assert req.kind_hint is MediaKind.ANIME
    assert parse("generic", {"path": "/x.mkv", "kind": "unknown"}).kind_hint is None
    with pytest.raises(WebhookPayloadError, match="Unknown kind"):
        parse("generic", {"path": "/x.mkv", "kind": "music"})


@pytest.mark.parametrize("payload", [{}, {"path": ""}, {"paths": []}, {"paths": "  \n "}, {"kind": "movie"}])
def test_generic_empty_payloads(payload: dict[str, object]) -> None:
    with pytest.raises(WebhookPayloadError):
        parse("generic", payload)


def test_unknown_source_and_bad_payload() -> None:
    with pytest.raises(WebhookPayloadError, match="Unknown webhook source"):
        parse("transmission", {"path": "/x"})
    with pytest.raises(WebhookPayloadError):
        parse_webhook("generic", ["not", "a", "dict"], category_kinds={}, path_mappings=[])  # type: ignore[arg-type]


def test_error_is_a_value_error() -> None:
    assert issubclass(WebhookPayloadError, ValueError)


# --------------------------------------------------------------------------- smuggled paths


@pytest.mark.parametrize("payload", [
    {"path": "/downloads/Evil.Name\n/storage"},  # "path" is one path: a newline is not a separator
    {"path": "/downloads/Evil.Name\r/storage"},
    {"paths": "/downloads/a.mkv\r/storage"},  # a lone CR is not a line break
    {"paths": ["/downloads/a.mkv", "/downloads/b\n.mkv"]},
    {"path": "/downloads/a\x00.mkv"},
    {"path": "/downloads/../storage/movies"},
    {"paths": ["/downloads/x/../../storage"]},
])  # fmt: skip
def test_generic_refuses_control_characters_and_dotdot(payload: dict[str, object]) -> None:
    with pytest.raises(WebhookPayloadError):
        parse("generic", payload)


@pytest.mark.parametrize("name", ["../../../storage/movies", "..", "a/b", "a\\b", "x\ny"])
def test_qbittorrent_name_must_be_one_component(name: str) -> None:
    with pytest.raises(WebhookPayloadError):
        parse("qbittorrent", {"save_path": "/downloads/complete", "name": name})


def test_qbittorrent_and_sabnzbd_paths_checked_too() -> None:
    with pytest.raises(WebhookPayloadError, match=r"\.\."):
        parse("qbittorrent", {"content_path": "/downloads/../storage/movies"})
    with pytest.raises(WebhookPayloadError, match="control character"):
        parse("sabnzbd", {"complete_dir": "/downloads/x\n/storage", "status": "0"})


def test_names_with_dots_are_fine() -> None:
    req = parse("qbittorrent", {"save_path": "/downloads/complete", "name": "Show...S01..1080p"})
    assert req.paths == ["/storage/downloads/complete/Show...S01..1080p"]


# --------------------------------------------------------------------------- download area


@pytest.fixture
def area(tmp_path: Path) -> dict[str, Path]:
    media = tmp_path / "media"
    dirs = {"media": media, "downloads": media / "downloads", "movies": media / "movies", "tv": media / "tv"}
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def confine(paths: list[Path | str], area: dict[str, Path], *, areas: list[Path] | None = None) -> list[str]:
    return confine_paths(
        [str(p) for p in paths],
        download_areas=[str(a) for a in (areas if areas is not None else [area["downloads"]])],
        allowed_roots=[str(area["media"])],
        library_roots=[str(area["movies"]), str(area["tv"])],
    )


def test_confine_accepts_downloads_and_normalizes(area: dict[str, Path]) -> None:
    release = area["downloads"] / "radarr" / "Film.2010"
    assert confine([release, f"{release}{'/'}"], area) == [str(release)]
    assert confine([str(area["downloads"]) + "/./x.mkv"], area) == [str(area["downloads"] / "x.mkv")]


@pytest.mark.parametrize("target", ["media", "movies", "tv", "downloads"])
def test_confine_refuses_roots_and_the_download_folder_itself(area: dict[str, Path], target: str) -> None:
    with pytest.raises(WebhookPathRefused):
        confine([area[target]], area)


def test_confine_refuses_library_and_outside(area: dict[str, Path], tmp_path: Path) -> None:
    with pytest.raises(WebhookPathRefused, match="not inside a download folder"):
        confine([area["movies"] / "Film (2010)"], area)
    with pytest.raises(WebhookPathRefused, match="outside the allowed roots"):
        confine([tmp_path / "elsewhere" / "x.mkv"], area)
    with pytest.raises(WebhookPathRefused, match="absolute"):
        confine(["relative/x.mkv"], area)
    # The normalized form counts: ".." cannot climb out of the download folder.
    with pytest.raises(WebhookPathRefused):
        confine([f"{area['downloads']}/../movies"], area)


def test_confine_needs_a_download_area(area: dict[str, Path]) -> None:
    with pytest.raises(WebhookPathRefused, match="watch folder"):
        confine([area["downloads"] / "x.mkv"], area, areas=[])


def test_confine_broad_mapping_target_does_not_open_the_library(area: dict[str, Path]) -> None:
    # A mapping of the whole share ("/data" -> "/media") makes /media a download area, but the
    # library below it stays off limits and so do the roots themselves.
    broad = [area["media"]]
    assert confine([area["downloads"] / "x"], area, areas=broad) == [str(area["downloads"] / "x")]
    with pytest.raises(WebhookPathRefused, match="inside the library"):
        confine([area["movies"] / "Film (2010)"], area, areas=broad)
    with pytest.raises(WebhookPathRefused):
        confine([area["movies"]], area, areas=broad)


def test_confine_download_folder_inside_a_library_root(area: dict[str, Path]) -> None:
    incoming = area["movies"] / "_incoming"
    incoming.mkdir()
    assert confine([incoming / "Film.2010"], area, areas=[incoming]) == [str(incoming / "Film.2010")]


# --------------------------------------------------------------------------- categories


@pytest.mark.parametrize(
    ("category", "kind"),
    [
        ("TV", MediaKind.SERIES),
        ("tv/hd", MediaKind.SERIES),
        ("tv-sonarr", MediaKind.SERIES),
        ("Movies", MediaKind.MOVIE),
        ("radarr4k", None),
        ("anime_seasonal", MediaKind.ANIME),
        ("music", None),
        ("", None),
    ],
)
def test_kind_for_category(category: str, kind: MediaKind | None) -> None:
    assert kind_for_category(category, CATEGORIES) is kind


def test_category_map_from_settings_strings() -> None:
    assert kind_for_category("docs", {"docs": "movie"}) is MediaKind.MOVIE  # type: ignore[dict-item]
