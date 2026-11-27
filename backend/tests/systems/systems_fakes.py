"""Deterministic stand-ins for sibling modules (parser, matcher, fileops, watcher, ...).

They implement the contract signatures from SPEC §4.3 with just enough behavior to drive the
pipeline and API through every state transition.
"""

from __future__ import annotations

import os
import re
import shutil
import zlib
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePath
from typing import Any

from schema_app.domain import (
    Candidate,
    ConflictPolicy,
    EpisodeInfo,
    FileOpPlan,
    FileOpResult,
    MatchHint,
    MatchResult,
    MediaKind,
    MovieInfo,
    ParsedMedia,
    ProviderRef,
    ProviderSettings,
    ProviderStatus,
    QualityInfo,
    RenameMode,
    SeriesInfo,
    SidecarMove,
    WatchFolderSpec,
)

# --------------------------------------------------------------------------- parser

_SE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})")
_YEAR = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")
_CRC = re.compile(r"\[([0-9A-Fa-f]{8})\]")
_RES = re.compile(r"(2160p|1080p|720p|480p)", re.I)


def fake_parse(path: str | PurePath) -> ParsedMedia:
    text = str(path)
    name = os.path.basename(text)
    stem, _, ext = name.rpartition(".")
    if not stem:
        stem, ext = name, ""
    clean = stem.replace(".", " ").replace("_", " ")
    se = _SE.search(clean)
    year = _YEAR.search(clean)
    crc = _CRC.search(stem)
    res = _RES.search(clean)
    cut = min([m.start() for m in (se, year, crc, res) if m] or [len(clean)])
    title = re.sub(r"\[[^\]]*\]", "", clean[:cut]).strip(" -") or None
    quality = QualityInfo(
        resolution=res.group(1).lower() if res else None,
        remux="remux" in clean.lower(),
        hdr=["HDR10"] if " hdr" in clean.lower() else [],
    )
    return ParsedMedia(
        path=text,
        filename=name,
        extension=ext.lower(),
        kind=MediaKind.SERIES if se else (MediaKind.MOVIE if year else MediaKind.UNKNOWN),
        title=title,
        year=int(year.group(1)) if year else None,
        season=int(se.group(1)) if se else None,
        episodes=[int(se.group(2))] if se else [],
        crc32=crc.group(1).upper() if crc else None,
        quality=quality,
        is_sample=bool(re.search(r"\bsample\b", clean, re.I)),
        is_extra=bool(re.search(r"\btrailer\b", clean, re.I)),
        confidence=0.9 if title else 0.1,
    )


# --------------------------------------------------------------------------- matcher


def series_candidate(title: str, year: int | None, season: int, episodes: list[int], *, score: float = 0.95,
                     provider: str = "fake", sid: str = "100") -> Candidate:  # fmt: skip
    year = year or 2010  # series always have a start year at real providers
    series = SeriesInfo(ref=ProviderRef(provider=provider, id=sid), title=title, year=year, ids={"tvdb": sid})
    eps = [EpisodeInfo(series_ref=series.ref, season=season, episode=e, title=f"Episode {e}") for e in episodes]
    return Candidate(id=f"{provider}:series:{sid}", kind=MediaKind.SERIES, provider=provider, title=title,
                     year=year, score=score, series=series, episodes=eps, reasons=["fake"])  # fmt: skip


def movie_candidate(title: str, year: int | None, *, score: float = 0.95, provider: str = "fake",
                    mid: str = "200") -> Candidate:  # fmt: skip
    movie = MovieInfo(ref=ProviderRef(provider=provider, id=mid), title=title, year=year, ids={"tmdb": mid})
    return Candidate(id=f"{provider}:movie:{mid}", kind=MediaKind.MOVIE, provider=provider, title=title,
                     year=year, score=score, movie=movie, reasons=["fake"])  # fmt: skip


class FakeMatcher:
    """Titles containing "unknown" never match; "vague" matches with low confidence."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.reconfigured: list[ProviderSettings] = []
        self.closed = False
        self.fail = False

    def _result(self, parsed: ParsedMedia, hint: MatchHint | None) -> MatchResult:
        title = (hint.query if hint and hint.query else None) or parsed.title or ""
        if self.fail:
            raise RuntimeError("matcher exploded")
        if not title or "unknown" in title.lower():
            return MatchResult(providers_tried=["fake"])
        score = 0.5 if "vague" in title.lower() else 0.95
        kind = (hint.kind if hint and hint.kind else None) or parsed.kind
        sid = str(abs(zlib.crc32(title.lower().encode())) % 100000)
        if kind in (MediaKind.SERIES, MediaKind.ANIME) or parsed.episodes:
            season = hint.season if hint and hint.season is not None else (parsed.season or 1)
            episodes = (hint.episodes if hint and hint.episodes else None) or parsed.episodes or [1]
            best = series_candidate(title, parsed.year, season, episodes, score=score, sid=sid)
            alt = series_candidate(title + " Alt", parsed.year, season, episodes, score=score - 0.2, sid=sid + "9")
            if kind == MediaKind.ANIME:
                best = best.model_copy(update={"kind": MediaKind.ANIME})
        else:
            best = movie_candidate(title, parsed.year, score=score, mid=sid)
            alt = movie_candidate(title + " Alt", parsed.year, score=score - 0.2, mid=sid + "9")
        return MatchResult(candidates=[best, alt], best=best, confidence=score, providers_tried=["fake"])

    async def match(self, parsed: ParsedMedia, hint: MatchHint | None = None) -> MatchResult:
        self.calls.append(("match", parsed.filename))
        return self._result(parsed, hint)

    async def match_many(self, items: Sequence[ParsedMedia], hint: MatchHint | None = None) -> list[MatchResult]:
        self.calls.append(("match_many", [p.filename for p in items]))
        return [self._result(p, hint) for p in items]

    async def search(self, query: str, kind: MediaKind, year: int | None = None,
                     provider: str | None = None) -> list[Candidate]:  # fmt: skip
        self.calls.append(("search", query))
        if kind == MediaKind.MOVIE:
            return [movie_candidate(query, year, score=0.9, mid="777")]
        return [series_candidate(query, year, 1, [], score=0.9, sid="777")]

    async def resolve(self, parsed: ParsedMedia, ref: ProviderRef, kind: MediaKind,
                      hint: MatchHint | None = None) -> Candidate:  # fmt: skip
        self.calls.append(("resolve", ref.key()))
        if kind == MediaKind.MOVIE:
            return movie_candidate("Picked Movie", 2001, score=0.8, provider=ref.provider, mid=ref.id)
        season = hint.season if hint and hint.season is not None else (parsed.season or 1)
        episodes = (hint.episodes if hint and hint.episodes else None) or parsed.episodes or [1]
        return series_candidate("Picked Show", 2010, season, episodes, score=0.8, provider=ref.provider, sid=ref.id)

    def provider_statuses(self) -> list[ProviderStatus]:
        return [ProviderStatus(name="fake", label="Fake DB", kinds=[MediaKind.MOVIE, MediaKind.SERIES],
                               requires_key=False, configured=True, enabled=True)]  # fmt: skip

    async def test_provider(self, name: str) -> ProviderStatus:
        if name != "fake":
            raise KeyError(name)
        status = self.provider_statuses()[0]
        return status.model_copy(update={"healthy": True, "last_checked": datetime.now(UTC)})

    async def reconfigure(self, settings: ProviderSettings) -> None:
        self.reconfigured.append(settings)

    async def aclose(self) -> None:
        self.closed = True


# --------------------------------------------------------------------------- fileops


class FakeFileOps:
    """Real moves/copies on tmp dirs, simplified (no temp files, no cross-device logic)."""

    def __init__(self) -> None:
        self.executed: list[FileOpPlan] = []
        self.undone: list[FileOpResult] = []
        self.fail_with: str | None = None
        self.fail_code: str | None = None  # FileOpResult.error_code of a failure

    def find_sidecars(self, video: str | Path, extensions: list[str]) -> list[Path]:
        video = Path(video)
        stem = video.stem
        out = []
        for entry in sorted(video.parent.iterdir()) if video.parent.is_dir() else []:
            if entry != video and entry.name.startswith(stem + ".") and entry.suffix.lstrip(".").lower() in extensions:
                out.append(entry)
        return out

    def plan_operation(self, source: str | Path, destination: str | Path, mode: RenameMode, policy: ConflictPolicy,
                       *, sidecar_extensions: list[str] | None = None, incoming_rank: int | None = None,
                       existing_rank: int | None = None, rename_folder: bool = False,
                       remove_empty_dirs: bool = False, protected_dirs: list[str] | None = None) -> FileOpPlan:  # fmt: skip
        src, dst = Path(source), Path(destination)
        sidecars = []
        if sidecar_extensions:
            for sc in self.find_sidecars(src, sidecar_extensions):
                suffix = sc.name[len(src.stem):]
                sidecars.append(SidecarMove(source=str(sc), destination=str(dst.with_name(dst.stem + suffix))))
        conflict = "none"
        if dst.exists():
            conflict = "same_file" if src.exists() and os.path.samefile(src, dst) else "exists"
        return FileOpPlan(source=str(src), destination=str(dst), mode=mode, policy=policy, sidecars=sidecars,
                          conflict=conflict, incoming_rank=incoming_rank, existing_rank=existing_rank)  # fmt: skip

    def execute(self, plan: FileOpPlan, *, allowed_roots: list[str], trash_dir: Path | None = None) -> FileOpResult:
        self.executed.append(plan)
        base = {"mode": plan.mode, "source": plan.source, "destination": plan.destination}
        if self.fail_with:
            return FileOpResult(ok=False, error=self.fail_with, error_code=self.fail_code, **base)
        src, dst = Path(plan.source), Path(plan.destination)
        if dst.exists():
            if plan.policy == ConflictPolicy.SKIP:
                return FileOpResult(ok=False, skipped=True, error="destination exists", **base)
            if plan.policy == ConflictPolicy.FAIL:
                return FileOpResult(ok=False, error="destination exists", **base)
        if plan.mode == RenameMode.TEST:
            return FileOpResult(ok=True, **base)
        dst.parent.mkdir(parents=True, exist_ok=True)
        size = src.stat().st_size
        if plan.mode == RenameMode.COPY:
            shutil.copy2(src, dst)
        else:
            os.replace(src, dst)
        moved = []
        for sc in plan.sidecars:
            if Path(sc.source).exists():
                if plan.mode == RenameMode.COPY:
                    shutil.copy2(sc.source, sc.destination)
                else:
                    os.replace(sc.source, sc.destination)
                moved.append(sc)
        return FileOpResult(ok=True, bytes=size, verified=True, sidecars=moved, **base)

    def undo(self, result: FileOpResult, *, allowed_roots: list[str]) -> FileOpResult:
        self.undone.append(result)
        base = {"mode": result.mode, "source": result.destination, "destination": result.source}
        dst = Path(result.destination)
        if not dst.exists():
            return FileOpResult(ok=False, error="destination is gone", **base)
        if result.mode == RenameMode.COPY:
            dst.unlink()
        elif result.mode != RenameMode.TEST:
            os.replace(dst, result.source)
        return FileOpResult(ok=True, **base)


# --------------------------------------------------------------------------- watcher


class FakeWatcher:
    instances: list[FakeWatcher] = []

    def __init__(self, on_ready: Any, on_status: Any = None, *, rescan_interval: float = 60.0,
                 ignore_patterns: list[str] | None = None, video_extensions: list[str] | None = None,
                 **_options: Any) -> None:  # fmt: skip
        self.on_ready = on_ready
        self.on_status = on_status
        self.ignore_patterns = ignore_patterns
        self.specs: list[WatchFolderSpec] = []
        self.started = False
        self.sync_calls = 0
        self.waiting: list[Any] = []  # watcher.WatchedFile entries returned by incoming()
        self.started_now: list[tuple[int, str | None]] = []
        self.scans = 0
        FakeWatcher.instances.append(self)

    def incoming(self) -> list[Any]:
        return list(self.waiting)

    async def start_now(self, folder_id: int, path: str | None = None) -> int:
        self.started_now.append((folder_id, path))
        return sum(1 for f in self.waiting if f.folder_id == folder_id and f.state == "settling"
                   and (path is None or os.path.normcase(f.path) == os.path.normcase(path)))  # fmt: skip

    async def scan_now(self) -> int:
        self.scans += 1
        return sum(1 for f in self.waiting if f.state == "settling")

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def sync(self, specs: list[WatchFolderSpec]) -> None:
        self.specs = list(specs)
        self.sync_calls += 1

    def status(self) -> dict[int, str]:
        return {s.id: ("watching" if s.enabled else "stopped") for s in self.specs}


# --------------------------------------------------------------------------- misc services


def fake_crc32_file(path: str | Path, *, chunk_size: int = 4 * 1024 * 1024) -> str:
    return f"{zlib.crc32(Path(path).read_bytes()) & 0xFFFFFFFF:08X}"


class FakeMediaServers:
    def __init__(self) -> None:
        self.refreshed: list[tuple[str, list[str]]] = []
        self.tested: list[str] = []

    async def test_server(self, config: Any) -> tuple[bool, str]:
        self.tested.append(config.token)
        return config.token == "good-token", "Fake server 1.0" if config.token == "good-token" else "unauthorized"

    async def refresh_paths(self, config: Any, paths: list[str]) -> None:
        self.refreshed.append((config.name, list(paths)))
