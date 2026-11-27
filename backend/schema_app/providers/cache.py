"""Provider response cache: stdlib sqlite3 key/value store with per-entry TTL + in-memory LRU.

One file (``<cache_dir>/cache.sqlite3``) is shared by every provider. Keys are opaque
strings built by ``providers.http`` (provider, URL, sorted params, language; never API
keys). Values are the raw response bytes, so a cached entry is byte-for-byte what the
provider sent. Disk work runs in a worker thread from async code.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("schema.providers.cache")

HOUR = 3600.0
DAY = 24 * HOUR

# SPEC §6.2 TTLs
TTL_SEARCH = 24 * HOUR
TTL_DETAILS = 7 * DAY
TTL_EPISODES = 24 * HOUR
TTL_ANIDB = 7 * DAY
TTL_TITLES_DUMP = 24 * HOUR
TTL_ANIME_LISTS = 24 * HOUR
TTL_TOKEN = 25 * DAY

CACHE_FILENAME = "cache.sqlite3"


class ResponseCache:
    """Thread-safe TTL cache. ``path=None`` keeps everything in memory (tests, demo)."""

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        memory_items: int = 2048,
        memory_max_bytes: int = 64 * 1024 * 1024,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path) if path is not None else None
        self._clock = clock
        self._lock = threading.Lock()
        self._mem: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self._mem_items = memory_items
        self._mem_max_bytes = memory_max_bytes
        self._mem_bytes = 0
        self.hits = 0
        self.misses = 0
        self._conn: sqlite3.Connection | None = None
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None,
                                       timeout=10.0)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS http_cache ("
                    " key TEXT PRIMARY KEY, value BLOB NOT NULL, expires REAL NOT NULL, stored REAL NOT NULL)"
                )
                conn.execute("CREATE INDEX IF NOT EXISTS http_cache_expires ON http_cache(expires)")
                conn.execute("PRAGMA journal_size_limit=67108864")
                # Holds provider session tokens (TVDB bearer): owner-only, whatever UMASK is.
                for suffix in ("", "-wal", "-shm"):
                    with contextlib.suppress(OSError):
                        os.chmod(f"{self.path}{suffix}", 0o600)
                self._conn = conn
            except (sqlite3.Error, OSError) as exc:  # unusable cache must never break matching
                log.warning("response cache disabled (%s): %s", self.path, exc)
                self._conn = None

    # ------------------------------------------------------------------ memory layer

    def _mem_get(self, key: str, now: float) -> bytes | None:
        item = self._mem.get(key)
        if item is None:
            return None
        expires, value = item
        if expires <= now:
            self._mem_drop(key)
            return None
        self._mem.move_to_end(key)
        return value

    def _mem_put(self, key: str, value: bytes, expires: float) -> None:
        if len(value) > self._mem_max_bytes // 8:
            return  # huge blobs (title dumps) stay on disk only
        self._mem_drop(key)
        self._mem[key] = (expires, value)
        self._mem_bytes += len(value)
        while len(self._mem) > self._mem_items or self._mem_bytes > self._mem_max_bytes:
            _k, (_e, v) = self._mem.popitem(last=False)
            self._mem_bytes -= len(v)

    def _mem_drop(self, key: str) -> None:
        old = self._mem.pop(key, None)
        if old is not None:
            self._mem_bytes -= len(old[1])

    # ------------------------------------------------------------------ sync API

    def get(self, key: str) -> bytes | None:
        now = self._clock()
        with self._lock:
            value = self._mem_get(key, now)
            if value is not None:
                self.hits += 1
                return value
            if self._conn is None:
                self.misses += 1
                return None
            try:
                row = self._conn.execute(
                    "SELECT value, expires FROM http_cache WHERE key = ?", (key,)
                ).fetchone()
            except sqlite3.Error as exc:
                log.debug("cache read failed: %s", exc)
                row = None
            if row is None or row[1] <= now:
                self.misses += 1
                return None
            value = bytes(row[0])
            self._mem_put(key, value, row[1])
            self.hits += 1
            return value

    def set(self, key: str, value: bytes, ttl: float) -> None:
        now = self._clock()
        expires = now + max(0.0, ttl)
        with self._lock:
            self._mem_put(key, value, expires)
            if self._conn is None:
                return
            try:
                self._conn.execute(
                    "INSERT OR REPLACE INTO http_cache (key, value, expires, stored) VALUES (?, ?, ?, ?)",
                    (key, sqlite3.Binary(value), expires, now),
                )
            except sqlite3.Error as exc:
                log.debug("cache write failed: %s", exc)

    def delete(self, key: str) -> None:
        with self._lock:
            self._mem_drop(key)
            if self._conn is not None:
                try:
                    self._conn.execute("DELETE FROM http_cache WHERE key = ?", (key,))
                except sqlite3.Error as exc:
                    log.debug("cache delete failed: %s", exc)

    def delete_prefix(self, prefix: str) -> int:
        """Drop every key starting with ``prefix`` (per-provider purge)."""
        with self._lock:
            for k in [k for k in self._mem if k.startswith(prefix)]:
                self._mem_drop(k)
            if self._conn is None:
                return 0
            escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            cur = self._conn.execute("DELETE FROM http_cache WHERE key LIKE ? ESCAPE '\\'", (escaped + "%",))
            return cur.rowcount or 0

    def purge_expired(self) -> int:
        now = self._clock()
        with self._lock:
            for k in [k for k, (e, _v) in self._mem.items() if e <= now]:
                self._mem_drop(k)
            if self._conn is None:
                return 0
            cur = self._conn.execute("DELETE FROM http_cache WHERE expires <= ?", (now,))
            return cur.rowcount or 0

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    # ------------------------------------------------------------------ async wrappers

    async def aget(self, key: str) -> bytes | None:
        if self._conn is None:
            return self.get(key)
        now = self._clock()
        with self._lock:
            value = self._mem_get(key, now)
            if value is not None:
                self.hits += 1
                return value
        return await asyncio.to_thread(self.get, key)

    async def aset(self, key: str, value: bytes, ttl: float) -> None:
        if self._conn is None:
            self.set(key, value, ttl)
            return
        await asyncio.to_thread(self.set, key, value, ttl)

    async def adelete(self, key: str) -> None:
        if self._conn is None:
            self.delete(key)
            return
        await asyncio.to_thread(self.delete, key)
