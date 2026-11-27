"""Binding catalog and the lazy binding mapping (SPEC §8.5, FileBot names).

``build_binding(parsed, candidate, probe)`` returns a :class:`Bindings` mapping whose values are
computed on first access. Undefined values raise :class:`BindingMissing` so the enclosing
``{…}`` block renders empty, exactly like FileBot's ``BindingException``.

String values are cleaned at bind time with ``services.paths.sanitize_component(value,
colon=":")``: path separators and Windows-illegal characters are neutralized (a "/" in a title
becomes "-", so it never creates a folder) while colons are kept, so ``{n.colon('∶')}`` still
works. The final per-component sanitization happens in ``Template.render``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from functools import cached_property, partial
from pathlib import PurePath
from typing import Any

from schema_app.api_models import BindingSpec
from schema_app.core.format.errors import BindingMissing
from schema_app.core.format.methods import sort_initial
from schema_app.core.format.values import BindingObject
from schema_app.domain import (
    Candidate,
    EpisodeInfo,
    MediaKind,
    MovieInfo,
    ParsedMedia,
    ProbeInfo,
    ProviderRef,
    SeriesInfo,
)
from schema_app.services import paths as _paths

ALL_KINDS = [MediaKind.MOVIE, MediaKind.SERIES, MediaKind.ANIME]
EPISODIC = [MediaKind.SERIES, MediaKind.ANIME]
MOVIES = [MediaKind.MOVIE]

# --------------------------------------------------------------------------- cleaning


def clean_text(value: str) -> str:
    """Neutralize separators/illegal characters in a binding value but keep colons."""
    return _paths.sanitize_component(value, colon=":")


def clean_value(value: Any) -> Any:
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, list | tuple):
        return [clean_value(item) for item in value]
    return value


# --------------------------------------------------------------------------- mapping


class Bindings(Mapping[str, Any]):
    """Lazy, memoized binding mapping. ``bindings[name]`` raises BindingMissing for unknown names
    and for values that are undefined (None)."""

    def __init__(
        self,
        resolvers: Mapping[str, Callable[[], Any]],
        *,
        kind: MediaKind = MediaKind.UNKNOWN,
        extension_suffix: str = "",
        overrides: Mapping[str, Any] | None = None,
    ) -> None:
        self._resolvers = dict(resolvers)
        self._overrides = dict(overrides or {})
        self._cache: dict[str, Any] = {}
        self.kind = kind
        self.extension_suffix = extension_suffix

    @classmethod
    def from_dict(
        cls,
        values: Mapping[str, Any],
        *,
        kind: MediaKind = MediaKind.UNKNOWN,
        extension_suffix: str = "",
    ) -> Bindings:
        """Bindings from plain values (tests, ad-hoc previews). String values are cleaned like
        computed bindings, so a "/" in a value still never creates a folder."""
        return cls({}, kind=kind, extension_suffix=extension_suffix,
                   overrides={name: clean_value(value) for name, value in values.items()})

    def __getitem__(self, name: str) -> Any:
        if name in self._cache:
            value = self._cache[name]
        else:
            if name in self._overrides:
                value = self._overrides[name]
            elif name in self._resolvers:
                value = self._resolvers[name]()
            else:
                raise BindingMissing(name)
            self._cache[name] = value
        if value is None:
            raise BindingMissing(name)
        return value

    def __iter__(self) -> Iterator[str]:
        seen: set[str] = set()
        for name in [*self._overrides, *self._resolvers]:
            if name in seen:
                continue
            seen.add(name)
            if self.get(name) is not None:
                yield name

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def __contains__(self, name: object) -> bool:
        if not isinstance(name, str):
            return False
        try:
            self[name]
        except KeyError:
            return False
        return True

    def __repr__(self) -> str:
        return f"<Bindings kind={self.kind.value} suffix={self.extension_suffix!r}>"


# --------------------------------------------------------------------------- helpers

_SPLIT_RE = re.compile(r"[\\/]")
_EXT_SEGMENT_RE = re.compile(r"\.[A-Za-z0-9][A-Za-z0-9_-]{0,9}$")
_SUBTITLE_FLAGS = frozenset({"forced", "sdh", "cc", "hi", "default", "full", "signs", "songs", "commentary"})


def _basename(path: str) -> str:
    parts = [p for p in _SPLIT_RE.split(path) if p]
    return parts[-1] if parts else path


def _parent(path: str) -> str:
    stripped = path.rstrip("\\/")
    idx = max(stripped.rfind("/"), stripped.rfind("\\"))
    if idx <= 0:
        return stripped[: idx + 1] if idx == 0 else ""
    return stripped[:idx]


def _stem(filename: str, extension: str) -> str:
    if extension and filename.lower().endswith("." + extension.lower()):
        return filename[: -(len(extension) + 1)]
    return filename.rsplit(".", 1)[0] if "." in filename else filename


def _sidecar_suffix(sidecar_name: str, video_stem: str) -> str:
    if video_stem and sidecar_name.startswith(video_stem) and len(sidecar_name) > len(video_stem):
        rest = sidecar_name[len(video_stem) :]
        if rest.startswith("."):
            return rest
    stem, suffix = sidecar_name, ""
    for _ in range(4):
        match = _EXT_SEGMENT_RE.search(stem)
        if not match or match.start() == 0:
            break
        suffix = match.group(0) + suffix
        stem = stem[: match.start()]
    return suffix


def _num(value: Any) -> Any:
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return value


_PART_TITLE_RE = re.compile(
    r"^(?P<base>.*?)[\s,:-]*(?:\((?P<n1>\d+)\)|Part\s+(?P<n2>\d+)|Pt\.?\s*(?P<n3>\d+))\s*$", re.IGNORECASE
)


def join_titles(titles: list[str]) -> str | None:
    """Multi-episode titles: identical titles once, "Title (1)"/"Title (2)" → "Title (1-2)",
    otherwise joined with " & "."""
    unique: list[str] = []
    for title in titles:
        if title and title not in unique:
            unique.append(title)
    if not unique:
        return None
    if len(unique) == 1:
        return unique[0]
    matches = [_PART_TITLE_RE.match(title) for title in unique]
    if all(matches):
        bases = {m.group("base").strip().casefold() for m in matches if m}
        if len(bases) == 1 and next(iter(bases)):
            numbers = [int(m.group("n1") or m.group("n2") or m.group("n3")) for m in matches if m]
            base = matches[0].group("base").strip() if matches[0] else ""
            return f"{base} ({numbers[0]}-{numbers[-1]})"
    return " & ".join(unique)


def _vf_from_dims(width: int | None, height: int | None) -> str | None:
    w, h = width or 0, height or 0
    if not w and not h:
        return None
    if w >= 7600 or h >= 4200:
        return "4320p"
    if w >= 3600 or h >= 2000:
        return "2160p"
    if w >= 2400 or h >= 1400:
        return "1440p"
    if w >= 1800 or h >= 1000:
        return "1080p"
    if w >= 1200 or h >= 700:
        return "720p"
    if h >= 560:
        return "576p"
    if h >= 470 or w >= 700:
        return "480p"
    return f"{h}p" if h else None


def _hd_class(width: int | None, height: int | None, vf: str | None) -> str | None:
    w, h = width or 0, height or 0
    if w or h:
        if w >= 3600 or h >= 2000:
            return "UHD"
        if w >= 2400 or h >= 1400:
            return "QHD"
        if w >= 1800 or h >= 1000:
            return "FHD"
        if w >= 1200 or h >= 700:
            return "HD"
        return "SD"
    return {"4320p": "UHD", "2160p": "UHD", "1440p": "QHD", "1080p": "FHD", "720p": "HD",
            "576p": "SD", "480p": "SD"}.get(vf or "")  # fmt: skip


def _channel_count(channels: str | None) -> int | None:
    if not channels:
        return None
    match = re.fullmatch(r"\s*(\d+)(?:\.(\d+))?\s*(?:ch)?\s*", channels)
    if not match:
        return None
    return int(match.group(1)) + int(match.group(2) or 0)


_HDR_ORDER = ["HDR10+", "HDR10", "HDR", "HLG"]


def _hdr_label(values: list[str]) -> tuple[str | None, bool]:
    normalized = [v.strip() for v in values if v and v.strip()]
    dovi = any(v.upper() in {"DV", "DOVI", "DOLBY VISION"} for v in normalized)
    others = [v for v in normalized if v.upper() not in {"DV", "DOVI", "DOLBY VISION"}]
    best = next((h for h in _HDR_ORDER if h in others), others[0] if others else None)
    if dovi and best:
        return f"DV+{best}", True
    if dovi:
        return "DV", True
    return best, False


# --------------------------------------------------------------------------- binding objects


class FileValue(BindingObject):
    type_label = "File"
    properties = frozenset({"name", "path", "parent", "dir", "folder", "ext", "extension", "nameWithoutExtension"})

    def __init__(self, path: str) -> None:
        self.path = path

    def _property(self, name: str) -> Any:
        base = _basename(self.path)
        if name == "name":
            return base
        if name == "path":
            return self.path
        if name == "parent":
            return _parent(self.path) or None
        if name in ("dir", "folder"):
            parent = _parent(self.path)
            return FileValue(parent) if parent else None
        if name in ("ext", "extension"):
            return base.rsplit(".", 1)[1].lower() if "." in base else None
        if name == "nameWithoutExtension":
            return base.rsplit(".", 1)[0] if "." in base else base
        return None

    def __str__(self) -> str:
        return self.path


class EpisodeValue(BindingObject):
    type_label = "Episode"
    properties = frozenset({"season", "episode", "absolute", "title", "special", "airdate", "seriesName"})

    def __init__(self, info: EpisodeInfo, series_name: str | None) -> None:
        self.info = info
        self.series_name = series_name

    def _property(self, name: str) -> Any:
        info = self.info
        if name == "season":
            return info.season
        if name == "episode":
            return info.episode
        if name == "absolute":
            return info.absolute
        if name == "title":
            return clean_text(info.title) if info.title else None
        if name == "special":
            return info.season == 0
        if name == "airdate":
            return info.air_date
        if name == "seriesName":
            return clean_text(self.series_name) if self.series_name else None
        return None

    def __str__(self) -> str:
        info = self.info
        parts: list[str] = []
        if self.series_name:
            parts.append(clean_text(self.series_name))
        if info.season is not None and info.episode is not None:
            parts.append(f"{info.season}x{info.episode:02d}")
        elif info.absolute is not None:
            parts.append(f"{info.absolute:02d}")
        if info.title:
            parts.append(clean_text(info.title))
        return " - ".join(parts)


class InfoValue(BindingObject):
    type_label = "Info"
    properties = frozenset({
        "network", "status", "overview", "genres", "certification", "rating", "runtime", "language",
        "year", "title", "name", "originalTitle", "id", "collection", "aliases",
    })  # fmt: skip

    def __init__(self, obj: MovieInfo | SeriesInfo) -> None:
        self.obj = obj

    def _property(self, name: str) -> Any:
        obj = self.obj
        movie = obj if isinstance(obj, MovieInfo) else None
        series = obj if isinstance(obj, SeriesInfo) else None
        value: Any = None
        if name == "network":
            value = series.network if series else None
        elif name == "status":
            value = series.status if series else None
        elif name == "overview":
            value = obj.overview
        elif name == "genres":
            value = list(obj.genres) or None
        elif name == "certification":
            value = obj.certification
        elif name == "rating":
            value = round(obj.rating, 1) if obj.rating is not None else None
        elif name == "runtime":
            value = movie.runtime_minutes if movie else None
        elif name == "language":
            value = obj.original_language
        elif name == "year":
            value = obj.year
        elif name in ("title", "name"):
            value = obj.title
        elif name == "originalTitle":
            value = obj.original_title
        elif name == "id":
            value = _num(obj.ref.id)
        elif name == "collection":
            value = movie.collection if movie else None
        elif name == "aliases":
            value = list(obj.aliases) or None
        return clean_value(value)

    def __str__(self) -> str:
        return clean_text(self.obj.title)


class PresetValue(BindingObject):
    """``{plex}``, ``{jellyfin}``, ``{emby}``, ``{kodi}``: standard relative path (no extension,
    no top-level library folder). ``.id`` adds the server's id tags, ``.name`` is the file name."""

    type_label = "Preset"
    properties = frozenset({"id", "name", "path"})

    def __init__(self, standard: str, bindings: Bindings, kind: MediaKind, *, ids: bool = False) -> None:
        self.standard = standard
        self.bindings = bindings
        self.kind = kind
        self.ids = ids
        self._path: str | None = None

    def _render(self) -> str:
        if self._path is None:
            from schema_app.core.format.engine import compile_format
            from schema_app.core.format.presets import preset_expression

            expression = preset_expression(self.standard, self.kind, ids=self.ids)
            raw = compile_format(expression).render_raw(self.bindings)
            self._path = "/".join(part.strip() for part in raw.split("/") if part.strip())
        return self._path

    def _property(self, name: str) -> Any:
        if name == "id":
            return PresetValue(self.standard, self.bindings, self.kind, ids=True)
        if name == "name":
            path = self._render()
            return path.rsplit("/", 1)[-1] if path else None
        if name == "path":
            return self._render() or None
        return None

    def __str__(self) -> str:
        return self._render()


# --------------------------------------------------------------------------- context


class _Ctx:
    """Facts shared by binding resolvers for one (file, match) pair."""

    def __init__(
        self,
        parsed: ParsedMedia,
        candidate: Candidate | None,
        probe: ProbeInfo | None,
        sidecar: str | PurePath | None,
    ) -> None:
        self.parsed = parsed
        self.candidate = candidate
        self.probe = probe
        self.sidecar = os.fspath(sidecar) if sidecar is not None else None
        self.bindings: Bindings | None = None

    @cached_property
    def kind(self) -> MediaKind:
        return self.candidate.kind if self.candidate else self.parsed.kind

    @cached_property
    def movie(self) -> MovieInfo | None:
        return self.candidate.movie if self.candidate else None

    @cached_property
    def series(self) -> SeriesInfo | None:
        return self.candidate.series if self.candidate else None

    @cached_property
    def meta(self) -> MovieInfo | SeriesInfo | None:
        return self.movie or self.series

    @cached_property
    def episodes(self) -> list[EpisodeInfo]:
        if self.candidate is not None:
            return list(self.candidate.episodes)
        if self.kind == MediaKind.MOVIE:
            return []
        parsed = self.parsed
        ref = ProviderRef(provider="parsed", id="")
        season = parsed.season if parsed.season is not None else (0 if parsed.special else None)
        if parsed.episodes:
            return [
                EpisodeInfo(
                    series_ref=ref,
                    season=season,
                    episode=number,
                    absolute=parsed.absolute[i] if i < len(parsed.absolute) else None,
                    air_date=parsed.air_date,
                )
                for i, number in enumerate(parsed.episodes)
            ]
        if parsed.absolute:
            return [EpisodeInfo(series_ref=ref, season=season, absolute=a) for a in parsed.absolute]
        if parsed.air_date is not None:
            return [EpisodeInfo(series_ref=ref, season=season, air_date=parsed.air_date)]
        if parsed.special:
            return [EpisodeInfo(series_ref=ref, season=0, title=parsed.special_title)]
        return []

    @cached_property
    def episodic(self) -> bool:
        if self.kind in (MediaKind.SERIES, MediaKind.ANIME):
            return True
        return self.kind == MediaKind.UNKNOWN and bool(self.episodes)

    @cached_property
    def first(self) -> EpisodeInfo | None:
        return self.episodes[0] if self.episodes else None

    @cached_property
    def preset_kind(self) -> MediaKind:
        if self.kind in (MediaKind.MOVIE, MediaKind.SERIES, MediaKind.ANIME):
            return self.kind
        return MediaKind.SERIES if self.episodic else MediaKind.MOVIE

    @cached_property
    def name(self) -> str | None:
        if self.meta is not None:
            return self.meta.title
        if self.candidate is not None:
            return self.candidate.title
        return self.parsed.title

    @cached_property
    def year(self) -> int | None:
        if self.series is not None:
            if self.series.year:
                return self.series.year
            if self.series.first_aired:
                return self.series.first_aired.year
        if self.movie is not None and self.movie.year:
            return self.movie.year
        if self.candidate is not None and self.candidate.year:
            return self.candidate.year
        return self.parsed.year

    @cached_property
    def ids(self) -> dict[str, str]:
        merged: dict[str, str] = {}
        if self.meta is not None:
            merged.update({k: str(v) for k, v in self.meta.ids.items() if v})
            merged.setdefault(self.meta.ref.provider, self.meta.ref.id)
        elif self.candidate is not None:
            parts = self.candidate.id.split(":")
            if len(parts) >= 3:
                merged.setdefault(parts[0], parts[-1])
        return merged

    @cached_property
    def numbered(self) -> list[EpisodeInfo]:
        return [ep for ep in self.episodes if ep.episode is not None]

    @cached_property
    def extension(self) -> str:
        if self.sidecar:
            base = _basename(self.sidecar)
            return base.rsplit(".", 1)[1].lower() if "." in base else ""
        return self.parsed.extension

    @cached_property
    def video_stem(self) -> str:
        return _stem(self.parsed.filename, self.parsed.extension)

    @cached_property
    def extension_suffix(self) -> str:
        if self.sidecar:
            return _sidecar_suffix(_basename(self.sidecar), self.video_stem)
        return f".{self.parsed.extension}" if self.parsed.extension else ""

    @cached_property
    def sidecar_tags(self) -> list[str]:
        if not self.sidecar:
            return []
        suffix = self.extension_suffix
        segments = [s for s in suffix.split(".") if s]
        return segments[:-1]  # drop the real extension

    @cached_property
    def channels(self) -> str | None:
        if self.probe and self.probe.audio_channels:
            return self.probe.audio_channels
        return self.parsed.quality.audio_channels

    @cached_property
    def hdr(self) -> tuple[str | None, bool]:
        values = (self.probe.hdr if self.probe and self.probe.hdr else None) or self.parsed.quality.hdr
        return _hdr_label(list(values))

    @cached_property
    def vf(self) -> str | None:
        if self.probe and (self.probe.width or self.probe.height):
            label = _vf_from_dims(self.probe.width, self.probe.height)
            if label:
                return label
        return self.parsed.quality.resolution

    @cached_property
    def size(self) -> int | None:
        try:
            return os.path.getsize(self.parsed.path)
        except (OSError, ValueError):
            return None

    @cached_property
    def duration(self) -> float | None:
        return self.probe.duration_seconds if self.probe and self.probe.duration_seconds else None


# --------------------------------------------------------------------------- registry


@dataclass(frozen=True, slots=True)
class BindingDef:
    spec: BindingSpec
    fn: Callable[[_Ctx], Any]
    clean: bool = True


BINDINGS: dict[str, BindingDef] = {}
EXTRA_SPECS: list[BindingSpec] = []  # dotted catalog entries (plex.id …), not standalone bindings


def _binding(
    name: str, group: str, kinds: list[MediaKind], example: str, type_: str, description: str, *, clean: bool = True
) -> Callable[[Callable[[_Ctx], Any]], Callable[[_Ctx], Any]]:
    def register(fn: Callable[[_Ctx], Any]) -> Callable[[_Ctx], Any]:
        spec = BindingSpec(name=name, description=description, kinds=list(kinds), group=group, example=example, type=type_)
        BINDINGS[name] = BindingDef(spec, fn, clean)
        return fn

    return register


# ---- Naming


@_binding("n", "Naming", ALL_KINDS, "Starfall Station", "str", "Series or movie name")
def _n(ctx: _Ctx) -> Any:
    return ctx.name


@_binding("y", "Naming", ALL_KINDS, "2016", "int", "Movie year; for episodes the series' start year")
def _y(ctx: _Ctx) -> Any:
    return ctx.year


@_binding("ny", "Naming", ALL_KINDS, "Starfall Station (2016)", "str", "Name (Year); just the name when the year is unknown")
def _ny(ctx: _Ctx) -> Any:
    name, year = ctx.name, ctx.year
    if not name:
        return None
    if year is None or re.search(rf"\(\s*{year}\s*\)\s*$", name):
        return name
    return f"{name} ({year})"


@_binding("primaryTitle", "Naming", ALL_KINDS, "Kaze no Tsubasa", "str", "Original-language title (trailing brackets removed)")
def _primary_title(ctx: _Ctx) -> Any:
    if ctx.meta is None:
        return None
    title = ctx.meta.original_title or ctx.meta.title
    return re.sub(r"\s*(?:\([^()]*\)|\[[^\[\]]*\])\s*$", "", title) or title


@_binding("alias", "Naming", ALL_KINDS, "[Wings of the Wind, 風の翼]", "list", "Alternative titles")
def _alias(ctx: _Ctx) -> Any:
    return list(ctx.meta.aliases) if ctx.meta and ctx.meta.aliases else None


@_binding("az", "Naming", ALL_KINDS, "S", "str", "Sort initial of the name (The/A/An skipped; digits → 0-9; other → #)")
def _az(ctx: _Ctx) -> Any:
    return sort_initial(ctx.name) if ctx.name else None


@_binding("decade", "Naming", ALL_KINDS, "2010", "int", "Decade of the year")
def _decade(ctx: _Ctx) -> Any:
    return ctx.year // 10 * 10 if ctx.year else None


@_binding("type", "Naming", ALL_KINDS, "Episode", "str", "Episode, MultiEpisode, Movie or MoviePart (FileBot names)")
def _type(ctx: _Ctx) -> Any:
    if ctx.episodic:
        return "MultiEpisode" if len(ctx.episodes) > 1 else "Episode"
    return "MoviePart" if ctx.parsed.part else "Movie"


@_binding("anime", "Naming", ALL_KINDS, "false", "bool", "True for anime")
def _anime(ctx: _Ctx) -> Any:
    return bool((ctx.series and ctx.series.is_anime) or ctx.kind == MediaKind.ANIME)


# ---- Episode


def _require_episodic(ctx: _Ctx) -> bool:
    return ctx.episodic and bool(ctx.episodes)


@_binding("s", "Episode", EPISODIC, "1", "int", "Season number (0 for specials)")
def _s(ctx: _Ctx) -> Any:
    return ctx.first.season if _require_episodic(ctx) and ctx.first else None


@_binding("e", "Episode", EPISODIC, "2", "int", "Episode number (first one of a multi-episode)")
def _e(ctx: _Ctx) -> Any:
    return ctx.numbered[0].episode if _require_episodic(ctx) and ctx.numbered else None


@_binding("es", "Episode", EPISODIC, "[2, 3]", "list", "All episode numbers")
def _es(ctx: _Ctx) -> Any:
    if not _require_episodic(ctx):
        return None
    return [ep.episode for ep in ctx.numbered] or None


def _range(ctx: _Ctx) -> tuple[EpisodeInfo, EpisodeInfo] | None:
    if not _require_episodic(ctx) or not ctx.numbered:
        return None
    return ctx.numbered[0], ctx.numbered[-1]


@_binding("s00", "Episode", EPISODIC, "01", "str", "Zero-padded season")
def _s00(ctx: _Ctx) -> Any:
    pair = _range(ctx)
    if not pair or pair[0].season is None:
        return None
    first, last = pair
    if last.season is not None and last.season != first.season:
        return f"{first.season:02d}-{last.season:02d}"
    return f"{first.season:02d}"


@_binding("e00", "Episode", EPISODIC, "02", "str", "Zero-padded episode (multi-episode: 02-03)")
def _e00(ctx: _Ctx) -> Any:
    pair = _range(ctx)
    if not pair:
        return None
    first, last = pair
    if first is last or (first.season == last.season and first.episode == last.episode):
        return f"{first.episode:02d}"
    return f"{first.episode:02d}-{last.episode:02d}"


@_binding("sxe", "Episode", EPISODIC, "1x02", "str", "1x02 style (multi-episode: 1x02-03)")
def _sxe(ctx: _Ctx) -> Any:
    pair = _range(ctx)
    if not pair or pair[0].season is None:
        return None
    first, last = pair
    head = f"{first.season}x{first.episode:02d}"
    if first is last or (first.season == last.season and first.episode == last.episode):
        return head
    if last.season == first.season or last.season is None:
        return f"{head}-{last.episode:02d}"
    return f"{head}-{last.season}x{last.episode:02d}"


@_binding("s00e00", "Episode", EPISODIC, "S01E02", "str", "S01E02 style (multi-episode: S01E02-E03)")
def _s00e00(ctx: _Ctx) -> Any:
    pair = _range(ctx)
    if not pair or pair[0].season is None:
        return None
    first, last = pair
    head = f"S{first.season:02d}E{first.episode:02d}"
    if first is last or (first.season == last.season and first.episode == last.episode):
        return head
    if last.season == first.season or last.season is None:
        return f"{head}-E{last.episode:02d}"
    return f"{head}-S{last.season:02d}E{last.episode:02d}"


@_binding("absolute", "Episode", EPISODIC, "15", "int", "Absolute episode number (anime)")
def _absolute(ctx: _Ctx) -> Any:
    if not _require_episodic(ctx):
        return None
    return next((ep.absolute for ep in ctx.episodes if ep.absolute is not None), None)


@_binding("t", "Episode", EPISODIC, "Dead Reckoning (1-2)", "str", "Episode title (multi-episode titles joined with ' & ')")
def _t(ctx: _Ctx) -> Any:
    if not _require_episodic(ctx):
        return None
    return join_titles([ep.title for ep in ctx.episodes if ep.title])


@_binding("airdate", "Episode", EPISODIC, "2016-09-14", "date", "Episode air date")
def _airdate(ctx: _Ctx) -> Any:
    if not _require_episodic(ctx):
        return None
    return ctx.first.air_date if ctx.first else None


@_binding("startdate", "Episode", EPISODIC, "2016-09-07", "date", "Series first-aired date")
def _startdate(ctx: _Ctx) -> Any:
    return ctx.series.first_aired if ctx.series else None


@_binding("special", "Episode", EPISODIC, "2", "int", "Special number for specials (season 0), false for regular episodes")
def _special(ctx: _Ctx) -> Any:
    if not _require_episodic(ctx) or ctx.first is None:
        return None
    if ctx.first.season == 0:
        return ctx.first.episode or True
    return False


@_binding("regular", "Episode", EPISODIC, "true", "bool", "True for regular (non-special) episodes")
def _regular(ctx: _Ctx) -> Any:
    if not _require_episodic(ctx) or ctx.first is None:
        return None
    return ctx.first.season != 0


@_binding("sn", "Episode", EPISODIC, "Season 1", "str", "Season name (when the provider names seasons)")
def _sn(ctx: _Ctx) -> Any:
    return None


@_binding("sc", "Episode", EPISODIC, "3", "int", "Number of seasons of the series")
def _sc(ctx: _Ctx) -> Any:
    return ctx.series.season_count if ctx.series else None


@_binding("episode", "Episode", EPISODIC, "Starfall Station - 1x02 - Dead Reckoning (1)", "object",
          "Episode object: .season .episode .absolute .title .special .airdate .seriesName", clean=False)
def _episode(ctx: _Ctx) -> Any:
    if not _require_episodic(ctx) or ctx.first is None:
        return None
    return EpisodeValue(ctx.first, ctx.name)


@_binding("episodes", "Episode", EPISODIC, "[…]", "list", "All episodes of a multi-episode file", clean=False)
def _episodes(ctx: _Ctx) -> Any:
    if not _require_episodic(ctx):
        return None
    return [EpisodeValue(ep, ctx.name) for ep in ctx.episodes]


# ---- Movie


@_binding("collection", "Movie", MOVIES, "The Meridian Collection", "str", "Movie collection")
def _collection(ctx: _Ctx) -> Any:
    return ctx.movie.collection if ctx.movie else None


@_binding("pi", "Movie", MOVIES, "1", "int", "Part index of a multi-part movie (CD1 → 1)")
def _pi(ctx: _Ctx) -> Any:
    return ctx.parsed.part


@_binding("pc", "Movie", MOVIES, "2", "int", "Part count of a multi-part movie (from the batch)")
def _pc(ctx: _Ctx) -> Any:
    return None


# ---- Info


@_binding("d", "Info", ALL_KINDS, "2016-09-14", "date", "Episode air date / movie release date")
def _d(ctx: _Ctx) -> Any:
    if ctx.episodic and ctx.first is not None and ctx.first.air_date:
        return ctx.first.air_date
    if ctx.movie is not None and ctx.movie.release_date:
        return ctx.movie.release_date
    return ctx.parsed.air_date


@_binding("genre", "Info", ALL_KINDS, "Science Fiction", "str", "First genre")
def _genre(ctx: _Ctx) -> Any:
    return ctx.meta.genres[0] if ctx.meta and ctx.meta.genres else None


@_binding("genres", "Info", ALL_KINDS, "[Science Fiction, Drama]", "list", "All genres")
def _genres(ctx: _Ctx) -> Any:
    return list(ctx.meta.genres) if ctx.meta and ctx.meta.genres else None


@_binding("language", "Info", ALL_KINDS, "en", "str", "Original language")
def _language(ctx: _Ctx) -> Any:
    return ctx.meta.original_language if ctx.meta else None


@_binding("country", "Info", ALL_KINDS, "US", "str", "Country of origin (when the provider supplies it)")
def _country(ctx: _Ctx) -> Any:
    return None


@_binding("runtime", "Info", ALL_KINDS, "112", "int", "Runtime in minutes")
def _runtime(ctx: _Ctx) -> Any:
    return ctx.movie.runtime_minutes if ctx.movie else None


@_binding("certification", "Info", ALL_KINDS, "TV-14", "str", "Content rating")
def _certification(ctx: _Ctx) -> Any:
    return ctx.meta.certification if ctx.meta else None


@_binding("rating", "Info", ALL_KINDS, "8.1", "float", "Rating (one decimal)")
def _rating(ctx: _Ctx) -> Any:
    return round(ctx.meta.rating, 1) if ctx.meta and ctx.meta.rating is not None else None


@_binding("info", "Info", ALL_KINDS, "…", "object",
          "Metadata object: .network .status .overview .genres .certification .rating .runtime .language .year .title .id",
          clean=False)
def _info(ctx: _Ctx) -> Any:
    return InfoValue(ctx.meta) if ctx.meta else None


# ---- IDs


@_binding("id", "IDs", ALL_KINDS, "900001", "int", "Database id (movies: TMDb; series: the matching provider's id)")
def _id(ctx: _Ctx) -> Any:
    if ctx.movie is not None:
        return _num(ctx.ids.get("tmdb") or ctx.movie.ref.id)
    if ctx.series is not None:
        ref = ctx.series.ref
        if ref.provider in ("tvdb", "tmdb", "anidb", "tvmaze", "trakt"):
            return _num(ref.id)
        return _num(ctx.ids.get("tvdb") or ctx.ids.get("tmdb") or ref.id)
    return None


@_binding("tmdbid", "IDs", ALL_KINDS, "800001", "int", "TMDb id")
def _tmdbid(ctx: _Ctx) -> Any:
    return _num(ctx.ids.get("tmdb"))


@_binding("tvdbid", "IDs", EPISODIC, "900001", "int", "TheTVDB id")
def _tvdbid(ctx: _Ctx) -> Any:
    return _num(ctx.ids.get("tvdb"))


@_binding("imdbid", "IDs", ALL_KINDS, "tt9000001", "str", "IMDb id")
def _imdbid(ctx: _Ctx) -> Any:
    return ctx.ids.get("imdb")


@_binding("anidbid", "IDs", EPISODIC, "90001", "int", "AniDB id (Schema)")
def _anidbid(ctx: _Ctx) -> Any:
    return _num(ctx.ids.get("anidb"))


@_binding("provider", "IDs", ALL_KINDS, "tvdb", "str", "Provider that matched (Schema)")
def _provider(ctx: _Ctx) -> Any:
    return ctx.candidate.provider if ctx.candidate else None


@_binding("db", "IDs", ALL_KINDS, "tvdb", "str",
          "Database the numbering follows (Schema): the matching provider, or tvdb/tmdb for AniDB episodes mapped through Anime-Lists")
def _db(ctx: _Ctx) -> Any:
    mapped = {ep.ids["mapped"] for ep in ctx.episodes if ep.ids.get("mapped")}
    if len(mapped) == 1:
        return mapped.pop()
    return ctx.candidate.provider if ctx.candidate else None


# ---- Quality


@_binding("vf", "Quality", ALL_KINDS, "1080p", "str", "Video format (resolution class)")
def _vf(ctx: _Ctx) -> Any:
    return ctx.vf


@_binding("vc", "Quality", ALL_KINDS, "x264", "str", "Video codec")
def _vc(ctx: _Ctx) -> Any:
    return (ctx.probe.video_codec if ctx.probe else None) or ctx.parsed.quality.video_codec


@_binding("ac", "Quality", ALL_KINDS, "DDP", "str", "Audio codec")
def _ac(ctx: _Ctx) -> Any:
    return (ctx.probe.audio_codec if ctx.probe else None) or ctx.parsed.quality.audio_codec


@_binding("channels", "Quality", ALL_KINDS, "5.1", "str", "Audio channel layout")
def _channels(ctx: _Ctx) -> Any:
    return ctx.channels


@_binding("af", "Quality", ALL_KINDS, "6ch", "str", "Audio channel count (FileBot style)")
def _af(ctx: _Ctx) -> Any:
    count = _channel_count(ctx.channels)
    return f"{count}ch" if count else None


@_binding("aco", "Quality", ALL_KINDS, "Atmos", "str", "Object audio (Atmos / DTS:X)")
def _aco(ctx: _Ctx) -> Any:
    if ctx.parsed.quality.atmos:
        return "Atmos"
    codec = (ctx.probe.audio_codec if ctx.probe else None) or ctx.parsed.quality.audio_codec
    return "DTS:X" if codec and codec.upper() in ("DTS-X", "DTS:X") else None


@_binding("acf", "Quality", ALL_KINDS, "DDP 5.1", "str", "Audio codec and channels (Schema)")
def _acf(ctx: _Ctx) -> Any:
    codec = (ctx.probe.audio_codec if ctx.probe else None) or ctx.parsed.quality.audio_codec
    if not codec:
        return None
    return f"{codec} {ctx.channels}" if ctx.channels else codec


@_binding("hdr", "Quality", ALL_KINDS, "HDR10", "str", "HDR format: DV+HDR10, DV, HDR10+, HDR10, HDR, HLG")
def _hdr(ctx: _Ctx) -> Any:
    return ctx.hdr[0]


@_binding("dovi", "Quality", ALL_KINDS, "false", "bool", "Dolby Vision present")
def _dovi(ctx: _Ctx) -> Any:
    return ctx.hdr[1]


@_binding("bitdepth", "Quality", ALL_KINDS, "10", "int", "Video bit depth")
def _bitdepth(ctx: _Ctx) -> Any:
    return (ctx.probe.bit_depth if ctx.probe else None) or ctx.parsed.quality.bit_depth


@_binding("resolution", "Quality", ALL_KINDS, "1920x1080", "str", "Exact resolution (needs a media probe)")
def _resolution(ctx: _Ctx) -> Any:
    if ctx.probe and ctx.probe.width and ctx.probe.height:
        return f"{ctx.probe.width}x{ctx.probe.height}"
    return None


@_binding("width", "Quality", ALL_KINDS, "1920", "int", "Video width (needs a media probe)")
def _width(ctx: _Ctx) -> Any:
    return ctx.probe.width if ctx.probe else None


@_binding("height", "Quality", ALL_KINDS, "1080", "int", "Video height (needs a media probe)")
def _height(ctx: _Ctx) -> Any:
    return ctx.probe.height if ctx.probe else None


@_binding("hd", "Quality", ALL_KINDS, "FHD", "str", "Definition class: UHD, QHD, FHD, HD or SD")
def _hd(ctx: _Ctx) -> Any:
    probe = ctx.probe
    return _hd_class(probe.width if probe else None, probe.height if probe else None, ctx.vf)


@_binding("fps", "Quality", ALL_KINDS, "23.976", "float", "Frame rate (needs a media probe)")
def _fps(ctx: _Ctx) -> Any:
    return round(ctx.probe.frame_rate, 3) if ctx.probe and ctx.probe.frame_rate else None


@_binding("bitrate", "Quality", ALL_KINDS, "8000", "int", "Overall bit rate in kbps (needs a media probe)")
def _bitrate(ctx: _Ctx) -> Any:
    return ctx.probe.bitrate_kbps if ctx.probe else None


@_binding("vs", "Quality", ALL_KINDS, "BluRay", "str", "Video source (BluRay, WEB-DL, HDTV …)")
def _vs(ctx: _Ctx) -> Any:
    return ctx.parsed.quality.source


@_binding("source", "Quality", ALL_KINDS, "BluRay", "str", "Video source (same as vs)")
def _source(ctx: _Ctx) -> Any:
    return ctx.parsed.quality.source


@_binding("edition", "Quality", ALL_KINDS, "Director's Cut", "str", "Edition (Director's Cut, Extended …)")
def _edition(ctx: _Ctx) -> Any:
    return ctx.parsed.edition


@_binding("tags", "Quality", ALL_KINDS, "[Director's Cut]", "list", "Edition-like tags")
def _tags(ctx: _Ctx) -> Any:
    tags = [ctx.parsed.edition] if ctx.parsed.edition else []
    if ctx.parsed.quality.s3d:
        tags.append("3D")
    return tags or None


@_binding("s3d", "Quality", ALL_KINDS, "SBS", "str", "Stereoscopic 3D layout (SBS / TAB / OU)")
def _s3d(ctx: _Ctx) -> Any:
    return ctx.parsed.quality.s3d


@_binding("group", "Quality", ALL_KINDS, "SPARKS", "str", "Release group")
def _group(ctx: _Ctx) -> Any:
    return ctx.parsed.release_group


@_binding("remux", "Quality", ALL_KINDS, "false", "bool", "Remux release (Schema)")
def _remux(ctx: _Ctx) -> Any:
    return bool(ctx.parsed.quality.remux)


@_binding("version", "Quality", EPISODIC, "2", "int", "Release revision, e.g. 07v2 → 2 (Schema)")
def _version(ctx: _Ctx) -> Any:
    return ctx.parsed.version


@_binding("duration", "Quality", ALL_KINDS, "2700", "int", "Duration in seconds (needs a media probe)")
def _duration(ctx: _Ctx) -> Any:
    return round(ctx.duration) if ctx.duration else None


@_binding("seconds", "Quality", ALL_KINDS, "2700", "int", "Duration in seconds (needs a media probe)")
def _seconds(ctx: _Ctx) -> Any:
    return round(ctx.duration) if ctx.duration else None


@_binding("minutes", "Quality", ALL_KINDS, "45", "int", "Duration in minutes (needs a media probe)")
def _minutes(ctx: _Ctx) -> Any:
    return round(ctx.duration / 60) if ctx.duration else None


@_binding("hours", "Quality", ALL_KINDS, "0∶45", "str", "Duration as H∶MM (ratio colon, file-name safe)")
def _hours(ctx: _Ctx) -> Any:
    if not ctx.duration:
        return None
    total_minutes = round(ctx.duration / 60)
    return f"{total_minutes // 60}∶{total_minutes % 60:02d}"


# ---- File


@_binding("fn", "File", ALL_KINDS, "Starfall.Station.S01E02E03.720p.HDTV.x264-KILLERS", "str", "Original file name without extension")
def _fn(ctx: _Ctx) -> Any:
    return ctx.video_stem or None


@_binding("original", "File", ALL_KINDS, "Starfall.Station.S01E02E03.720p.HDTV.x264-KILLERS", "str", "Original file name (same as fn)")
def _original(ctx: _Ctx) -> Any:
    return ctx.video_stem or None


@_binding("ext", "File", ALL_KINDS, "mkv", "str", "File extension")
def _ext(ctx: _Ctx) -> Any:
    return ctx.extension or None


@_binding("f", "File", ALL_KINDS, "/storage/incoming/Starfall.Station.S01E02E03.mkv", "object",
          "Source file: .name .parent .dir .ext .path", clean=False)
def _f(ctx: _Ctx) -> Any:
    return FileValue(ctx.sidecar or ctx.parsed.path) if (ctx.sidecar or ctx.parsed.path) else None


@_binding("folder", "File", ALL_KINDS, "/storage/incoming", "object", "Parent folder of the source file: .name .path", clean=False)
def _folder(ctx: _Ctx) -> Any:
    parent = _parent(ctx.parsed.path)
    return FileValue(parent) if parent else None


@_binding("crc32", "File", ALL_KINDS, "A1B2C3D4", "str", "CRC32 from the file name (or computed by the pipeline)")
def _crc32(ctx: _Ctx) -> Any:
    return ctx.parsed.crc32


@_binding("lang", "File", ALL_KINDS, "eng", "str", "Subtitle language tag (sidecar files only)")
def _lang(ctx: _Ctx) -> Any:
    for tag in ctx.sidecar_tags:
        if tag.lower() in _SUBTITLE_FLAGS:
            continue
        if re.fullmatch(r"[A-Za-z]{2,3}(?:[-_][A-Za-z]{2,4})?", tag):
            return tag
    return None


@_binding("subt", "File", ALL_KINDS, ".eng.forced", "str", "Subtitle suffix (sidecar files only)")
def _subt(ctx: _Ctx) -> Any:
    if not ctx.sidecar_tags:
        return None
    return "." + ".".join(ctx.sidecar_tags)


@_binding("bytes", "File", ALL_KINDS, "1468006400", "int", "File size in bytes")
def _bytes(ctx: _Ctx) -> Any:
    return ctx.size


@_binding("megabytes", "File", ALL_KINDS, "1400", "int", "File size in MiB")
def _megabytes(ctx: _Ctx) -> Any:
    size = _bytes_value(ctx)
    return round(size / 1024**2) if size is not None else None


@_binding("gigabytes", "File", ALL_KINDS, "1.4", "float", "File size in GiB (one decimal)")
def _gigabytes(ctx: _Ctx) -> Any:
    size = _bytes_value(ctx)
    return round(size / 1024**3, 1) if size is not None else None


def _bytes_value(ctx: _Ctx) -> int | None:
    if ctx.bindings is not None:
        value = ctx.bindings.get("bytes")
        if isinstance(value, int):
            return value
    return ctx.size


# ---- Context (batch)


@_binding("di", "Context", ALL_KINDS, "1", "int", "Duplicate index among files matched to the same episode/movie in the batch")
def _di(ctx: _Ctx) -> Any:
    return None


@_binding("dc", "Context", ALL_KINDS, "2", "int", "Duplicate count among files matched to the same episode/movie in the batch")
def _dc(ctx: _Ctx) -> Any:
    return None


# ---- Presets

_PRESET_DESCRIPTIONS = {
    "plex": "Plex standard path: 'Name (Year)/Name (Year)' or 'Name (Year)/Season 01/Name (Year) - S01E02 - Title'",
    "jellyfin": "Jellyfin standard path (Season 00 for specials)",
    "emby": "Emby standard path (Season 00 for specials)",
    "kodi": "Kodi standard path (unpadded 'Season 1')",
}
_PRESET_EXAMPLES = {
    "plex": "Starfall Station (2016)/Season 01/Starfall Station (2016) - S01E02-E03 - Dead Reckoning (1-2)",
    "jellyfin": "Starfall Station (2016)/Season 01/Starfall Station (2016) - S01E02-E03 - Dead Reckoning (1-2)",
    "emby": "Starfall Station (2016)/Season 01/Starfall Station (2016) - S01E02-E03 - Dead Reckoning (1-2)",
    "kodi": "Starfall Station (2016)/Season 1/Starfall Station (2016) - S01E02-E03 - Dead Reckoning (1-2)",
}
_PRESET_ID_EXAMPLES = {
    "plex": "Starfall Station (2016) {tvdb-900001}/Season 01/…",
    "jellyfin": "Starfall Station (2016) [tvdbid-900001]/Season 01/…",
    "emby": "Starfall Station (2016) [tvdbid=900001]/Season 01/…",
    "kodi": "Starfall Station (2016) {tvdb=900001}/Season 1/…",
}


def _register_preset(standard: str) -> None:
    def resolve(ctx: _Ctx) -> Any:
        if ctx.bindings is None:
            return None
        return PresetValue(standard, ctx.bindings, ctx.preset_kind)

    _binding(standard, "Presets", ALL_KINDS, _PRESET_EXAMPLES[standard], "object", _PRESET_DESCRIPTIONS[standard], clean=False)(resolve)
    EXTRA_SPECS.append(
        BindingSpec(name=f"{standard}.id", description=f"{_PRESET_DESCRIPTIONS[standard]} with id tags",
                    kinds=list(ALL_KINDS), group="Presets", example=_PRESET_ID_EXAMPLES[standard], type="object")
    )
    if standard == "plex":
        EXTRA_SPECS.append(
            BindingSpec(name="plex.name", description="File name part of the Plex path", kinds=list(ALL_KINDS),
                        group="Presets", example="Starfall Station (2016) - S01E02-E03 - Dead Reckoning (1-2)", type="str")
        )


for _standard in ("plex", "jellyfin", "emby", "kodi"):
    _register_preset(_standard)


# Bindings FileBot has that Schema does not provide (validate() reports them as warnings).
UNSUPPORTED_FILEBOT_BINDINGS = frozenset(
    {
        "home", "output", "drive", "defines", "label", "mediaFile", "mediaFileName", "i", "model", "self",
        "object", "file", "json", "localize", "order", "db", "XEM", "AnimeList", "historic", "omdb", "ci",
        "cy", "votes", "actors", "director", "languages", "audioLanguages", "textLanguages", "sy",
        "episodelist", "series", "movie", "today", "dt", "ct", "age", "xattr", "relativeFile", "root",
        "files", "vk", "hpi", "ar", "ws", "vcf", "cf", "khz", "kbps", "mbps", "vbr", "abr", "chapters",
        "media", "video", "audio", "text", "image", "menu", "mediaTitle", "mediaTags", "music", "medium",
        "artist", "albumArtist", "album", "photo", "exif", "camera", "location", "pn",
    }
)  # fmt: skip
ROOT_RELATIVE_BINDINGS = frozenset({"home", "output", "drive", "root", "relativeFile"})

OBJECT_PROPERTIES: dict[str, frozenset[str]] = {
    "episode": EpisodeValue.properties,
    "info": InfoValue.properties,
    "f": FileValue.properties,
    "folder": FileValue.properties,
    "plex": PresetValue.properties,
    "jellyfin": PresetValue.properties,
    "emby": PresetValue.properties,
    "kodi": PresetValue.properties,
}


def binding_specs() -> list[BindingSpec]:
    specs = [definition.spec for definition in BINDINGS.values()]
    return specs + list(EXTRA_SPECS)


# --------------------------------------------------------------------------- factory


def _resolve(definition: BindingDef, ctx: _Ctx) -> Any:
    value = definition.fn(ctx)
    return clean_value(value) if definition.clean else value


def build_binding(
    parsed: ParsedMedia,
    candidate: Candidate | None,
    probe: ProbeInfo | None,
    *,
    sidecar: str | PurePath | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Bindings:
    """Lazy bindings for one file and its (optional) match. ``overrides`` win over computed values
    (the pipeline passes batch facts such as ``pc``, ``di``, ``dc``, ``crc32`` and ``bytes``)."""
    ctx = _Ctx(parsed, candidate, probe, sidecar)
    resolvers = {name: partial(_resolve, definition, ctx) for name, definition in BINDINGS.items()}
    bindings = Bindings(
        resolvers,
        kind=ctx.kind,
        extension_suffix=ctx.extension_suffix,
        overrides={name: clean_value(value) for name, value in (overrides or {}).items()},
    )
    ctx.bindings = bindings
    return bindings


__all__ = [
    "BINDINGS",
    "Bindings",
    "EpisodeValue",
    "FileValue",
    "InfoValue",
    "OBJECT_PROPERTIES",
    "PresetValue",
    "ROOT_RELATIVE_BINDINGS",
    "UNSUPPORTED_FILEBOT_BINDINGS",
    "binding_specs",
    "build_binding",
    "clean_text",
    "clean_value",
    "join_titles",
]
