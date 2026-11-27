"""In-process event bus: persists log events and fans ``WsMessage`` pushes out to WebSockets.

``publish`` and ``log`` are safe to call from the event loop *and* from worker threads (sync
FastAPI routes run in the threadpool): deliveries are marshalled onto the loop the bus was bound to.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import threading
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, select

from schema_app.api_models import EventOut, WsMessage
from schema_app.db import Database, utcnow
from schema_app.models import Event

log = logging.getLogger("schema.events")

LEVELS = ("debug", "info", "warning", "error")
RETENTION = timedelta(days=30)
MAX_EVENTS = 50_000
PRUNE_EVERY = 250  # inserts between pruning passes
QUEUE_SIZE = 1000
RESYNC_MESSAGE: dict[str, Any] = {"type": "resync", "data": {}}

_PY_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}
_SECRET_KEYS = {"token", "api_key", "apikey", "password", "secret", "key"}


def _scrub(data: dict[str, Any]) -> dict[str, Any]:
    """Drop obvious secrets from event payloads (they are never logged)."""
    clean: dict[str, Any] = {}
    for key, value in data.items():
        if key.lower() in _SECRET_KEYS or key.lower().endswith(("_token", "_key", "_password")):
            continue
        clean[key] = value
    return clean


def event_out(row: Event) -> EventOut:
    level = row.level if row.level in LEVELS else "info"
    return EventOut(
        id=row.id,
        ts=row.ts,
        level=level,  # type: ignore[arg-type]
        category=row.category,
        message=row.message,
        job_id=row.job_id,
        data=row.data or {},
    )


class EventBus:
    def __init__(self, db: Database | None = None) -> None:
        self.db = db
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._inserts = 0
        self._lock = threading.Lock()
        self._changes = itertools.count(1)
        self.version = 0  # bumped by every publish: read models (the Home board) cache per version

    def _bump(self) -> None:
        self.version = next(self._changes)  # itertools.count is atomic under the GIL

    # ------------------------------------------------------------------ lifecycle

    def bind(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._loop = loop or asyncio.get_running_loop()

    def unbind(self) -> None:
        self._loop = None
        self._subscribers.clear()

    # ------------------------------------------------------------------ subscriptions (loop only)

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    # ------------------------------------------------------------------ publishing

    def publish(self, type: str, data: dict[str, Any] | Any) -> None:
        """Push ``WsMessage(type, data)`` to every connected socket (fire and forget)."""
        self._bump()
        if hasattr(data, "model_dump"):
            data = data.model_dump(mode="json")
        message = WsMessage(type=type, data=data).model_dump(mode="json")  # type: ignore[arg-type]
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._fanout(message)
        else:
            with contextlib.suppress(RuntimeError):  # loop shutting down
                loop.call_soon_threadsafe(self._fanout, message)

    def _fanout(self, message: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # Slow consumer (or a burst such as a 10k-item match): rather than silently dropping
                # updates and leaving its UI stale, discard the backlog and tell it to refetch.
                while True:
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                queue.put_nowait(RESYNC_MESSAGE)

    # ------------------------------------------------------------------ persistent log

    def log(self, level: str, category: str, message: str, job_id: int | None = None, **data: Any) -> EventOut | None:
        """Persist an ``events`` row, mirror it to Python logging and push an ``event`` message.

        Blocking (one SQLite insert): from async code prefer :meth:`alog`.
        """
        level = level if level in LEVELS else "info"
        payload = _scrub(data)
        logging.getLogger(f"schema.{category}").log(_PY_LEVELS[level], "%s", message)
        if self.db is None:
            return None
        try:
            with self.db.session() as session:
                row = Event(level=level, category=category, message=message, job_id=job_id, data=payload)
                session.add(row)
                session.flush()
                out = event_out(row)
            self._maybe_prune()
        except Exception:  # never let logging break the caller
            log.exception("could not persist event")
            return None
        self.publish("event", out)
        return out

    async def alog(
        self, level: str, category: str, message: str, job_id: int | None = None, **data: Any
    ) -> EventOut | None:
        return await asyncio.to_thread(self.log, level, category, message, job_id, **data)

    def _maybe_prune(self) -> None:
        with self._lock:
            self._inserts += 1
            due = self._inserts % PRUNE_EVERY == 1  # first insert after start, then every N
        if due:
            self.prune()

    def prune(self) -> int:
        """Delete events older than 30 days and keep at most 50 000 rows. Returns rows deleted."""
        if self.db is None:
            return 0
        deleted = 0
        with self.db.session() as session:
            result = session.execute(delete(Event).where(Event.ts < utcnow() - RETENTION))
            deleted += result.rowcount or 0
            total = session.scalar(select(func.count(Event.id))) or 0
            if total > MAX_EVENTS:
                cutoff = session.scalar(
                    select(Event.id).order_by(Event.id.desc()).offset(MAX_EVENTS - 1).limit(1)
                )
                if cutoff is not None:
                    result = session.execute(delete(Event).where(Event.id < cutoff))
                    deleted += result.rowcount or 0
        return deleted
