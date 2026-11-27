"""Download-client webhook payload parsing (SABnzbd, qBittorrent, generic).

Contract: docs/SPEC.md §7.7.

Payloads arrive as JSON or as decoded form fields (every value a string). Paths are translated
from the client's view to Schema's with ``path_mappings`` (``source`` = client path, ``target`` =
Schema path). A webhook starts an auto-executing job, so :func:`confine_paths` then holds every
path to the download area: below a watch folder or a path-mapping target, inside the allowed roots,
and never an allowed root, a library root, or a folder that contains one.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, Field

from schema_app.domain import MediaKind, PathMapping
from schema_app.services import paths as paths_mod
from schema_app.services.paths import map_path


class IngestRequest(BaseModel):
    paths: list[str] = Field(min_length=1)  # local paths (after path mapping)
    kind_hint: MediaKind | None = None
    source: str  # "sabnzbd" | "qbittorrent" | "generic"
    label: str | None = None  # job / torrent name
    category: str | None = None


class WebhookPayloadError(ValueError):
    """Payload missing required fields, or reports a failed download (nothing to ingest)."""


class WebhookPathRefused(ValueError):
    """A (mapped) webhook path lies outside the download area (answered with 403)."""


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


_SOURCES = {
    "sabnzbd": "sabnzbd", "sab": "sabnzbd",
    "qbittorrent": "qbittorrent", "qbit": "qbittorrent", "qb": "qbittorrent",
    "generic": "generic",
}  # fmt: skip
_KIND_ALIASES = {
    "movie": MediaKind.MOVIE, "movies": MediaKind.MOVIE, "film": MediaKind.MOVIE, "films": MediaKind.MOVIE,
    "series": MediaKind.SERIES, "tv": MediaKind.SERIES, "show": MediaKind.SERIES, "shows": MediaKind.SERIES,
    "episode": MediaKind.SERIES, "anime": MediaKind.ANIME, "unknown": MediaKind.UNKNOWN,
}  # fmt: skip
# SABnzbd SAB_PP_STATUS: 0 OK, 1 failed verification, 2 failed unpack, 3 both, -1 failed post-processing
_SAB_STATUS = {"1": "failed verification", "2": "failed to unpack", "3": "failed verification and unpack",
               "-1": "failed post-processing"}  # fmt: skip


def parse_webhook(
    source: str,
    payload: dict[str, Any],
    *,
    category_kinds: dict[str, MediaKind],
    path_mappings: list[PathMapping],
) -> IngestRequest:
    """Normalize a JSON or form payload (already decoded to a dict) from one client.

    sabnzbd: fields from the bundled post-processing script (complete_dir, category, status, name).
    qbittorrent: fields from "Run external program" (content_path or save_path+name, category, hash).
    generic: {"path"|"paths", "kind"?, "category"?}.
    """
    name = _SOURCES.get((source or "").strip().lower())
    if name is None:
        raise WebhookPayloadError(
            f"Unknown webhook source '{source}' (expected sabnzbd, qbittorrent or generic)"
        )
    if not isinstance(payload, dict):
        raise WebhookPayloadError("Payload must be a JSON object or form fields")
    data = {str(k).strip().lower(): v for k, v in payload.items()}

    if name == "sabnzbd":
        raw_paths, label, category, kind = _sabnzbd(data)
    elif name == "qbittorrent":
        raw_paths, label, category, kind = _qbittorrent(data)
    else:
        raw_paths, label, category, kind = _generic(data)

    for raw in raw_paths:
        _check_path(raw)
    paths = [map_path(p, path_mappings) for p in raw_paths]
    paths = list(dict.fromkeys(p for p in paths if p))
    if not paths:
        raise WebhookPayloadError("Payload has no path to import")
    if kind is None and category:
        kind = kind_for_category(category, category_kinds)
    return IngestRequest(paths=paths, kind_hint=kind, source=name, label=label, category=category)


def kind_for_category(category: str, category_kinds: dict[str, MediaKind]) -> MediaKind | None:
    """Category -> kind: exact match (case-insensitive), then each "/"-separated level ("tv/hd"),
    then each word ("tv-sonarr" -> "tv")."""
    table = {str(k).strip().lower(): MediaKind(v) for k, v in (category_kinds or {}).items() if k}
    key = category.strip().lower()
    if not key:
        return None
    if key in table:
        return table[key]
    for level in re.split(r"[\\/]+", key):
        if level in table:
            return table[level]
    for word in re.split(r"[^a-z0-9]+", key):
        if word in table:
            return table[word]
    return None


def _check_path(path: str) -> None:
    """Refuse control characters (a newline would smuggle in a second path) and ".." components."""
    if _CONTROL_RE.search(path):
        raise WebhookPayloadError("Path contains a control character (send one path per field, or a JSON list)")
    if ".." in re.split(r"[\\/]", path):
        raise WebhookPayloadError(f"Path must not contain '..': {path}")


# --------------------------------------------------------------------------- download area


def _key(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def confine_paths(
    paths: Iterable[str],
    *,
    download_areas: Iterable[str],
    allowed_roots: list[str],
    library_roots: Iterable[str],
) -> list[str]:
    """Normalize webhook paths and hold them to the download area. Raises ``WebhookPathRefused``.

    ``download_areas`` are the watch folders and the path-mapping targets. Every path must be
    absolute, inside ``allowed_roots``, strictly below a download area, and must not be (or
    contain) an allowed root or a library root. A path inside a library root is accepted only
    when the download area holding it is itself inside that library root.
    """
    areas = [a.strip() for a in download_areas if a and a.strip() and os.path.isabs(a.strip())]
    libraries = [r.strip() for r in library_roots if r and r.strip()]
    roots = [r for r in allowed_roots if r and r.strip()]
    out: list[str] = []
    for raw in paths:
        if not os.path.isabs(raw):
            raise WebhookPathRefused(f"path must be absolute: {raw}")
        path = os.path.normpath(raw)
        if not paths_mod.is_within(path, roots):
            raise WebhookPathRefused(f"path is outside the allowed roots: {path}")
        key = _key(path)
        for root in [*roots, *libraries]:
            if key == _key(root) or paths_mod.is_within(root, [path]):
                raise WebhookPathRefused(f"path is (or contains) an allowed root or a library root: {path}")
        holders = [a for a in areas if key != _key(a) and paths_mod.is_within(path, [a])]
        if not holders:
            raise WebhookPathRefused(
                f"path is not inside a download folder: {path} (add a watch folder, or a path mapping, "
                "for the folder your download app saves to)"
            )
        for library in libraries:
            if paths_mod.is_within(path, [library]) and not any(paths_mod.is_within(a, [library]) for a in holders):
                raise WebhookPathRefused(f"path is inside the library: {path}")
        if path not in out:
            out.append(path)
    return out


# --------------------------------------------------------------------------- per client


def _first(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            value = value[0] if value else None
        if value is None:
            continue
        text = _clean(str(value))
        if text:
            return text
    return None


def _clean(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()  # a quoted %F that the client passed through literally
    return text


def _sabnzbd(data: dict[str, Any]) -> tuple[list[str], str | None, str | None, MediaKind | None]:
    status = _first(data, "status", "pp_status", "sab_pp_status")
    if status is not None and status not in {"0", "ok", "OK", "Completed", "completed"}:
        reason = _SAB_STATUS.get(status, f"status {status}")
        message = _first(data, "fail_msg", "sab_fail_msg")
        detail = f": {message}" if message else ""
        raise WebhookPayloadError(f"SABnzbd reports the download {reason}{detail}; nothing to import")
    path = _first(data, "complete_dir", "sab_complete_dir", "directory", "dir", "path")
    if not path:
        raise WebhookPayloadError("SABnzbd payload has no complete_dir")
    label = _first(data, "name", "final_name", "sab_final_name", "nzbname", "filename")
    category = _first(data, "category", "cat", "sab_cat")
    if category in {"*", "Default", "default"}:
        category = None
    return [path], label, category, _kind(data)


def _qbittorrent(data: dict[str, Any]) -> tuple[list[str], str | None, str | None, MediaKind | None]:
    name = _first(data, "name", "torrent_name")
    path = _first(data, "content_path", "contentpath", "path")
    if not path:
        save = _first(data, "save_path", "savepath", "root_path")
        if save and name:
            if re.search(r"[\\/]", name) or name.strip() in {".", ".."} or _CONTROL_RE.search(name):
                raise WebhookPayloadError("qBittorrent torrent name must be a single file or folder name")
            sep = "\\" if ("\\" in save and "/" not in save) else "/"
            path = save.rstrip("/\\") + sep + name
        elif save:
            path = save
    if not path:
        raise WebhookPayloadError("qBittorrent payload has no content_path (use %F in the command line)")
    category = _first(data, "category", "cat")
    return [path], name, category, _kind(data)


def _generic(data: dict[str, Any]) -> tuple[list[str], str | None, str | None, MediaKind | None]:
    """"paths": a JSON list, or one path per line (form posts); "path": exactly one path."""
    paths: list[str] = []
    for key in ("paths", "path"):
        value = data.get(key)
        if value is None:
            continue
        if isinstance(value, list | tuple):
            items: list[Any] = list(value)
        elif key == "paths":
            items = re.split(r"\r?\n", str(value))
        else:
            items = [value]
        paths.extend(_clean(str(item)) for item in items if item is not None and _clean(str(item)))
    if not paths:
        raise WebhookPayloadError('Generic payload needs "path" or "paths"')
    label = _first(data, "label", "name")
    category = _first(data, "category")
    return paths, label, category, _kind(data, "kind", "type", "media_kind")


def _kind(data: dict[str, Any], *keys: str) -> MediaKind | None:
    raw = _first(data, *(keys or ("kind",)))
    if raw is None:
        return None
    kind = _KIND_ALIASES.get(raw.lower())
    if kind is None:
        raise WebhookPayloadError(f"Unknown kind '{raw}' (expected movie, series or anime)")
    return None if kind is MediaKind.UNKNOWN else kind
