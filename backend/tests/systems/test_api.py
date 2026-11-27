"""Every HTTP route (happy path + key 4xx), auth flow, CSRF, WebSocket and SPA serving."""

from __future__ import annotations

import pytest
from conftest import ADMIN, CLIENT_HEADERS, make_config
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from schema_app.api_models import SECRET_MASK

# --------------------------------------------------------------------------- health & auth


def test_health_is_public(make_env):
    env = make_env(login=False, configure=False)
    env.client.cookies.clear()
    response = env.client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.1.0", "commit": None}


def test_health_reports_the_build_commit_to_signed_in_browsers(make_env, monkeypatch):
    sha = "89361e3a1b2c3d4e5f60718293a4b5c6d7e8f901"
    monkeypatch.setenv("SCHEMA_COMMIT", sha.upper())
    env = make_env(configure=False)
    assert env.client.get("/api/health").json()["commit"] == sha  # Settings > About sends the cookie
    anonymous = TestClient(env.app)
    assert anonymous.get("/api/health").json() == {"status": "ok", "version": "0.1.0", "commit": None}
    monkeypatch.setenv("SCHEMA_COMMIT", "not-a-sha; rm -rf /")
    assert env.client.get("/api/health").json()["commit"] is None


def test_auth_setup_login_logout_flow(make_env):
    env = make_env(login=False, configure=False)
    client = env.client
    status = client.get("/api/auth/status").json()
    assert status == {"setup_required": True, "authenticated": False, "username": None, "auth_enabled": True,
                      "demo": False, "version": "0.1.0", "setup_token_required": True}  # fmt: skip
    assert client.get("/api/dashboard").status_code == 401
    assert client.post("/api/auth/setup", json={"username": "ad", "password": "short"}).status_code == 422

    response = client.post("/api/auth/setup", json=ADMIN)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "schema_session=" in cookie and "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    assert "Secure" not in cookie
    assert response.json()["authenticated"] is True and response.json()["username"] == "admin"
    assert client.get("/api/dashboard").status_code == 200
    assert client.post("/api/auth/setup", json=ADMIN).status_code == 409

    out = client.post("/api/auth/logout")
    assert out.status_code == 200 and out.json()["authenticated"] is False
    client.cookies.clear()
    assert client.get("/api/dashboard").status_code == 401
    assert client.get("/api/auth/status").json()["setup_required"] is False

    bad = client.post("/api/auth/login", json={"username": "admin", "password": "wrong-password"})
    assert bad.status_code == 401
    assert client.post("/api/auth/login", json={"username": "nobody", "password": "whatever-123"}).status_code == 401
    good = client.post("/api/auth/login", json={"username": "ADMIN", "password": ADMIN["password"]})
    assert good.status_code == 200 and good.json()["authenticated"] is True
    assert client.get("/api/jobs").status_code == 200


def test_setup_requires_the_setup_code(make_env):
    env = make_env(login=False, configure=False)
    client = env.client
    no_code = {k: v for k, v in ADMIN.items() if k != "setup_token"}
    assert client.post("/api/auth/setup", json=no_code).status_code == 403
    assert client.post("/api/auth/setup", json={**ADMIN, "setup_token": "wrong"}).status_code == 403
    assert client.get("/api/auth/status").json()["setup_required"] is True
    assert client.post("/api/auth/setup", json=ADMIN).status_code == 200
    assert client.get("/api/auth/status").json()["setup_token_required"] is False


def test_generated_setup_code_is_logged(make_env, caplog):
    with caplog.at_level("WARNING", logger="schema"):
        env = make_env(login=False, configure=False, setup_token=None)
    code = env.app.state.ctx.setup_token
    assert code and code in caplog.text
    assert env.client.post("/api/auth/setup", json={**ADMIN, "setup_token": code}).status_code == 200


def test_failed_logins_are_throttled_before_password_checks(env, monkeypatch):
    monkeypatch.setattr("schema_app.api.auth.FAILED_LOGIN_DELAY", 0)
    env.client.cookies.clear()
    limit = env.app.state.ctx.throttle.PER_IP
    env.app.state.ctx.throttle.BASE_DELAY = 0  # no progressive delay in this test
    env.app.state.ctx.throttle._clock = lambda: 1000.0  # freeze the window
    for _ in range(limit):
        bad = env.client.post("/api/auth/login", json={"username": "admin", "password": "wrong-password"})
        assert bad.status_code == 401
    blocked = env.client.post("/api/auth/login", json={"username": "admin", "password": ADMIN["password"]})
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) > 0
    env.app.state.ctx.throttle._clock = lambda: 1000.0 + env.app.state.ctx.throttle.WINDOW + 1
    assert env.client.post("/api/auth/login", json=ADMIN).status_code == 200


def test_websocket_rejects_foreign_origin(env):
    from starlette.websockets import WebSocketDisconnect

    foreign = {"origin": "http://testserver:8080"}  # same host, another port: same-site for SameSite=Lax
    with pytest.raises(WebSocketDisconnect), env.client.websocket_connect("/api/ws", headers=foreign) as ws:
        ws.receive_json()
    with env.client.websocket_connect("/api/ws", headers={"origin": "http://testserver"}) as ws:
        assert ws.receive_json()["type"] == "hello"


def test_api_docs_are_off_by_default(env):
    assert env.client.get("/api/openapi.json").status_code != 200
    assert env.client.get("/api/docs").status_code != 200


def test_tampered_cookie_is_rejected(env):
    env.client.cookies.set("schema_session", "garbage.value.sig")
    assert env.client.get("/api/jobs").status_code == 401


def test_secure_cookie_behind_https_proxy(make_env):
    env = make_env(login=False, configure=False)
    response = env.client.post("/api/auth/setup", json=ADMIN, headers={"X-Forwarded-Proto": "https"})
    cookies = response.headers.get_list("set-cookie")
    assert any(c.startswith("__Host-schema_session=") and "Secure" in c for c in cookies)


def test_csrf_requires_client_header_or_json(env):
    raw = TestClient(env.app)  # no X-Schema-Client default header
    raw.cookies = env.client.cookies
    assert raw.post("/api/settings/api-key").status_code == 403
    assert raw.post("/api/settings/api-key", headers={"X-Schema-Client": "web"}).status_code == 200
    assert raw.post("/api/jobs/1/cancel", content=b"x", headers={"content-type": "text/plain"}).status_code == 403
    assert raw.get("/api/settings").status_code == 200  # reads are fine


def test_auth_disabled_mode(make_env):
    env = make_env(login=False, auth="disabled", allowed_hosts="testserver")
    status = env.client.get("/api/auth/status").json()
    assert status["auth_enabled"] is False and status["authenticated"] is True and status["setup_required"] is False
    assert env.client.get("/api/dashboard").status_code == 200
    assert env.client.post("/api/auth/setup", json=ADMIN).status_code == 409
    assert env.client.post("/api/auth/login", json=ADMIN).json()["authenticated"] is True


# --------------------------------------------------------------------------- dashboard & events


def test_dashboard_shape(env):
    env.touch("Film.2010.mkv")
    env.create_job(env.incoming, auto_execute=True)
    data = env.client.get("/api/dashboard").json()
    assert set(data) == {"queue", "totals", "active_jobs", "recent_operations", "recent_errors", "watchers", "providers"}
    assert data["queue"]["completed"] >= 0 and "queued" in data["queue"]
    assert data["providers"][0]["name"] == "fake"
    assert isinstance(data["totals"]["files_renamed_total"], int)


def test_events_filters(env):
    env.ctx.bus.log("error", "webhook", "boom", job_id=7, token="secret-value", detail="x")
    env.ctx.bus.log("warning", "watcher", "hmm")
    errors = env.client.get("/api/events", params={"level": "error"}).json()
    assert [e["message"] for e in errors] == ["boom"]
    assert errors[0]["data"] == {"detail": "x"}  # secrets are scrubbed
    assert [e["message"] for e in env.client.get("/api/events", params={"category": "watcher"}).json()] == ["hmm"]
    assert [e["message"] for e in env.client.get("/api/events", params={"job_id": 7}).json()] == ["boom"]
    assert len(env.client.get("/api/events", params={"limit": 1}).json()) == 1
    assert env.client.get("/api/events", params={"limit": 501}).status_code == 422


# --------------------------------------------------------------------------- parse & search


def test_parse_and_search(env):
    parsed = env.client.post("/api/parse", json={"names": ["Show.S01E02.mkv", "Film.2010.mkv"]}).json()
    assert parsed[0]["episodes"] == [2] and parsed[1]["year"] == 2010
    assert env.client.post("/api/parse", json={"names": []}).status_code == 422
    found = env.client.get("/api/search", params={"q": "Matrix", "kind": "movie", "year": 1999}).json()
    assert found[0]["title"] == "Matrix" and found[0]["year"] == 1999
    assert env.client.get("/api/search", params={"q": ""}).status_code == 422


# --------------------------------------------------------------------------- formats


def test_formats_seeded_and_catalog(env):
    formats = env.client.get("/api/formats").json()
    defaults = {f["kind"]: f for f in formats if f["is_default"]}
    assert set(defaults) == {"movie", "series", "anime"}
    assert all(not f["builtin"] for f in defaults.values())
    assert any(f["builtin"] for f in formats)
    catalog = env.client.get("/api/formats/catalog").json()
    names = {b["name"] for b in catalog["bindings"]}
    assert {"n", "s00e00", "t", "plex", "vf"} <= names
    assert {"pad", "colon"} <= {f["name"] for f in catalog["filters"]}
    assert any(p["id"] == "plex-series" for p in catalog["presets"])


def test_formats_crud_and_default_switch(env):
    created = env.client.post("/api/formats", json={"name": "Mine", "kind": "movie", "expression": "{n}", "is_default": True})
    assert created.status_code == 201
    fmt = created.json()
    defaults = [f for f in env.client.get("/api/formats").json() if f["kind"] == "movie" and f["is_default"]]
    assert [f["id"] for f in defaults] == [fmt["id"]]
    updated = env.client.put(f"/api/formats/{fmt['id']}", json={"name": "Mine2", "kind": "movie", "expression": "{ny}"})
    assert updated.status_code == 200 and updated.json()["expression"] == "{ny}" and updated.json()["is_default"] is False
    assert env.client.post("/api/formats", json={"name": "x", "kind": "movie", "expression": "{n"}).status_code == 400
    assert env.client.put(f"/api/formats/{fmt['id']}", json={"name": "x", "kind": "movie", "expression": "{"}).status_code == 400
    assert env.client.delete(f"/api/formats/{fmt['id']}").status_code == 204
    assert env.client.delete(f"/api/formats/{fmt['id']}").status_code == 404
    assert env.client.put("/api/formats/9999", json={"name": "x", "kind": "movie", "expression": "{n}"}).status_code == 404


def test_builtin_formats_are_read_only(env):
    builtin = next(f for f in env.client.get("/api/formats").json() if f["builtin"])
    body = {"name": "x", "kind": builtin["kind"], "expression": "{n}"}
    assert env.client.put(f"/api/formats/{builtin['id']}", json=body).status_code == 409
    assert env.client.delete(f"/api/formats/{builtin['id']}").status_code == 409


def test_format_validate(env):
    ok = env.client.post("/api/formats/validate", json={"expression": "{n} - {s00e00}{' - ' + t}", "kind": "series"}).json()
    assert ok["ok"] is True
    assert ok["samples"] and all(s["output"] for s in ok["samples"])
    bad = env.client.post("/api/formats/validate", json={"expression": "{n.nope()}", "kind": "movie"}).json()
    assert bad["ok"] is False
    assert bad["issues"][0]["start"] >= 0 and bad["issues"][0]["severity"] == "error"
    syntax = env.client.post("/api/formats/validate", json={"expression": "{n", "kind": "movie"}).json()
    assert syntax["ok"] is False and syntax["samples"] == []
    mine = env.client.post("/api/formats/validate",
                           json={"expression": "{fn}", "kind": "movie", "samples": ["Film.2010.mkv"]}).json()  # fmt: skip
    assert mine["samples"] == [{"source": "Film.2010.mkv", "output": "Film.2010.mkv", "error": None}]


# --------------------------------------------------------------------------- settings & providers


def test_settings_roundtrip_and_masking(env):
    current = env.client.get("/api/settings").json()
    assert current["webhook_api_key"] == SECRET_MASK
    current["media_servers"] = [{"id": "s1", "kind": "jellyfin", "name": "JF", "url": "http://jf", "token": "tok"}]
    current["min_confidence"] = 0.7
    saved = env.client.put("/api/settings", json=current).json()
    assert saved["media_servers"][0]["token"] == SECRET_MASK
    assert saved["min_confidence"] == 0.7
    saved["media_servers"][0]["name"] = "Jellyfin"
    env.client.put("/api/settings", json=saved)
    assert env.ctx.store.get_app().media_servers[0].token == "tok"  # mask kept the stored secret
    bad = dict(saved, allowed_roots=["relative/root"])
    assert env.client.put("/api/settings", json=bad).status_code == 400
    bad = dict(saved, media_servers=[{"id": "new", "kind": "plex", "name": "P", "url": "u", "token": SECRET_MASK}])
    assert env.client.put("/api/settings", json=bad).status_code == 400
    # A masked token can't follow the server to a new URL (it would be sent to whoever runs that host).
    moved = dict(saved, media_servers=[dict(saved["media_servers"][0], url="http://attacker.example")])
    assert env.client.put("/api/settings", json=moved).status_code == 400
    probe = dict(saved["media_servers"][0], url="http://attacker.example")
    assert env.client.post("/api/mediaservers/test", json=probe).status_code == 400
    assert env.ctx.store.get_app().media_servers[0].url == "http://jf"
    assert env.client.put("/api/settings", json=dict(saved, min_confidence=2)).status_code == 422


def test_api_key_reveal_and_rotate(env):
    key = env.client.get("/api/settings/api-key").json()["api_key"]
    assert len(key) > 20
    rotated = env.client.post("/api/settings/api-key").json()["api_key"]
    assert rotated != key
    assert env.client.get("/api/settings/api-key").json()["api_key"] == rotated


def test_providers_get_put_test(env):
    out = env.client.get("/api/providers").json()
    assert out["statuses"][0]["name"] == "fake"
    assert set(out["credentials"]) >= {"tmdb_api_key", "tvdb_api_key", "anidb_client_version"}
    assert out["credentials"]["tmdb_api_key"] is None
    body = {"credentials": {"tmdb_api_key": "abc", "anidb_client_version": "3"}, "language": "de",
            "priority": {"movie": ["trakt", "tmdb"]}, "disabled": ["tvmaze"]}  # fmt: skip
    saved = env.client.put("/api/providers", json=body).json()
    assert saved["credentials"]["tmdb_api_key"] == SECRET_MASK
    assert saved["credentials"]["anidb_client_version"] == "3"
    assert saved["language"] == "de" and saved["priority"]["movie"] == ["trakt", "tmdb"]
    assert saved["disabled"] == ["tvmaze"]
    applied = env.fakes.matcher.reconfigured[-1]
    assert applied.credentials.tmdb_api_key == "abc" and applied.credentials.anidb_client_version == 3
    env.client.put("/api/providers", json={"credentials": {"tmdb_api_key": SECRET_MASK, "anidb_client_version": ""}})
    creds = env.ctx.store.get_providers().credentials
    assert creds.tmdb_api_key == "abc" and creds.anidb_client_version is None
    assert env.client.put("/api/providers", json={"credentials": {"bogus": "x"}}).status_code == 400
    assert env.client.put("/api/providers", json={"credentials": {"anidb_client_version": "x"}}).status_code == 400
    tested = env.client.post("/api/providers/fake/test")
    assert tested.status_code == 200 and tested.json()["healthy"] is True
    assert env.client.post("/api/providers/nope/test").status_code == 404


def test_mediaserver_test_substitutes_stored_token(env):
    env.settings(media_servers=[{"id": "s1", "kind": "plex", "name": "Plex", "url": "http://p", "token": "good-token"}])
    masked = env.client.get("/api/settings").json()["media_servers"][0]
    result = env.client.post("/api/mediaservers/test", json=masked).json()
    assert result == {"ok": True, "message": "Fake server 1.0"}
    fresh = {"id": "s2", "kind": "plex", "name": "New", "url": "http://p", "token": "bad"}
    assert env.client.post("/api/mediaservers/test", json=fresh).json()["ok"] is False
    unknown = dict(fresh, token=SECRET_MASK)
    assert env.client.post("/api/mediaservers/test", json=unknown).status_code == 400


# --------------------------------------------------------------------------- watch folders


def test_watch_folder_crud(env, tmp_path):
    body = {"path": str(env.incoming), "kind": "series", "settle_seconds": 5}
    created = env.client.post("/api/watch-folders", json=body)
    assert created.status_code == 201
    folder = created.json()
    assert folder["kind"] == "series" and folder["status"] == "watching" and folder["auto_execute"] is True
    assert env.client.post("/api/watch-folders", json=body).status_code == 409
    assert env.client.post("/api/watch-folders", json={"path": str(tmp_path)}).status_code == 403
    assert env.client.post("/api/watch-folders", json={"path": "relative"}).status_code == 400
    assert env.client.post("/api/watch-folders", json={"path": str(env.root / "x"), "settle_seconds": 0}).status_code == 422
    updated = env.client.put(f"/api/watch-folders/{folder['id']}", json=dict(body, enabled=False)).json()
    assert updated["enabled"] is False and updated["status"] == "stopped"
    assert env.fakes.watchers[-1].specs[0].enabled is False
    assert env.client.put("/api/watch-folders/999", json=body).status_code == 404
    assert env.client.delete(f"/api/watch-folders/{folder['id']}").status_code == 204
    assert env.client.get("/api/watch-folders").json() == []
    assert env.fakes.watchers[-1].specs == []
    assert env.client.delete(f"/api/watch-folders/{folder['id']}").status_code == 404


def test_settings_change_restarts_watcher(env):
    before = len(env.fakes.watchers)
    env.settings(ignore_patterns=["*.tmp", "*.junk"])
    assert len(env.fakes.watchers) == before + 1
    assert env.fakes.watchers[-1].ignore_patterns == ["*.tmp", "*.junk"]


# --------------------------------------------------------------------------- filesystem browser


def test_fs_list(env, tmp_path):
    env.touch("Film.2010.mkv")
    env.touch("notes.txt")
    (env.incoming / "Sub").mkdir()
    default = env.client.get("/api/fs/list").json()
    assert default["path"] == str(env.root) and default["roots"] == [str(env.root)]
    assert {e["name"] for e in default["entries"]} == {"incoming", "library"}
    listing = env.client.get("/api/fs/list", params={"path": str(env.incoming)}).json()
    assert [e["name"] for e in listing["entries"]] == ["Sub", "Film.2010.mkv", "notes.txt"]
    by_name = {e["name"]: e for e in listing["entries"]}
    assert by_name["Film.2010.mkv"]["is_video"] is True and by_name["notes.txt"]["is_video"] is False
    assert by_name["Sub"]["is_dir"] is True and by_name["Film.2010.mkv"]["size"] == len(b"video-bytes")
    assert listing["parent"] == str(env.root)
    assert env.client.get("/api/fs/list", params={"path": str(env.root)}).json()["parent"] is None
    assert env.client.get("/api/fs/list", params={"path": str(tmp_path)}).status_code == 403
    assert env.client.get("/api/fs/list", params={"path": str(env.root / "missing")}).status_code == 404
    assert env.client.get("/api/fs/list", params={"path": str(env.incoming / "notes.txt")}).status_code == 400
    assert env.client.get("/api/fs/list", params={"path": "relative"}).status_code == 400


def test_fs_list_without_roots(make_env):
    env = make_env(configure=False)
    assert env.client.get("/api/fs/list").json() == {"path": "", "parent": None, "entries": [], "roots": []}


# --------------------------------------------------------------------------- webhooks


def test_webhooks(env, tmp_path):
    key = env.client.get("/api/settings/api-key").json()["api_key"]
    src = env.touch("Film.2010.mkv")
    anon = TestClient(env.app)  # no cookie, no client header
    assert anon.post("/api/webhooks/generic", json={"path": str(src)}).status_code == 401
    assert anon.post("/api/webhooks/generic", json={"path": str(src)}, headers={"X-Api-Key": "wrong"}).status_code == 401
    assert anon.post("/api/webhooks/nzbget", json={}, headers={"X-Api-Key": key}).status_code == 404
    assert anon.post("/api/webhooks/generic", json={}, headers={"X-Api-Key": key}).status_code == 400
    assert anon.post("/api/webhooks/generic", content=b"{nope", headers={"X-Api-Key": key,
                     "content-type": "application/json"}).status_code == 400  # fmt: skip
    outside = tmp_path / "x.mkv"
    outside.write_bytes(b"x")
    assert anon.post("/api/webhooks/generic", json={"path": str(outside)}, headers={"X-Api-Key": key}).status_code == 403
    # Inside the allowed roots is not enough: a webhook job runs on its own, so it is held to the
    # download area (watch folders and path-mapping targets).
    refused = anon.post("/api/webhooks/generic", json={"path": str(src)}, headers={"X-Api-Key": key})
    assert refused.status_code == 403 and "download folder" in refused.json()["detail"]
    env.settings(path_mappings=[{"source": "/downloads", "target": str(env.incoming)}])
    for path in (env.root, env.incoming, env.library / "Movies", env.library / "Movies" / "Film (2010)"):
        hit = anon.post("/api/webhooks/generic", json={"path": str(path)}, headers={"X-Api-Key": key})
        assert hit.status_code == 403, path
    assert env.client.get("/api/jobs").json() == []

    accepted = anon.post(f"/api/webhooks/qbittorrent?apikey={key}", data={"path": str(src), "category": "movies",
                                                                          "name": "Film"})  # fmt: skip
    assert accepted.status_code == 202
    body = accepted.json()
    assert body["paths"] == [str(src)]
    job = env.wait(body["job_id"])
    assert job["source"] == "webhook" and job["auto_execute"] is True and job["kind"] == "movie"
    assert job["label"] == "Film"


# --------------------------------------------------------------------------- operations


def test_operations_list_filters(env):
    env.touch("Film.2010.mkv")
    job = env.create_job(env.incoming, auto_execute=True)
    env.wait(job["id"])
    ops = env.client.get("/api/operations").json()
    assert len(ops) == 1
    assert env.client.get("/api/operations", params={"job_id": job["id"]}).json() == ops
    assert env.client.get("/api/operations", params={"job_id": 999}).json() == []
    assert env.client.get("/api/operations", params={"status": "undone"}).json() == []
    assert env.client.post("/api/operations/999/undo").status_code == 404


# --------------------------------------------------------------------------- demo


def test_demo_reset_is_404_outside_demo(env):
    assert env.client.post("/api/demo/reset").status_code == 404


# --------------------------------------------------------------------------- websocket


def test_websocket_hello_and_push(env):
    with env.client.websocket_connect("/api/ws") as ws:
        assert ws.receive_json() == {"type": "hello", "data": {"version": "0.1.0"}}
        env.touch("Film.2010.mkv")
        response = env.client.post("/api/jobs", json={"paths": [str(env.incoming)]})
        job_id = response.json()["id"]
        seen = []
        changed = []
        while True:
            message = ws.receive_json()
            seen.append(message["type"])
            if message["type"] == "job.items_changed":
                changed.append(message["data"])
            if message["type"] == "job.updated" and message["data"]["status"] == "ready":
                break
        assert "job.items_changed" in seen and "event" in seen
        assert message["data"]["id"] == job_id
        # matching pushes batches, not one message per file: the job and the ids of its saved items
        item_id = env.client.get(f"/api/jobs/{job_id}").json()["items"][0]["id"]
        assert {"job_id": job_id, "item_ids": [item_id]} in changed


def test_websocket_requires_session(env):
    anon = TestClient(env.app)
    with pytest.raises(WebSocketDisconnect), anon.websocket_connect("/api/ws") as ws:
        ws.receive_json()


# --------------------------------------------------------------------------- SPA


def test_spa_serving_with_history_fallback(tmp_path, fakes):
    from schema_app.main import create_app

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>schema</html>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    (dist / "brand").mkdir()
    (dist / "brand" / "mark.png").write_bytes(b"png")
    with TestClient(create_app(make_config(tmp_path, static_dir=dist)), headers=CLIENT_HEADERS) as client:
        assert client.get("/").text == "<html>schema</html>"
        assert client.get("/jobs/12").text == "<html>schema</html>"
        asset = client.get("/assets/app.js")
        assert asset.text == "console.log(1)" and "immutable" in asset.headers["cache-control"]
        assert client.get("/brand/mark.png").content == b"png"
        assert client.get("/assets/missing.js").status_code == 404
        assert client.get("/api/does-not-exist").status_code == 401  # /api needs a session first
        assert client.get("/../secret").text == "<html>schema</html>"
        assert client.get("/api/health").json()["status"] == "ok"


def test_missing_spa_dir_serves_api_only(env):
    assert env.client.get("/").status_code == 404
    assert env.client.get("/api/health").status_code == 200


# --------------------------------------------------------------------------- session revocation

NEW_PASSWORD = "a-brand-new-secret"


def _browser(env, cookie: str) -> TestClient:
    """Another browser holding a copy of ``cookie`` (e.g. a second device, or a stolen cookie)."""
    other = TestClient(env.app, headers=CLIENT_HEADERS)
    other.cookies.set("schema_session", cookie)
    return other


def test_logout_all_revokes_old_cookies(env):
    old = env.client.cookies.get("schema_session")
    other = _browser(env, old)
    assert other.get("/api/jobs").status_code == 200
    raw = TestClient(env.app)  # no X-Schema-Client header: CSRF rule applies
    raw.cookies.set("schema_session", old)
    assert raw.post("/api/auth/logout-all").status_code == 403

    out = env.client.post("/api/auth/logout-all")
    assert out.status_code == 200 and out.json()["authenticated"] is False
    assert 'schema_session=""' in out.headers["set-cookie"] or "Max-Age=0" in out.headers["set-cookie"]
    assert other.get("/api/jobs").status_code == 401  # the copy is dead too
    assert other.post("/api/auth/logout-all").status_code == 401

    env.client.cookies.clear()
    assert env.client.post("/api/auth/login", json=ADMIN).status_code == 200  # new epoch, new cookie
    assert env.client.get("/api/jobs").status_code == 200
    assert other.get("/api/jobs").status_code == 401


def test_plain_logout_only_signs_out_this_browser(env):
    other = _browser(env, env.client.cookies.get("schema_session"))
    assert env.client.post("/api/auth/logout").status_code == 200
    assert other.get("/api/jobs").status_code == 200


def test_logout_all_closes_open_websockets(env):
    with env.client.websocket_connect("/api/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        assert len(env.app.state.ctx.session_watchers) == 1
        assert env.client.post("/api/auth/logout-all").status_code == 200
        with pytest.raises(WebSocketDisconnect) as closed:
            for _ in range(50):  # the "signed out" event may arrive first
                ws.receive_json()
        assert closed.value.code == 1008


def test_password_change_revokes_other_sessions_and_keeps_this_one(env):
    other = _browser(env, env.client.cookies.get("schema_session"))
    short = {"current_password": ADMIN["password"], "new_password": "short"}
    assert env.client.post("/api/auth/password", json=short).status_code == 422

    body = {"current_password": ADMIN["password"], "new_password": NEW_PASSWORD}
    response = env.client.post("/api/auth/password", json=body)
    assert response.status_code == 200 and response.json()["authenticated"] is True
    assert "schema_session=" in response.headers["set-cookie"] and "HttpOnly" in response.headers["set-cookie"]
    assert env.client.get("/api/jobs").status_code == 200  # this browser got a fresh cookie
    assert other.get("/api/jobs").status_code == 401

    env.client.cookies.clear()
    assert env.client.post("/api/auth/login", json=ADMIN).status_code == 401
    assert env.client.post("/api/auth/login", json={"username": "admin", "password": NEW_PASSWORD}).status_code == 200


def test_wrong_current_password_is_401_and_throttled(env, monkeypatch):
    monkeypatch.setattr("schema_app.api.auth.FAILED_LOGIN_DELAY", 0)
    throttle = env.app.state.ctx.throttle
    throttle.BASE_DELAY = 0  # no progressive delay in this test
    throttle._clock = lambda: 1000.0  # freeze the window
    body = {"current_password": "wrong-password", "new_password": NEW_PASSWORD}
    for _ in range(throttle.PER_IP):
        assert env.client.post("/api/auth/password", json=body).status_code == 401
    assert env.client.get("/api/jobs").status_code == 200  # a wrong password doesn't sign you out
    right = {**body, "current_password": ADMIN["password"]}
    blocked = env.client.post("/api/auth/password", json=right)
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) > 0
    throttle._clock = lambda: 1000.0 + throttle.WINDOW + 1
    assert env.client.post("/api/auth/password", json=right).status_code == 200


def test_session_endpoints_need_a_session(make_env):
    env = make_env(login=True, configure=False)
    env.client.cookies.clear()
    assert env.client.post("/api/auth/logout-all").status_code == 401
    body = {"current_password": ADMIN["password"], "new_password": NEW_PASSWORD}
    assert env.client.post("/api/auth/password", json=body).status_code == 401


def test_session_endpoints_conflict_when_auth_disabled(make_env):
    env = make_env(login=False, auth="disabled", allowed_hosts="testserver")
    assert env.client.post("/api/auth/logout-all").status_code == 409
    body = {"current_password": "whatever-123", "new_password": NEW_PASSWORD}
    assert env.client.post("/api/auth/password", json=body).status_code == 409


def test_pre_epoch_cookie_still_works(env):
    """Cookies issued before the upgrade carry no epoch: they count as epoch 0 and stay valid."""
    from sqlalchemy import select

    from schema_app.auth import _password_fingerprint
    from schema_app.models import User

    ctx = env.app.state.ctx
    with ctx.db.session() as session:
        user = session.scalar(select(User))
        legacy = ctx.signer._serializer.dumps({"uid": user.id, "fp": _password_fingerprint(user.password_hash)})
    env.client.cookies.clear()
    env.client.cookies.set("schema_session", legacy)
    assert env.client.get("/api/jobs").status_code == 200
    assert env.client.post("/api/auth/logout-all").status_code == 200
    env.client.cookies.set("schema_session", legacy)
    assert env.client.get("/api/jobs").status_code == 401  # epoch 0 != 1


def test_v2_database_upgrade_adds_session_epoch(tmp_path):
    from sqlalchemy import select, text

    from schema_app.db import SCHEMA_VERSION, Database
    from schema_app.models import User

    db = Database(tmp_path / "v2.db")
    with db.engine.begin() as conn:  # the v2 users table, before session_epoch existed
        conn.execute(text(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(64) NOT NULL UNIQUE, "
            "password_hash VARCHAR(255) NOT NULL, created_at DATETIME NOT NULL, last_login_at DATETIME)"
        ))
        conn.execute(text("INSERT INTO users (username, password_hash, created_at) "
                          "VALUES ('admin', 'x', '2026-01-01 00:00:00')"))
        conn.execute(text("PRAGMA user_version = 2"))
    db.init()
    with db.engine.connect() as conn:
        columns = {row[1]: row for row in conn.execute(text("PRAGMA table_info(users)"))}
    assert "session_epoch" in columns and columns["session_epoch"][3] == 1  # NOT NULL
    assert db.user_version() == SCHEMA_VERSION == 6
    with db.session() as session:
        assert session.scalar(select(User)).session_epoch == 0
    db.init()  # idempotent
    db.dispose()


# --------------------------------------------------------------------------- item paging & slim payloads


def _paging_job(env) -> dict:
    for n in range(1, 6):
        env.touch(f"Show.S01E0{n}.1080p.mkv")
    env.touch("Film.2010.mkv")
    env.touch("unknown.thing.2011.mkv")  # -> review
    return env.create_job(env.incoming)


def test_job_items_paging_counts_and_filters(env):
    job = _paging_job(env)
    assert job["total"] == 7 and job["items_total"] == 7 and job["next_after"] is None
    assert len(job["items"]) == 7 and all(not i["slim"] for i in job["items"])  # default stays full
    all_ids = [i["id"] for i in job["items"]]

    seen: list[int] = []
    after = None
    while True:
        params = {"limit": 3, **({"after": after} if after is not None else {})}
        page = env.client.get(f"/api/jobs/{job['id']}/items", params=params).json()
        assert page["total"] == 7 and page["limit"] == 3
        assert page["counts"] == job["counts"]
        seen += [i["id"] for i in page["items"]]
        after = page["next_after"]
        if after is None:
            break
    assert seen == all_ids

    by_offset = env.client.get(f"/api/jobs/{job['id']}/items", params={"limit": 2, "offset": 5}).json()
    assert [i["id"] for i in by_offset["items"]] == all_ids[5:] and by_offset["next_after"] is None

    review = env.client.get(f"/api/jobs/{job['id']}/items", params={"status": "review"}).json()
    assert review["total"] == 1 and review["items"][0]["filename"] == "unknown.thing.2011.mkv"
    both = env.client.get(f"/api/jobs/{job['id']}/items", params={"status": "review,matched"}).json()
    assert both["total"] == 7
    repeated = env.client.get(f"/api/jobs/{job['id']}/items", params=[("status", "review"), ("status", "matched")])
    assert repeated.json()["total"] == 7
    shows = env.client.get(f"/api/jobs/{job['id']}/items", params={"q": "show.s01"}).json()
    assert shows["total"] == 5
    by_title = env.client.get(f"/api/jobs/{job['id']}/items", params={"q": "FILM"}).json()
    assert [i["filename"] for i in by_title["items"]] == ["Film.2010.mkv"]
    assert env.client.get(f"/api/jobs/{job['id']}/items", params={"q": "100%_"}).json()["total"] == 0

    header = env.client.get(f"/api/jobs/{job['id']}", params={"limit": 0}).json()
    assert header["items"] == [] and header["items_total"] == 7 and header["counts"] == job["counts"]
    filtered = env.client.get(f"/api/jobs/{job['id']}", params={"status": "review", "view": "slim"}).json()
    assert filtered["items_total"] == 1 and filtered["total"] == 7 and filtered["items"][0]["slim"] is True

    assert env.client.get(f"/api/jobs/{job['id']}/items", params={"status": "bogus"}).status_code == 422
    assert env.client.get(f"/api/jobs/{job['id']}/items", params={"limit": 5000}).status_code == 422
    assert env.client.get(f"/api/jobs/{job['id']}/items", params={"limit": 0}).status_code == 422
    assert env.client.get("/api/jobs/999/items").status_code == 404


def test_slim_items_and_full_item_endpoint(env):
    job = _paging_job(env)
    full = {i["id"]: i for i in job["items"]}
    page = env.client.get(f"/api/jobs/{job['id']}/items").json()
    for item in page["items"]:
        source = full[item["id"]]
        assert item["slim"] is True
        assert item["candidate_count"] == len(source["candidates"]) == source["candidate_count"]
        assert len(item["candidates"]) <= 1
        # every field the list rows render is still there
        for key in ("status", "filename", "parsed", "proposed_path", "proposed_relative", "confidence", "warnings",
                    "error", "crc_ok", "final_path", "path_overridden", "selected_candidate_id"):  # fmt: skip
            assert item[key] == source[key], key
        if item["candidates"]:
            chosen = item["candidates"][0]
            assert chosen["id"] == (source["selected_candidate_id"] or source["candidates"][0]["id"])
            assert chosen["reasons"] == []
            info = chosen["movie"] or chosen["series"]
            assert info["ids"] == {} and info["aliases"] == [] and info["overview"] is None
            assert all(e["ids"] == {} for e in chosen["episodes"])

    show = next(i for i in job["items"] if i["filename"].startswith("Show.S01E01"))
    got = env.client.get(f"/api/items/{show['id']}").json()
    assert got == show and len(got["candidates"]) == 2 and got["slim"] is False
    assert got["candidates"][0]["reasons"] == ["fake"]
    assert env.client.get("/api/items/99999").status_code == 404


def test_bulk_by_filter_and_execute_by_text(env):
    job = _paging_job(env)
    ids = {i["filename"]: i["id"] for i in job["items"]}
    assert env.client.post("/api/items/bulk", json={"action": "skip"}).status_code == 422
    assert env.client.post("/api/items/bulk", json={"action": "skip", "item_ids": []}).status_code == 422
    assert env.client.post("/api/items/bulk", json={"action": "skip", "job_id": 999}).status_code == 404

    skipped = env.client.post("/api/items/bulk", json={"action": "skip", "job_id": job["id"], "statuses": ["matched"],
                                                       "q": "S01E0"})  # fmt: skip
    assert skipped.status_code == 200
    body = skipped.json()
    assert sorted(i["id"] for i in body) == sorted(ids[f"Show.S01E0{n}.1080p.mkv"] for n in range(1, 6))
    assert all(i["status"] == "skipped" and i["slim"] for i in body)
    counts = env.client.get(f"/api/jobs/{job['id']}", params={"limit": 0}).json()["counts"]
    assert counts == {"skipped": 5, "matched": 1, "review": 1}
    nothing = env.client.post("/api/items/bulk", json={"action": "approve", "job_id": job["id"], "q": "nope"})
    assert nothing.json() == []

    # explicit ids keep returning full items
    back = env.client.post("/api/items/bulk", json={"action": "review", "item_ids": [ids["Show.S01E01.1080p.mkv"]]})
    assert back.json()[0]["slim"] is False and back.json()[0]["status"] == "review"

    assert env.client.post(f"/api/jobs/{job['id']}/execute", json={"q": "zzz"}).status_code == 409
    response = env.client.post(f"/api/jobs/{job['id']}/execute", json={"q": "film", "mode": "copy"})
    assert response.status_code == 200, response.text
    done = env.wait(job["id"])
    statuses = {i["filename"]: i["status"] for i in done["items"]}
    assert statuses["Film.2010.mkv"] == "done"
    assert statuses["Show.S01E01.1080p.mkv"] == "review" and statuses["Show.S01E02.1080p.mkv"] == "skipped"


def test_websocket_item_updates_are_slim(env):
    env.touch("Show.S01E02.1080p.mkv")
    job = env.create_job(env.incoming)
    [item] = job["items"]
    with env.client.websocket_connect("/api/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        assert env.client.patch(f"/api/items/{item['id']}", json={"status": "approved"}).status_code == 200
        updates = []
        while True:
            message = ws.receive_json()
            if message["type"] == "item.updated":
                updates.append(message["data"])
            if message["type"] == "job.updated":
                break
    assert updates and all(u["slim"] is True and len(u["candidates"]) <= 1 and u["probe"] is None for u in updates)
    final = updates[-1]
    assert final["status"] == "approved"
    assert final["candidate_count"] == 2 and final["candidates"][0]["reasons"] == []
