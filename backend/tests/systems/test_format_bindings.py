"""Bindings (SPEC §8.5, FileBot names) computed from ParsedMedia + Candidate + ProbeInfo."""

from __future__ import annotations

from datetime import date

import pytest
from format_helpers import render_sample, sample

from schema_app.core.format import BindingMissing, build_binding, compile_format
from schema_app.core.format.bindings import BINDINGS, join_titles
from schema_app.domain import (
    Candidate,
    EpisodeInfo,
    MediaKind,
    MovieInfo,
    ParsedMedia,
    ProbeInfo,
    ProviderRef,
    QualityInfo,
    SeriesInfo,
)

MULTI = "multi-episode"
SPECIAL = "special"
PROBED = "title with"
ANIME = "anime absolute"
OVA = "anime special"
EDITION = "movie with edition"
PART = "multi-part"
NOYEAR = "movie without"


@pytest.mark.parametrize(
    ("expr", "note", "expected"),
    [
        ("{n}", MULTI, "Starfall Station"),
        ("{y}", MULTI, "2016"),
        ("{ny}", MULTI, "Starfall Station (2016)"),
        ("{s}", MULTI, "1"),
        ("{e}", MULTI, "2"),
        ("{es}", MULTI, "[2, 3]"),
        ("{s00}", MULTI, "01"),
        ("{e00}", MULTI, "02-03"),
        ("{sxe}", MULTI, "1x02-03"),
        ("{s00e00}", MULTI, "S01E02-E03"),
        ("{t}", MULTI, "Dead Reckoning (1-2)"),
        ("{type}", MULTI, "MultiEpisode"),
        ("{absolute}", MULTI, "2"),
        ("{airdate}", MULTI, "2016-09-14"),
        ("{d.format('yyyy')}", MULTI, "2016"),
        ("{startdate}", MULTI, "2016-09-07"),
        ("{regular}", MULTI, "true"),
        ("{special ? 'Specials' : 'Season ' + s.pad(2)}", MULTI, "Season 01"),
        ("{sc}", MULTI, "3"),
        ("{episode.title}", MULTI, "Dead Reckoning (1)"),
        ("{episode}", MULTI, "Starfall Station - 1x02 - Dead Reckoning (1)"),
        ("{episodes.size()}", MULTI, "2"),
        ("{vf} {vc} {vs} {source} {group}", MULTI, "720p x264 HDTV HDTV KILLERS"),
        ("{hd}", MULTI, "HD"),
        ("{id} {tvdbid} {tmdbid} {imdbid} {provider}", MULTI, "900001 900001 800001 tt9000001 tvdb"),
        ("{genre} {genres.size()} {certification} {rating} {language}", MULTI, "Science Fiction 2 TV-14 8.1 en"),
        ("{info.network} {info.status}", MULTI, "Orbital Broadcasting Ended"),
        ("{az} {decade} {anime}", MULTI, "S 2010 false"),
        ("{fn}", MULTI, "Starfall.Station.S01E02E03.720p.HDTV.x264-KILLERS"),
        ("{ext} {original}", MULTI, "mkv Starfall.Station.S01E02E03.720p.HDTV.x264-KILLERS"),
        ("{f.name}", MULTI, "Starfall.Station.S01E02E03.720p.HDTV.x264-KILLERS.mkv"),
        ("{folder.name}", MULTI, "downloads"),
        ("{s} {s00e00} {sxe}", SPECIAL, "0 S00E02 0x02"),
        ("{special}", SPECIAL, "2"),
        ("{regular}", SPECIAL, "false"),
        ("{special ? 'Specials' : 'Season ' + s.pad(2)}", SPECIAL, "Specials"),
        ("{episode.special}", SPECIAL, "true"),
        ("{t}", PROBED, "Either-Or: Part One".replace(":", " -")),
        ("{vf} {vc} {resolution} {width}x{height} {bitdepth} {fps}", PROBED, "1080p HEVC 1920x1080 1920x1080 10 23.976"),
        ("{ac} {channels} {af} {acf} {hd}", PROBED, "DDP 5.1 6ch DDP 5.1 FHD"),
        ("{bitrate} {duration} {minutes} {hours}", PROBED, "6500 2712 45 0∶45"),
        ("{n} {s00e00} {absolute} {absolute.pad(3)}", ANIME, "Kaze no Tsubasa S02E03 15 015"),
        ("{anime} {anidbid} {tvdbid} {id}", ANIME, "true 90001 900101 90001"),
        ("{primaryTitle}", ANIME, "風の翼"),
        ("{alias.first()}", ANIME, "Wings of the Wind"),
        ("{crc32} {group}", ANIME, "A1B2C3D4 SubsPlease"),
        ("{s00e00} {special}", OVA, "S00E01 1"),
        ("{ny}", EDITION, "The Quiet Meridian (2019)"),
        ("{edition} {tags}", EDITION, "Director's Cut [Director's Cut]"),
        ("{collection}", EDITION, "The Meridian Collection"),
        ("{runtime} {certification} {rating}", EDITION, "124 PG-13 7.5"),
        ("{id} {tmdbid} {imdbid} {type}", EDITION, "700001 700001 tt7000001 Movie"),
        ("{d}", EDITION, "2019-10-04"),
        ("{ac} {channels} {af}", EDITION, "DTS 5.1 6ch"),
        ("{az}", EDITION, "Q"),
        ("{pi}/{pc} {type}", PART, "1/2 MoviePart"),
        ("{ny}", NOYEAR, "Static Bloom"),
    ],
)
def test_sample_bindings(expr: str, note: str, expected: str) -> None:
    assert compile_format(expr).render(sample(note).bindings(), strict=True).removesuffix(".mkv").removesuffix(".avi") == expected


@pytest.mark.parametrize(
    ("name", "note"),
    [("t", EDITION), ("s", EDITION), ("s00e00", EDITION), ("special", EDITION), ("episode", EDITION),
     ("y", NOYEAR), ("collection", MULTI), ("pc", EDITION), ("lang", MULTI), ("subt", MULTI),
     ("resolution", MULTI), ("hdr", MULTI), ("aco", MULTI), ("di", MULTI), ("sn", MULTI), ("country", MULTI)],
)  # fmt: skip
def test_undefined_bindings_raise_binding_missing(name: str, note: str) -> None:
    with pytest.raises(BindingMissing):
        sample(note).bindings()[name]


def _parsed(filename: str, kind: MediaKind = MediaKind.SERIES, **fields) -> ParsedMedia:
    return ParsedMedia(path="/in/" + filename, filename=filename, extension=filename.rsplit(".", 1)[1], kind=kind, **fields)


def test_hdr_and_dolby_vision_labels() -> None:
    parsed = _parsed("x.mkv", MediaKind.MOVIE, quality=QualityInfo(resolution="2160p", hdr=["DV", "HDR10"], atmos=True,
                                                                    audio_codec="TrueHD", audio_channels="7.1", remux=True))
    bindings = build_binding(parsed, None, None)
    assert bindings["hdr"] == "DV+HDR10"
    assert bindings["dovi"] is True
    assert bindings["aco"] == "Atmos"
    assert bindings["af"] == "8ch"
    assert bindings["remux"] is True
    assert bindings["hd"] == "UHD"


@pytest.mark.parametrize(
    ("hdr", "expected"),
    [(["HDR10"], "HDR10"), (["DV"], "DV"), (["HDR10+", "HDR10"], "HDR10+"), (["HLG"], "HLG")],
)
def test_hdr_variants(hdr: list[str], expected: str) -> None:
    bindings = build_binding(_parsed("x.mkv", MediaKind.MOVIE, quality=QualityInfo(hdr=hdr)), None, None)
    assert bindings["hdr"] == expected


def test_probe_wins_over_filename_quality() -> None:
    parsed = _parsed("x.mkv", MediaKind.MOVIE, quality=QualityInfo(resolution="720p", video_codec="x264"))
    probe = ProbeInfo(width=1920, height=800, video_codec="HEVC")
    bindings = build_binding(parsed, None, probe)
    assert (bindings["vf"], bindings["vc"], bindings["resolution"]) == ("1080p", "HEVC", "1920x800")


@pytest.mark.parametrize(
    ("width", "height", "vf"),
    [(3840, 1600, "2160p"), (1920, 1080, "1080p"), (1280, 534, "720p"), (720, 576, "576p"), (720, 480, "480p")],
)
def test_vf_buckets_from_probe(width: int, height: int, vf: str) -> None:
    bindings = build_binding(_parsed("x.mkv", MediaKind.MOVIE), None, ProbeInfo(width=width, height=height))
    assert bindings["vf"] == vf


def test_parse_only_bindings_without_candidate() -> None:
    parsed = _parsed("Show.S02E05.mkv", title="Show", season=2, episodes=[5], year=2020)
    bindings = build_binding(parsed, None, None)
    assert compile_format("{n} - {s00e00}").render(bindings) == "Show - S02E05.mkv"
    with pytest.raises(BindingMissing):
        bindings["t"]


def test_sidecar_bindings_and_extension_suffix() -> None:
    parsed = _parsed("Show.S01E01.mkv", title="Show", season=1, episodes=[1])
    bindings = build_binding(parsed, None, None, sidecar="/in/Show.S01E01.eng.forced.srt")
    assert bindings.extension_suffix == ".eng.forced.srt"
    assert bindings["lang"] == "eng"
    assert bindings["subt"] == ".eng.forced"
    assert bindings["ext"] == "srt"
    assert compile_format("{n} - {s00e00}").render(bindings) == "Show - S01E01.eng.forced.srt"


def test_overrides_win_and_feed_context_bindings() -> None:
    parsed = _parsed("Movie.CD2.avi", MediaKind.MOVIE, title="Movie", part=2)
    bindings = build_binding(parsed, None, None, overrides={"pc": 3, "di": 2, "dc": 2, "crc32": "ABCDEF01", "bytes": 3 * 1024**3})
    assert compile_format("{n} {pi}-{pc} {di}-{dc} {crc32} {gigabytes} {megabytes}").render(bindings) == (
        "Movie 2-3 2-2 ABCDEF01 3.0 3072.avi"
    )


def test_bytes_from_disk(tmp_path) -> None:
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"x" * 2048)
    parsed = ParsedMedia(path=str(video), filename="Movie.mkv", extension="mkv", kind=MediaKind.MOVIE, title="Movie")
    assert build_binding(parsed, None, None)["bytes"] == 2048


def test_ny_does_not_duplicate_year() -> None:
    movie = MovieInfo(ref=ProviderRef(provider="tmdb", id="1"), title="Heat (1995)", year=1995)
    candidate = Candidate(id="tmdb:movie:1", kind=MediaKind.MOVIE, provider="tmdb", title="Heat (1995)", year=1995, score=1, movie=movie)
    assert build_binding(_parsed("h.mkv", MediaKind.MOVIE), candidate, None)["ny"] == "Heat (1995)"


@pytest.mark.parametrize(
    ("titles", "expected"),
    [
        (["Dead Reckoning (1)", "Dead Reckoning (2)"], "Dead Reckoning (1-2)"),
        (["Finale Part 1", "Finale Part 2"], "Finale (1-2)"),
        (["Pilot", "Second"], "Pilot & Second"),
        (["Same", "Same"], "Same"),
        ([], None),
    ],
)
def test_multi_episode_title_join(titles: list[str], expected: str | None) -> None:
    assert join_titles(titles) == expected


def test_cross_season_multi_episode() -> None:
    series = SeriesInfo(ref=ProviderRef(provider="tvdb", id="1"), title="Show", year=2010)
    episodes = [EpisodeInfo(series_ref=series.ref, season=1, episode=10), EpisodeInfo(series_ref=series.ref, season=2, episode=1)]
    candidate = Candidate(id="tvdb:series:1", kind=MediaKind.SERIES, provider="tvdb", title="Show", score=1, series=series, episodes=episodes)
    bindings = build_binding(_parsed("x.mkv"), candidate, None)
    assert (bindings["s00e00"], bindings["sxe"]) == ("S01E10-S02E01", "1x10-2x01")


def test_values_are_cleaned_at_bind_time_but_keep_colons() -> None:
    series = SeriesInfo(ref=ProviderRef(provider="tvdb", id="1"), title="Face/Off: Again?", year=2010)
    candidate = Candidate(id="tvdb:series:1", kind=MediaKind.SERIES, provider="tvdb", title="x", score=1, series=series,
                          episodes=[EpisodeInfo(series_ref=series.ref, season=1, episode=1, title="A/B")])
    bindings = build_binding(_parsed("x.mkv"), candidate, None)
    assert bindings["n"] == "Face-Off: Again"
    assert bindings["episode"].get_property("title") == "A-B"


def test_series_id_tag_follows_source_database() -> None:
    series = SeriesInfo(ref=ProviderRef(provider="tmdb", id="800001"), title="Starfall Station", year=2016,
                        ids={"tmdb": "800001", "tvdb": "900001"})
    candidate = Candidate(id="tmdb:series:800001", kind=MediaKind.SERIES, provider="tmdb", title="Starfall Station",
                          score=1, series=series, episodes=[EpisodeInfo(series_ref=series.ref, season=1, episode=1)])
    bindings = build_binding(_parsed("x.mkv"), candidate, None)
    assert compile_format("{plex.id}").render(bindings).startswith("Starfall Station (2016) {tmdb-800001}/Season 01/")
    assert compile_format("{kodi.id}").render(bindings).startswith("Starfall Station (2016) {tmdb=800001}/Season 1/")


def test_movie_id_tag_falls_back_to_imdb() -> None:
    movie = MovieInfo(ref=ProviderRef(provider="trakt", id="5"), title="Heat", year=1995, ids={"imdb": "tt0113277"})
    candidate = Candidate(id="trakt:movie:5", kind=MediaKind.MOVIE, provider="trakt", title="Heat", year=1995, score=1, movie=movie)
    bindings = build_binding(_parsed("h.mkv", MediaKind.MOVIE), candidate, None)
    assert compile_format("{plex.id}").render(bindings) == "Heat (1995) {imdb-tt0113277}/Heat (1995) {imdb-tt0113277}.mkv"
    assert compile_format("{jellyfin.id}").render(bindings) == "Heat (1995) [imdbid-tt0113277]/Heat (1995).mkv"


@pytest.mark.parametrize(
    ("expr", "note", "expected"),
    [
        ("{plex}", MULTI, "Starfall Station (2016)/Season 01/Starfall Station (2016) - S01E02-E03 - Dead Reckoning (1-2).mkv"),
        ("{plex}", SPECIAL, "Starfall Station (2016)/Specials/Starfall Station (2016) - S00E02 - Christmas Special.mkv"),
        ("{plex.id}", MULTI, "Starfall Station (2016) {tvdb-900001}/Season 01/Starfall Station (2016) - S01E02-E03 - Dead Reckoning (1-2).mkv"),
        ("{plex.name}", MULTI, "Starfall Station (2016) - S01E02-E03 - Dead Reckoning (1-2).mkv"),
        ("{jellyfin}", SPECIAL, "Starfall Station (2016)/Season 00/Starfall Station (2016) - S00E02 - Christmas Special.mkv"),
        ("{jellyfin.id}", ANIME, "Kaze no Tsubasa (2018) [tvdbid-900101]/Season 02/Kaze no Tsubasa (2018) - S02E03 - Crosswind.mkv"),
        ("{emby.id}", SPECIAL, "Starfall Station (2016) [tvdbid=900001]/Season 00/Starfall Station (2016) - S00E02 - Christmas Special.mkv"),
        ("{kodi}", PROBED, "Starfall Station (2016)/Season 2/Starfall Station (2016) - S02E10 - Either-Or - Part One.mkv"),
        ("{kodi.id}", OVA, "Kaze no Tsubasa (2018) {tvdb=900101}/Season 0/Kaze no Tsubasa (2018) - S00E01 - Wings Over the Sea.mkv"),
        ("{plex}", EDITION, "The Quiet Meridian (2019)/The Quiet Meridian (2019) {edition-Director's Cut}.mkv"),
        ("{plex.id}", EDITION, "The Quiet Meridian (2019) {tmdb-700001}/The Quiet Meridian (2019) {tmdb-700001} {edition-Director's Cut}.mkv"),
        ("{plex}", PART, "Paper Moons (2004)/Paper Moons (2004) - pt1.avi"),
        ("{jellyfin.id}", PART, "Paper Moons (2004) [tmdbid-700005]/Paper Moons (2004) - pt1.avi"),
        ("{jellyfin.id}", EDITION, "The Quiet Meridian (2019) [tmdbid-700001]/The Quiet Meridian (2019) - Director's Cut.mkv"),
        ("{emby.id}", EDITION, "The Quiet Meridian (2019) [tmdbid=700001]/The Quiet Meridian (2019) - Director's Cut.mkv"),
        ("{kodi.id}", EDITION, "The Quiet Meridian (2019) {tmdb=700001}/The Quiet Meridian (2019) - Director's Cut.mkv"),
        ("{plex}", NOYEAR, "Static Bloom/Static Bloom.mkv"),
    ],
)
def test_preset_bindings(expr: str, note: str, expected: str) -> None:
    assert render_sample(expr, note) == expected


def test_every_binding_has_catalog_metadata() -> None:
    for name, definition in BINDINGS.items():
        spec = definition.spec
        assert spec.name == name and spec.description and spec.example and spec.group and spec.kinds


def test_episode_order_of_unknown_kind_uses_episodes() -> None:
    parsed = _parsed("Show.1x02.mkv", MediaKind.UNKNOWN, title="Show", season=1, episodes=[2])
    bindings = build_binding(parsed, None, None)
    assert bindings["type"] == "Episode"
    assert compile_format("{plex}").render(bindings) == "Show/Season 01/Show - S01E02.mkv"


def test_daily_show_air_date_binding() -> None:
    parsed = _parsed("Late.2024.03.04.mkv", title="The Late Hour", air_date=date(2024, 3, 4))
    bindings = build_binding(parsed, None, None)
    assert compile_format("{n} - {airdate.format('yyyy-MM-dd')}").render(bindings) == "The Late Hour - 2024-03-04.mkv"


@pytest.mark.parametrize(
    ("mapped", "db", "jellyfin", "plex"),
    [
        ("tvdb", "tvdb", "Sky Pirates (2017) [tvdbid-99100]/Season 01/", "Sky Pirates (2017) {tvdb-99100}/Season 01/"),
        ("tmdb", "tmdb", "Sky Pirates (2017) [tmdbid-4400]/Season 01/", "Sky Pirates (2017) {tmdb-4400}/Season 01/"),
        (None, "anidb", "Sky Pirates (2017) [tvdbid-99100]/Season 01/", "Sky Pirates (2017) {tvdb-99100}/Season 01/"),
    ],
)  # fmt: skip
def test_anidb_series_id_tag_follows_the_mapped_numbering(mapped: str | None, db: str, jellyfin: str, plex: str) -> None:
    # An AniDB match whose Anime-Lists entry knows both ids: the tag names the database whose
    # season/episode numbers the file carries ({db}), not just "TVDB unless the provider is TMDb".
    series = SeriesInfo(ref=ProviderRef(provider="anidb", id="61002"), title="Sky Pirates", year=2017, is_anime=True,
                        ids={"anidb": "61002", "tvdb": "99100", "tmdb": "4400"})
    ids = {"anidb": "710003"} | ({"mapped": mapped} if mapped else {})
    episode = EpisodeInfo(series_ref=series.ref, season=1, episode=15, absolute=3, title="Iron Gulls", ids=ids)
    candidate = Candidate(id="anidb:anime:61002", kind=MediaKind.ANIME, provider="anidb", title="Sky Pirates",
                          year=2017, score=1, series=series, episodes=[episode])
    bindings = build_binding(_parsed("x.mkv", MediaKind.ANIME), candidate, None)
    assert bindings["db"] == db and bindings["provider"] == "anidb"
    assert compile_format("{jellyfin.id}").render(bindings).startswith(jellyfin)
    assert compile_format("{plex.id}").render(bindings).startswith(plex)
