"""Small helpers shared by the HTTP-backed providers."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, ClassVar
from urllib.parse import urlsplit, urlunsplit

import httpx

from schema_app.domain import EpisodeInfo, ProviderCredentials
from schema_app.providers.base import MetadataProvider
from schema_app.providers.cache import ResponseCache
from schema_app.providers.http import HttpClient, Limiter, Sleep

ISO2_TO_ISO3 = {
    "en": "eng", "de": "deu", "fr": "fra", "es": "spa", "it": "ita", "ja": "jpn", "pt": "por",
    "nl": "nld", "ru": "rus", "zh": "zho", "ko": "kor", "sv": "swe", "da": "dan", "fi": "fin",
    "no": "nor", "pl": "pol", "cs": "ces", "hu": "hun", "tr": "tur", "he": "heb", "ar": "ara",
    "el": "ell", "uk": "ukr", "ro": "ron", "th": "tha", "vi": "vie", "id": "ind", "hi": "hin",
}

_YEAR_SUFFIX = re.compile(r"^(?P<title>.*?)\s*\((?P<year>(?:19|20)\d{2})\)\s*$")

# Poster hosts the UI may load images from (keep in step with the CSP ``img-src``).
IMAGE_HOSTS = frozenset({"image.tmdb.org", "artworks.thetvdb.com", "static.tvmaze.com"})
_URL_JUNK = re.compile(r"[\x00-\x20\x7f\"'<>\\`]")


def safe_image_url(url: Any) -> str | None:
    """``url`` when it is an https URL on one of ``IMAGE_HOSTS`` (default port, no user info),
    else None: provider data is passed to the browser, so anything else is dropped here."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url or _URL_JUNK.search(url):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or parts.username or parts.password or port not in (None, 443):
        return None
    return url if (parts.hostname or "").lower() in IMAGE_HOSTS else None


def tvdb_thumbnail(url: Any) -> str | None:
    """TVDB's small variant of an artwork URL: ".../banners/posters/81189-10.jpg" ->
    ".../banners/posters/81189-10_t.jpg" (~70 KB instead of ~450 KB; v4 artwork uses the same
    "_t" suffix). The old "/banners/_cache/..." thumbnails answer 403 now and are rewritten the
    same way. Validated like ``safe_image_url``."""
    url = safe_image_url(url)
    if url is None:
        return None
    parts = urlsplit(url)
    path = parts.path
    if not path.startswith("/banners/"):
        return url
    if path.startswith("/banners/_cache/"):
        path = "/banners/" + path[len("/banners/_cache/"):]
    stem, dot, ext = path.rpartition(".")
    if dot and "/" not in ext and not stem.endswith("_t"):
        path = f"{stem}_t.{ext}"
    return urlunsplit(parts._replace(path=path))


def parse_date(raw: Any) -> date | None:
    if not raw:
        return None
    text = str(raw).strip()
    try:
        if "T" in text:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def year_of(*values: Any) -> int | None:
    for v in values:
        if isinstance(v, int) and 1800 < v < 2200:
            return v
        if isinstance(v, str) and re.match(r"^\d{4}", v):
            y = int(v[:4])
            if 1800 < y < 2200:
                return y
        if isinstance(v, date):
            return v.year
    return None


def split_title_year(title: str) -> tuple[str, int | None]:
    """'Harbor Lights (2021)' -> ('Harbor Lights', 2021) (TVDB disambiguation suffix)."""
    m = _YEAR_SUFFIX.match(title or "")
    if m and m.group("title"):
        return m.group("title"), int(m.group("year"))
    return title, None


def fill_absolute(episodes: list[EpisodeInfo]) -> list[EpisodeInfo]:
    """Fill missing ``absolute`` numbers cumulatively over regular seasons (SPEC §6.4 rule b).

    Provider-supplied absolute numbers are kept; only gaps are filled, and only when the
    provider gave none at all (mixing native and derived numbering would be misleading).
    """
    regular = [e for e in episodes if e.season and e.season > 0 and e.episode is not None]
    if any(e.absolute for e in regular):
        return episodes
    regular.sort(key=lambda e: (e.season or 0, e.episode or 0))
    for n, e in enumerate(regular, start=1):
        e.absolute = n
    return episodes


def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if not v:
            continue
        key = v.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(v.strip())
    return out


class HttpProvider(MetadataProvider):
    """Base for providers that talk HTTP: owns HttpClients and exposes their counters."""

    default_rate: ClassVar[float] = 5.0
    default_burst: ClassVar[float] = 5.0

    def __init__(
        self,
        credentials: ProviderCredentials,
        *,
        language: str = "en",
        cache: ResponseCache | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep | None = None,
        **_: Any,
    ) -> None:
        super().__init__(credentials, language=language)
        self.cache = cache
        self.transport = transport
        self.sleep = sleep
        self._clients: list[HttpClient] = []
        self.status_error: str | None = None  # persistent condition worth showing (ban, quota)

    def make_client(self, base_url: str = "", *, limiter: Limiter | None = None, **kwargs: Any) -> HttpClient:
        from schema_app.providers.http import TokenBucket

        if limiter is None:
            limiter = TokenBucket(self.default_rate, self.default_burst, sleep=self.sleep)
        client = HttpClient(
            self.name, base_url=base_url, limiter=limiter, cache=self.cache, transport=self.transport,
            sleep=self.sleep, **kwargs,
        )
        self._clients.append(client)
        return client

    @property
    def requests_made(self) -> int:
        return sum(c.requests_made for c in self._clients)

    @property
    def cache_hits(self) -> int:
        return sum(c.cache_hits for c in self._clients)

    async def aclose(self) -> None:
        for c in self._clients:
            await c.aclose()
