"""``/api/health`` and ``/api/auth/*`` (SPEC §5, §11)."""

from __future__ import annotations

import asyncio
import logging
import threading

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from schema_app import __version__, build_commit
from schema_app.api_models import AuthStatus, Credentials, SetupCredentials
from schema_app.auth import (
    Attempt,
    SessionUser,
    Throttled,
    authenticate,
    change_password,
    clear_session_cookies,
    constant_time_equals,
    create_user,
    revoke_sessions,
    session_token,
    set_session_cookie,
    user_count,
    verify_password,
)
from schema_app.deps import CLIENT_HEADER, AppContext, CtxDep, UserDep, check_csrf, is_https, session_user
from schema_app.models import User

log = logging.getLogger("schema.auth")

router = APIRouter()

FAILED_LOGIN_DELAY = 0.4  # seconds, awaited (no thread held); slows down password guessing
SCRYPT_CONCURRENCY = 2  # concurrent password checks (128 MiB each): requests beyond wait without a thread
_setup_lock = threading.Lock()  # makes "no user yet? create one" atomic across threadpool workers
AUTH_DISABLED = "authentication is disabled (SCHEMA_AUTH=disabled)"
_sleep = asyncio.sleep  # awaited, so a waiting sign-in holds no threadpool thread (tests patch this)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _reserve(ctx: AppContext, request: Request, username: str | None) -> Attempt:
    """Reserve an attempt before any scrypt work; 429 when this client is over the limit."""
    try:
        return ctx.throttle.reserve(_client_ip(request), username)
    except Throttled as exc:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "too many failed attempts; try again later",
            headers={"Retry-After": str(exc.retry_after)},
        ) from None


def _scrypt_gate(ctx: AppContext) -> asyncio.Semaphore:
    gate = ctx.extras.get("scrypt_gate")
    if gate is None:
        gate = ctx.extras["scrypt_gate"] = asyncio.Semaphore(SCRYPT_CONCURRENCY)
    return gate


def _database_ok(ctx: AppContext) -> bool:
    try:
        with ctx.db.engine.connect() as conn:
            conn.execute(text("SELECT count(*) FROM sqlite_master")).scalar()
    except Exception as exc:
        log.error("health check: database unavailable: %s", exc)
        return False
    return True


@router.get("/health")
def health(request: Request, ctx: CtxDep) -> JSONResponse:
    """Liveness for the container HEALTHCHECK and uptime monitors: 503 ``degraded`` when the
    database can't be read. The build commit is only shown to a signed-in browser (Settings → About)."""
    ok = _database_ok(ctx)
    commit = None
    if ok and session_user(ctx, session_token(request.cookies)) is not None:
        commit = build_commit()
    return JSONResponse(
        {"status": "ok" if ok else "degraded", "version": __version__, "commit": commit},
        status_code=status.HTTP_200_OK if ok else status.HTTP_503_SERVICE_UNAVAILABLE,
    )


def _status(ctx: AppContext, user: SessionUser | None) -> AuthStatus:
    enabled = ctx.config.auth_enabled
    setup_required = False
    if enabled:
        with ctx.db.session() as session:
            setup_required = user_count(session) == 0
    return AuthStatus(
        setup_required=setup_required,
        authenticated=user is not None,
        username=user.username if (user is not None and enabled) else None,
        auth_enabled=enabled,
        demo=ctx.config.demo,
        version=__version__,
        setup_token_required=setup_required and ctx.setup_token is not None,
    )


def _require_auth_enabled(ctx: AppContext) -> None:
    if not ctx.config.auth_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, AUTH_DISABLED)


@router.get("/auth/status", response_model=AuthStatus)
def auth_status(request: Request, ctx: CtxDep) -> AuthStatus:
    return _status(ctx, session_user(ctx, session_token(request.cookies)))


_SETUP_DONE = "done"
_SETUP_BAD_CODE = "bad-code"


def _setup(ctx: AppContext, credentials: SetupCredentials) -> tuple[str, SessionUser] | str:
    with _setup_lock, ctx.db.session() as session:
        if user_count(session) > 0:
            return _SETUP_DONE
        if ctx.setup_token is not None and not constant_time_equals(credentials.setup_token, ctx.setup_token):
            return _SETUP_BAD_CODE
        user = create_user(session, credentials.username, credentials.password)
        token = ctx.signer.issue(user)
        current = SessionUser(id=user.id, username=user.username)
    ctx.setup_token = None
    return token, current


@router.post("/auth/setup", response_model=AuthStatus)
async def auth_setup(
    credentials: SetupCredentials, request: Request, response: Response, ctx: CtxDep
) -> AuthStatus:
    check_csrf(request)
    _require_auth_enabled(ctx)
    attempt = _reserve(ctx, request, None)
    try:
        async with _scrypt_gate(ctx):
            outcome = await run_in_threadpool(_setup, ctx, credentials)
    except BaseException:
        ctx.throttle.release(attempt)
        raise
    if outcome == _SETUP_DONE:
        ctx.throttle.release(attempt)
        raise HTTPException(status.HTTP_409_CONFLICT, "setup is already complete")
    if outcome == _SETUP_BAD_CODE:  # the reservation stands as a failure
        await ctx.bus.alog("warning", "system", "Setup attempt with a wrong setup code", client=_client_ip(request))
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "invalid setup code: it is printed in the container log (docker logs schema)"
        )
    assert isinstance(outcome, tuple)
    ctx.throttle.success(attempt)
    token, current = outcome
    set_session_cookie(response, token, https=is_https(request))
    await ctx.bus.alog("info", "system", f"Admin account {current.username!r} created")
    return await run_in_threadpool(_status, ctx, current)


def _login(ctx: AppContext, credentials: Credentials) -> tuple[str, SessionUser] | None:
    with ctx.db.session() as session:
        user = authenticate(session, credentials.username, credentials.password)
        if user is None:
            return None
        session.flush()  # a rehashed password is part of the cookie's fingerprint
        return ctx.signer.issue(user), SessionUser(id=user.id, username=user.username)


@router.post("/auth/login", response_model=AuthStatus)
async def auth_login(
    credentials: Credentials, request: Request, response: Response, ctx: CtxDep
) -> AuthStatus:
    check_csrf(request)
    if not ctx.config.auth_enabled:
        return _status(ctx, session_user(ctx, None))  # no DB work when auth is off
    ip = _client_ip(request)
    attempt = _reserve(ctx, request, credentials.username)  # before scrypt: refused attempts cost no CPU
    try:
        if attempt.delay:
            await _sleep(attempt.delay)
        async with _scrypt_gate(ctx):
            outcome = await run_in_threadpool(_login, ctx, credentials)
    except BaseException:
        ctx.throttle.release(attempt)
        raise
    if outcome is None:  # the reservation stands as a failure
        await _sleep(FAILED_LOGIN_DELAY)
        await ctx.bus.alog("warning", "system", "Failed sign-in attempt", client=ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid username or password")
    ctx.throttle.success(attempt)
    token, current = outcome
    set_session_cookie(response, token, https=is_https(request))
    return await run_in_threadpool(_status, ctx, current)


@router.post("/auth/logout", response_model=AuthStatus)
def auth_logout(request: Request, response: Response, ctx: CtxDep) -> AuthStatus:
    """Sign out *this browser* only. Deliberately no epoch bump: the single admin is usually signed in
    on several devices, and signing out of the phone shouldn't end the desktop session. Revoking every
    cookie (e.g. one that may have leaked) is ``/auth/logout-all``.

    Needs the ``X-Schema-Client`` header (a JSON content type alone isn't enough here: the request has
    no body), so another site can't sign the user out."""
    if not request.headers.get(CLIENT_HEADER):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "missing X-Schema-Client header")
    clear_session_cookies(response, https=is_https(request))
    if not ctx.config.auth_enabled:
        return _status(ctx, session_user(ctx, None))
    return _status(ctx, None)


@router.post("/auth/logout-all", response_model=AuthStatus)
def auth_logout_all(request: Request, response: Response, ctx: CtxDep, user: UserDep) -> AuthStatus:
    """Sign out everywhere: bump the session epoch (every cookie issued so far stops working, open
    WebSockets are closed) and clear this browser's cookie."""
    _require_auth_enabled(ctx)
    with ctx.db.session() as session:
        row = session.get(User, user.id)
        if row is not None:
            revoke_sessions(row)
    if row is None:  # deleted while signed in
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")
    ctx.session_watchers.notify()
    clear_session_cookies(response, https=is_https(request))
    ctx.bus.log("info", "system", f"Signed {user.username!r} out of every session", client=_client_ip(request))
    return _status(ctx, None)


def _change_password(ctx: AppContext, user: SessionUser, body: PasswordChange) -> str | None:
    """The new cookie, None for a wrong current password; 401 if the account is gone."""
    with ctx.db.session() as session:
        row = session.get(User, user.id)
        if row is None:  # deleted while signed in
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")
        if not verify_password(body.current_password, row.password_hash):
            return None
        change_password(row, body.new_password)
        session.flush()
        return ctx.signer.issue(row)


@router.post("/auth/password", response_model=AuthStatus)
async def auth_change_password(
    body: PasswordChange, request: Request, response: Response, ctx: CtxDep, user: UserDep
) -> AuthStatus:
    """Change the password: verify the current one (throttled like sign-in), rehash, revoke every other
    session and re-issue this browser's cookie in the new epoch."""
    _require_auth_enabled(ctx)
    ip = _client_ip(request)
    attempt = _reserve(ctx, request, user.username)  # before scrypt: refused attempts cost no CPU
    try:
        if attempt.delay:
            await _sleep(attempt.delay)
        async with _scrypt_gate(ctx):
            token = await run_in_threadpool(_change_password, ctx, user, body)
    except BaseException:
        ctx.throttle.release(attempt)
        raise
    if token is None:  # the reservation stands as a failure
        await _sleep(FAILED_LOGIN_DELAY)
        await ctx.bus.alog("warning", "system", "Password change with a wrong current password", client=ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "current password is incorrect")
    ctx.throttle.success(attempt)
    ctx.session_watchers.notify()
    set_session_cookie(response, token, https=is_https(request))
    await ctx.bus.alog("info", "system", f"Password changed for {user.username!r}; other sessions signed out", client=ip)
    return await run_in_threadpool(_status, ctx, user)
