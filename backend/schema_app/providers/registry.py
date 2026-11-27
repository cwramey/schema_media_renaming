"""Provider registry: builds providers from ``ProviderSettings`` and serves fallback chains.

``chain(kind)`` = providers listed in ``settings.priority[kind]`` that exist, are not
disabled, are configured and support the kind, in priority order. Demo mode prepends
the offline ``fixtures`` provider to every chain (SPEC §6.2).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from schema_app.domain import MediaKind, ProviderSettings, ProviderStatus, default_priority
from schema_app.providers.anidb import AnidbProvider
from schema_app.providers.animelists import AnimeLists
from schema_app.providers.base import MetadataProvider, ProviderError, ProviderNotConfigured
from schema_app.providers.cache import ResponseCache
from schema_app.providers.fixtures import FixturesProvider
from schema_app.providers.http import Sleep
from schema_app.providers.tmdb import TmdbProvider
from schema_app.providers.trakt import TraktProvider
from schema_app.providers.tvdb import TvdbProvider
from schema_app.providers.tvmaze import TvmazeProvider

log = logging.getLogger("schema.providers")

PROVIDER_CLASSES: dict[str, type[MetadataProvider]] = {
    "tmdb": TmdbProvider,
    "tvdb": TvdbProvider,
    "anidb": AnidbProvider,
    "trakt": TraktProvider,
    "tvmaze": TvmazeProvider,
}
_KIND_ORDER = (MediaKind.MOVIE, MediaKind.SERIES, MediaKind.ANIME)


@dataclass
class _Health:
    healthy: bool | None = None
    last_error: str | None = None
    last_checked: datetime | None = None
    carried_requests: int = 0
    carried_hits: int = 0


class ProviderRegistry:
    def __init__(
        self,
        settings: ProviderSettings,
        *,
        cache: ResponseCache | None = None,
        demo: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep | None = None,
    ) -> None:
        self.settings = settings
        self.cache = cache
        self.demo = demo
        self.transport = transport
        self.sleep = sleep
        self.animelists = AnimeLists(cache=cache, transport=transport, sleep=sleep)
        self.providers: dict[str, MetadataProvider] = {}
        self._health: dict[str, _Health] = {}
        self._build()

    # ------------------------------------------------------------------ construction

    def _mapping_target(self) -> str:
        """Which TV database AniDB episodes are mapped onto (first of tmdb/tvdb in anime priority)."""
        order = self._priority(MediaKind.ANIME)
        for name in order:
            if name in ("tmdb", "tvdb"):
                return name
        return "tvdb"

    def _build(self) -> None:
        creds = self.settings.credentials
        lang = self.settings.language or "en"
        common = {"language": lang, "cache": self.cache, "transport": self.transport, "sleep": self.sleep}
        for name, cls in PROVIDER_CLASSES.items():
            extra = {}
            if cls is AnidbProvider:
                extra = {"animelists": self.animelists, "mapping_target": self._mapping_target()}
            self.providers[name] = cls(creds, **common, **extra)  # type: ignore[arg-type]
            self._health.setdefault(name, _Health())
        if self.demo:
            self.providers["fixtures"] = FixturesProvider(creds, language=lang)
            self._health.setdefault("fixtures", _Health())

    def _priority(self, kind: MediaKind) -> list[str]:
        prio = self.settings.priority or {}
        order = prio.get(kind)
        if order is None:
            order = default_priority().get(kind, [])
        return list(dict.fromkeys(order))

    # ------------------------------------------------------------------ queries

    def get(self, name: str) -> MetadataProvider | None:
        return self.providers.get(name)

    def is_enabled(self, name: str) -> bool:
        return name not in set(self.settings.disabled or [])

    def available(self, name: str, kind: MediaKind | None = None) -> bool:
        p = self.providers.get(name)
        if p is None or not self.is_enabled(name):
            return False
        try:
            if not p.is_configured():
                return False
        except ProviderNotConfigured:
            return False
        return kind is None or kind == MediaKind.UNKNOWN or kind in p.kinds

    def chain(self, kind: MediaKind) -> list[MetadataProvider]:
        if kind == MediaKind.UNKNOWN:
            seen: dict[str, MetadataProvider] = {}
            for k in (MediaKind.MOVIE, MediaKind.SERIES):
                for p in self.chain(k):
                    seen.setdefault(p.name, p)
            return list(seen.values())
        out: list[MetadataProvider] = []
        if self.demo and "fixtures" in self.providers and self.is_enabled("fixtures"):
            out.append(self.providers["fixtures"])
        for name in self._priority(kind):
            if name == "fixtures":
                continue
            provider = self.providers.get(name)
            if self.demo and provider is not None and not provider.requires_key:
                # demo mode stays offline unless the user supplied real credentials:
                # keyless network providers (TVmaze) are left out of the chains
                continue
            if self.available(name, kind):
                out.append(self.providers[name])
        return out

    # ------------------------------------------------------------------ health / status

    def record_error(self, name: str, error: Exception) -> None:
        h = self._health.setdefault(name, _Health())
        h.last_error = str(error)
        h.healthy = False
        h.last_checked = datetime.now(UTC)

    def record_success(self, name: str) -> None:
        h = self._health.setdefault(name, _Health())
        if h.healthy is False:
            h.healthy = True
            h.last_error = None

    def status(self, name: str) -> ProviderStatus:
        p = self.providers[name]
        h = self._health.setdefault(name, _Health())
        configured = False
        try:
            configured = bool(p.is_configured())
        except ProviderError:
            configured = False
        persistent = getattr(p, "status_error", None)
        return ProviderStatus(
            name=name,
            label=p.label,
            kinds=[k for k in _KIND_ORDER if k in p.kinds],
            requires_key=p.requires_key,
            configured=configured,
            enabled=self.is_enabled(name),
            healthy=False if persistent else h.healthy,
            last_error=persistent or h.last_error,
            last_checked=h.last_checked,
            rate_limit=p.rate_limit_label,
            requests_made=h.carried_requests + int(getattr(p, "requests_made", 0) or 0),
            cache_hits=h.carried_hits + int(getattr(p, "cache_hits", 0) or 0),
        )

    def statuses(self) -> list[ProviderStatus]:
        names = (["fixtures"] if "fixtures" in self.providers else []) + list(PROVIDER_CLASSES)
        return [self.status(n) for n in names if n in self.providers]

    async def test(self, name: str) -> ProviderStatus:
        """Run the provider's healthcheck. Raises ``KeyError`` for an unknown provider name."""
        if name not in self.providers:
            raise KeyError(name)
        p = self.providers[name]
        h = self._health.setdefault(name, _Health())
        h.last_checked = datetime.now(UTC)
        try:
            if not p.is_configured():
                raise ProviderNotConfigured(f"{name}: credentials missing")
            await p.healthcheck()
        except ProviderError as exc:
            h.healthy = False
            h.last_error = str(exc)
        except Exception as exc:  # a broken provider must not break the settings page
            log.exception("provider %s healthcheck crashed", name)
            h.healthy = False
            h.last_error = f"{type(exc).__name__}: {exc}"
        else:
            h.healthy = True
            h.last_error = None
        return self.status(name)

    # ------------------------------------------------------------------ lifecycle

    async def reconfigure(self, settings: ProviderSettings) -> None:
        old = self.providers
        for name, p in old.items():
            h = self._health.setdefault(name, _Health())
            h.carried_requests += int(getattr(p, "requests_made", 0) or 0)
            h.carried_hits += int(getattr(p, "cache_hits", 0) or 0)
        self.settings = settings
        self.providers = {}
        self._build()
        for name, p in old.items():
            try:
                await p.aclose()
            except Exception:  # pragma: no cover - best effort
                log.debug("closing %s failed", name, exc_info=True)
        # credentials may have changed: forget stale health verdicts
        for h in self._health.values():
            h.healthy = None
            h.last_error = None

    async def aclose(self) -> None:
        for p in self.providers.values():
            try:
                await p.aclose()
            except Exception:  # pragma: no cover - best effort
                log.debug("closing %s failed", p.name, exc_info=True)
        await self.animelists.aclose()
        if self.cache is not None:
            self.cache.close()
