"""Plex / Jellyfin / Emby library refresh.

Contract: docs/SPEC.md §7.6.

* Plex: ``GET /library/sections`` (``X-Plex-Token``) to find the section whose location contains
  the folder, then ``GET /library/sections/{key}/refresh?path=<folder>`` (partial scan). When no
  section matches, every section is refreshed (``/library/sections/all/refresh``).
* Jellyfin/Emby: ``POST /Library/Media/Updated`` with the touched folders, falling back to
  ``POST /Library/Refresh`` (full scan) when the server rejects the targeted call.
* :class:`RefreshDebouncer` collects folders per server and refreshes once things go quiet
  (default 5 s), de-duplicated by folder.

Paths are mapped Schema -> server with ``MediaServerConfig.path_mappings`` (``source`` = Schema
path, ``target`` = server path). Tokens never appear in messages or logs.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

import httpx

from schema_app import __version__
from schema_app.domain import MediaServerConfig
from schema_app.services.paths import map_path

log = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_LABELS = {"plex": "Plex", "jellyfin": "Jellyfin", "emby": "Emby"}


class MediaServerError(Exception):
    """A refresh or test failed; the message is safe to show to users (no secrets)."""


# --------------------------------------------------------------------------- HTTP plumbing


def _kind(config: MediaServerConfig) -> str:
    return (config.kind or "").strip().lower()


def _headers(config: MediaServerConfig) -> dict[str, str]:
    kind = _kind(config)
    base = {"Accept": "application/json", "User-Agent": f"Schema/{__version__}"}
    if kind == "plex":
        base.update(
            {
                "X-Plex-Token": config.token,
                "X-Plex-Product": "Schema",
                "X-Plex-Version": __version__,
                "X-Plex-Client-Identifier": f"schema-{config.id}",
            }
        )
    elif kind == "jellyfin":
        base["X-Emby-Token"] = config.token
        base["Authorization"] = (
            f'MediaBrowser Client="Schema", Device="Schema", DeviceId="schema-{config.id}", '
            f'Version="{__version__}", Token="{config.token}"'
        )
    else:
        base["X-Emby-Token"] = config.token
    return base


def _client(config: MediaServerConfig) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=config.url.rstrip("/"), headers=_headers(config), timeout=TIMEOUT, follow_redirects=False
    )


def _explain(config: MediaServerConfig, exc: Exception) -> str:
    label = _LABELS.get(_kind(config), config.kind)
    url = config.url.rstrip("/")
    if isinstance(exc, httpx.ConnectTimeout | httpx.ReadTimeout | httpx.WriteTimeout | httpx.PoolTimeout):
        return f"{label} at {url} did not answer in time. Is the URL right and the server running?"
    if isinstance(exc, httpx.ConnectError):
        return f"Could not connect to {label} at {url}. Check the address and port, and that Schema can reach it."
    if isinstance(exc, httpx.UnsupportedProtocol | httpx.InvalidURL):
        return f"'{url}' is not a valid URL. Use something like http://192.168.1.20:32400."
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            what = "token" if _kind(config) == "plex" else "API key"
            return f"{label} rejected the {what} ({code}). Paste a fresh {what} in the media-server settings."
        if code == 404:
            return f"{label} answered 404 for {exc.request.url.path}. Is this really a {label} server URL?"
        return f"{label} returned HTTP {code} for {exc.request.url.path}."
    if isinstance(exc, ValueError):
        return f"{label} at {url} sent an unexpected (non-JSON) answer. Is this a {label} server?"
    if isinstance(exc, httpx.HTTPError):
        return f"Talking to {label} at {url} failed: {exc.__class__.__name__}"
    return f"{label}: {exc}"


async def _get_json(client: httpx.AsyncClient, path: str, **params: Any) -> Any:
    response = await client.get(path, params=params or None)
    response.raise_for_status()
    return response.json()


# --------------------------------------------------------------------------- test


async def test_server(config: MediaServerConfig) -> tuple[bool, str]:
    """Authenticated ping. Returns (ok, human message e.g. "Plex 1.41.2, 4 libraries")."""
    kind = _kind(config)
    if kind not in _LABELS:
        return False, f"Unknown media server type '{config.kind}' (expected plex, jellyfin or emby)."
    if not config.url.strip():
        return False, "Enter the server URL, for example http://192.168.1.20:32400."
    if not config.token.strip():
        return False, "Enter the token / API key."
    try:
        async with _client(config) as client:
            if kind == "plex":
                root = (await _get_json(client, "/")).get("MediaContainer", {})
                sections = await _plex_sections(client)
                name = root.get("friendlyName")
                version = root.get("version", "?")
                named = f" '{name}'" if name else ""
                return True, f"Plex {version}{named}, {_plural(len(sections), 'library', 'libraries')}"
            info = await _get_json(client, "/System/Info")
            folders = await _get_json(client, "/Library/VirtualFolders")
            label = _LABELS[kind]
            product = str(info.get("ProductName") or "")
            if kind == "jellyfin" and "emby" in product.lower():
                label = "Emby (configured as Jellyfin)"
            elif kind == "emby" and "jellyfin" in product.lower():
                label = "Jellyfin (configured as Emby)"
            name = info.get("ServerName")
            named = f" '{name}'" if name else ""
            count = len(folders) if isinstance(folders, list) else len(folders.get("Items", []))
            return (
                True,
                f"{label} {info.get('Version', '?')}{named}, {_plural(count, 'library', 'libraries')}",
            )
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        return False, _explain(config, exc if not isinstance(exc, AttributeError) else ValueError())


test_server.__test__ = False  # type: ignore[attr-defined]  # not a pytest test when imported by name


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


# --------------------------------------------------------------------------- refresh


def _folders_for(paths: list[str]) -> list[str]:
    """Folders to refresh, decided on the Schema side: a folder stays as is, a file becomes its
    parent folder."""
    folders: list[str] = []
    for raw in paths:
        if not raw:
            continue
        folder = (raw.rstrip("/\\") or raw) if os.path.isdir(raw) else _parent(raw)
        if folder and folder not in folders:
            folders.append(folder)
    return folders


def _dedupe_folders(folders: list[str]) -> list[str]:
    """Drop duplicates and folders already covered by another folder in the list."""
    keep: list[str] = []
    for folder in sorted(dict.fromkeys(folders), key=len):
        if not any(_contains(parent, folder) for parent in keep):
            keep.append(folder)
    return keep


def _parent(path: str) -> str:
    pure = PureWindowsPath(path) if _windowsy(path) else PurePosixPath(path.replace("\\", "/"))
    return str(pure.parent)


def _windowsy(path: str) -> bool:
    return (len(path) > 1 and path[1] == ":") or path.startswith("\\\\")


def _contains(parent: str, child: str) -> bool:
    """Component-wise prefix test that works for both POSIX and Windows-style server paths."""
    fold = _windowsy(parent) or _windowsy(child)
    norm_p = parent.replace("\\", "/").rstrip("/")
    norm_c = child.replace("\\", "/").rstrip("/")
    if fold:
        norm_p, norm_c = norm_p.lower(), norm_c.lower()
    return norm_c == norm_p or norm_c.startswith(norm_p + "/") or (norm_p == "" and norm_c.startswith("/"))


async def _plex_sections(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    data = await _get_json(client, "/library/sections")
    sections = data.get("MediaContainer", {}).get("Directory", []) or []
    return [s for s in sections if isinstance(s, dict)]


async def refresh_paths(config: MediaServerConfig, paths: list[str]) -> None:
    """Targeted refresh of the folders containing ``paths`` (after mapping Schema paths to server
    paths). Plex: partial scan of the matching library section with ``path=``; Jellyfin/Emby:
    POST /Library/Media/Updated. Falls back to a full library refresh when no section matches.

    Raises :class:`MediaServerError` (message safe to show) when the server can't be reached or
    rejects the request. Disabled servers and empty ``paths`` are a no-op.
    """
    if not config.enabled or not paths:
        return
    kind = _kind(config)
    if kind not in _LABELS:
        raise MediaServerError(f"Unknown media server type '{config.kind}'")
    # Decide file vs folder on the Schema side (where the paths exist), then translate.
    folders = _dedupe_folders([map_path(f, config.path_mappings) for f in _folders_for(paths)])
    if not folders:
        return
    try:
        async with _client(config) as client:
            if kind == "plex":
                await _refresh_plex(client, folders)
            else:
                await _refresh_emby_like(client, config, folders)
    except httpx.HTTPError as exc:
        raise MediaServerError(_explain(config, exc)) from exc
    except ValueError as exc:
        raise MediaServerError(_explain(config, exc)) from exc


async def _refresh_plex(client: httpx.AsyncClient, folders: list[str]) -> None:
    sections = await _plex_sections(client)
    targets: dict[str, list[str]] = {}
    unmatched: list[str] = []
    for folder in folders:
        best_key, best_len = None, -1
        for section in sections:
            for location in section.get("Location", []) or []:
                loc = str(location.get("path") or "")
                if loc and _contains(loc, folder) and len(loc) > best_len:
                    best_key, best_len = str(section.get("key")), len(loc)
        if best_key is None:
            unmatched.append(folder)
        else:
            targets.setdefault(best_key, []).append(folder)
    for key, section_folders in targets.items():
        for folder in section_folders:
            response = await client.get(f"/library/sections/{key}/refresh", params={"path": folder})
            response.raise_for_status()
            log.info("plex: partial scan of section %s for %s", key, folder)
    if unmatched:
        log.info("plex: no library section contains %s; refreshing all sections", ", ".join(unmatched))
        response = await client.get("/library/sections/all/refresh")
        response.raise_for_status()


async def _refresh_emby_like(
    client: httpx.AsyncClient, config: MediaServerConfig, folders: list[str]
) -> None:
    body = {"Updates": [{"Path": folder, "UpdateType": "Created"} for folder in folders]}
    response = await client.post("/Library/Media/Updated", json=body)
    if response.status_code in (401, 403):
        response.raise_for_status()
    if response.is_success:
        log.info("%s: media updated for %s", _kind(config), ", ".join(folders))
        return
    log.info("%s: /Library/Media/Updated returned %s; running a full library refresh", _kind(config),
             response.status_code)  # fmt: skip
    fallback = await client.post("/Library/Refresh")
    fallback.raise_for_status()


# --------------------------------------------------------------------------- debounce

RefreshFn = Callable[[MediaServerConfig, list[str]], Awaitable[None]]
ErrorFn = Callable[[MediaServerConfig, Exception], Any]


class RefreshDebouncer:
    """Per-server debounce: ``add()`` folders as files land; one refresh runs ``delay`` seconds
    after the last ``add`` for that server (never later than ``max_delay`` after the first), with
    folders de-duplicated. Must be used from a running event loop.

    ``on_error(config, exc)`` (sync or async) receives refresh failures; otherwise they are logged.
    """

    def __init__(self, *, delay: float = 5.0, max_delay: float = 60.0, refresh: RefreshFn | None = None,
                 on_error: ErrorFn | None = None) -> None:  # fmt: skip
        self.delay = delay
        self.max_delay = max_delay
        self._refresh = refresh or refresh_paths
        self._on_error = on_error
        self._pending: dict[str, tuple[MediaServerConfig, list[str], float]] = {}
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._running: set[asyncio.Task[None]] = set()

    def add(self, config: MediaServerConfig, paths: list[str]) -> None:
        if not config.enabled or not paths:
            return
        loop = asyncio.get_running_loop()
        now = loop.time()
        _, known, first = self._pending.get(config.id, (config, [], now))
        merged = known + [p for p in paths if p and p not in known]
        self._pending[config.id] = (config, merged, first)
        timer = self._timers.pop(config.id, None)
        if timer is not None:
            timer.cancel()
        wait = max(0.0, min(self.delay, first + self.max_delay - now))
        self._timers[config.id] = loop.call_later(wait, self._fire, config.id)

    def _fire(self, server_id: str) -> None:
        self._timers.pop(server_id, None)
        entry = self._pending.pop(server_id, None)
        if entry is None:
            return
        config, paths, _ = entry
        task = asyncio.ensure_future(self._run(config, paths))
        self._running.add(task)
        task.add_done_callback(self._running.discard)

    async def _run(self, config: MediaServerConfig, paths: list[str]) -> None:
        try:
            await self._refresh(config, paths)
        except Exception as exc:
            if self._on_error is None:
                log.warning("media server refresh failed (%s): %s", config.name, exc)
                return
            try:
                outcome = self._on_error(config, exc)
                if inspect.isawaitable(outcome):
                    await outcome
            except Exception:
                log.exception("media server on_error callback failed")

    @property
    def pending(self) -> dict[str, list[str]]:
        return {server_id: list(entry[1]) for server_id, entry in self._pending.items()}

    async def flush(self) -> None:
        """Run every pending refresh now and wait for all refreshes to finish."""
        for server_id in list(self._timers):
            timer = self._timers.pop(server_id)
            timer.cancel()
            self._fire(server_id)
        if self._running:
            await asyncio.gather(*list(self._running), return_exceptions=True)

    async def aclose(self) -> None:
        """Cancel pending timers (dropping their refreshes) and wait for running ones."""
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        self._pending.clear()
        if self._running:
            with contextlib.suppress(Exception):
                await asyncio.gather(*list(self._running), return_exceptions=True)
