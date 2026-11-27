"""Typed persistence for user settings (``AppSettings``, ``ProviderSettings``) in the ``settings`` table.

* Defaults are seeded on first run.
* Provider credentials from the environment (``TMDB_API_KEY`` ...) override stored values.
* Secrets (provider keys, media-server tokens, the webhook API key) are masked with
  ``SECRET_MASK`` in API responses; writing ``SECRET_MASK`` back keeps the stored secret.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select

from schema_app.api_models import SECRET_MASK, AppSettings, ProvidersIn
from schema_app.config import Config
from schema_app.db import Database
from schema_app.domain import MediaKind, MediaServerConfig, ProviderCredentials, ProviderSettings
from schema_app.models import Setting, WatchFolder
from schema_app.services import fileops
from schema_app.services import paths as paths_mod

log = logging.getLogger(__name__)

KEY_APP = "app"
KEY_PROVIDERS = "providers"
KEY_API_KEY = "webhook_api_key"
KEY_FORMATS_SEEDED = "formats_seeded"
KEY_DEMO_SEEDED = "demo_seeded"

SECRET_CREDENTIALS = ("tmdb_api_key", "tvdb_api_key", "tvdb_pin", "trakt_client_id")
CREDENTIAL_FIELDS = tuple(ProviderCredentials.model_fields)


def same_server_url(a: str | None, b: str | None) -> bool:
    return (a or "").strip().rstrip("/").lower() == (b or "").strip().rstrip("/").lower()


class SettingsError(ValueError):
    """Invalid settings payload (mapped to HTTP 400)."""


def _mask(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return SECRET_MASK


def _is_absolute(path: str) -> bool:
    # Accept POSIX and Windows absolute forms regardless of the host OS.
    return Path(path).is_absolute() or path.startswith(("/", "\\")) or (len(path) > 2 and path[1] == ":")


def check_root(what: str, root: str) -> None:
    """Raise SettingsError when ``root`` can't be used as a root (``what`` names it in the message):
    "/" or anything that is, holds or sits inside Schema's data folder (``paths.root_problem``)."""
    problem = paths_mod.root_problem(root)
    if problem:
        raise SettingsError(f"{what} {root!r} can't be used: {problem}")


def validate_roots(app: AppSettings) -> None:
    """Allowed and library roots of ``app``: absolute, and see :func:`check_root`."""
    for root in app.allowed_roots:
        if not root.strip() or not _is_absolute(root.strip()):
            raise SettingsError(f"allowed root must be an absolute path: {root!r}")
        check_root("allowed root", root.strip())
    for kind, root in app.library_roots.items():
        if not root or not root.strip():
            continue
        if not _is_absolute(root.strip()):
            raise SettingsError(f"library root for {kind} must be an absolute path: {root!r}")
        check_root(f"library root for {kind}", root.strip())


def _related(a: str, b: str) -> bool:
    a, b = os.path.normcase(os.path.abspath(a)), os.path.normcase(os.path.abspath(b))
    return a == b or a.startswith(b.rstrip(os.sep) + os.sep) or b.startswith(a.rstrip(os.sep) + os.sep)


class SettingsStore:
    def __init__(self, db: Database, config: Config) -> None:
        self.db = db
        self.config = config
        self._lock = threading.RLock()
        self._app: AppSettings | None = None
        self._providers: ProviderSettings | None = None
        self._api_key: str | None = None
        self._cross_mount_warned: set[tuple[str, str]] = set()
        # Schema's own folder (secret.key, schema.db, the trash) is never a source, destination or
        # browsable folder, whatever the roots say; the generated demo library inside it stays usable.
        paths_mod.protect([config.data_dir], exempt=[config.demo_dir] if config.demo else [])
        self._warn_stored_roots()
        self.warn_cross_mounts()

    def _warn_stored_roots(self) -> None:
        """Roots saved before they were checked ("/", or the folder holding /config) keep working for
        everything else, but say why the data folder inside them stays out of reach."""
        try:
            app = self.get_app()
        except Exception:  # no settings table yet
            return
        for what, root in [("allowed root", r) for r in app.allowed_roots] + [
            (f"library root for {kind}", r) for kind, r in app.library_roots.items()
        ]:
            problem = paths_mod.root_problem(root) if root and root.strip() else None
            if problem:
                log.warning("The %s %s is unsafe (%s); Schema's data folder inside it stays off limits. "
                            "Narrow it in Settings.", what, root, problem)  # fmt: skip

    # ------------------------------------------------------------------ raw key/value

    def get_value(self, key: str, default: Any = None) -> Any:
        with self.db.session() as session:
            row = session.get(Setting, key)
            return default if row is None else row.value

    def set_value(self, key: str, value: Any) -> None:
        with self.db.session() as session:
            row = session.get(Setting, key)
            if row is None:
                session.add(Setting(key=key, value=value))
            else:
                row.value = value

    # ------------------------------------------------------------------ seeding

    def seed(self) -> None:
        """Store defaults for anything missing (first run) and warm the caches."""
        with self._lock:
            if self.get_value(KEY_APP) is None:
                self.set_value(KEY_APP, AppSettings().model_dump(mode="json", exclude={"webhook_api_key"}))
            if self.get_value(KEY_PROVIDERS) is None:
                self.set_value(KEY_PROVIDERS, ProviderSettings().model_dump(mode="json"))
            if not self.get_value(KEY_API_KEY):
                self.set_value(KEY_API_KEY, secrets.token_urlsafe(24))
            self.invalidate()

    def invalidate(self) -> None:
        with self._lock:
            self._app = None
            self._providers = None
            self._api_key = None

    # ------------------------------------------------------------------ app settings

    def get_app(self) -> AppSettings:
        """Effective settings with real secrets (internal use). ``webhook_api_key`` is always masked."""
        with self._lock:
            if self._app is None:
                raw = self.get_value(KEY_APP) or {}
                raw = {k: v for k, v in raw.items() if k != "webhook_api_key"}
                self._app = AppSettings.model_validate(raw)
            return self._app.model_copy(deep=True)

    def get_app_masked(self) -> AppSettings:
        app = self.get_app()
        app.media_servers = [server.model_copy(update={"token": _mask(server.token) or ""}) for server in app.media_servers]
        app.webhook_api_key = SECRET_MASK
        return app

    def put_app(self, incoming: AppSettings) -> AppSettings:
        """Replace the stored settings. Masked secrets keep their stored values. Returns masked."""
        with self._lock:
            current = self.get_app()
            stored = {server.id: server for server in current.media_servers}
            servers: list[MediaServerConfig] = []
            for server in incoming.media_servers:
                server = server.model_copy()
                if not server.id:
                    server.id = uuid.uuid4().hex
                if server.token == SECRET_MASK:
                    if server.id not in stored:
                        raise SettingsError(f"media server {server.name!r}: token is required")
                    if not same_server_url(stored[server.id].url, server.url):
                        # Otherwise anyone who can edit settings could re-point a saved server at
                        # their own host and receive its token without ever seeing it.
                        raise SettingsError(f"media server {server.name!r}: re-enter the token after changing the URL")
                    server.token = stored[server.id].token
                if server.kind not in {"plex", "jellyfin", "emby"}:
                    raise SettingsError(f"media server {server.name!r}: unknown kind {server.kind!r}")
                servers.append(server)
            validate_roots(incoming)
            updated = incoming.model_copy(
                update={
                    "media_servers": servers,
                    "allowed_roots": [r.strip() for r in incoming.allowed_roots if r.strip()],
                    "library_roots": {k: v.strip() for k, v in incoming.library_roots.items() if v and v.strip()},
                    "video_extensions": [e.lower().lstrip(".") for e in incoming.video_extensions if e.strip()],
                    "sidecar_extensions": [e.lower().lstrip(".") for e in incoming.sidecar_extensions if e.strip()],
                    "webhook_api_key": SECRET_MASK,
                }
            )
            self.set_value(KEY_APP, updated.model_dump(mode="json", exclude={"webhook_api_key"}))
            self._app = None
            masked = self.get_app_masked()
        self.warn_cross_mounts()
        return masked

    def update_app(self, **changes: Any) -> AppSettings:
        """Patch selected fields (internal use, e.g. demo bootstrap). Returns the new settings."""
        with self._lock:
            current = self.get_app()
            updated = current.model_copy(update=changes)
            self.set_value(KEY_APP, AppSettings.model_validate(updated.model_dump()).model_dump(
                mode="json", exclude={"webhook_api_key"}
            ))
            self._app = None
            return self.get_app()

    # ------------------------------------------------------------------ webhook API key

    def get_api_key(self) -> str:
        with self._lock:
            if self._api_key is None:
                key = self.get_value(KEY_API_KEY)
                if not key:
                    key = secrets.token_urlsafe(24)
                    self.set_value(KEY_API_KEY, key)
                self._api_key = str(key)
            return self._api_key

    def set_api_key(self, key: str) -> None:
        """Restore a known webhook key (settings restore); download clients keep working."""
        key = key.strip()
        if len(key) < 16:
            raise SettingsError("webhook API key is too short")
        with self._lock:
            self.set_value(KEY_API_KEY, key)
            self._api_key = key

    def rotate_api_key(self) -> str:
        with self._lock:
            key = secrets.token_urlsafe(24)
            self.set_value(KEY_API_KEY, key)
            self._api_key = key
            return key

    # ------------------------------------------------------------------ providers

    def _stored_providers(self) -> ProviderSettings:
        raw = self.get_value(KEY_PROVIDERS) or {}
        return ProviderSettings.model_validate(raw)

    def stored_providers(self) -> ProviderSettings:
        """What is saved in the database (without environment-variable credentials)."""
        with self._lock:
            return self._stored_providers()

    def get_providers(self) -> ProviderSettings:
        """Effective provider settings: stored values overlaid with environment credentials."""
        with self._lock:
            if self._providers is None:
                stored = self._stored_providers()
                env = self.config.env_credentials()
                overlay = {k: v for k, v in env.model_dump().items() if v is not None}
                stored.credentials = stored.credentials.model_copy(update=overlay)
                self._providers = stored
            return self._providers.model_copy(deep=True)

    def env_overridden(self) -> set[str]:
        return {k for k, v in self.config.env_credentials().model_dump().items() if v is not None}

    def put_providers(self, incoming: ProvidersIn) -> ProviderSettings:
        """Partial update. Unknown credential names -> SettingsError. Returns effective settings."""
        with self._lock:
            stored = self._stored_providers()
            if incoming.priority is not None:
                priority: dict[MediaKind, list[str]] = dict(stored.priority)
                for kind, names in incoming.priority.items():
                    seen: list[str] = []
                    for name in names:
                        name = name.strip().lower()
                        if name and name not in seen:
                            seen.append(name)
                    priority[kind] = seen
                stored.priority = priority
            if incoming.disabled is not None:
                stored.disabled = sorted({name.strip().lower() for name in incoming.disabled if name.strip()})
            if incoming.language is not None:
                language = incoming.language.strip()
                if not language:
                    raise SettingsError("language must not be empty")
                stored.language = language
            if incoming.credentials is not None:
                creds = stored.credentials.model_dump()
                for name, value in incoming.credentials.items():
                    if name not in CREDENTIAL_FIELDS:
                        raise SettingsError(f"unknown credential {name!r}")
                    if value == SECRET_MASK:
                        continue
                    if value is None or (isinstance(value, str) and not value.strip()):
                        creds[name] = None
                    elif name == "anidb_client_version":
                        try:
                            creds[name] = int(value)
                        except (TypeError, ValueError) as exc:
                            raise SettingsError("anidb_client_version must be an integer") from exc
                    else:
                        creds[name] = value.strip()
                stored.credentials = ProviderCredentials.model_validate(creds)
            self.set_value(KEY_PROVIDERS, stored.model_dump(mode="json"))
            self._providers = None
            return self.get_providers()

    def masked_credentials(self) -> dict[str, str | None]:
        creds = self.get_providers().credentials.model_dump()
        out: dict[str, str | None] = {}
        for name in CREDENTIAL_FIELDS:
            value = creds.get(name)
            if name in SECRET_CREDENTIALS:
                out[name] = _mask(value)
            else:
                out[name] = None if value is None else str(value)
        return out

    # ------------------------------------------------------------------ cross-mount check

    def move_pairs(self) -> list[tuple[str, str]]:
        """(download side, library side) folder pairs that file moves connect: watch folders, path
        mapping targets and allowed roots unrelated to any library root, against the library roots
        and the watch folders' destination roots."""
        app = self.get_app()
        with self.db.session() as session:
            folders = [(row.path, row.dest_root) for row in session.scalars(select(WatchFolder))]
        libraries = [r for r in app.library_roots.values() if r and r.strip()]
        libraries += [dest for _, dest in folders if dest and dest.strip()]
        sources = [path for path, _ in folders]
        sources += [m.target for m in app.path_mappings if m.target and _is_absolute(m.target)]
        sources += [r for r in app.allowed_roots if not any(_related(r, lib) for lib in libraries)]
        pairs: list[tuple[str, str]] = []
        for src in dict.fromkeys(s.strip() for s in sources if s and s.strip()):
            for dst in dict.fromkeys(d.strip() for d in libraries):
                if not _related(src, dst):
                    pairs.append((src, dst))
        return pairs

    def warn_cross_mounts(self) -> list[str]:
        """Log a warning for each download/watch -> library pair where a move can't be a rename
        (separate mounts, e.g. two Docker volumes of one disk: every move copies the whole file).
        Called at startup and whenever roots change; each pair is reported once. Never raises.
        Returns the new warnings."""
        try:
            pairs = self.move_pairs()
        except Exception as exc:  # the settings table may not exist yet (bare test stores)
            log.debug("cross-mount check skipped: %s", exc)
            return []
        warnings: list[str] = []
        for src, dst in pairs[:64]:
            if (src, dst) in self._cross_mount_warned:
                continue
            try:
                reason = fileops.cross_mount(src, dst)
            except Exception as exc:  # pragma: no cover - best effort
                log.debug("cross-mount check failed for %s -> %s: %s", src, dst, exc)
                continue
            if not reason:
                continue
            self._cross_mount_warned.add((src, dst))
            message = (
                f"Moving files from {src} to {dst} can't be a quick rename: {reason}. Every move "
                "between them copies the whole file. Mount one common parent folder into the "
                "container (e.g. a single /media volume) so moves are instant."
            )
            log.warning("%s", message)
            warnings.append(message)
        return warnings

    # ------------------------------------------------------------------ helpers

    def media_server(self, server_id: str) -> MediaServerConfig | None:
        for server in self.get_app().media_servers:
            if server.id == server_id:
                return server
        return None
