"""Authentication primitives (SPEC §11): scrypt password hashes, signed session cookies, API keys."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.responses import Response

from schema_app.db import utcnow
from schema_app.models import User

# Plain-HTTP name. Over HTTPS the cookie is ``__Host-``-prefixed and Secure, so browsers keep it off
# plain-HTTP apps on other ports of the same NAS (cookies ignore ports) and nothing can overwrite it
# from a sibling host. The plain name is still read, so existing sessions survive the switch.
SESSION_COOKIE = "schema_session"
SECURE_SESSION_COOKIE = "__Host-schema_session"
SESSION_MAX_AGE = 7 * 24 * 3600  # 7 days, renewed while in use (see SESSION_RENEW_AFTER)
SESSION_RENEW_AFTER = 24 * 3600  # re-issue a cookie on use once it is a day old (sliding expiry)

_SCRYPT_N = 2**17  # 128 MiB per hash at r=8; older 2**14 hashes still verify and are upgraded on login
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 64
_SCRYPT_MAX_MEM = 256 * 1024 * 1024  # refuse stored parameters that would need more than this
# At most two scrypt computations at once (2 x 128 MiB): the container is capped at 1.5 GB.
_SCRYPT_SLOTS = threading.BoundedSemaphore(2)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int, dklen: int) -> bytes:
    if n < 2 or r < 1 or p < 1 or 128 * r * (n + p + 2) > _SCRYPT_MAX_MEM:
        raise ValueError("unsupported scrypt parameters")
    with _SCRYPT_SLOTS:
        return hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=dklen,
            maxmem=128 * r * (n + p + 2) + 1024 * 1024,
        )


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """``scrypt$n$r$p$salt$hash`` (base64 parts)."""
    salt = salt or secrets.token_bytes(16)
    digest = _scrypt(password, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _SCRYPT_DKLEN)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, hash_b64 = encoded.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        digest = _scrypt(password, salt, int(n), int(r), int(p), len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


def needs_rehash(encoded: str) -> bool:
    """True when ``encoded`` was made with other scrypt parameters than new hashes use."""
    try:
        scheme, n, r, p, _salt, _hash = encoded.split("$")
        return scheme != "scrypt" or (int(n), int(r), int(p)) != (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
    except ValueError:
        return True


# Compared against when the username doesn't exist, so an unknown name costs the same single scrypt
# as a wrong password (computing a fresh hash there made it cost two, and showed which names exist).
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def constant_time_equals(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _password_fingerprint(password_hash: str) -> str:
    """Short fingerprint of the stored hash: changing the password invalidates old sessions."""
    return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()[:16]


class Throttled(Exception):  # noqa: N818 - reads as "the attempt was throttled"
    def __init__(self, retry_after: int) -> None:
        super().__init__(f"retry after {retry_after}s")
        self.retry_after = retry_after


@dataclass(frozen=True)
class Attempt:
    """A reserved sign-in attempt: counts as a failure until ``success()`` or ``release()``."""

    keys: tuple[str, ...]
    stamp: float
    delay: float  # seconds to wait before checking the password


class LoginThrottle:
    """Sign-in (and setup-code / current-password) attempts, in memory.

    * Per client IP: at most ``PER_IP`` failures per ``WINDOW``; further attempts are refused (429)
      *before* any scrypt work, so neither guessing nor hashing can be used to load the NAS.
    * Per (IP, username): no hard block (a per-username block let three LAN devices lock the admin
      out), but once a device has failed for an account, its next attempts wait ``BASE_DELAY``
      doubling per failure, capped at ``MAX_DELAY``. The account-wide failure count drives that delay
      when it is higher, which slows guessing spread over many devices. A device with no failures of
      its own is never slowed, so others can't lock the admin out.
    * ``reserve()`` records the attempt under the lock before the password is checked, so concurrent
      requests can't all slip past the limit; ``success()`` clears the counters, ``release()`` takes
      back an attempt that never got an answer.
    * Past ``MAX_KEYS`` tracked keys the least recently used are evicted (never a wholesale clear,
      which an attacker could use to wipe the admin's counters).
    """

    WINDOW = 15 * 60.0
    PER_IP = 10
    BASE_DELAY = 0.5
    MAX_DELAY = 8.0
    MAX_KEYS = 10_000

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._failures: OrderedDict[str, list[float]] = OrderedDict()  # least recently failed first
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> list[float]:
        stamps = self._failures.get(key)
        if not stamps:
            return []
        fresh = [t for t in stamps if now - t < self.WINDOW]
        if len(fresh) != len(stamps):
            if fresh:
                self._failures[key] = fresh
            else:
                del self._failures[key]
        return fresh

    def _add(self, key: str, now: float) -> None:
        stamps = self._failures.get(key)
        if stamps is None:
            self._evict(now)
            stamps = self._failures[key] = []
        stamps.append(now)
        self._failures.move_to_end(key)

    def _evict(self, now: float) -> None:
        """Make room for one more key: drop expired keys, then the least recently used ones."""
        while self._failures:
            key, stamps = next(iter(self._failures.items()))
            if len(self._failures) < self.MAX_KEYS and now - stamps[-1] < self.WINDOW:
                return
            del self._failures[key]

    def reserve(self, ip: str | None, username: str | None = None) -> Attempt:
        """Reserve one attempt, or raise :class:`Throttled` if this IP is over the limit."""
        now = self._clock()
        ip_key = f"ip:{ip or '?'}"
        user = (username or "").strip().lower()[:256]
        pair_key = f"pair:{ip or '?'}|{user}" if user else None
        user_key = f"user:{user}" if user else None
        with self._lock:
            ip_stamps = self._recent(ip_key, now)
            if len(ip_stamps) >= self.PER_IP:
                raise Throttled(int(self.WINDOW - (now - ip_stamps[-self.PER_IP])) + 1)
            delay = 0.0
            if pair_key and user_key:
                pair_failures = len(self._recent(pair_key, now))
                if pair_failures:
                    count = max(pair_failures, len(self._recent(user_key, now)))
                    delay = min(self.MAX_DELAY, self.BASE_DELAY * 2 ** min(count - 1, 16))
            keys = tuple(k for k in (ip_key, pair_key, user_key) if k)
            for key in keys:
                self._add(key, now)
        return Attempt(keys=keys, stamp=now, delay=delay)

    def success(self, attempt: Attempt) -> None:
        """The attempt succeeded: forget the failures of its IP, device+account and account."""
        with self._lock:
            for key in attempt.keys:
                self._failures.pop(key, None)

    def release(self, attempt: Attempt) -> None:
        """Take back an attempt that ended without an answer (an error, a request that was refused)."""
        with self._lock:
            for key in attempt.keys:
                stamps = self._failures.get(key)
                if stamps and attempt.stamp in stamps:
                    stamps.remove(attempt.stamp)
                    if not stamps:
                        del self._failures[key]

    def __len__(self) -> int:
        with self._lock:
            return len(self._failures)


@dataclass(frozen=True)
class SessionUser:
    id: int
    username: str


class SessionSigner:
    def __init__(self, secret_key: str) -> None:
        self._serializer = URLSafeTimedSerializer(secret_key, salt="schema-session")

    def issue(self, user: User) -> str:
        return self._serializer.dumps(
            {"uid": user.id, "fp": _password_fingerprint(user.password_hash), "ep": user.session_epoch or 0}
        )

    def read(self, token: str | None, *, max_age: int = SESSION_MAX_AGE) -> dict[str, Any] | None:
        if not token:
            return None
        try:
            data = self._serializer.loads(token, max_age=max_age)
        except BadSignature:
            return None
        return data if isinstance(data, dict) else None

    def renewed(self, token: str | None, *, older_than: int = SESSION_RENEW_AFTER) -> str | None:
        """A freshly dated copy of a valid ``token`` issued more than ``older_than`` seconds ago (the
        sliding part of the session lifetime), else None. Same payload, so revocation still applies."""
        if not token:
            return None
        try:
            data, issued = self._serializer.loads(token, max_age=SESSION_MAX_AGE, return_timestamp=True)
        except BadSignature:
            return None
        if not isinstance(data, dict) or (datetime.now(UTC) - issued).total_seconds() < older_than:
            return None
        return self._serializer.dumps(data)


# ---------------------------------------------------------------------- session cookie


def session_token(cookies: Mapping[str, str]) -> str | None:
    """The session cookie of a request: the ``__Host-`` one (HTTPS), else the plain-HTTP one."""
    return cookies.get(SECURE_SESSION_COOKIE) or cookies.get(SESSION_COOKIE) or None


def set_session_cookie(response: Response, token: str, *, https: bool) -> None:
    if https:
        response.set_cookie(
            SECURE_SESSION_COOKIE, token, max_age=SESSION_MAX_AGE, httponly=True, samesite="lax", secure=True,
            path="/",
        )
        # Drop a plain-HTTP-era cookie so it stops travelling to other apps on this host.
        response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="lax", secure=True)
    else:
        response.set_cookie(
            SESSION_COOKIE, token, max_age=SESSION_MAX_AGE, httponly=True, samesite="lax", secure=False, path="/"
        )


def clear_session_cookies(response: Response, *, https: bool) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="lax", secure=https)
    if https:  # browsers ignore a __Host- cookie (even a deletion) sent without Secure / over HTTP
        response.delete_cookie(SECURE_SESSION_COOKIE, path="/", httponly=True, samesite="lax", secure=True)


# ---------------------------------------------------------------------- user queries (sync)


def user_count(session: Session) -> int:
    return int(session.scalar(select(func.count(User.id))) or 0)


def create_user(session: Session, username: str, password: str) -> User:
    user = User(username=username.strip(), password_hash=hash_password(password))
    session.add(user)
    session.flush()
    return user


def authenticate(session: Session, username: str, password: str) -> User | None:
    """The user if the password is right. Hashes made with older (cheaper) scrypt parameters are
    upgraded in place; the caller commits and issues the cookie afterwards (it binds to the new hash,
    so the account's other devices sign in once more after the upgrade)."""
    user = session.scalar(select(User).where(func.lower(User.username) == username.strip().lower()))
    if user is None:
        verify_password(password, _DUMMY_HASH)  # same single scrypt as a wrong password
        return None
    if not verify_password(password, user.password_hash):
        return None
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    user.last_login_at = utcnow()
    return user


def _token_epoch(data: dict[str, Any]) -> int | None:
    """Epoch a cookie was issued in; cookies from before epochs existed count as epoch 0."""
    epoch = data.get("ep", 0)
    return epoch if isinstance(epoch, int) and not isinstance(epoch, bool) else None


def user_from_token(session: Session, signer: SessionSigner, token: str | None) -> SessionUser | None:
    data = signer.read(token)
    if not data:
        return None
    user = session.get(User, data.get("uid"))
    if user is None or not hmac.compare_digest(str(data.get("fp", "")), _password_fingerprint(user.password_hash)):
        return None
    if _token_epoch(data) != (user.session_epoch or 0):
        return None  # revoked by "sign out everywhere" or a password change
    return SessionUser(id=user.id, username=user.username)


def revoke_sessions(user: User) -> None:
    """Invalidate every session cookie issued to ``user`` so far (caller commits)."""
    user.session_epoch = (user.session_epoch or 0) + 1


def change_password(user: User, new_password: str) -> None:
    """Rehash and revoke all existing sessions (caller verifies the current password and commits)."""
    user.password_hash = hash_password(new_password)
    revoke_sessions(user)


class SessionWatchers:
    """Wake-up hooks of open WebSockets, so revoking sessions makes them re-check their cookie at once
    instead of at their next periodic check. Thread-safe: sync routes call :meth:`notify` from the
    threadpool, and each hook marshals itself onto its socket's event loop."""

    def __init__(self) -> None:
        self._hooks: set[Callable[[], None]] = set()
        self._lock = threading.Lock()

    def add(self, hook: Callable[[], None]) -> Callable[[], None]:
        with self._lock:
            self._hooks.add(hook)
        return lambda: self.discard(hook)

    def discard(self, hook: Callable[[], None]) -> None:
        with self._lock:
            self._hooks.discard(hook)

    def notify(self) -> None:
        with self._lock:
            hooks = list(self._hooks)
        for hook in hooks:
            try:
                hook()
            except RuntimeError:  # its loop is closed: the socket is gone
                self.discard(hook)

    def __len__(self) -> int:
        with self._lock:
            return len(self._hooks)
