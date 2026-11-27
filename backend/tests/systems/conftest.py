"""Fixtures for the systems tests (API, pipeline, format engine).

Sibling modules (parser, matcher, fileops, watcher, ...) are replaced by deterministic fakes
here so these unit tests never depend on the real sibling implementations. Tests that need the
real siblings are marked ``@pytest.mark.integration`` and skip the fakes.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_ABS_RE = re.compile(r"^(?:[\\/]|[A-Za-z]:)")


def fake_sanitize_component(value: str, *, colon: str = " - ") -> str:
    """Mirror of services.paths.sanitize_component's documented behavior (kept stable for tests)."""
    text = "" if value is None else str(value)
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    text = text.replace(":", colon)
    for bad, good in {"/": "-", "\\": "-", "|": "-", "*": "-", '"': "'", "<": "", ">": "", "?": ""}.items():
        text = text.replace(bad, good)
    text = re.sub(r"\s+", " ", text).strip(" ")
    while text and text[-1] in ". ":
        text = text[:-1]
    return "" if text in {".", ".."} else text


def fake_sanitize_relative_path(path: str) -> str:
    text = (path or "").strip()
    if not text:
        raise ValueError("path is empty")
    if _ABS_RE.match(text):
        raise ValueError(f"absolute paths are not allowed here: {text!r}")
    parts = []
    for raw in re.split(r"[\\/]", text):
        stripped = raw.strip()
        if stripped == "..":
            raise ValueError("'..' is not allowed")
        if stripped in {"", "."}:
            continue
        cleaned = fake_sanitize_component(stripped)
        if cleaned:
            parts.append(cleaned)
    if not parts:
        raise ValueError("path is empty after sanitization")
    return "/".join(parts)


def fake_is_within(path, roots) -> bool:
    import os

    if not roots or not path:
        return False
    candidate = os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))
    for root in roots:
        if not root:
            continue
        base = os.path.normcase(os.path.realpath(os.path.abspath(root)))
        if candidate == base or candidate.startswith(base.rstrip(os.sep) + os.sep):
            return True
    return False


@pytest.fixture(autouse=True)
def _fake_paths(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stable path-sanitization fakes for every non-integration test in this folder."""
    if request.node.get_closest_marker("integration"):
        return
    from schema_app.services import paths

    monkeypatch.setattr(paths, "sanitize_component", fake_sanitize_component)
    monkeypatch.setattr(paths, "sanitize_relative_path", fake_sanitize_relative_path)
    monkeypatch.setattr(paths, "is_within", fake_is_within)


# --------------------------------------------------------------------------- app fixtures

SETUP_TOKEN = "test-setup-code"
ADMIN = {"username": "admin", "password": "correct-horse-battery", "setup_token": SETUP_TOKEN}
CLIENT_HEADERS = {"X-Schema-Client": "pytest"}
RUNNING = {"queued", "matching", "executing"}


def fake_parse_webhook(source, payload, *, category_kinds, path_mappings):
    from schema_app.services.downloaders import IngestRequest, WebhookPayloadError

    paths = payload.get("paths") or ([payload["path"]] if payload.get("path") else [])
    if not paths:
        raise WebhookPayloadError("no path in payload")
    if str(payload.get("status", "0")) not in {"0", ""}:
        raise WebhookPayloadError("download failed")
    category = payload.get("category")
    return IngestRequest(paths=list(paths), kind_hint=category_kinds.get(category) if category else None,
                         source=source, label=payload.get("name"), category=category)  # fmt: skip


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Replace every sibling module the pipeline calls with deterministic fakes."""
    from systems_fakes import (
        FakeFileOps,
        FakeMatcher,
        FakeMediaServers,
        FakeWatcher,
        fake_crc32_file,
        fake_parse,
    )

    from schema_app.core import matcher as matcher_mod
    from schema_app.core import parser as parser_mod
    from schema_app.services import downloaders, fileops, hashing, mediaservers, probe, watcher

    ns = SimpleNamespace(matcher=FakeMatcher(), fileops=FakeFileOps(), media=FakeMediaServers())
    monkeypatch.setattr(parser_mod, "parse", fake_parse)
    monkeypatch.setattr(matcher_mod, "create_matcher", lambda settings, *, cache_dir, demo=False: ns.matcher)
    monkeypatch.setattr(fileops, "find_sidecars", ns.fileops.find_sidecars)
    monkeypatch.setattr(fileops, "plan_operation", ns.fileops.plan_operation)
    monkeypatch.setattr(fileops, "execute", ns.fileops.execute)
    monkeypatch.setattr(fileops, "undo", ns.fileops.undo)
    monkeypatch.setattr(hashing, "crc32_file", fake_crc32_file)
    monkeypatch.setattr(probe, "available", lambda: None)
    FakeWatcher.instances.clear()
    monkeypatch.setattr(watcher, "FolderWatcher", FakeWatcher)
    monkeypatch.setattr(mediaservers, "test_server", ns.media.test_server)
    monkeypatch.setattr(mediaservers, "refresh_paths", ns.media.refresh_paths)
    monkeypatch.setattr(mediaservers, "RefreshDebouncer", None, raising=False)  # refresh immediately
    monkeypatch.setattr(downloaders, "parse_webhook", fake_parse_webhook)
    ns.watchers = FakeWatcher.instances
    return ns


@dataclass
class Env:
    client: Any
    app: Any
    fakes: SimpleNamespace
    root: Path  # allowed root
    incoming: Path
    library: Path
    data: Path

    @property
    def ctx(self) -> Any:
        return self.app.state.ctx

    def touch(self, relative: str, content: bytes = b"video-bytes") -> Path:
        path = self.incoming / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def wait(self, job_id: int, timeout: float = 10.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            job = self.client.get(f"/api/jobs/{job_id}").json()
            if job["status"] not in RUNNING or time.monotonic() > deadline:
                return job
            time.sleep(0.02)

    def create_job(self, *paths: Path | str, **extra: Any) -> dict[str, Any]:
        response = self.client.post("/api/jobs", json={"paths": [str(p) for p in paths], **extra})
        assert response.status_code == 202, response.text
        return self.wait(response.json()["id"])

    def settings(self, **changes: Any) -> dict[str, Any]:
        current = self.client.get("/api/settings").json()
        current.update(changes)
        response = self.client.put("/api/settings", json=current)
        assert response.status_code == 200, response.text
        return response.json()


def make_config(tmp_path: Path, **overrides: Any) -> Any:
    from schema_app.config import Config

    values = {"data_dir": tmp_path / "data", "static_dir": tmp_path / "no-static", "demo": False, "auth": "required",
              "setup_token": SETUP_TOKEN}  # fmt: skip
    values.update(overrides)
    return Config(**values)


@pytest.fixture
def make_env(tmp_path: Path, fakes: SimpleNamespace):
    """Factory: start the app (lifespan included) with fakes, an admin session and media roots."""
    from fastapi.testclient import TestClient

    from schema_app.main import create_app

    stack: list[Any] = []

    def factory(*, login: bool = True, configure: bool = True, **config_overrides: Any) -> Env:
        root = tmp_path / "media"
        incoming, library = root / "incoming", root / "library"
        incoming.mkdir(parents=True, exist_ok=True)
        library.mkdir(parents=True, exist_ok=True)
        app = create_app(make_config(tmp_path, **config_overrides))
        client = TestClient(app, headers=CLIENT_HEADERS)
        client.__enter__()
        stack.append(client)
        env = Env(client=client, app=app, fakes=fakes, root=root, incoming=incoming, library=library,
                  data=tmp_path / "data")  # fmt: skip
        if login and app.state.ctx.config.auth_enabled:
            assert client.post("/api/auth/setup", json=ADMIN).status_code == 200
        if configure:
            env.settings(
                allowed_roots=[str(root)],
                library_roots={"movie": str(library / "Movies"), "series": str(library / "TV"),
                               "anime": str(library / "Anime")},  # fmt: skip
            )
        return env

    yield factory
    for client in reversed(stack):
        client.__exit__(None, None, None)


@pytest.fixture
def env(make_env) -> Env:
    return make_env()
