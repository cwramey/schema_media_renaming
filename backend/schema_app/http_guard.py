"""Request guard: a pure ASGI middleware in front of every HTTP request.

* **Body caps.** ``Content-Length`` over the route's cap is refused (413) before the app runs, and
  streamed (chunked) bodies are counted as they arrive. FastAPI reads and JSON-parses a body before
  any dependency runs, so without this an anonymous request could make the server build hundreds of
  MB of Python objects.
* **Session first.** Cookie-authenticated ``/api`` routes need a validly signed, unexpired session
  cookie *before* the body is read (401 otherwise). ``require_user`` still does the full check
  (account, password fingerprint, revocation epoch) afterwards.
* **Security headers** on every response (CSP, framing, sniffing, referrer) and ``Cache-Control:
  no-store`` on ``/api``.
* **Sliding sessions.** A session cookie older than ``SESSION_RENEW_AFTER`` is re-issued on a
  successful authenticated request, so an active browser stays signed in while an idle one expires.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from schema_app.auth import SECURE_SESSION_COOKIE, SESSION_COOKIE, session_token, set_session_cookie
from schema_app.deps import is_https

KIB = 1024
MIB = 1024 * KIB
DEFAULT_BODY_LIMIT = 1 * MIB  # every request not listed below
RESTORE_BODY_LIMIT = 16 * MIB  # POST /api/settings/restore: a whole settings backup
WEBHOOK_BODY_LIMIT = 64 * KIB  # download-client notifications
PUBLIC_BODY_LIMIT = 16 * KIB  # health and sign-in: anyone can send these

RESTORE_PATH = "/api/settings/restore"
WEBHOOK_PREFIX = "/api/webhooks/"
# /api routes that work without a session cookie (they check their own credentials, if any).
PUBLIC_API = frozenset({"/api/health", "/api/auth/status", "/api/auth/login", "/api/auth/setup", "/api/auth/logout"})
DOCS_API = frozenset({"/api/docs", "/api/docs/oauth2-redirect", "/api/openapi.json"})

# Poster CDNs of the metadata providers (TMDb, TheTVDB, TVmaze); the browser loads posters directly.
POSTER_HOSTS = ("https://image.tmdb.org", "https://artworks.thetvdb.com", "https://static.tvmaze.com")

_HOST_RE = re.compile(r"(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?")
_INLINE_SCRIPT_RE = re.compile(r"<script\b(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script\s*>", re.IGNORECASE | re.DOTALL)
_INLINE_STYLE_RE = re.compile(r"<style\b[^>]*>(.*?)</style\s*>", re.IGNORECASE | re.DOTALL)


def _csp_hash(content: str) -> str:
    # Browsers hash the element's text after HTML parsing, which turns CRLF / CR into LF.
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    return "'sha256-" + base64.b64encode(hashlib.sha256(normalized.encode("utf-8")).digest()).decode("ascii") + "'"


def inline_hashes(index_html: Path) -> tuple[list[str], list[str]]:
    """CSP hash sources of the inline ``<script>`` and ``<style>`` blocks of the built ``index.html``
    (the pre-paint theme script and background style). Read from the build, so editing them never
    needs a matching backend change."""
    try:
        html = index_html.read_text(encoding="utf-8")
    except OSError:
        return [], []
    scripts = [_csp_hash(m.group(1)) for m in _INLINE_SCRIPT_RE.finditer(html) if m.group(1).strip()]
    styles = [_csp_hash(m.group(1)) for m in _INLINE_STYLE_RE.finditer(html) if m.group(1).strip()]
    return sorted(set(scripts)), sorted(set(styles))


def build_csp(script_hashes: Iterable[str] = (), style_hashes: Iterable[str] = ()) -> str:
    """The Content-Security-Policy, minus ``connect-src`` (added per request with the WebSocket origin)."""
    scripts = " ".join(["'self'", *script_hashes])
    styles = " ".join(["'self'", *style_hashes])
    return "; ".join([
        "default-src 'self'",
        f"script-src {scripts}",
        f"style-src {styles}",
        "img-src 'self' data: " + " ".join(POSTER_HOSTS),
        "font-src 'self' data:",
        "object-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
        "form-action 'self'",
    ])


def body_limit(path: str) -> int:
    if path == RESTORE_PATH:
        return RESTORE_BODY_LIMIT
    if path.startswith(WEBHOOK_PREFIX):
        return WEBHOOK_BODY_LIMIT
    if path in PUBLIC_API:
        return PUBLIC_BODY_LIMIT
    return DEFAULT_BODY_LIMIT


class _BodyTooLarge(Exception):
    pass


def _json_response(status: int, detail: str, headers: dict[str, str] | None = None) -> Response:
    return Response(json.dumps({"detail": detail}), status_code=status, media_type="application/json",
                    headers=headers)  # fmt: skip


class RequestGuard:
    def __init__(self, app: ASGIApp, *, state: Any, csp: str) -> None:
        self.app = app
        self.state = state  # the FastAPI app's state: .config always, .ctx once the lifespan ran
        self.csp = csp

    # ------------------------------------------------------------------ policy

    def _needs_session(self, path: str) -> bool:
        if not (path == "/api" or path.startswith("/api/")):
            return False
        path = path.rstrip("/") or "/"
        if path in PUBLIC_API or path.startswith(WEBHOOK_PREFIX):
            return False
        config = getattr(self.state, "config", None)
        docs_on = config is not None and config.api_docs
        return not (docs_on and path in DOCS_API)

    def _headers(self, scope: Scope, api: bool) -> list[tuple[str, str]]:
        connect = "'self'"
        host = Headers(scope=scope).get("host", "")
        if _HOST_RE.fullmatch(host):
            connect += f" ws://{host} wss://{host}"  # older Safari doesn't count ws: as 'self'
        headers = [
            ("Content-Security-Policy", f"{self.csp}; connect-src {connect}"),
            ("X-Frame-Options", "DENY"),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
        ]
        if api:
            headers.append(("Cache-Control", "no-store"))
        return headers

    # ------------------------------------------------------------------ ASGI

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]
        api = path == "/api" or path.startswith("/api/")
        extra = self._headers(scope, api)
        renewal: list[tuple[str, str]] = []
        started = False

        async def send_guarded(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message.setdefault("headers", [])
                headers = MutableHeaders(scope=message)
                for name, value in extra:
                    headers[name] = value
                if renewal and message["status"] < 400 and not _sets_session_cookie(headers):
                    for name, value in renewal:
                        headers.append(name, value)
            await send(message)

        limit = body_limit(path)
        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            if not declared.isdigit():
                await _json_response(400, "invalid Content-Length")(scope, receive, send_guarded)
                return
            if int(declared) > limit:
                await _too_large(limit)(scope, receive, send_guarded)
                return

        if self._needs_session(path):
            verdict = self._check_session(scope)
            if verdict is None:
                await _json_response(401, "not authenticated")(scope, receive, send_guarded)
                return
            renewal.extend(verdict)

        received = 0
        exceeded = False

        async def receive_limited() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    raise _BodyTooLarge
            return message

        async def send_unless_exceeded(message: Message) -> None:
            if exceeded and not started:
                return  # the app turned the aborted body into an error of its own: answer 413 instead
            await send_guarded(message)

        try:
            await self.app(scope, receive_limited, send_unless_exceeded)
        except Exception:
            if not exceeded or started:
                raise
        if exceeded and not started:
            await _too_large(limit)(scope, receive, send_guarded)

    def _check_session(self, scope: Scope) -> list[tuple[str, str]] | None:
        """None to refuse; else the Set-Cookie headers renewing the session (often none)."""
        ctx = getattr(self.state, "ctx", None)
        if ctx is None or not ctx.config.auth_enabled:
            return []  # starting up (the route answers 503) or no sign-in at all
        request = Request(scope)
        token = session_token(request.cookies)
        if ctx.signer.read(token) is None:
            return None
        fresh = ctx.signer.renewed(token)
        if fresh is None:
            return []
        response = Response()
        set_session_cookie(response, fresh, https=is_https(request))
        return [("set-cookie", v.decode("latin-1")) for k, v in response.raw_headers if k == b"set-cookie"]


def _too_large(limit: int) -> Response:
    return _json_response(413, f"request body too large (limit {limit // KIB} KiB)", {"Connection": "close"})


_SESSION_PREFIXES = (f"{SESSION_COOKIE}=", f"{SECURE_SESSION_COOKIE}=")


def _sets_session_cookie(headers: MutableHeaders) -> bool:
    return any(value.startswith(_SESSION_PREFIXES) for value in headers.getlist("set-cookie"))
