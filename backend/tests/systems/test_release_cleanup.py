"""Download folders cleaned up after a move: leftovers (.jpg, .txt, samples, extras) go to a holding
folder once every video in the folder is in the library, driven through the API with the real file
operations (matcher, parser and media servers stay fakes)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from schema_app.domain import WatchFolderSpec
from schema_app.services import fileops, release

REAL_FILEOPS = {name: getattr(fileops, name) for name in ("find_sidecars", "plan_operation", "execute", "undo")}


@pytest.fixture
def real_fileops(fakes, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, fn in REAL_FILEOPS.items():
        monkeypatch.setattr(fileops, name, fn)


def _make(path: Path, content: bytes = b"video-bytes") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _war_dogs(base: Path) -> Path:
    folder = base / "War.Dogs.2016.1080p"
    _make(folder / "War.Dogs.2016.1080p.mkv")
    _make(folder / "War.Dogs.2016.1080p.en.srt", b"subs")
    _make(folder / "WWW.YTS.MX.jpg", b"img")
    _make(folder / "YTSProxies.com.txt", b"txt")
    _make(folder / "Featurettes" / "War.Dogs.2016.trailer.mkv")  # an extra: skipped
    _make(folder / "Sample" / "War.Dogs.2016.sample.mkv")  # a sample: skipped
    return folder


def _held(env) -> list[Path]:
    out: list[Path] = []
    for base in (release.holding_base(env.root), release.holding_base(env.incoming)):
        if base.is_dir():
            out += [name for day in base.iterdir() for slot in day.iterdir() for name in slot.iterdir()
                    if name.is_dir()]  # fmt: skip
    return out


def _webhook(env, path: Path) -> dict:
    mapping = {"source": str(env.incoming), "target": str(env.incoming)}  # webhook paths must be in the download area
    if mapping not in env.client.get("/api/settings").json()["path_mappings"]:
        env.settings(path_mappings=[mapping])
    key = env.client.get("/api/settings/api-key").json()["api_key"]
    response = TestClient(env.app).post("/api/webhooks/generic", json={"path": str(path)}, headers={"X-Api-Key": key})
    assert response.status_code == 202, response.text
    job = env.wait(response.json()["job_id"])
    return env.wait(job["id"])


def test_settings_default_on(env) -> None:
    settings = env.client.get("/api/settings").json()
    assert settings["clean_release_folders"] is True and settings["release_quiet_seconds"] == 120


def test_webhook_release_is_cleaned_after_the_move(env, real_fileops) -> None:
    folder = _war_dogs(env.incoming)
    job = _webhook(env, folder)
    assert job["status"] == "completed", job
    assert sorted(i["status"] for i in job["items"]) == ["done", "skipped", "skipped"]
    movie = env.library / "Movies" / "War Dogs (2016)"
    assert (movie / "War Dogs (2016).mkv").exists() and (movie / "War Dogs (2016).en.srt").exists()
    assert not folder.exists()
    assert env.incoming.is_dir()
    [held] = _held(env)
    assert sorted(str(p.relative_to(held)).replace("\\", "/") for p in held.rglob("*") if p.is_file()) == [
        "Featurettes/War.Dogs.2016.trailer.mkv", "Sample/War.Dogs.2016.sample.mkv", "WWW.YTS.MX.jpg",
        "YTSProxies.com.txt",
    ]  # fmt: skip
    events = env.client.get("/api/events", params={"category": "fileops"}).json()
    assert any("Cleaned up the download folder War.Dogs.2016.1080p" in e["message"] for e in events)

    # Undo still works: the folder is recreated and the movie goes back.
    op = env.client.get("/api/operations", params={"job_id": job["id"]}).json()[0]
    undone = env.client.post(f"/api/operations/{op['id']}/undo")
    assert undone.status_code == 200 and undone.json()["status"] == "undone", undone.text
    assert (folder / "War.Dogs.2016.1080p.mkv").exists()


def test_release_kept_while_something_still_downloads(env, real_fileops) -> None:
    folder = _war_dogs(env.incoming)
    _make(folder / "Featurettes" / "Behind.The.Scenes.mkv.part", b"partial")
    job = _webhook(env, folder)
    assert any(i["status"] == "done" for i in job["items"])
    assert (folder / "WWW.YTS.MX.jpg").exists() and _held(env) == []
    events = env.client.get("/api/events", params={"category": "fileops"}).json()
    assert any("Behind.The.Scenes.mkv.part is still downloading" in e["message"] for e in events)


def test_release_kept_while_a_video_waits_for_review(env, real_fileops) -> None:
    folder = _make(env.incoming / "Pack.2001" / "Movie.One.2001.mkv").parent
    _make(folder / "Unknown.Thing.mkv")  # never matches: stays for review
    _make(folder / "info.txt", b"txt")
    job = _webhook(env, folder)
    assert sorted(i["status"] for i in job["items"]) != ["done", "done"]
    assert (folder / "Unknown.Thing.mkv").exists() and (folder / "info.txt").exists()
    assert _held(env) == []


def test_watch_folder_release_below_a_category_folder(env, real_fileops) -> None:
    created = env.client.post("/api/watch-folders", json={"path": str(env.incoming)})
    assert created.status_code == 201, created.text
    folder = _war_dogs(env.incoming / "radarr")
    watcher = env.fakes.watchers[-1]
    spec = WatchFolderSpec(id=created.json()["id"], path=str(env.incoming))
    files = sorted(folder.rglob("*.mkv"))
    env.client.portal.call(watcher.on_ready, spec, files)
    job = env.wait(env.client.get("/api/jobs").json()[0]["id"])
    job = env.wait(job["id"])
    assert job["status"] == "completed", job
    assert not folder.exists()
    assert (env.incoming / "radarr").is_dir()  # the category folder stays
    [held] = _held(env)
    assert held.name == "War.Dogs.2016.1080p"
    assert held.parent.parent.parent == release.holding_base(env.incoming)  # same disk as the watch folder


def _watch_job(env, files: list[Path]) -> dict:
    created = env.client.post("/api/watch-folders", json={"path": str(env.incoming)})
    assert created.status_code == 201, created.text
    spec = WatchFolderSpec(id=created.json()["id"], path=str(env.incoming))
    env.client.portal.call(env.fakes.watchers[-1].on_ready, spec, files)
    job = env.wait(env.client.get("/api/jobs").json()[0]["id"])
    return env.wait(job["id"])


def _messages(env) -> list[str]:
    return [e["message"] for e in env.client.get("/api/events", params={"category": "fileops"}).json()]


def test_unknown_category_folder_is_never_held(env, real_fileops) -> None:
    """A movie dropped straight into an rdt-client category ("4k"): the category also holds a download
    still to unpack, photos and documents, and all of it stays (Nov 2026)."""
    category = env.incoming / "4k"
    movie = _make(category / "Movie.Name.2019.2160p.mkv")
    others = [
        _make(category / "Other.Show.S01.2160p" / "other.show.s01.part01.rar", b"rar"),
        _make(category / "Other.Show.S01.2160p" / "other.show.s01.r00", b"rar"),
        _make(category / "IMG_0001.jpg", b"photo"),
        _make(category / "taxes.pdf", b"pdf"),
    ]
    job = _watch_job(env, [movie])
    assert job["status"] == "completed", job
    assert (env.library / "Movies" / "Movie Name (2019)" / "Movie Name (2019).mkv").exists()
    assert all(p.exists() for p in others) and _held(env) == []
    assert any("Left the download folder 4k in place: its name does not match Movie.Name.2019.2160p.mkv" in m
               for m in _messages(env))  # fmt: skip


def test_release_inside_an_unknown_category_is_cleaned(env, real_fileops) -> None:
    folder = _war_dogs(env.incoming / "4k")
    other = _make(env.incoming / "4k" / "Other.Download" / "other.part01.rar", b"rar")
    job = _watch_job(env, sorted(folder.rglob("*.mkv")))
    assert job["status"] == "completed", job
    assert not folder.exists() and other.exists()
    [held] = _held(env)
    assert held.name == "War.Dogs.2016.1080p"


def test_webhook_naming_a_category_folder_leaves_it(env, real_fileops) -> None:
    category = env.incoming / "4k"
    movie = _make(category / "Movie.Name.2019.2160p.mkv")
    photo = _make(category / "IMG_0001.jpg", b"photo")
    job = _webhook(env, category)
    assert job["status"] == "completed", job
    assert not movie.exists() and photo.exists() and _held(env) == []


def test_release_with_archives_is_kept(env, real_fileops) -> None:
    folder = _war_dogs(env.incoming)
    _make(folder / "War.Dogs.2016.Extras.part01.rar", b"rar")
    _make(folder / "War.Dogs.2016.Extras.r00", b"rar")
    job = _webhook(env, folder)
    assert any(i["status"] == "done" for i in job["items"])
    assert (folder / "WWW.YTS.MX.jpg").exists() and _held(env) == []
    assert any("War.Dogs.2016.Extras.part01.rar, which is not download clutter" in m for m in _messages(env))


def test_folder_not_named_after_the_movie_is_kept(env, real_fileops) -> None:
    """A home video next to the photos, or a torrent merged into a folder the user already had."""
    folder = env.incoming / "Holiday Photos"
    movie = _make(folder / "War.Dogs.2016.1080p.mkv")
    photo = _make(folder / "beach.jpg", b"photo")
    job = _webhook(env, folder)
    assert job["status"] == "completed", job
    assert not movie.exists() and photo.exists() and _held(env) == []
    assert any("its name does not match War.Dogs.2016.1080p.mkv" in m for m in _messages(env))

    merged = _war_dogs(env.incoming / "again")
    _make(merged / "My Edits" / "notes.txt", b"mine")  # a folder that isn't release clutter
    _webhook(env, merged)
    assert (merged / "My Edits" / "notes.txt").exists() and _held(env) == []
    assert any("it holds the folder My Edits" in m for m in _messages(env))


def test_watch_manager_knows_handled_files(env, real_fileops) -> None:
    """The watcher's is_known callback: files a job already took in (seeding in copy / link mode)."""
    seeding = _make(env.incoming / "Seeding" / "War.Dogs.2016.1080p.mkv")
    env.create_job(seeding)
    fresh = _make(env.incoming / "New.Movie.2020.mkv")
    files = {str(seeding): seeding.stat().st_mtime, str(fresh): fresh.stat().st_mtime}
    known = env.client.portal.call(env.ctx.watch._is_known, files)
    assert known == {os.path.normcase(os.path.normpath(str(seeding)))}


def test_manual_jobs_and_setting_off_leave_the_folder(env, real_fileops) -> None:
    folder = _war_dogs(env.incoming)
    job = env.create_job(folder)
    assert env.client.post(f"/api/jobs/{job['id']}/execute", json={}).status_code == 200
    env.wait(job["id"])
    assert (folder / "WWW.YTS.MX.jpg").exists()  # manual imports are never cleaned up

    env.settings(clean_release_folders=False)
    other = _war_dogs(env.incoming / "again")
    _webhook(env, other)
    assert (other / "WWW.YTS.MX.jpg").exists() and _held(env) == []
