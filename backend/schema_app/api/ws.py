"""``/api/ws``: server -> client push of ``WsMessage`` (cookie-authenticated; first message ``hello``)."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from schema_app import __version__
from schema_app.deps import AppContext, origin_allowed, websocket_user

router = APIRouter()

WS_POLICY_VIOLATION = 1008
SESSION_RECHECK_SECONDS = 60.0  # periodic re-validation of the handshake cookie (expiry, revocation)


async def _watch_session(websocket: WebSocket, ctx: AppContext, wake: asyncio.Event) -> None:
    """Close the socket once its cookie stops being valid: checked every ``SESSION_RECHECK_SECONDS``
    and immediately when sessions are revoked (``ctx.session_watchers.notify()`` sets ``wake``)."""
    while True:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(wake.wait(), SESSION_RECHECK_SECONDS)
        wake.clear()
        if await run_in_threadpool(websocket_user, websocket, ctx) is None:
            await websocket.close(code=WS_POLICY_VIOLATION)
            return


async def _pump(websocket: WebSocket, queue: asyncio.Queue[dict[str, Any]]) -> None:
    while True:
        message = await queue.get()
        await websocket.send_json(message)


async def _drain(websocket: WebSocket) -> None:
    """Read (and ignore) client frames until the socket closes."""
    while True:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            return


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    ctx: AppContext | None = getattr(websocket.app.state, "ctx", None)
    if ctx is None:
        await websocket.close(code=WS_POLICY_VIOLATION)
        return
    origin, host = websocket.headers.get("origin"), websocket.headers.get("host")
    if not origin_allowed(origin, host, ctx.config.allowed_origin_list):
        await websocket.close(code=WS_POLICY_VIOLATION)
        return
    user = await run_in_threadpool(websocket_user, websocket, ctx)
    if user is None:
        await websocket.close(code=WS_POLICY_VIOLATION)
        return
    await websocket.accept()
    queue = ctx.bus.subscribe()
    loop, wake = asyncio.get_running_loop(), asyncio.Event()
    remove_watcher = ctx.session_watchers.add(lambda: loop.call_soon_threadsafe(wake.set))
    tasks: list[asyncio.Task[None]] = []
    try:
        await websocket.send_json({"type": "hello", "data": {"version": __version__}})
        tasks = [
            asyncio.create_task(_pump(websocket, queue)),
            asyncio.create_task(_drain(websocket)),
            asyncio.create_task(_watch_session(websocket, ctx, wake)),
        ]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, WebSocketDisconnect | RuntimeError):
                raise exc
    except WebSocketDisconnect:
        pass
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task
        ctx.bus.unsubscribe(queue)
        remove_watcher()
