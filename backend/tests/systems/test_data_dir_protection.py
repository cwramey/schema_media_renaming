"""Schema's data folder (secret.key, schema.db) is unreachable through settings, restore, watch
folders, the file browser and jobs, and moves across separate mounts are reported."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from schema_app.config import Config
from schema_app.db import Database
from schema_app.models import WatchFolder
from schema_app.services import fileops
from schema_app.services import paths as paths_mod
from schema_app.services.settings_store import SettingsStore


@pytest.fixture(autouse=True)
def _unprotect():  # type: ignore[no-untyped-def]
    yield
    paths_mod.protect([])


def test_settings_refuse_roots_that_reach_the_data_dir(env, tmp_path: Path) -> None:
    saved = env.client.get("/api/settings").json()
    filesystem_root = os.path.abspath(os.sep)
    for roots in ([str(env.data)], [str(tmp_path)], [filesystem_root], [str(env.data / "trash")]):
        response = env.client.put("/api/settings", json=dict(saved, allowed_roots=roots))
        assert response.status_code == 400, roots
        assert "can't be used" in response.json()["detail"]
    bad_library = dict(saved, library_roots={"movie": str(env.data / "Movies")})
    assert env.client.put("/api/settings", json=bad_library).status_code == 400
    assert env.client.get("/api/settings").json()["allowed_roots"] == saved["allowed_roots"]


def test_restore_refuses_roots_that_reach_the_data_dir(env, tmp_path: Path) -> None:
    backup = env.client.get("/api/settings/backup").json()
    for app in (dict(backup["app"], allowed_roots=[str(tmp_path)]),
                dict(backup["app"], library_roots={"movie": os.path.abspath(os.sep)})):  # fmt: skip
        response = env.client.post("/api/settings/restore", json=dict(backup, app=app))
        assert response.status_code == 400 and "can't be used" in response.json()["detail"]
    folder = {"path": str(env.data / "trash")}
    assert env.client.post("/api/settings/restore", json=dict(backup, watch_folders=[folder])).status_code == 400
    assert env.client.get("/api/settings").json()["allowed_roots"] == backup["app"]["allowed_roots"]


@pytest.mark.integration  # the real paths.is_within, not this folder's fake
def test_old_settings_covering_the_data_dir_still_cant_reach_it(env, tmp_path: Path) -> None:
    """A root saved before these checks (here: the folder holding /config) is still no way in."""
    store = env.app.state.ctx.store
    store.update_app(allowed_roots=[str(tmp_path)])
    secret = env.data / "secret.key"
    assert secret.exists()
    # file browser: the data folder is neither listed nor browsable
    listing = env.client.get("/api/fs/list", params={"path": str(tmp_path)}).json()
    assert "data" not in {e["name"] for e in listing["entries"]} and "media" in {e["name"] for e in listing["entries"]}
    assert env.client.get("/api/fs/list", params={"path": str(env.data)}).status_code == 403
    # jobs and watch folders
    assert env.client.post("/api/jobs", json={"paths": [str(secret)]}).status_code == 403
    assert env.client.post("/api/watch-folders", json={"path": str(env.data)}).status_code == 403
    assert env.client.post("/api/watch-folders", json={"path": str(tmp_path)}).status_code == 403  # holds it
    dest = {"path": str(env.incoming), "dest_root": os.path.abspath(os.sep)}
    assert env.client.post("/api/watch-folders", json=dest).status_code == 403
    assert env.client.post("/api/watch-folders", json={"path": str(env.incoming)}).status_code == 201


def _skip_without_symlinks(tmp: Path) -> None:
    try:
        os.symlink(tmp, tmp / "symlink-probe", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need Developer Mode or admin rights on this Windows host")
    os.unlink(tmp / "symlink-probe")


@pytest.mark.integration  # the real paths.is_within, not this folder's fake
def test_fs_list_hides_symlinks_leaving_the_roots(env, tmp_path: Path) -> None:
    _skip_without_symlinks(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.mkv").write_bytes(b"x" * 1234)
    os.symlink(outside / "private.mkv", env.incoming / "leak.mkv")
    os.symlink(env.data / "secret.key", env.incoming / "key.mkv")
    env.touch("Film.2010.mkv")
    os.symlink(env.incoming / "Film.2010.mkv", env.incoming / "alias.mkv")
    entries = {e["name"]: e for e in env.client.get("/api/fs/list", params={"path": str(env.incoming)}).json()["entries"]}
    assert "leak.mkv" not in entries and "key.mkv" not in entries
    assert entries["alias.mkv"]["size"] == len(b"video-bytes")


@pytest.fixture
def store(tmp_path: Path):  # type: ignore[no-untyped-def]
    db = Database(tmp_path / "schema.db")
    db.init()
    created: list[SettingsStore] = []

    def make(**overrides: object) -> SettingsStore:
        config = Config(data_dir=tmp_path / "data", **overrides)  # type: ignore[arg-type]
        made = SettingsStore(db, config)
        made.seed()
        created.append(made)
        return made

    yield make
    db.dispose()


def test_store_protects_its_data_dir_except_the_demo(store, tmp_path: Path) -> None:
    store()
    assert paths_mod.is_protected_path(tmp_path / "data" / "secret.key")
    assert paths_mod.is_protected_path(tmp_path / "data" / "demo" / "library")
    store(demo=True)
    assert paths_mod.is_protected_path(tmp_path / "data" / "secret.key")
    assert not paths_mod.is_protected_path(tmp_path / "data" / "demo" / "library")


def test_cross_mount_pairs_are_warned_once(
    store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    downloads, movies = tmp_path / "media" / "downloads", tmp_path / "media" / "movies"
    downloads.mkdir(parents=True)
    movies.mkdir(parents=True)
    checked: list[tuple[str, str]] = []

    def fake(src: str, dst: str) -> str | None:
        checked.append((src, dst))
        return "they are separate mounts (/downloads and /movies) of the same filesystem"

    monkeypatch.setattr(fileops, "cross_mount", fake)
    settings = store()
    with settings.db.session() as session:
        session.add(WatchFolder(path=str(downloads / "tv")))
    caplog.set_level(logging.WARNING)
    app = settings.get_app().model_copy(
        update={"allowed_roots": [str(tmp_path / "media")], "library_roots": {"movie": str(movies)}}
    )
    settings.put_app(app)
    assert checked == [(str(downloads / "tv"), str(movies))]  # the root holding both is no pair
    warnings = [r.getMessage() for r in caplog.records if "copies the whole file" in r.getMessage()]
    assert len(warnings) == 1 and str(downloads / "tv") in warnings[0]
    settings.put_app(app)  # nothing new: not warned again
    assert len([r for r in caplog.records if "copies the whole file" in r.getMessage()]) == 1
    restarted = store()  # a restart reports it again (from SettingsStore.__init__) ...
    assert len(checked) == 2
    assert len([r for r in caplog.records if "copies the whole file" in r.getMessage()]) == 2
    assert restarted.warn_cross_mounts() == []  # ... once
