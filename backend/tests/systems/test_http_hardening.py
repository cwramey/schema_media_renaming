"""HTTP hardening: body caps, session-before-body, security headers, compression, DNS-rebinding
guard, login throttling, password hashing cost and session cookies."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from conftest import ADMIN, CLIENT_HEADERS, make_config
from fastapi.testclient import TestClient

from schema_app import auth as auth_mod
from schema_app.auth import LoginThrottle, Throttled
from schema_app.http_guard import (
    DEFAULT_BODY_LIMIT,
    RESTORE_BODY_LIMIT,
    WEBHOOK_BODY_LIMIT,
    build_csp,
    inline_hashes,
)

# --------------------------------------------------------------------------- body caps & session first


def test_anonymous_body_is_never_parsed(make_env):
    env = make_env(configure=False)
    anonymous = TestClient(env.app, headers=CLIENT_HEADERS)
    bad_json = anonymous.put("/api/settings", content=b"{not json", headers={"content-type": "application/json"})
    assert bad_json.status_code == 401  # was 422: the body used to be parsed before the auth check
    assert anonymous.post("/api/jobs", json={"paths": ["/x"]}).status_code == 401
    forged = TestClient(env.app, headers=CLIENT_HEADERS, cookies={"schema_session": "forged.value.sig"})
    assert forged.put("/api/settings", json={}).status_code == 401


def test_declared_body_over_the_cap_is_refused_before_reading(make_env):
    env = make_env(configure=False)
    too_big = b"[" + b"{}," * (DEFAULT_BODY_LIMIT // 3) + b"{}]"
    anonymous = TestClient(env.app, headers=CLIENT_HEADERS)
    response = anonymous.put("/api/settings", content=too_big, headers={"content-type": "application/json"})
    assert response.status_code == 413
    signed_in = env.client.put("/api/settings", content=too_big, headers={"content-type": "application/json"})
    assert signed_in.status_code == 413 and "limit" in signed_in.json()["detail"]
    login = anonymous.post("/api/auth/login", content=b"x" * 64 * 1024, headers={"content-type": "application/json"})
    assert login.status_code == 413  # sign-in bodies get a small cap


def test_streamed_body_is_counted(make_env):
    env = make_env(configure=False)

    def chunks():  # no Content-Length: chunked transfer encoding
        for _ in range(DEFAULT_BODY_LIMIT // (64 * 1024) + 2):
            yield b" " * (64 * 1024)

    response = env.client.put("/api/settings", content=chunks(), headers={"content-type": "application/json"})
    assert response.status_code == 413
    assert env.client.get("/api/settings").status_code == 200  # the app is fine afterwards


def test_restore_and_webhooks_have_their_own_caps(make_env):
    env = make_env(configure=False)
    big_backup = {"schema_backup": 1, "padding": "x" * (2 * DEFAULT_BODY_LIMIT)}
    assert 2 * DEFAULT_BODY_LIMIT < RESTORE_BODY_LIMIT
    restore = env.client.post("/api/settings/restore", json=big_backup)
    assert restore.status_code != 413  # parsed (and then judged on its content)
    key = env.client.get("/api/settings/api-key").json()["api_key"]
    hook = TestClient(env.app, headers={"X-Api-Key": key})
    response = hook.post("/api/webhooks/generic", json={"path": "/x", "pad": "x" * WEBHOOK_BODY_LIMIT})
    assert response.status_code == 413
    small = hook.post("/api/webhooks/generic", json={"status": "1", "path": "/x"})
    assert small.status_code not in (401, 413)  # read and judged on its content


def test_public_routes_need_no_session(make_env):
    env = make_env(configure=False)
    anonymous = TestClient(env.app, headers=CLIENT_HEADERS)
    assert anonymous.get("/api/health").status_code == 200
    assert anonymous.get("/api/auth/status").status_code == 200
    assert anonymous.post("/api/auth/logout").status_code == 200
    assert anonymous.post("/api/auth/login", json={"username": "admin", "password": "wrong-password"}).status_code == 401
    assert anonymous.post("/api/auth/setup", json=ADMIN).status_code == 409
    key = env.client.get("/api/settings/api-key").json()["api_key"]
    assert anonymous.post(f"/api/webhooks/generic?apikey={key}", json={"status": "1"}).status_code not in (401, 413)
    assert anonymous.post("/api/webhooks/generic", json={"status": "1"}).status_code == 401  # API key, not cookie


def test_api_docs_stay_public_when_enabled(make_env):
    env = make_env(configure=False, api_docs=True)
    assert TestClient(env.app).get("/api/openapi.json").status_code == 200


# --------------------------------------------------------------------------- headers & compression


def _dist(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><html><head><style>\r\n  body { background: #000; }\r\n</style>"
        "<script>\n  document.documentElement.dataset.theme = 'dark';\n</script>"
        '<script type="module" src="/assets/app.js"></script></head><body></body></html>',
        encoding="utf-8", newline="",
    )
    (dist / "assets" / "app.js").write_text("console.log('schema');\n" * 2000)
    (dist / "assets" / "app.js.map").write_text('{"sources": ["secret.ts"]}')
    return dist


def test_security_headers_and_csp_hashes(tmp_path, fakes):
    from schema_app.main import create_app

    dist = _dist(tmp_path)
    scripts, styles = inline_hashes(dist / "index.html")
    assert len(scripts) == 1 and len(styles) == 1
    # Browsers hash the parsed text, where CRLF became LF.
    import base64
    import hashlib

    expected_style = base64.b64encode(hashlib.sha256(b"\n  body { background: #000; }\n").digest()).decode()
    assert styles == [f"'sha256-{expected_style}'"]

    with TestClient(create_app(make_config(tmp_path, static_dir=dist)), headers=CLIENT_HEADERS) as client:
        page = client.get("/")
        csp = page.headers["content-security-policy"]
        assert scripts[0] in csp and styles[0] in csp
        for directive in ("default-src 'self'", "frame-ancestors 'none'", "object-src 'none'", "base-uri 'none'",
                          "https://image.tmdb.org", "https://artworks.thetvdb.com", "https://static.tvmaze.com",
                          "connect-src 'self' ws://testserver wss://testserver"):  # fmt: skip
            assert directive in csp
        assert "unsafe-inline" not in csp and "unsafe-eval" not in csp and "*" not in csp
        assert page.headers["x-frame-options"] == "DENY"
        assert page.headers["x-content-type-options"] == "nosniff"
        assert page.headers["referrer-policy"] == "no-referrer"
        assert page.headers["cache-control"] == "no-cache"  # the SPA's own header survives
        assert client.get("/api/health").headers["cache-control"] == "no-store"
        assert client.get("/assets/app.js.map").status_code == 404  # source maps are never served


def test_api_key_response_is_not_cached(env):
    for response in (env.client.get("/api/settings/api-key"), env.client.post("/api/settings/api-key")):
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert "content-security-policy" in response.headers


def test_assets_and_json_are_gzipped(tmp_path, fakes):
    from schema_app.main import create_app

    dist = _dist(tmp_path)
    with TestClient(create_app(make_config(tmp_path, static_dir=dist)), headers=CLIENT_HEADERS) as client:
        asset = client.get("/assets/app.js", headers={"Accept-Encoding": "gzip"})
        assert asset.headers["content-encoding"] == "gzip"
        assert asset.text.startswith("console.log")  # httpx decodes it
        assert int(asset.headers.get("content-length", 0)) < len(asset.content) / 5
        small = client.get("/api/health", headers={"Accept-Encoding": "gzip"})
        assert "content-encoding" not in small.headers  # under minimum_size
        assert "immutable" in asset.headers["cache-control"]


def test_build_csp_without_inline_blocks():
    csp = build_csp()
    assert "script-src 'self';" in csp and "style-src 'self';" in csp


# --------------------------------------------------------------------------- DNS rebinding & demo


def test_auth_disabled_needs_allowed_hosts(tmp_path, fakes):
    from schema_app.config import StartupError
    from schema_app.main import create_app

    with pytest.raises(StartupError, match="SCHEMA_ALLOWED_HOSTS"):
        create_app(make_config(tmp_path, auth="disabled"))
    create_app(make_config(tmp_path, auth="disabled", allow_any_host=True))  # explicit opt-out
    app = create_app(make_config(tmp_path, auth="disabled", allowed_hosts="nas.lan"))
    with TestClient(app, base_url="http://nas.lan", headers=CLIENT_HEADERS) as client:
        assert client.get("/api/dashboard").status_code == 200
        rebound = client.get("/api/settings/backup?include_secrets=true", headers={"host": "evil.example"})
        assert rebound.status_code == 400
        assert client.get("/api/health", headers={"host": "127.0.0.1:8420"}).status_code == 200  # healthcheck


def test_auth_required_needs_no_allowed_hosts(tmp_path, fakes):
    from schema_app.main import create_app

    create_app(make_config(tmp_path))
    create_app(make_config(tmp_path, demo=True))  # the CI smoke test: docker run -e SCHEMA_DEMO=1


def test_demo_mode_still_needs_the_setup_code(make_env):
    env = make_env(demo=True, login=False, configure=False)
    status = env.client.get("/api/auth/status").json()
    assert status["demo"] is True and status["setup_token_required"] is True
    no_code = {k: v for k, v in ADMIN.items() if k != "setup_token"}
    assert env.client.post("/api/auth/setup", json=no_code).status_code == 403
    assert env.client.post("/api/auth/setup", json=ADMIN).status_code == 200


# --------------------------------------------------------------------------- health


def test_health_reports_a_broken_database(env):
    from sqlalchemy import create_engine

    real = env.ctx.db.engine
    broken = create_engine(f"sqlite:///{env.data / 'missing-dir' / 'nope.db'}")
    env.ctx.db.engine = broken
    try:
        response = env.client.get("/api/health")
    finally:
        env.ctx.db.engine = real
        broken.dispose()
    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "version": "0.1.0", "commit": None}
    assert env.client.get("/api/health").json()["status"] == "ok"


# --------------------------------------------------------------------------- login throttle


def _clocked(start: float = 1000.0) -> tuple[LoginThrottle, SimpleNamespace]:
    now = SimpleNamespace(t=start)
    return LoginThrottle(clock=lambda: now.t), now


def test_throttle_blocks_an_ip_but_never_the_username():
    throttle, _ = _clocked()
    for device in ("10.0.0.2", "10.0.0.3", "10.0.0.4"):
        for _ in range(throttle.PER_IP):
            throttle.reserve(device, "admin")  # a failure each (never released)
        with pytest.raises(Throttled) as blocked:
            throttle.reserve(device, "admin")
        assert blocked.value.retry_after > 0
    attempt = throttle.reserve("10.0.0.9", "ADMIN ")  # the admin's own device: no lockout, no delay
    assert attempt.delay == 0
    throttle.success(attempt)


def test_throttle_delay_grows_per_device_and_account():
    throttle, _ = _clocked()
    delays = [throttle.reserve("10.0.0.2", "admin").delay for _ in range(6)]
    assert delays == [0, 0.5, 1.0, 2.0, 4.0, 8.0]
    assert throttle.reserve("10.0.0.2", "other").delay == 0  # another account from that device
    # A second device that failed once waits as long as the account's total failures say.
    throttle.reserve("10.0.0.3", "admin")
    assert throttle.reserve("10.0.0.3", "admin").delay == throttle.MAX_DELAY


def test_throttle_reserves_before_the_password_check():
    """Concurrent attempts can't all pass the check before any failure is recorded."""
    throttle, now = _clocked()
    attempts = [throttle.reserve("10.0.0.2", f"user{i}") for i in range(throttle.PER_IP)]
    with pytest.raises(Throttled):
        throttle.reserve("10.0.0.2", "admin")
    throttle.release(attempts[0])  # an attempt that ended without an answer gives its slot back
    throttle.reserve("10.0.0.2", "admin")
    now.t += throttle.WINDOW + 1
    throttle.reserve("10.0.0.2", "admin")  # the window passed


def test_throttle_evicts_oldest_keys_instead_of_clearing():
    throttle, now = _clocked()
    throttle.MAX_KEYS = 50
    for _ in range(5):
        throttle.reserve("10.0.0.2", "admin")  # the admin's device: 3 keys
    for i in range(200):
        now.t += 0.01
        throttle.reserve(f"10.1.0.{i}")  # a flood of new keys
        if i % 20 == 0:  # the admin's device keeps trying now and then
            throttle.release(throttle.reserve("10.0.0.2", "admin"))
    assert len(throttle) <= throttle.MAX_KEYS
    assert throttle.reserve("10.0.0.2", "admin").delay > 0  # its counters survived the flood


def test_login_from_another_device_works_while_others_are_blocked(env, monkeypatch):
    monkeypatch.setattr("schema_app.api.auth.FAILED_LOGIN_DELAY", 0)
    monkeypatch.setattr(auth_mod, "verify_password", lambda password, _hash: password == ADMIN["password"])
    env.ctx.throttle.BASE_DELAY = 0
    wrong = {"username": "admin", "password": "wrong-password"}
    for n in range(3):
        device = TestClient(env.app, headers=CLIENT_HEADERS, client=(f"10.0.0.{n + 2}", 5000))
        for _ in range(env.ctx.throttle.PER_IP):
            assert device.post("/api/auth/login", json=wrong).status_code == 401
        assert device.post("/api/auth/login", json=ADMIN).status_code == 429
    admin = TestClient(env.app, headers=CLIENT_HEADERS, client=("10.0.0.9", 5000))
    assert admin.post("/api/auth/login", json=ADMIN).status_code == 200


def test_failed_login_delays_are_awaited(env, monkeypatch):
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("schema_app.api.auth._sleep", fake_sleep)
    wrong = {"username": "admin", "password": "wrong-password"}
    assert env.client.post("/api/auth/login", json=wrong).status_code == 401
    assert env.client.post("/api/auth/login", json=wrong).status_code == 401
    assert slept == [0.4, env.ctx.throttle.BASE_DELAY, 0.4]  # failure delay, then the progressive one


# --------------------------------------------------------------------------- password hashing


@pytest.mark.real_scrypt
def test_unknown_username_costs_one_scrypt(monkeypatch):
    from schema_app.auth import authenticate

    calls: list[int] = []
    real = auth_mod._scrypt

    def counting(*args, **kwargs):
        calls.append(args[2])
        return real(*args, **kwargs)

    monkeypatch.setattr(auth_mod, "_scrypt", counting)
    session = SimpleNamespace(scalar=lambda _query: None)
    assert authenticate(session, "nobody", "whatever-123") is None
    assert calls == [auth_mod._SCRYPT_N]


@pytest.mark.real_scrypt
def test_old_hashes_verify_and_are_upgraded_on_login(env, monkeypatch):
    from sqlalchemy import select

    from schema_app.models import User

    with monkeypatch.context() as patch:
        patch.setattr(auth_mod, "_SCRYPT_N", 2**14)
        legacy = auth_mod.hash_password(ADMIN["password"])
    assert legacy.startswith("scrypt$16384$") and auth_mod.needs_rehash(legacy)
    with env.ctx.db.session() as session:
        session.scalar(select(User)).password_hash = legacy
    env.client.cookies.clear()
    assert env.client.post("/api/auth/login", json=ADMIN).status_code == 200
    with env.ctx.db.session() as session:
        upgraded = session.scalar(select(User)).password_hash
    assert upgraded.startswith("scrypt$131072$8$1$") and not auth_mod.needs_rehash(upgraded)
    assert env.client.get("/api/jobs").status_code == 200  # the cookie binds to the upgraded hash
    assert auth_mod.verify_password(ADMIN["password"], upgraded)


def test_scrypt_refuses_absurd_stored_parameters():
    huge = "scrypt$1048576$64$1$" + "AAAAAAAAAAAAAAAAAAAAAA==$" + "A" * 88
    assert auth_mod.verify_password("x", huge) is False


# --------------------------------------------------------------------------- session cookie


def test_session_cookie_lifetime_is_a_week(make_env):
    env = make_env(login=False, configure=False)
    cookie = env.client.post("/api/auth/setup", json=ADMIN).headers["set-cookie"]
    assert "Max-Age=604800" in cookie and "schema_session=" in cookie


def _issued_days_ago(env, monkeypatch, days: float) -> str:
    from sqlalchemy import select

    from schema_app.models import User

    past = time.time() - days * 86400
    with env.ctx.db.session() as session:
        user = session.scalar(select(User))
        with monkeypatch.context() as patch:
            patch.setattr("itsdangerous.timed.time", SimpleNamespace(time=lambda: past))
            return env.ctx.signer.issue(user)


def _with_cookie(env, token: str) -> TestClient:
    client = TestClient(env.app, headers=CLIENT_HEADERS)
    client.cookies.set("schema_session", token)
    return client


def test_session_is_renewed_on_use_and_expires_when_idle(env, monkeypatch):
    fresh = env.client.get("/api/jobs")
    assert "set-cookie" not in fresh.headers  # a new cookie isn't re-issued on every request

    renewed = _with_cookie(env, _issued_days_ago(env, monkeypatch, 3)).get("/api/jobs")
    cookie = renewed.headers["set-cookie"]
    assert renewed.status_code == 200 and cookie.startswith("schema_session=") and "Max-Age=604800" in cookie
    token = cookie.split(";", 1)[0].split("=", 1)[1]
    assert env.ctx.signer.renewed(token) is None  # fresh again
    assert _with_cookie(env, token).get("/api/jobs").status_code == 200

    response = _with_cookie(env, _issued_days_ago(env, monkeypatch, 8)).get("/api/jobs")
    assert response.status_code == 401 and "set-cookie" not in response.headers


def test_renewal_skips_failed_and_cookie_setting_responses(env, monkeypatch):
    browser = _with_cookie(env, _issued_days_ago(env, monkeypatch, 3))
    assert "set-cookie" not in browser.get("/api/jobs/999999").headers  # 404
    out = browser.post("/api/auth/logout-all")
    assert out.status_code == 200
    assert all(c.startswith('schema_session=""') for c in out.headers.get_list("set-cookie"))


def test_https_uses_a_host_prefixed_cookie_and_reads_the_old_one(env):
    old = env.client.cookies.get("schema_session")
    https = TestClient(env.app, base_url="https://testserver", headers=CLIENT_HEADERS)
    https.cookies.set("schema_session", old)  # issued over plain HTTP before
    assert https.get("/api/jobs").status_code == 200
    login = https.post("/api/auth/login", json=ADMIN)
    cookies = login.headers.get_list("set-cookie")
    assert any(c.startswith("__Host-schema_session=") and "Secure" in c and "Path=/" in c for c in cookies)
    assert any(c.startswith('schema_session=""') for c in cookies)  # the plain cookie is dropped
    token = next(c.split(";", 1)[0].split("=", 1)[1] for c in cookies if c.startswith("__Host-"))
    reader = TestClient(env.app, base_url="https://testserver", headers=CLIENT_HEADERS)
    reader.cookies.set("__Host-schema_session", token)
    assert reader.get("/api/jobs").status_code == 200
    out = reader.post("/api/auth/logout")
    assert any(c.startswith('__Host-schema_session=""') for c in out.headers.get_list("set-cookie"))


def test_logout_needs_the_client_header(env):
    raw = TestClient(env.app)
    raw.cookies = env.client.cookies
    assert raw.post("/api/auth/logout", headers={"content-type": "application/json"}, content=b"{}").status_code == 403
    assert env.client.get("/api/jobs").status_code == 200  # still signed in
    assert raw.post("/api/auth/logout", headers={"X-Schema-Client": "web"}).status_code == 200


def test_renewal_keeps_the_payload():
    signer = auth_mod.SessionSigner("k" * 40)
    payload = {"uid": 1, "fp": "abc", "ep": 2}
    token = signer._serializer.dumps(payload)
    assert signer.renewed(token) is None  # too fresh
    again = signer.renewed(token, older_than=0)
    assert again and signer.read(again) == payload
