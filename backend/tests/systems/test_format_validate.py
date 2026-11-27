"""validate(), catalog(), presets and samples (SPEC §8.4–§8.7), FileBot paste-in examples and a
couple of integration checks against the real services.paths sanitizers."""

from __future__ import annotations

import pytest
from format_helpers import render_sample, sample

from schema_app.core.format import (
    DEFAULT_FORMATS,
    PRESETS,
    catalog,
    compile_format,
    get_preset,
    render_samples,
    samples_for,
    validate,
)
from schema_app.core.format.presets import PRESET_BINDINGS
from schema_app.core.format.samples import SAMPLES
from schema_app.domain import MediaKind


def _messages(expr: str, kind: MediaKind) -> list[tuple[str, str]]:
    return [(issue.severity, issue.message) for issue in validate(expr, kind)]


# --------------------------------------------------------------------------- validate


def test_valid_default_formats_have_no_issues() -> None:
    for kind in (MediaKind.MOVIE, MediaKind.SERIES, MediaKind.ANIME):
        assert validate(DEFAULT_FORMATS[kind], kind) == []


def test_syntax_error_is_single_positioned_issue() -> None:
    issues = validate("{n} - {s00e00", MediaKind.SERIES)
    assert len(issues) == 1
    assert issues[0].severity == "error" and issues[0].start == 6


def test_unknown_method_is_error_with_suggestion() -> None:
    issues = validate("{n.lowr()}", MediaKind.SERIES)
    assert issues[0].severity == "error"
    assert "did you mean 'lower'" in issues[0].message
    assert (issues[0].start, issues[0].end) == (3, 9)


def test_unsupported_filebot_method_is_warning() -> None:
    assert ("warning", "Unsupported FileBot method 'transliterate()'; this block will render empty") in _messages(
        "{n.transliterate('Any-Latin')}", MediaKind.SERIES
    )


def test_arity_error() -> None:
    assert any("takes 1–2 argument(s)" in m for s, m in _messages("{e.pad()}", MediaKind.SERIES) if s == "error")


def test_unknown_binding_is_warning_with_suggestion() -> None:
    assert ("warning", "Unknown binding 'sooeoo'") in _messages("{sooeoo}", MediaKind.SERIES)
    assert any("did you mean 's00e00'" in m for _, m in _messages("{s00e0}", MediaKind.SERIES))


@pytest.mark.parametrize("name", ["home", "output", "drive", "label", "mediaFile", "i", "defines"])
def test_unsupported_filebot_bindings_are_warnings(name: str) -> None:
    issues = validate("{n}/{" + name + "}", MediaKind.MOVIE)
    assert any(i.severity == "warning" and f"Unsupported FileBot binding '{name}'" in i.message for i in issues)


def test_output_binding_hint_mentions_library_root() -> None:
    assert any("relative to the library root" in m for _, m in _messages("{output}/{n}", MediaKind.MOVIE))


def test_binding_not_available_for_kind_is_warning() -> None:
    issues = validate("{n} - {t}", MediaKind.MOVIE)
    assert [(i.severity, i.start) for i in issues] == [("warning", 7)]
    assert "not available for movie formats" in issues[0].message


def test_fallback_contexts_do_not_warn_about_kind() -> None:
    assert validate("{ny}{any{' - ' + t}{''}}{t ?: ''}", MediaKind.MOVIE) == []


def test_unknown_object_property_is_warning() -> None:
    assert any("'episode' has no property 'tittle'" in m for _, m in _messages("{episode.tittle}", MediaKind.SERIES))


def test_absolute_format_is_error() -> None:
    assert any(s == "error" and "relative to the library root" in m for s, m in _messages("/media/{n}", MediaKind.MOVIE))
    assert any(s == "error" for s, _ in _messages("C:\\Media\\{n}", MediaKind.MOVIE))


def test_trailing_separator_is_error() -> None:
    assert any(s == "error" and "must end with a file name" in m for s, m in _messages("{n}/", MediaKind.MOVIE))


def test_sample_block_errors_become_warnings() -> None:
    issues = validate("{n} {n < 3}", MediaKind.SERIES)
    assert [(i.severity, i.start) for i in issues] == [("warning", 5)]
    assert "Cannot compare" in issues[0].message


def test_empty_render_for_samples_is_reported() -> None:
    assert any("empty path" in m for _, m in _messages("{t}", MediaKind.MOVIE))


# --------------------------------------------------------------------------- samples


def test_samples_cover_spec_cases() -> None:
    notes = {s.note for s in SAMPLES}
    for needed in ("multi-episode", "special", "anime absolute 15 → S02E03", "movie with edition", "multi-part movie",
                   "movie without a year"):
        assert needed in notes
    assert len(samples_for(MediaKind.UNKNOWN)) == len(SAMPLES)
    assert {s.kind for s in samples_for(MediaKind.MOVIE)} == {MediaKind.MOVIE}


def test_render_samples_outputs() -> None:
    results = render_samples(DEFAULT_FORMATS[MediaKind.MOVIE], MediaKind.MOVIE)
    outputs = {r.source: r.output for r in results}
    assert outputs["The.Quiet.Meridian.2019.Directors.Cut.1080p.BluRay.x264-SPARKS.mkv"] == (
        "The Quiet Meridian (2019)/The Quiet Meridian (2019) - Director's Cut.mkv"
    )
    assert outputs["Paper.Moons.2004.CD1.DVDRip.XviD-FiCO.avi"] == "Paper Moons (2004)/Paper Moons (2004) - pt1.avi"
    assert outputs["Static Bloom.mkv"] == "Static Bloom/Static Bloom.mkv"


def test_render_samples_reports_empty_paths_and_syntax_errors() -> None:
    assert all(r.error and "empty path" in r.error for r in render_samples("{t}", MediaKind.MOVIE))
    assert all(r.error and "Missing" in r.error for r in render_samples("{n", MediaKind.SERIES))


def test_default_series_and_anime_formats() -> None:
    assert render_sample(DEFAULT_FORMATS[MediaKind.SERIES], "multi-episode") == (
        "Starfall Station (2016)/Season 01/Starfall Station - S01E02-E03 - Dead Reckoning (1-2).mkv"
    )
    assert render_sample(DEFAULT_FORMATS[MediaKind.SERIES], "special") == (
        "Starfall Station (2016)/Specials/Starfall Station - S00E02 - Christmas Special.mkv"
    )
    assert render_sample(DEFAULT_FORMATS[MediaKind.ANIME], "anime absolute") == (
        "Kaze no Tsubasa/Season 02/Kaze no Tsubasa - S02E03 - Crosswind.mkv"
    )
    assert render_sample(get_preset("anime-absolute").expression, "anime absolute") == (
        "Kaze no Tsubasa/Kaze no Tsubasa - 015 - Crosswind.mkv"
    )


# --------------------------------------------------------------------------- presets & catalog


def test_preset_ids_and_kinds() -> None:
    ids = {p.id for p in PRESETS}
    for prefix in ("default", "plex", "plex-ids", "jellyfin", "emby", "kodi"):
        for kind in ("movie", "series", "anime"):
            assert f"{prefix}-{kind}" in ids
    assert "anime-absolute" in ids
    assert get_preset("nope") is None


@pytest.mark.parametrize("preset_id", sorted(PRESET_BINDINGS))
def test_preset_expression_matches_its_binding(preset_id: str) -> None:
    preset = get_preset(preset_id)
    assert preset is not None
    binding_template = compile_format(PRESET_BINDINGS[preset_id])
    expr_template = compile_format(preset.expression)
    for item in samples_for(preset.kind):
        bindings = item.bindings()
        assert expr_template.render(bindings) == binding_template.render(bindings), item.source


def test_every_preset_validates_cleanly() -> None:
    for preset in PRESETS:
        assert [i for i in validate(preset.expression, preset.kind) if i.severity == "error"] == [], preset.id


def test_catalog_contents() -> None:
    cat = catalog()
    names = {b.name for b in cat.bindings}
    p0 = set(
        ["n", "y", "ny", "s", "e", "es", "s00", "e00", "sxe", "s00e00", "t", "d", "airdate", "absolute", "special", "regular", "sn", "startdate", "id", "tmdbid", "tvdbid", "imdbid", "vf", "vc", "ac", "af", "channels", "resolution", "width", "height", "hd", "hdr", "dovi", "bitdepth", "fps", "bitrate", "ext", "fn", "f", "folder", "original", "group", "vs", "source", "tags", "edition", "lang", "subt", "crc32", "plex", "jellyfin", "emby", "kodi", "di", "dc", "pi", "pc", "anime", "genre", "genres", "collection", "certification", "rating"]
    )
    assert set(p0) <= names
    assert {"plex.id", "plex.name", "jellyfin.id", "emby.id"} <= names
    groups = {b.group for b in cat.bindings}
    assert {"Naming", "Episode", "Movie", "Info", "IDs", "Quality", "File", "Presets", "Context"} <= groups
    filters = {f.name for f in cat.filters}
    assert {"pad", "lower", "upper", "upperInitial", "lowerTrail", "colon", "slash", "space", "replace", "replaceAll",
            "removeAll", "remove", "before", "after", "match", "ascii", "acronym", "sortName", "sortInitial", "trim",
            "take", "replaceTrailingBrackets", "removeIllegalCharacters", "join", "first", "last", "sort", "size",
            "contains", "startsWith", "endsWith", "isEmpty", "format", "toInteger", "toString", "truncate",
            "joining"} <= filters
    assert len(cat.presets) == len(PRESETS)


# --------------------------------------------------------------------------- FileBot paste-in examples


@pytest.mark.parametrize(
    ("expr", "note", "expected"),
    [
        ("{n} - {s00e00} - {t}", "multi-episode", "Starfall Station - S01E02-E03 - Dead Reckoning (1-2)"),
        ("{ny}/{'Season '+s}/{n} - {s00e00} - {t}", "special",
         "Starfall Station (2016)/Season 0/Starfall Station - S00E02 - Christmas Special"),
        ("{n}/{episode.special ? 'Specials' : 'Season ' + s}/{n} - {sxe} - {t}", "special",
         "Starfall Station/Specials/Starfall Station - 0x02 - Christmas Special"),
        ("{[vf, vc, ac]}", "title with", "[1080p, HEVC, DDP]"),
        ("{n} {[vf, vc, ac]}", "movie with edition", "The Quiet Meridian [1080p, x264, DTS]"),
        ("{n.colon(' - ')} {any{edition}{'Theatrical'}}", "movie with edition", "The Quiet Meridian Director's Cut"),
        ("{any{edition}{'Theatrical'}}", "movie without", "Theatrical"),
        ("{n.replaceAll(/[:]/, '')} - {t.replaceAll(/[:]/, '')}", "title with", "Starfall Station - Either-Or Part One"),
        ("{az}/{n}", "multi-episode", "S/Starfall Station"),
        ("{n.upper()} - {s00e00.lower()}", "multi-episode", "STARFALL STATION - s01e02-e03"),
        ("{n} [{tmdbid}]/{ny} [{vf} {source}]", "movie with edition",
         "The Quiet Meridian [700001]/The Quiet Meridian (2019) [1080p BluRay]"),
        ("{ny}{' {edition-' + edition + '}'}", "movie with edition", "The Quiet Meridian (2019) {edition-Director's Cut}"),
        ("{n} - {absolute.pad(3)} - {t} [{group}][{crc32}]", "anime absolute",
         "Kaze no Tsubasa - 015 - Crosswind [SubsPlease][A1B2C3D4]"),
        ("{primaryTitle.ascii()} - {s00e00}", "anime absolute", "Feng noYi - S02E03"),
        ("{n} ({y}) - {d.format('yyyy-MM-dd')}", "movie with edition", "The Quiet Meridian (2019) - 2019-10-04"),
        ("{genres.take(2).join(', ')}/{ny}", "movie with edition", "Thriller, Drama/The Quiet Meridian (2019)"),
        ("{collection}/{ny}", "movie with edition", "The Meridian Collection/The Quiet Meridian (2019)"),
        ("{ny}{pi ? ' CD' + pi : ''}", "multi-part", "Paper Moons (2004) CD1"),
        ("{plex.id}", "multi-episode",
         "Starfall Station (2016) {tvdb-900001}/Season 01/Starfall Station (2016) - S01E02-E03 - Dead Reckoning (1-2)"),
        ("{n.space('.')}.{s00e00}.{vf}", "title with", "Starfall.Station.S02E10.1080p"),
        ("{n} - {s00e00} - {t.replacePart()}", "multi-episode", "Starfall Station - S01E02-E03 - Dead Reckoning (1-2)"),
        ("{n.sortName()}/{n}", "movie with edition", "Quiet Meridian, The/The Quiet Meridian"),
    ],
)
def test_filebot_examples(expr: str, note: str, expected: str) -> None:
    rendered = compile_format(expr).render(sample(note).bindings())
    assert rendered.rsplit(".", 1)[0] == expected


# --------------------------------------------------------------------------- integration: real sanitizers


@pytest.mark.integration
def test_real_paths_sanitizers_render_samples() -> None:
    assert render_sample(DEFAULT_FORMATS[MediaKind.SERIES], "title with") == (
        "Starfall Station (2016)/Season 02/Starfall Station - S02E10 - Either-Or - Part One.mkv"
    )
    assert render_sample("{plex.id}", "movie with edition") == (
        "The Quiet Meridian (2019) {tmdb-700001}/The Quiet Meridian (2019) {tmdb-700001} {edition-Director's Cut}.mkv"
    )


@pytest.mark.integration
def test_real_paths_sanitizers_handle_reserved_names_and_long_names() -> None:
    from format_helpers import render

    assert render("{n}/x", ext=".mkv", n="CON") == "CON_/x.mkv"
    long_name = render("{n}", ext=".mkv", n="x" * 400)
    assert long_name.endswith(".mkv") and len(long_name.encode()) <= 255


def test_slow_regex_is_reported_by_validate_quickly() -> None:
    import time

    expr = "{n}/{'" + "a" * 40 + "bc'.replaceAll(/(a|aa)*c/, '')}"
    started = time.perf_counter()
    issues = validate(expr, MediaKind.MOVIE)
    # One timed-out call per sample render at most: well under a second each.
    assert time.perf_counter() - started < 0.5 * max(1, len(samples_for(MediaKind.MOVIE)))
    assert issues and all(i.severity == "warning" for i in issues)
    assert any("took longer than" in i.message for i in issues)
