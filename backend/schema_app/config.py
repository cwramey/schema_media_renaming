"""Process configuration from the environment (SPEC §14).

Everything the operator sets with environment variables lives here. User-editable settings
(library roots, conflict policy, provider keys ...) live in the database instead; see
``services/settings_store.py``.
"""

from __future__ import annotations

import logging
import os
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from schema_app.domain import ProviderCredentials

log = logging.getLogger("schema.config")

_PACKAGE_DIR = Path(__file__).resolve().parent
# backend/schema_app -> repo root -> frontend/dist (the SPEC default "../frontend/dist" seen from backend/)
DEFAULT_STATIC_DIR = _PACKAGE_DIR.parent.parent / "frontend" / "dist"

SECRET_KEY_FILE = "secret.key"
DB_FILE = "schema.db"


class Config(BaseSettings):
    """``SCHEMA_*`` variables plus the provider credential variables that seed/override the DB."""

    model_config = SettingsConfigDict(env_prefix="SCHEMA_", extra="ignore", populate_by_name=True)

    data_dir: Path = Path("./data")
    host: str = "0.0.0.0"
    port: int = 8420
    static_dir: Path = DEFAULT_STATIC_DIR
    secret_key: str | None = None
    auth: Literal["required", "disabled"] = "required"
    demo: bool = False
    log_level: str = "info"
    # First-run admin creation needs this code (generated and printed to the log when unset), so
    # whoever reaches the port first on the LAN can't claim a fresh instance.
    setup_token: str | None = None
    # Comma-separated host names the server answers to (e.g. "nas.lan,192.168.1.20,schema.example.com").
    # Unset accepts any Host; set it to block DNS-rebinding pages from reaching the API. Required when
    # SCHEMA_AUTH=disabled (see ``startup_problem``).
    allowed_hosts: str | None = None
    # Explicit opt-out of that requirement: serve with auth disabled to any Host header. Any website
    # the user visits can then drive the API through DNS rebinding.
    allow_any_host: bool = False
    # Extra browser origins allowed to open the WebSocket, e.g. "https://schema.example.com" when a
    # reverse proxy rewrites the Host header.
    allowed_origins: str | None = None
    # Interactive OpenAPI docs at /api/docs (off by default: they map the whole API unauthenticated).
    api_docs: bool = False

    # Provider credentials (no SCHEMA_ prefix). When set they override what is stored in the DB.
    tmdb_api_key: str | None = Field(default=None, validation_alias=AliasChoices("TMDB_API_KEY", "tmdb_api_key"))
    tvdb_api_key: str | None = Field(default=None, validation_alias=AliasChoices("TVDB_API_KEY", "tvdb_api_key"))
    tvdb_pin: str | None = Field(default=None, validation_alias=AliasChoices("TVDB_PIN", "tvdb_pin"))
    anidb_client: str | None = Field(default=None, validation_alias=AliasChoices("ANIDB_CLIENT", "anidb_client"))
    anidb_client_version: int | None = Field(
        default=None, validation_alias=AliasChoices("ANIDB_CLIENT_VERSION", "anidb_client_version")
    )
    trakt_client_id: str | None = Field(
        default=None, validation_alias=AliasChoices("TRAKT_CLIENT_ID", "trakt_client_id")
    )

    @field_validator("auth", mode="before")
    @classmethod
    def _normalize_auth(cls, value: object) -> object:
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"0", "false", "off", "no", "none", "disable"}:
                return "disabled"
            if lowered in {"1", "true", "on", "yes", "enabled", "enable"}:
                return "required"
            return lowered
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_level(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator(
        "tmdb_api_key", "tvdb_api_key", "tvdb_pin", "anidb_client", "trakt_client_id", "secret_key", "setup_token",
        "allowed_hosts", "allowed_origins", mode="before"
    )
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("anidb_client_version", mode="before")
    @classmethod
    def _blank_int_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    # ------------------------------------------------------------------ derived paths

    @property
    def allowed_host_list(self) -> list[str]:
        return [h.strip().lower() for h in (self.allowed_hosts or "").split(",") if h.strip()]

    @property
    def allowed_origin_list(self) -> list[str]:
        return [o.strip().rstrip("/").lower() for o in (self.allowed_origins or "").split(",") if o.strip()]

    @property
    def auth_enabled(self) -> bool:
        return self.auth != "disabled"

    def startup_problem(self) -> str | None:
        """Why this configuration must not serve, or None.

        With sign-in disabled the only thing between a malicious web page and the API is the Host
        header: a DNS-rebinding page reaches the server under its own host name and the browser
        happily lets it read every response (settings backups with secrets included)."""
        if not self.auth_enabled and not self.allowed_host_list and not self.allow_any_host:
            return (
                "SCHEMA_AUTH=disabled needs SCHEMA_ALLOWED_HOSTS, the comma-separated host names or IPs you "
                "open Schema with (e.g. SCHEMA_ALLOWED_HOSTS=192.168.1.20,nas.lan). Without it any website "
                "can reach the unauthenticated API through DNS rebinding. Set SCHEMA_ALLOW_ANY_HOST=true to "
                "accept that risk anyway."
            )
        return None

    @property
    def db_path(self) -> Path:
        return self.data_dir / DB_FILE

    @property
    def trash_dir(self) -> Path:
        return self.data_dir / "trash"

    @property
    def demo_dir(self) -> Path:
        return self.data_dir / "demo"

    def env_credentials(self) -> ProviderCredentials:
        """Provider credentials supplied through the environment (unset fields are None)."""
        return ProviderCredentials(
            tmdb_api_key=self.tmdb_api_key,
            tvdb_api_key=self.tvdb_api_key,
            tvdb_pin=self.tvdb_pin,
            anidb_client=self.anidb_client,
            anidb_client_version=self.anidb_client_version,
            trakt_client_id=self.trakt_client_id,
        )


@lru_cache(maxsize=1)
def get_config() -> Config:
    return Config()


class StartupError(RuntimeError):
    """The configuration is unsafe or unusable; the message says what to change."""


def ensure_data_dir(config: Config) -> Path:
    """Create the data directory (and its trash folder) if needed; returns the absolute path."""
    data_dir = config.data_dir.expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "trash").mkdir(exist_ok=True)
    config.data_dir = data_dir
    return data_dir


def load_secret_key(config: Config) -> str:
    """``SCHEMA_SECRET_KEY`` if set, else ``<data_dir>/secret.key`` (generated once, mode 0600)."""
    if config.secret_key:
        return config.secret_key
    path = config.data_dir / SECRET_KEY_FILE
    if path.is_file():
        key = path.read_text(encoding="utf-8").strip()
        if key:
            return key
    key = secrets.token_urlsafe(48)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, key.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - e.g. some network filesystems
        log.warning("could not restrict permissions on %s", path)
    return key
