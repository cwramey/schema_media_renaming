"""Seeded default formats and media-server presets (SPEC §8.5–§8.6).

Preset expressions are ordinary, editable format expressions. The ``{plex}``, ``{jellyfin}``,
``{emby}`` and ``{kodi}`` bindings render the very same expressions (see
:func:`preset_expression`), so a preset and its binding can never disagree.

Paths are relative to the library root (Schema's per-kind library roots replace FileBot's
top-level ``Movies/`` / ``TV Shows/`` folders).
"""

from __future__ import annotations

from schema_app.api_models import FormatPreset
from schema_app.domain import MediaKind

DEFAULT_FORMATS: dict[MediaKind, str] = {
    MediaKind.MOVIE: "{ny}/{ny}{' - ' + edition}{' - pt' + pi}",
    MediaKind.SERIES: "{n} ({y})/{special ? 'Specials' : 'Season ' + s.pad(2)}/{n} - {s00e00}{' - ' + t}",
    MediaKind.ANIME: "{n}/{special ? 'Specials' : 'Season ' + s.pad(2)}/{n} - {s00e00}{' - ' + t}",
}
DEFAULT_FORMATS[MediaKind.UNKNOWN] = DEFAULT_FORMATS[MediaKind.MOVIE]

# Specials have no absolute number; fall back to S00Exx instead of leaving "Name -  - Title".
ANIME_ABSOLUTE = "{n}/{n} - {any{absolute.pad(3)}{s00e00}}{' - ' + t}"

# standard -> (opening, key suffix, separator, closing) for id tags like " {tmdb-603}"
_TAG_STYLE: dict[str, tuple[str, str, str, str]] = {
    "plex": (" {", "", "-", "}"),
    "jellyfin": (" [", "id", "-", "]"),
    "emby": (" [", "id", "=", "]"),
    "kodi": (" {", "", "=", "}"),
}

_SEASON_FOLDER: dict[str, str] = {
    "plex": "{special ? 'Specials' : 'Season ' + s.pad(2)}",
    "jellyfin": "{'Season ' + s.pad(2)}",
    "emby": "{'Season ' + s.pad(2)}",
    "kodi": "{'Season ' + s}",
}

STANDARDS = tuple(_TAG_STYLE)


def _tag(standard: str, db: str, binding: str) -> str:
    opening, suffix, sep, closing = _TAG_STYLE[standard]
    return f"'{opening}{db}{suffix}{sep}' + {binding} + '{closing}'"


def _movie_tag(standard: str) -> str:
    return "{any{" + _tag(standard, "tmdb", "tmdbid") + "}{" + _tag(standard, "imdb", "imdbid") + "}}"


def _series_tag(standard: str) -> str:
    # By the database the numbering follows ({db}: the provider, or the Anime-Lists target of
    # AniDB episodes): TMDb-numbered series get the TMDb id, otherwise TheTVDB when known.
    return (
        "{any{db != 'tmdb' ? " + _tag(standard, "tvdb", "tvdbid") + " : null}"
        "{" + _tag(standard, "tmdb", "tmdbid") + "}"
        "{" + _tag(standard, "tvdb", "tvdbid") + "}}"
    )


def preset_expression(standard: str, kind: MediaKind, *, ids: bool = False) -> str:
    """Expression behind ``{<standard>}`` / ``{<standard>.id}`` for ``kind``."""
    if standard not in _TAG_STYLE:
        raise KeyError(standard)
    if kind == MediaKind.MOVIE:
        tag = _movie_tag(standard) if ids else ""
        if standard == "plex":
            return f"{{ny}}{tag}/{{ny}}{tag}{{' {{edition-' + edition + '}}'}}{{' - pt' + pi}}"
        # " - <edition>" is Jellyfin/Emby's multi-version label: without it two cuts of one film
        # render to the same name and one replaces the other.
        return f"{{ny}}{tag}/{{ny}}{{' - ' + edition}}{{' - pt' + pi}}"
    tag = _series_tag(standard) if ids else ""
    return f"{{ny}}{tag}/{_SEASON_FOLDER[standard]}/{{ny}} - {{s00e00}}{{' - ' + t}}"


_KIND_LABEL = {MediaKind.MOVIE: "movies", MediaKind.SERIES: "series", MediaKind.ANIME: "anime"}

# preset id prefix -> (display name, standard, ids, equivalent binding, description)
_PRESET_FAMILIES: list[tuple[str, str, str, bool, str, str]] = [
    ("plex", "Plex", "plex", False, "{plex}", "Plex naming standard."),
    ("plex-ids", "Plex + IDs", "plex", True, "{plex.id}", "Plex naming with {tmdb-…}/{tvdb-…} id tags for exact matching."),
    ("jellyfin", "Jellyfin", "jellyfin", True, "{jellyfin.id}", "Jellyfin naming with [tmdbid-…]/[tvdbid-…] tags; specials in Season 00."),
    ("emby", "Emby", "emby", True, "{emby.id}", "Emby naming with [tmdbid=…]/[tvdbid=…] tags; specials in Season 00."),
    ("kodi", "Kodi", "kodi", False, "{kodi}", "Kodi naming with unpadded 'Season 1' folders."),
]

PRESET_BINDINGS: dict[str, str] = {}  # preset id -> equivalent binding expression (used by tests/docs)


def _build_presets() -> list[FormatPreset]:
    presets: list[FormatPreset] = []
    for kind in (MediaKind.MOVIE, MediaKind.SERIES, MediaKind.ANIME):
        presets.append(
            FormatPreset(
                id=f"default-{kind.value}",
                name=f"Schema default ({_KIND_LABEL[kind]})",
                kind=kind,
                expression=DEFAULT_FORMATS[kind],
                description="Schema's default layout for " + _KIND_LABEL[kind] + ".",
            )
        )
    for prefix, name, standard, ids, binding, description in _PRESET_FAMILIES:
        for kind in (MediaKind.MOVIE, MediaKind.SERIES, MediaKind.ANIME):
            preset_id = f"{prefix}-{kind.value}"
            presets.append(
                FormatPreset(
                    id=preset_id,
                    name=f"{name} ({_KIND_LABEL[kind]})",
                    kind=kind,
                    expression=preset_expression(standard, kind, ids=ids),
                    description=f"{description} Same result as {binding}.",
                )
            )
            PRESET_BINDINGS[preset_id] = binding
    presets.append(
        FormatPreset(
            id="anime-absolute",
            name="Anime absolute",
            kind=MediaKind.ANIME,
            expression=ANIME_ABSOLUTE,
            description="Anime with absolute episode numbers: 'Name/Name - 015 - Title'.",
        )
    )
    return presets


PRESETS: list[FormatPreset] = _build_presets()
_BY_ID = {preset.id: preset for preset in PRESETS}


def get_preset(preset_id: str) -> FormatPreset | None:
    return _BY_ID.get(preset_id)
