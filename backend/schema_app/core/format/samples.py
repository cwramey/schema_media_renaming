"""Built-in validation samples (SPEC §8.7).

Fixed, synthetic ``ParsedMedia`` + ``Candidate`` pairs (no parser, no network) built from the
fictional demo catalogue (SPEC §6.5). ``/api/formats/validate`` renders a format against the
samples for the requested kind.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from schema_app.api_models import FormatSample
from schema_app.core.format.bindings import Bindings, build_binding
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


@dataclass(frozen=True)
class Sample:
    source: str
    kind: MediaKind
    parsed: ParsedMedia
    candidate: Candidate | None
    probe: ProbeInfo | None = None
    overrides: Mapping[str, Any] = field(default_factory=dict)
    note: str = ""

    def bindings(self) -> Bindings:
        return build_binding(self.parsed, self.candidate, self.probe, overrides=self.overrides)


_INCOMING = "/storage/downloads/"

_STARFALL = SeriesInfo(
    ref=ProviderRef(provider="tvdb", id="900001"),
    title="Starfall Station",
    year=2016,
    first_aired=date(2016, 9, 7),
    ids={"tvdb": "900001", "tmdb": "800001", "imdb": "tt9000001"},
    network="Orbital Broadcasting",
    status="Ended",
    genres=["Science Fiction", "Drama"],
    season_count=3,
    certification="TV-14",
    rating=8.14,
    original_language="en",
)
_KAZE = SeriesInfo(
    ref=ProviderRef(provider="anidb", id="90001"),
    title="Kaze no Tsubasa",
    original_title="風の翼",
    year=2018,
    first_aired=date(2018, 4, 6),
    aliases=["Wings of the Wind", "風の翼"],
    ids={"anidb": "90001", "tvdb": "900101"},
    genres=["Animation", "Adventure"],
    is_anime=True,
    season_count=3,
    original_language="ja",
)


def _series_candidate(series: SeriesInfo, kind: MediaKind, episodes: list[EpisodeInfo]) -> Candidate:
    return Candidate(
        id=f"{series.ref.provider}:{kind.value}:{series.ref.id}",
        kind=kind,
        provider=series.ref.provider,
        title=series.title,
        year=series.year,
        score=0.97,
        series=series,
        episodes=episodes,
    )


def _movie_candidate(movie: MovieInfo) -> Candidate:
    return Candidate(
        id=f"{movie.ref.provider}:movie:{movie.ref.id}",
        kind=MediaKind.MOVIE,
        provider=movie.ref.provider,
        title=movie.title,
        year=movie.year,
        score=0.96,
        movie=movie,
    )


def _episode(series: SeriesInfo, season: int, number: int, title: str, aired: date | None, absolute: int | None = None) -> EpisodeInfo:
    return EpisodeInfo(series_ref=series.ref, season=season, episode=number, absolute=absolute, title=title, air_date=aired)


def _parsed(filename: str, kind: MediaKind, **fields: Any) -> ParsedMedia:
    extension = filename.rsplit(".", 1)[1].lower()
    return ParsedMedia(path=_INCOMING + filename, filename=filename, extension=extension, kind=kind, confidence=0.9, **fields)


def _build() -> list[Sample]:
    samples: list[Sample] = []

    name = "Starfall.Station.S01E02E03.720p.HDTV.x264-KILLERS.mkv"
    samples.append(Sample(
        name, MediaKind.SERIES,
        _parsed(name, MediaKind.SERIES, title="Starfall Station", season=1, episodes=[2, 3], release_group="KILLERS",
                quality=QualityInfo(resolution="720p", source="HDTV", video_codec="x264")),
        _series_candidate(_STARFALL, MediaKind.SERIES, [
            _episode(_STARFALL, 1, 2, "Dead Reckoning (1)", date(2016, 9, 14), 2),
            _episode(_STARFALL, 1, 3, "Dead Reckoning (2)", date(2016, 9, 21), 3),
        ]),
        note="multi-episode",
    ))

    name = "Starfall Station - S00E02 - Christmas Special.mkv"
    samples.append(Sample(
        name, MediaKind.SERIES,
        _parsed(name, MediaKind.SERIES, title="Starfall Station", season=0, episodes=[2], special=True),
        _series_candidate(_STARFALL, MediaKind.SERIES, [_episode(_STARFALL, 0, 2, "Christmas Special", date(2016, 12, 24))]),
        note="special",
    ))

    name = "Starfall.Station.S02E10.PROPER.1080p.BluRay.x265.10bit-GROUP.mkv"
    samples.append(Sample(
        name, MediaKind.SERIES,
        _parsed(name, MediaKind.SERIES, title="Starfall Station", season=2, episodes=[10], release_group="GROUP",
                quality=QualityInfo(resolution="1080p", source="BluRay", video_codec="x265", bit_depth=10, proper=True)),
        _series_candidate(_STARFALL, MediaKind.SERIES, [_episode(_STARFALL, 2, 10, "Either/Or: Part One", date(2017, 11, 29), 20)]),
        ProbeInfo(container="matroska", duration_seconds=2712.0, width=1920, height=1080, video_codec="HEVC", bit_depth=10,
                  frame_rate=23.976, audio_codec="DDP", audio_channels="5.1", bitrate_kbps=6500),
        note="title with ':' and '/', media probe",
    ))

    name = "[SubsPlease] Kaze no Tsubasa - 15 (1080p) [A1B2C3D4].mkv"
    samples.append(Sample(
        name, MediaKind.ANIME,
        _parsed(name, MediaKind.ANIME, title="Kaze no Tsubasa", absolute=[15], release_group="SubsPlease", crc32="A1B2C3D4",
                quality=QualityInfo(resolution="1080p")),
        _series_candidate(_KAZE, MediaKind.ANIME, [_episode(_KAZE, 2, 3, "Crosswind", date(2019, 7, 19), 15)]),
        note="anime absolute 15 → S02E03",
    ))

    name = "Kaze no Tsubasa - OVA [BD 1080p].mkv"
    samples.append(Sample(
        name, MediaKind.ANIME,
        _parsed(name, MediaKind.ANIME, title="Kaze no Tsubasa", special=True, special_title="OVA",
                quality=QualityInfo(resolution="1080p", source="BluRay")),
        _series_candidate(_KAZE, MediaKind.ANIME, [_episode(_KAZE, 0, 1, "Wings Over the Sea", date(2019, 12, 20))]),
        note="anime special (OVA)",
    ))

    name = "The.Quiet.Meridian.2019.Directors.Cut.1080p.BluRay.x264-SPARKS.mkv"
    meridian = MovieInfo(
        ref=ProviderRef(provider="tmdb", id="700001"), title="The Quiet Meridian", year=2019,
        release_date=date(2019, 10, 4), ids={"tmdb": "700001", "imdb": "tt7000001"},
        genres=["Thriller", "Drama"], collection="The Meridian Collection", runtime_minutes=124,
        certification="PG-13", rating=7.46, original_language="en",
    )
    samples.append(Sample(
        name, MediaKind.MOVIE,
        _parsed(name, MediaKind.MOVIE, title="The Quiet Meridian", year=2019, edition="Director's Cut", release_group="SPARKS",
                quality=QualityInfo(resolution="1080p", source="BluRay", video_codec="x264", audio_codec="DTS", audio_channels="5.1")),
        _movie_candidate(meridian),
        note="movie with edition",
    ))

    name = "Paper.Moons.2004.CD1.DVDRip.XviD-FiCO.avi"
    paper = MovieInfo(
        ref=ProviderRef(provider="tmdb", id="700005"), title="Paper Moons", year=2004, release_date=date(2004, 3, 12),
        ids={"tmdb": "700005", "imdb": "tt7000005"}, genres=["Comedy"], runtime_minutes=98,
    )
    samples.append(Sample(
        name, MediaKind.MOVIE,
        _parsed(name, MediaKind.MOVIE, title="Paper Moons", year=2004, part=1, release_group="FiCO",
                quality=QualityInfo(resolution="480p", source="DVD", video_codec="XviD")),
        _movie_candidate(paper),
        overrides={"pc": 2},
        note="multi-part movie",
    ))

    name = "Static Bloom.mkv"
    bloom = MovieInfo(ref=ProviderRef(provider="tmdb", id="700099"), title="Static Bloom", ids={"tmdb": "700099"})
    samples.append(Sample(
        name, MediaKind.MOVIE,
        _parsed(name, MediaKind.MOVIE, title="Static Bloom"),
        _movie_candidate(bloom),
        note="movie without a year",
    ))
    return samples


SAMPLES: list[Sample] = _build()


def samples_for(kind: MediaKind) -> list[Sample]:
    """Samples for one kind; UNKNOWN returns every sample."""
    if kind == MediaKind.UNKNOWN:
        return list(SAMPLES)
    return [sample for sample in SAMPLES if sample.kind == kind]


def render_samples(template_or_expr: Any, kind: MediaKind) -> list[FormatSample]:
    """Render a format for every sample of ``kind``. Swallowed block errors are not sample
    errors (validate() reports them as warnings); an empty or invalid final path is."""
    from schema_app.core.format.engine import Template, compile_format
    from schema_app.core.format.errors import FormatSyntaxError

    samples = samples_for(kind)
    if isinstance(template_or_expr, Template):
        template = template_or_expr
    else:
        try:
            template = compile_format(str(template_or_expr))
        except FormatSyntaxError as exc:
            return [FormatSample(source=sample.source, error=exc.message) for sample in samples]
    results: list[FormatSample] = []
    for sample in samples:
        result = template.render_detailed(sample.bindings())
        if result.path is None:
            message = next((i.message for i in result.issues if i.severity == "error"), "Format produced an empty path")
            results.append(FormatSample(source=sample.source, error=message))
        else:
            results.append(FormatSample(source=sample.source, output=result.path))
    return results
