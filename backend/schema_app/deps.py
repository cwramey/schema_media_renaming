"""FastAPI dependencies: the app context, DB sessions, authentication and CSRF checks."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, WebSocket, status
from sqlalchemy.orm import Session

from schema_app.auth import (
    LoginThrottle,
    SessionSigner,
    SessionUser,
    SessionWatchers,
    constant_time_equals,
    session_token,
    user_from_token,
)
from schema_app.config import Config
from schema_app.db import Database
from schema_app.services.events import EventBus
from schema_app.services.settings_store import SettingsStore

if TYPE_CHECKING:
    from schema_app.core.pipeline import Pipeline, WatchManager

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CLIENT_HEADER = "x-schema-client"
ANONYMOUS = SessionUser(id=0, username="admin")


@dataclass
class AppContext:
    """Everything the routes need, built by the lifespan in ``main.py``."""

    config: Config
    db: Database
    store: SettingsStore
    bus: EventBus
    signer: SessionSigner
    matcher: Any = None
    pipeline: Pipeline | None = None
    watch: WatchManager | None = None
    matcher_error: str | None = None
    setup_token: str | None = None  # one-time code for first-run admin creation (None: not required)
    throttle: LoginThrottle = field(default_factory=LoginThrottle)
    session_watchers: SessionWatchers = field(default_factory=SessionWatchers)  # open WebSockets
    extras: dict[str, Any] = field(default_factory=dict)


def get_ctx(request: Request) -> AppContext:
    ctx = getattr(request.app.state, "ctx", None)
    if ctx is None:  # pragma: no cover - only before the lifespan ran
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "starting up")
    return ctx


CtxDep = Annotated[AppContext, Depends(get_ctx)]


def get_session(ctx: CtxDep) -> Iterator[Session]:
    with ctx.db.session() as session:
        yield session


# Commit before the response is sent (the default "request" scope would commit afterwards and
# race the client's next request).
SessionDep = Annotated[Session, Depends(get_session, scope="function")]


def session_user(ctx: AppContext, token: str | None) -> SessionUser | None:
    """Resolve a session cookie to a user (blocking: one indexed SELECT)."""
    if not ctx.config.auth_enabled:
        return ANONYMOUS
    if not token:
        return None
    with ctx.db.session() as session:
        return user_from_token(session, ctx.signer, token)


def check_csrf(request: Request) -> None:
    """Cookie-authenticated mutations need a JSON body or the ``X-Schema-Client`` header (SPEC §11)."""
    if request.method not in MUTATING_METHODS:
        return
    if request.headers.get(CLIENT_HEADER):
        return
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type == "application/json":
        return
    # Body-less POSTs (undo, cancel, rematch, execute ...) are where this usually bites.
    raise HTTPException(
        status.HTTP_403_FORBIDDEN,
        "missing X-Schema-Client header: mutating requests without a JSON body must send 'X-Schema-Client: web'",
    )


def require_user(request: Request, ctx: CtxDep) -> SessionUser:
    """401 unless signed in (or ``SCHEMA_AUTH=disabled``); enforces the CSRF rule for mutations."""
    user = session_user(ctx, session_token(request.cookies))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")
    check_csrf(request)
    return user


UserDep = Annotated[SessionUser, Depends(require_user)]


def websocket_user(websocket: WebSocket, ctx: AppContext) -> SessionUser | None:
    return session_user(ctx, session_token(websocket.cookies))


def origin_allowed(origin: str | None, host: str | None, extra: list[str]) -> bool:
    """Browsers always send Origin on WebSocket handshakes, and SameSite=Lax ignores ports: without this
    check a page served by another app on the NAS (UGOS, a torrent web UI ...) could open /api/ws
    with the user's cookie. The origin must be this server's own Host (or listed in
    SCHEMA_ALLOWED_ORIGINS). Non-browser clients send no Origin and are left to the cookie check."""
    if not origin:
        return True
    origin = origin.strip().rstrip("/").lower()
    if origin in extra:
        return True
    try:
        netloc = urlsplit(origin).netloc
    except ValueError:
        return False
    return bool(host) and netloc == host.strip().lower()


def require_api_key(request: Request, ctx: CtxDep) -> None:
    """Webhooks authenticate with ``X-Api-Key`` or ``?apikey=`` (never the cookie)."""
    supplied = request.headers.get("x-api-key") or request.query_params.get("apikey")
    if not constant_time_equals(supplied, ctx.store.get_api_key()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing API key")


def pipeline_of(ctx: AppContext) -> Pipeline:
    if ctx.pipeline is None:  # pragma: no cover
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "pipeline not running")
    return ctx.pipeline


def is_https(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    return request.url.scheme == "https" or forwarded == "https"
