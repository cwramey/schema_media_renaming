"""Folders renamed in place / emptied source folders removed, driven through the API with the real
file operations (matcher, parser and media servers stay fakes)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from schema_app.api_models import ItemStatus
from schema_app.core.pipeline import _norm_key, shared_source_dirs
from schema_app.services import fileops

# Captured at import, before the ``fakes`` fixture swaps them out.
REAL_FILEOPS = {name: getattr(fileops, name) for name in ("find_sidecars", "plan_operation", "execute", "undo")}


@pytest.fixture
def real_fileops(fakes, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, fn in REAL_FILEOPS.items():
        monkeypatch.setattr(fileops, name, fn)


@pytest.fixture
def movies(env, real_fileops) -> Path:
    env.settings(media_servers=[{"id": "jf", "kind": "jellyfin", "name": "Jellyfin", "url": "http://jf", "token": "t"}])
    folder = env.library / "Movies"
    folder.mkdir(parents=True)
    return folder


def _make(path: Path, content: bytes = b"video-bytes") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _alien(movies: Path) -> tuple[Path, Path]:
    old_dir = movies / "Alien.1979"
    _make(old_dir / "Alien.1979.4K.mkv")
    _make(old_dir / "Alien.1979.4K.en.srt", b"subs")
    _make(old_dir / "poster.jpg", b"img")
    return old_dir, movies / "Alien (1979)"


def _execute(env, job: dict, **body) -> dict:
    response = env.client.post(f"/api/jobs/{job['id']}/execute", json=body)
    assert response.status_code == 200, response.text
    return env.wait(job["id"])


def test_settings_default_on_and_round_trip(env) -> None:
    settings = env.client.get("/api/settings").json()
    assert settings["rename_folders_in_place"] is True
    assert settings["remove_empty_source_folders"] is True
    saved = env.settings(rename_folders_in_place=False, remove_empty_source_folders=False)
    assert saved["rename_folders_in_place"] is False and saved["remove_empty_source_folders"] is False
    again = env.client.get("/api/settings").json()
    assert again["rename_folders_in_place"] is False and again["remove_empty_source_folders"] is False


def test_alien_folder_renamed_in_place_then_undone(env, movies) -> None:
    old_dir, new_dir = _alien(movies)
    inode = os.stat(old_dir).st_ino
    job = env.create_job(old_dir)
    item = job["items"][0]
    assert item["status"] == "matched"
    assert item["folder_rename"] == {"source": str(old_dir), "destination": str(new_dir)}  # preview

    job = _execute(env, job)
    assert job["status"] == "completed"
    item = job["items"][0]
    assert item["status"] == "done"
    assert item["final_path"] == str(new_dir / "Alien (1979).mkv")
    assert item["folder_rename"] == {"source": str(old_dir), "destination": str(new_dir)}
    assert not old_dir.exists()
    assert os.stat(new_dir).st_ino == inode
    assert sorted(p.name for p in new_dir.iterdir()) == ["Alien (1979).en.srt", "Alien (1979).mkv", "poster.jpg"]

    op = env.client.get("/api/operations", params={"job_id": job["id"]}).json()[0]
    assert op["folder_rename"] == {"source": str(old_dir), "destination": str(new_dir)}
    assert op["source"] == str(old_dir / "Alien.1979.4K.mkv")
    # Jellyfin hears about both folders (the old one no longer exists: it rescans the parent).
    refreshed = {p for name, paths in env.fakes.media.refreshed if name == "Jellyfin" for p in paths}
    assert {str(new_dir), str(old_dir)} <= refreshed

    undone = env.client.post(f"/api/operations/{op['id']}/undo")
    assert undone.status_code == 200 and undone.json()["status"] == "undone", undone.text
    assert (old_dir / "Alien.1979.4K.mkv").exists() and (old_dir / "Alien.1979.4K.en.srt").exists()
    assert (old_dir / "poster.jpg").exists() and not new_dir.exists()


def test_dry_run_shows_the_folder_rename_and_changes_nothing(env, movies) -> None:
    old_dir, new_dir = _alien(movies)
    job = env.create_job(old_dir)
    job = _execute(env, job, mode="test")
    item = job["items"][0]
    assert item["status"] == "matched"
    assert f"dry_run_ok: {new_dir / 'Alien (1979).mkv'}" in item["warnings"]
    assert item["folder_rename"] == {"source": str(old_dir), "destination": str(new_dir)}
    assert sorted(p.name for p in old_dir.iterdir()) == ["Alien.1979.4K.en.srt", "Alien.1979.4K.mkv", "poster.jpg"]
    assert not new_dir.exists()
    op = env.client.get("/api/operations").json()[0]
    assert op["mode"] == "test" and op["folder_rename"]["destination"] == str(new_dir)


def test_collection_folder_is_not_renamed(env, movies) -> None:
    pack = movies / "Pack 2001-2002"
    _make(pack / "Movie.One.2001.mkv")
    _make(pack / "Movie.Two.2002.mkv")
    _make(pack / "poster.jpg", b"img")
    job = env.create_job(pack)
    assert [i["folder_rename"] for i in job["items"]] == [None, None]
    job = _execute(env, job)
    assert job["status"] == "completed"
    assert all(i["folder_rename"] is None for i in job["items"])
    assert (movies / "Movie One (2001)" / "Movie One (2001).mkv").exists()
    assert (movies / "Movie Two (2002)" / "Movie Two (2002).mkv").exists()
    assert sorted(p.name for p in pack.iterdir()) == ["poster.jpg"]  # kept: not empty


def test_skipped_extras_travel_with_the_renamed_folder(env, movies) -> None:
    old_dir, new_dir = _alien(movies)
    _make(old_dir / "featurettes" / "Alien.1979.trailer.mkv")  # an extra: skipped
    job = env.create_job(old_dir)
    statuses = sorted(i["status"] for i in job["items"])
    assert statuses == ["matched", "skipped"]
    job = _execute(env, job)
    assert (new_dir / "featurettes" / "Alien.1979.trailer.mkv").exists()
    assert not old_dir.exists()


def test_download_folder_removed_after_move(env, movies) -> None:
    download = _make(env.incoming / "X.2000.1080p" / "X.2000.1080p.mkv")
    job = env.create_job(download.parent)
    assert job["items"][0]["folder_rename"] is None
    job = _execute(env, job)
    assert job["items"][0]["status"] == "done"
    assert (movies / "X (2000)" / "X (2000).mkv").exists()
    assert not download.parent.exists()
    assert env.incoming.is_dir()  # the downloads folder itself stays
    op = env.client.get("/api/operations").json()[0]
    assert op["removed_dirs"] == [str(download.parent)]
    assert env.client.post(f"/api/operations/{op['id']}/undo").status_code == 200
    assert download.exists()


def test_existing_destination_folder_falls_back_to_a_move(env, movies) -> None:
    old_dir, new_dir = _alien(movies)
    _make(new_dir / "folder.jpg", b"img")
    job = env.create_job(old_dir)
    assert job["items"][0]["folder_rename"] is None
    job = _execute(env, job)
    assert (new_dir / "Alien (1979).mkv").exists() and (new_dir / "folder.jpg").exists()
    assert (old_dir / "poster.jpg").exists()  # the old folder isn't empty, so it stays


def test_copy_mode_never_renames_folders(env, movies) -> None:
    old_dir, new_dir = _alien(movies)
    job = env.create_job(old_dir, mode="copy")
    assert job["items"][0]["folder_rename"] is None
    job = _execute(env, job)
    assert (old_dir / "Alien.1979.4K.mkv").exists() and (new_dir / "Alien (1979).mkv").exists()


def test_settings_off_keeps_the_old_behavior(env, movies) -> None:
    env.settings(rename_folders_in_place=False, remove_empty_source_folders=False)
    old_dir, new_dir = _alien(movies)
    (old_dir / "poster.jpg").unlink()
    job = env.create_job(old_dir)
    assert job["items"][0]["folder_rename"] is None
    _execute(env, job)
    assert (new_dir / "Alien (1979).mkv").exists()
    assert old_dir.is_dir() and not any(old_dir.iterdir())  # left behind, empty, as before


def test_shared_source_dirs() -> None:
    items = [
        ("/m/Pack/a.mkv", ItemStatus.MATCHED),
        ("/m/Pack/b.mkv", ItemStatus.DONE),
        ("/m/Alien/alien.mkv", ItemStatus.MATCHED),
        ("/m/Alien/featurettes/trailer.mkv", ItemStatus.SKIPPED),
        ("/m/Box/Disc 1/one.mkv", ItemStatus.MATCHED),
        ("/m/Box/main.mkv", ItemStatus.MATCHED),
    ]
    shared = shared_source_dirs(items)
    assert _norm_key("/m/Pack") in shared  # two files right in it
    assert _norm_key("/m/Alien") not in shared  # only a skipped extra below it
    assert _norm_key("/m/Box") in shared  # another movie in a sub-folder
    assert _norm_key("/m/Box/Disc 1") not in shared
