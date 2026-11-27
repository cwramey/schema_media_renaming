"""Home board, Home actions, History, watcher start-now, settings backup, and the regressions for
bug 1 (failed jobs re-running by themselves) and bug 2 (bonus clips colliding with the feature)."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import ADMIN, CLIENT_HEADERS, make_config
from fastapi.testclient import TestClient
from systems_fakes import movie_candidate

from schema_app.api_models import SECRET_MASK
from schema_app.core import parser as parser_mod
from schema_app.core.parser import parse as real_parse  # captured before the fakes replace it
from schema_app.domain import MatchResult, ProbeInfo, WatchFolderSpec
from schema_app.models import Job, JobItem
from schema_app.services import probe as probe_mod
from schema_app.services.watcher import WatchedFile


def items_by_name(job: dict) -> dict[str, dict]:
    return {item["filename"]: item for item in job["items"]}


def home(env) -> dict:
    from schema_app.api.home import forget_home

    forget_home(env.ctx)  # tests also change rows directly, which the board's 1 s cache can't see
    response = env.client.get("/api/home")
    assert response.status_code == 200, response.text
    return response.json()


def act(env, key: str, action: str):
    return env.client.post("/api/home/actions", json={"key": key, "action": action})


def crow_matcher(env, monkeypatch, *, runtime: int | None = None) -> None:
    """Everything inside a "The Crow (1994)" folder matches The Crow (like a folder-title match)."""
    original = env.fakes.matcher._result

    def result(parsed, hint):
        if "The Crow (1994)" in parsed.path:
            best = movie_candidate("The Crow", 1994, mid="9340")
            if runtime:
                best = best.model_copy(update={"movie": best.movie.model_copy(update={"runtime_minutes": runtime})})
            return MatchResult(candidates=[best], best=best, confidence=0.95)
        return original(parsed, hint)

    monkeypatch.setattr(env.fakes.matcher, "_result", result)


# --------------------------------------------------------------------------- bug 1


def test_failed_job_is_not_rerun_when_the_watcher_offers_its_files_again(env):
    """Job #6 (a manual import of the downloads folder) failed and ran again unprompted: after a
    restart the watcher re-offers every file still in the folder, and the de-dup only looked at
    *open* jobs. A file some job already took in must only run again when the user retries it."""
    env.fakes.fileops.fail_with = "disk full"
    src = env.touch("Film.2010.mkv")
    trailer = env.touch("Film.2010.trailer.mkv")
    job = env.create_job(env.incoming)
    env.client.post(f"/api/jobs/{job['id']}/execute")
    job = env.wait(job["id"])
    assert job["status"] == "failed"
    env.fakes.fileops.fail_with = None
    executed = len(env.fakes.fileops.executed)

    folder = env.client.post("/api/watch-folders", json={"path": str(env.incoming)}).json()
    watcher = env.fakes.watchers[-1]
    spec = WatchFolderSpec(id=folder["id"], path=str(env.incoming))
    env.client.portal.call(watcher.on_ready, spec, [src, trailer])  # what the rescan after a restart does
    assert [j["id"] for j in env.client.get("/api/jobs").json()] == [job["id"]]
    assert env.client.get(f"/api/jobs/{job['id']}").json()["status"] == "failed"
    assert len(env.fakes.fileops.executed) == executed
    events = env.client.get("/api/events", params={"category": "watcher"}).json()
    assert any("already handled" in e["message"] for e in events)

    # A file that changed since (a new download with the same name) is new work.
    later = time.time() + 120
    os.utime(src, (later, later))
    env.client.portal.call(watcher.on_ready, spec, [src, trailer])
    jobs = env.client.get("/api/jobs").json()
    assert len(jobs) == 2 and jobs[0]["source"] == "watch"
    assert [i["filename"] for i in env.wait(jobs[0]["id"])["items"]] == ["Film.2010.mkv"]


def test_failed_job_stays_failed_across_a_restart(tmp_path, fakes):
    from schema_app.main import create_app

    media = tmp_path / "media"
    media.mkdir()
    film = media / "Film.2010.mkv"
    film.write_bytes(b"x")
    config = make_config(tmp_path)
    with TestClient(create_app(config), headers=CLIENT_HEADERS) as client:
        client.post("/api/auth/setup", json=ADMIN)
        settings = client.get("/api/settings").json()
        settings.update(allowed_roots=[str(media)], library_roots={"movie": str(media / "lib")})
        client.put("/api/settings", json=settings)
        ctx = client.app.state.ctx
        with ctx.db.session() as session:
            failed = Job(label="downloads", source="manual", status="failed", mode="move", input_paths=[str(media)],
                         error="internal error: boom")  # fmt: skip
            session.add(failed)
            session.flush()
            session.add_all([
                JobItem(job_id=failed.id, source_path=str(film), status="pending"),
                JobItem(job_id=failed.id, source_path=str(media / "Other.2011.mkv"), status="error", error="disk full"),
            ])  # fmt: skip
            job_id = failed.id
        ctx.pipeline._stopping = True
    with TestClient(create_app(config), headers=CLIENT_HEADERS) as client:
        client.post("/api/auth/login", json=ADMIN)
        time.sleep(0.3)  # give the (running) workers a chance to pick anything up
        job = client.get(f"/api/jobs/{job_id}").json()
        assert job["status"] == "failed"
        assert {i["status"] for i in job["items"]} == {"pending", "error"}
        assert not any(call[0] == "match_many" for call in fakes.matcher.calls)


def test_refired_webhook_does_not_rerun_failed_files(env):
    env.fakes.fileops.fail_with = "disk full"
    env.touch("Torrent/Film.2010.mkv")
    job = env.create_job(env.incoming / "Torrent")
    env.client.post(f"/api/jobs/{job['id']}/execute")
    assert env.wait(job["id"])["status"] == "failed"
    env.fakes.fileops.fail_with = None
    env.settings(path_mappings=[{"source": str(env.incoming), "target": str(env.incoming)}])  # the download area
    key = env.client.get("/api/settings/api-key").json()["api_key"]
    response = env.client.post("/api/webhooks/generic", json={"path": str(env.incoming / "Torrent")},
                               headers={"X-Api-Key": key})  # fmt: skip
    assert response.status_code == 202
    hooked = env.wait(response.json()["job_id"])
    assert hooked["status"] == "completed" and hooked["total"] == 0
    assert env.client.get(f"/api/jobs/{job['id']}").json()["status"] == "failed"
    # a new file in the same download is still processed
    env.touch("Torrent/Film.2010.en.mkv")
    response = env.client.post("/api/webhooks/generic", json={"path": str(env.incoming / "Torrent")},
                               headers={"X-Api-Key": key})  # fmt: skip
    assert [i["filename"] for i in env.wait(response.json()["job_id"])["items"]] == ["Film.2010.en.mkv"]


# --------------------------------------------------------------------------- bug 2


def test_bonus_clips_next_to_the_feature_are_skipped_not_colliding(env, monkeypatch):
    crow_matcher(env, monkeypatch)
    env.touch("The Crow (1994)/The.Crow.1994.1080p.mkv", b"F" * 40_000)
    env.touch("The Crow (1994)/The Shootout at Top Dollar's.mkv", b"s" * 1_000)
    env.touch("The Crow (1994)/The Funboy Fight.mkv", b"f" * 1_000)
    job = env.create_job(env.incoming)
    items = items_by_name(job)
    feature = items["The.Crow.1994.1080p.mkv"]
    assert feature["status"] == "matched", feature["warnings"]
    assert feature["proposed_relative"] == "The Crow (1994)/The Crow (1994).mkv"
    for name in ("The Shootout at Top Dollar's.mkv", "The Funboy Fight.mkv"):
        assert items[name]["status"] == "skipped", items[name]["warnings"]
        assert "extra" in items[name]["warnings"]
        assert "duplicate_in_batch" not in items[name]["warnings"]
    assert job["status"] == "ready"


def test_bonus_clip_of_a_movie_already_in_the_library_is_skipped(env, monkeypatch):
    crow_matcher(env, monkeypatch)
    existing = env.library / "Movies" / "The Crow (1994)" / "The Crow (1994).mkv"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"F" * 40_000)
    env.touch("The Crow (1994)/The Funboy Fight.mkv", b"f" * 1_000)
    env.touch("The Crow (1994)/The Shootout at Top Dollar's.mkv", b"s" * 1_000)
    job = env.create_job(env.incoming)
    for item in job["items"]:
        assert item["status"] == "skipped", item["warnings"]
        assert "extra" in item["warnings"] and "destination_exists" not in item["warnings"]


def test_same_size_copy_of_a_movie_in_the_library_still_needs_review(env, monkeypatch):
    """A second copy of the feature is a real conflict, not a clip."""
    crow_matcher(env, monkeypatch)
    existing = env.library / "Movies" / "The Crow (1994)" / "The Crow (1994).mkv"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"F" * 40_000)
    env.touch("The Crow (1994)/The.Crow.1994.720p.mkv", b"f" * 9_000)  # smaller, but the same title
    job = env.create_job(env.incoming)
    item = job["items"][0]
    assert item["status"] == "review" and "destination_exists" in item["warnings"]


def test_reel_keyword_marks_a_bonus_clip(env, monkeypatch):
    monkeypatch.setattr(parser_mod, "parse", real_parse)
    env.touch("Blade Runner 2049 (2017)/Blade Runner 2049 Environments Reel by DNEG.mkv")
    env.touch("Blade Runner 2049 (2017)/Blade.Runner.2049.2017.2160p.mkv")
    job = env.create_job(env.incoming)
    items = items_by_name(job)
    reel = items["Blade Runner 2049 Environments Reel by DNEG.mkv"]
    assert reel["status"] == "skipped" and "extra" in reel["warnings"]
    assert items["Blade.Runner.2049.2017.2160p.mkv"]["status"] == "matched"


def test_short_runtime_against_the_matched_title_marks_a_bonus_clip(env, monkeypatch):
    crow_matcher(env, monkeypatch, runtime=102)
    durations = {"The Funboy Fight.mkv": 240.0, "The.Crow.1994.mkv": 6100.0}
    monkeypatch.setattr(probe_mod, "available", lambda: "ffprobe")
    monkeypatch.setattr(probe_mod, "probe", lambda path: ProbeInfo(duration_seconds=durations[os.path.basename(path)]))
    env.touch("The Crow (1994)/The Funboy Fight.mkv", b"same-size")  # no size hint: runtime decides
    env.touch("elsewhere/The Crow (1994)/The.Crow.1994.mkv", b"same-size")
    job = env.create_job(env.incoming)
    items = items_by_name(job)
    assert items["The Funboy Fight.mkv"]["status"] == "skipped"
    assert "extra" in items["The Funboy Fight.mkv"]["warnings"]
    assert items["The.Crow.1994.mkv"]["status"] == "matched"


def test_explicit_rematch_keeps_a_suspected_clip(env, monkeypatch):
    crow_matcher(env, monkeypatch)
    env.touch("The Crow (1994)/The.Crow.1994.1080p.mkv", b"F" * 40_000)
    env.touch("The Crow (1994)/The Funboy Fight.mkv", b"f" * 1_000)
    job = env.create_job(env.incoming)
    clip = items_by_name(job)["The Funboy Fight.mkv"]
    assert clip["status"] == "skipped"
    out = env.client.post(f"/api/items/{clip['id']}/rematch", json={"hint": {}}).json()
    assert out["status"] != "skipped"


def test_extras_heuristics_respect_skip_extras_setting(env, monkeypatch):
    crow_matcher(env, monkeypatch)
    env.settings(skip_extras=False)
    env.touch("The Crow (1994)/The.Crow.1994.1080p.mkv", b"F" * 40_000)
    env.touch("The Crow (1994)/The Funboy Fight.mkv", b"f" * 1_000)
    job = env.create_job(env.incoming)
    clip = items_by_name(job)["The Funboy Fight.mkv"]
    assert clip["status"] != "skipped" and "extra" not in clip["warnings"]


# --------------------------------------------------------------------------- bug 4


def test_default_settle_time_is_30_seconds(env):
    from schema_app.api_models import WatchFolderIn
    from schema_app.domain import WatchFolderSpec as Spec
    from schema_app.models import WatchFolder

    assert WatchFolderIn(path="/x").settle_seconds == 30
    assert Spec(id=1, path="/x").settle_seconds == 30
    assert WatchFolder.__table__.c.settle_seconds.default.arg == 30
    folder = env.client.post("/api/watch-folders", json={"path": str(env.incoming)}).json()
    assert folder["settle_seconds"] == 30


# --------------------------------------------------------------------------- GET /api/home


def test_home_needs_you_cards_and_counts(env):
    env.touch("unknown.thing.2011.mkv")
    env.touch("Vague.Movie.2010.mkv")
    env.touch("Film.2010.trailer.mkv")
    job = env.create_job(env.incoming)
    board = home(env)
    assert board["needs_you_count"] == 2
    cards = {c["title"]: c for c in board["needs_you"]}
    assert set(cards) == {"unknown.thing.2011.mkv", "Vague.Movie.2010.mkv"}
    nomatch = cards["unknown.thing.2011.mkv"]
    assert nomatch["reason_code"] == "no_match" and nomatch["reason"] == "No match found"
    assert nomatch["primary"] is None and nomatch["key"] == f"item:{nomatch['item_id']}"
    assert nomatch["job_id"] == job["id"] and nomatch["kind"] == "item"
    vague = cards["Vague.Movie.2010.mkv"]
    assert vague["reason_code"] == "low_confidence"
    assert vague["reason"] == "Not sure this is Vague Movie (2010) (50% match)"
    assert vague["primary"] == {"label": "Use Vague Movie (2010)", "action": "approve"}
    assert vague["candidate_title"] == "Vague Movie (2010)" and vague["confidence"] == 0.5
    assert board["server_time"]
    assert board["status"]["providers"][0]["name"] == "fake"


def test_home_error_item_extra_reason_and_event_cards(env):
    env.fakes.fileops.fail_with = "No space left on device"
    env.touch("Film.2010.mkv")
    job = env.create_job(env.incoming)
    env.client.post(f"/api/jobs/{job['id']}/execute")
    env.wait(job["id"])
    env.fakes.fileops.fail_with = None
    env.ctx.bus.log("error", "mediaserver", "Refresh of Jellyfin failed: connection refused")
    env.ctx.bus.log("error", "mediaserver", "Refresh of Jellyfin failed: connection refused")  # duplicate
    board = home(env)
    by_code = {c["reason_code"]: c for c in board["needs_you"]}
    error = by_code["error"]
    assert error["reason"] == "Not enough free space to move this file"
    assert error["primary"] == {"label": "Retry", "action": "retry"}
    events = [c for c in board["needs_you"] if c["kind"] == "event"]
    assert len(events) == 1  # the job's own failure events are covered by its item card
    assert events[0]["title"] == "Refresh of Jellyfin failed: connection refused"
    assert events[0]["primary"] == {"label": "Dismiss", "action": "dismiss"}
    assert events[0]["reason_code"] == "event" and events[0]["key"].startswith("event:")
    assert board["needs_you_count"] == 2

    # A suspected bonus clip that collides with the feature already in the library.
    env.touch("Other/Clip.2012.mkv")
    other = env.create_job(env.incoming / "Other")
    with env.ctx.db.session() as session:
        row = session.get(JobItem, other["items"][0]["id"])
        row.status, row.warnings = "review", ["extra", "destination_exists"]
    card = next(c for c in home(env)["needs_you"] if c["title"] == "Clip.2012.mkv")
    assert card["reason_code"] == "extra"
    assert card["reason"] == "Looks like a bonus clip. Clip (2012) is already in your library."
    assert card["primary"] == {"label": "Skip as extra", "action": "skip"}


def test_home_source_missing_is_not_a_name_conflict(env):
    """Job 8: 1,019 cards said "A file with that name already exists" because the error text
    "Source file no longer exists" contains "exist". The reason comes from the typed code now."""
    env.fakes.fileops.fail_with = "Source file no longer exists: /media/tv/X.Men 97/Season 1/X.Men.97.S01E10.mp4"
    env.fakes.fileops.fail_code = "source_missing"
    env.touch("Film.2010.mkv")
    job = env.create_job(env.incoming)
    env.client.post(f"/api/jobs/{job['id']}/execute")
    item = env.wait(job["id"])["items"][0]
    assert item["status"] == "error" and "source_missing" in item["warnings"]
    card = next(c for c in home(env)["needs_you"] if c["kind"] == "item")
    assert card["reason_code"] == "source_missing"
    assert card["reason"] == "The file was moved or deleted before Schema could rename it"
    assert card["primary"] == {"label": "Dismiss", "action": "dismiss"}

    # Rows from before the typed warning are recognised by their exact error, not by "exist".
    with env.ctx.db.session() as session:
        session.get(JobItem, item["id"]).warnings = []
    assert next(c for c in home(env)["needs_you"] if c["kind"] == "item")["reason_code"] == "source_missing"


def test_home_groups_many_vanished_files_of_one_job(env):
    env.fakes.fileops.fail_with = "Source file no longer exists: somewhere"
    env.fakes.fileops.fail_code = "source_missing"
    for n in range(1, 7):
        env.touch(f"Film.20{10 + n}.mkv")
    job = env.create_job(env.incoming, label="tv")
    env.client.post(f"/api/jobs/{job['id']}/execute")
    env.wait(job["id"])
    board = home(env)
    assert board["needs_you_count"] == 6
    assert [c["kind"] for c in board["needs_you"]] == ["group"]
    group = board["needs_you"][0]
    assert group["key"] == f"group:{job['id']}" and group["count"] == 6
    assert group["title"] == "6 files from job 'tv' no longer exist at their source"
    assert group["reason_code"] == "source_missing"
    assert group["primary"] == {"label": "Dismiss all", "action": "dismiss"}
    assert act(env, group["key"], "skip").status_code == 409
    assert act(env, group["key"], "dismiss").json() == {"ok": True}
    assert home(env)["needs_you_count"] == 0
    assert act(env, group["key"], "dismiss").status_code == 404


def test_home_refresh_clears_cards_for_files_gone_from_disk(env):
    """Refresh re-checks the disk: an emptied downloads folder leaves nothing to review."""
    gone = env.touch("unknown.thing.2011.mkv")
    env.touch("Vague.Movie.2010.mkv")
    env.create_job(env.incoming)
    assert home(env)["needs_you_count"] == 2
    os.remove(gone)
    assert env.client.post("/api/home/refresh").json() == {"cleared": 1, "started": 0}
    assert [c["title"] for c in home(env)["needs_you"]] == ["Vague.Movie.2010.mkv"]
    assert env.client.post("/api/home/refresh").json() == {"cleared": 0, "started": 0}

    # A watch folder that is missing itself (an unmounted share) proves nothing: its cards stay.
    share = env.incoming / "share"
    share.mkdir()
    env.client.post("/api/watch-folders", json={"path": str(share)})
    env.touch("share/unknown.other.2012.mkv")
    env.create_job(share)
    for path in share.iterdir():
        path.unlink()
    share.rmdir()
    assert env.client.post("/api/home/refresh").json() == {"cleared": 0, "started": 0}
    assert home(env)["needs_you_count"] == 2


def test_home_dismiss_all_clears_items_and_events(env):
    env.touch("unknown.thing.2011.mkv")
    env.touch("Vague.Movie.2010.mkv")
    env.create_job(env.incoming)
    env.ctx.bus.log("error", "mediaserver", "Refresh of Jellyfin failed: connection refused")
    assert home(env)["needs_you_count"] == 3
    assert env.client.post("/api/home/dismiss-all").json() == {"cleared": 3}
    board = home(env)
    assert board["needs_you_count"] == 0 and board["needs_you"] == []
    assert env.client.post("/api/home/dismiss-all").json() == {"cleared": 0}
    assert (env.incoming / "Vague.Movie.2010.mkv").exists()  # the files themselves are left alone


def test_home_title_mismatch_goes_to_review_and_approve_works_from_matched(env, monkeypatch):
    original = env.fakes.matcher._result

    def result(parsed, hint):
        out = original(parsed, hint)
        if "Mismatch" in parsed.path:
            out = out.model_copy(update={"confidence": 0.99, "warnings": [*out.warnings, "episode_title_mismatch"]})
        return out

    monkeypatch.setattr(env.fakes.matcher, "_result", result)
    env.touch("Mismatch.Film.2010.mkv")
    env.touch("Film.2010.mkv")
    job = env.create_job(env.incoming)
    items = items_by_name(job)
    flagged, plain = items["Mismatch.Film.2010.mkv"], items["Film.2010.mkv"]
    assert flagged["status"] == "review"  # never auto-run, however confident
    card = next(c for c in home(env)["needs_you"] if c["item_id"] == flagged["id"])
    assert card["reason_code"] == "episode_mismatch"
    assert plain["status"] == "matched"
    assert act(env, f"item:{plain['id']}", "approve").json() == {"ok": True}
    env.wait(job["id"])
    assert env.client.get(f"/api/items/{plain['id']}").json()["status"] == "done"


def test_home_solved_problems_clear_per_setting(env):
    env.fakes.fileops.fail_with = "disk full"
    src = env.touch("Film.2010.mkv")
    first = env.create_job(src)
    env.client.post(f"/api/jobs/{first['id']}/execute")
    env.wait(first["id"])
    env.fakes.fileops.fail_with = None
    second = env.create_job(src, mode="copy")
    env.client.post(f"/api/jobs/{second['id']}/execute")
    assert env.wait(second["id"])["status"] == "completed"
    board = home(env)
    assert board["needs_you_count"] == 0
    [solved] = [c for c in board["needs_you"] if c["kind"] == "item"]
    assert solved["primary"] == {"label": "Dismiss", "action": "dismiss"}
    assert solved["reason"].startswith("Solved")
    env.settings(clear_solved_after_hours=0)
    assert [c for c in home(env)["needs_you"] if c["kind"] == "item"] == []
    env.settings(clear_solved_after_hours=None)
    assert len([c for c in home(env)["needs_you"] if c["kind"] == "item"]) == 1
    assert act(env, solved["key"], "dismiss").json() == {"ok": True}
    assert [c for c in home(env)["needs_you"] if c["kind"] == "item"] == []


def test_home_old_error_events_clear_after_the_threshold(env):
    env.ctx.bus.log("error", "provider", "TMDb said no")
    from schema_app.models import Event

    with env.ctx.db.session() as session:
        for row in session.query(Event).filter(Event.message == "TMDb said no"):
            row.ts = datetime.now(UTC) - timedelta(hours=30)
    assert [c for c in home(env)["needs_you"] if c["kind"] == "event"] == []  # default: 24 h
    env.settings(clear_solved_after_hours=168)
    assert len([c for c in home(env)["needs_you"] if c["kind"] == "event"]) == 1
    env.settings(clear_solved_after_hours=0)  # 0: only dismissing clears events
    assert len([c for c in home(env)["needs_you"] if c["kind"] == "event"]) == 1


def test_home_incoming_renaming_library_and_status(env):
    folder = env.client.post("/api/watch-folders", json={"path": str(env.incoming), "auto_execute": False}).json()
    watcher = env.fakes.watchers[-1]
    now = datetime.now(UTC)
    settling = env.touch("New.Film.2026.mkv", b"x" * 500)
    watcher.waiting = [
        WatchedFile(folder_id=folder["id"], path=str(settling), size=500, mtime_ns=time.time_ns() + 10**9,
                    state="settling", settle_seconds=30, stable_since=now, settles_at=now + timedelta(seconds=30)),
        WatchedFile(folder_id=folder["id"], path=str(env.incoming / "Down.2025.mkv.part"), size=10, mtime_ns=1,
                    state="partial", settle_seconds=30, stable_since=None, settles_at=None),
        WatchedFile(folder_id=999, path="/elsewhere/x.mkv", size=1, mtime_ns=1, state="settling",
                    settle_seconds=30, stable_since=now, settles_at=now),
    ]  # fmt: skip
    board = home(env)
    incoming = {f["name"]: f for f in board["incoming"]}
    assert set(incoming) == {"New.Film.2026.mkv", "Down.2025.mkv.part"}
    assert incoming["New.Film.2026.mkv"]["state"] == "settling"
    assert incoming["New.Film.2026.mkv"]["title_guess"] == "New Film (2026)"
    assert incoming["New.Film.2026.mkv"]["settle_seconds"] == 30 and incoming["New.Film.2026.mkv"]["settles_at"]
    assert incoming["Down.2025.mkv.part"]["state"] == "partial"
    assert incoming["Down.2025.mkv.part"]["settles_at"] is None
    assert incoming["Down.2025.mkv.part"]["title_guess"] == "Down (2025)"
    assert board["status"]["watch"] == [{"id": folder["id"], "path": str(env.incoming), "status": "watching",
                                        "enabled": True}]  # fmt: skip

    # Library: done, non-test operations with the matched title.
    film = env.touch("Done/Film.2010.mkv", b"y" * 1234)
    job = env.create_job(film, mode="copy")
    env.client.post(f"/api/jobs/{job['id']}/execute")
    env.wait(job["id"])
    library = home(env)["library"]
    assert library["total_files"] == 1 and library["total_bytes"] == 1234
    [card] = library["recent"]
    assert card["title"] == "Film (2010)" and card["kind"] == "movie" and card["can_undo"] is True
    assert card["destination"].endswith("Film (2010).mkv")

    # Renaming: items of an active job.
    with env.ctx.db.session() as session:
        running = Job(label="running", source="manual", status="executing", mode="move", input_paths=[], progress=0.4)
        session.add(running)
        session.flush()
        session.add(JobItem(job_id=running.id, source_path=str(env.incoming / "Busy.2001.mkv"), status="approved"))
    [renaming] = home(env)["renaming"]
    assert renaming["status"] == "approved" and renaming["filename"] == "Busy.2001.mkv"
    assert renaming["job_progress"] == pytest.approx(0.4)
    assert renaming["title"] == "Busy.2001"  # not parsed yet: the file name


def test_home_hides_incoming_files_that_were_already_handled(env):
    folder = env.client.post("/api/watch-folders", json={"path": str(env.incoming), "auto_execute": False}).json()
    src = env.touch("unknown.thing.2011.mkv")
    env.create_job(src)
    st = os.stat(src)
    now = datetime.now(UTC)
    env.fakes.watchers[-1].waiting = [
        WatchedFile(folder_id=folder["id"], path=str(src), size=st.st_size, mtime_ns=st.st_mtime_ns,
                    state="settling", settle_seconds=30, stable_since=now, settles_at=now),
    ]  # fmt: skip
    assert home(env)["incoming"] == []


# --------------------------------------------------------------------------- POST /api/home/actions


def test_home_actions_skip_approve_retry_dismiss(env):
    env.touch("unknown.thing.2011.mkv")
    env.touch("Vague.Movie.2010.mkv")
    job = env.create_job(env.incoming)
    items = items_by_name(job)
    unknown, vague = items["unknown.thing.2011.mkv"], items["Vague.Movie.2010.mkv"]

    assert act(env, f"item:{vague['id']}", "approve").json() == {"ok": True}
    env.wait(job["id"])
    assert env.client.get(f"/api/items/{vague['id']}").json()["status"] == "done"
    assert act(env, f"item:{vague['id']}", "approve").status_code == 409  # done now

    assert act(env, f"item:{unknown['id']}", "retry").status_code == 200  # re-matches; still no match
    env.wait(job["id"])
    assert env.client.get(f"/api/items/{unknown['id']}").json()["status"] == "review"
    assert act(env, f"item:{unknown['id']}", "skip").status_code == 200
    assert env.client.get(f"/api/items/{unknown['id']}").json()["status"] == "skipped"
    assert home(env)["needs_you_count"] == 0

    # retry of a failed move runs it again
    env.fakes.fileops.fail_with = "disk full"
    env.touch("Again/Film.2010.mkv")
    failing = env.create_job(env.incoming / "Again")
    env.client.post(f"/api/jobs/{failing['id']}/execute")
    item = env.wait(failing["id"])["items"][0]
    assert item["status"] == "error"
    assert act(env, f"item:{item['id']}", "approve").status_code == 409  # approve is for review items
    env.fakes.fileops.fail_with = None
    assert act(env, f"item:{item['id']}", "retry").status_code == 200
    assert env.wait(failing["id"])["items"][0]["status"] == "done"

    # events only dismiss
    event = env.ctx.bus.log("error", "system", "Something broke")
    assert act(env, f"event:{event.id}", "skip").status_code == 409
    assert act(env, f"event:{event.id}", "dismiss").json() == {"ok": True}
    assert all(c["key"] != f"event:{event.id}" for c in home(env)["needs_you"])
    assert act(env, "event:999999", "dismiss").status_code == 404
    assert act(env, "item:999999", "skip").status_code == 404
    assert act(env, "bogus", "skip").status_code == 404
    assert env.client.post("/api/home/actions", json={"key": "item:1", "action": "explode"}).status_code == 422


# --------------------------------------------------------------------------- start now


def test_start_now_endpoint(env):
    folder = env.client.post("/api/watch-folders", json={"path": str(env.incoming)}).json()
    watcher = env.fakes.watchers[-1]
    now = datetime.now(UTC)
    path = str(env.incoming / "A.2020.mkv")
    watcher.waiting = [
        WatchedFile(folder_id=folder["id"], path=path, size=1, mtime_ns=1, state="settling", settle_seconds=30,
                    stable_since=now, settles_at=now),
        WatchedFile(folder_id=folder["id"], path=path + ".part", size=1, mtime_ns=1, state="partial",
                    settle_seconds=30, stable_since=None, settles_at=None),
    ]  # fmt: skip
    url = f"/api/watch-folders/{folder['id']}/start-now"
    assert env.client.post(url, json={}).json() == {"started": 1}
    assert env.client.post(url, json={"path": path}).json() == {"started": 1}
    assert env.client.post(url, json={"path": path + ".part"}).json() == {"started": 0}
    assert env.client.post(url).json() == {"started": 1}  # no body at all
    assert watcher.started_now[1] == (folder["id"], os.path.normpath(path))
    assert env.client.post(url, json={"path": "relative.mkv"}).status_code == 400
    assert env.client.post("/api/watch-folders/999/start-now", json={}).status_code == 404


def test_home_refresh_starts_files_waiting_in_watch_folders(env):
    folder = env.client.post("/api/watch-folders", json={"path": str(env.incoming)}).json()
    watcher = env.fakes.watchers[-1]
    now = datetime.now(UTC)
    path = str(env.incoming / "A.2020.mkv")
    watcher.waiting = [
        WatchedFile(folder_id=folder["id"], path=path, size=1, mtime_ns=1, state="settling", settle_seconds=30,
                    stable_since=now, settles_at=now),
        WatchedFile(folder_id=folder["id"], path=path + ".part", size=1, mtime_ns=1, state="partial",
                    settle_seconds=30, stable_since=None, settles_at=None),
    ]  # fmt: skip
    assert env.client.post("/api/home/refresh").json() == {"cleared": 0, "started": 1}
    assert watcher.scans == 1


# --------------------------------------------------------------------------- GET /api/history


def _seed_history(env) -> dict[str, int]:
    for name in ("Alpha.2001.mkv", "Beta.2002.mkv", "Show.S01E01.mkv"):
        env.touch(f"moved/{name}", b"m" * 100)
    moved = env.create_job(env.incoming / "moved", mode="copy")
    env.client.post(f"/api/jobs/{moved['id']}/execute")
    env.wait(moved["id"])
    ops = env.client.get("/api/operations").json()
    beta = next(op for op in ops if "Beta" in op["source"])
    assert env.client.post(f"/api/operations/{beta['id']}/undo").status_code == 200
    env.touch("skipped/Gamma.2003.trailer.mkv")
    env.create_job(env.incoming / "skipped")
    env.fakes.fileops.fail_with = "disk full"
    env.touch("broken/Delta.2004.mkv")
    broken = env.create_job(env.incoming / "broken")
    env.client.post(f"/api/jobs/{broken['id']}/execute")
    env.wait(broken["id"])
    env.fakes.fileops.fail_with = None
    return {"beta": beta["id"]}


def test_library_cards_carry_episode_numbers(env):
    """Home folds a run of episodes of one show into one row, so series cards name the show and episode."""
    for name in ("Alpha.2001.mkv", "Show.S01E01.mkv"):
        env.touch(f"lib/{name}", b"m" * 100)
    job = env.create_job(env.incoming / "lib", mode="copy")
    env.client.post(f"/api/jobs/{job['id']}/execute")
    env.wait(job["id"])
    cards = {c["title"]: c for c in env.client.get("/api/home").json()["library"]["recent"]}
    episode = cards["Show (2010) · S01E01"]
    assert (episode["show"], episode["season"], episode["episode"]) == ("Show (2010)", 1, 1)
    movie = cards["Alpha (2001)"]
    assert (movie["show"], movie["season"], movie["episode"]) == (None, None, None)


def test_history_filters_and_pagination(env):
    seeded = _seed_history(env)
    everything = env.client.get("/api/history").json()
    types = [(e["type"], e["title"]) for e in everything["entries"]]
    assert everything["next_before"] is None
    assert ("problem", "Delta (2004)") in types
    assert ("skipped", "Gamma") in types  # skipped before matching: the parsed title
    assert ("undone", "Beta (2002)") in types
    assert ("moved", "Alpha (2001)") in types and ("moved", "Show (2010) · S01E01") in types
    assert len(types) == 5  # the failed move shows once (as the item still in error)
    stamps = [e["ts"] for e in everything["entries"]]
    assert stamps == sorted(stamps, reverse=True)

    undone = next(e for e in everything["entries"] if e["type"] == "undone")
    assert undone["operation_id"] == seeded["beta"] and undone["can_undo"] is False
    alpha = next(e for e in everything["entries"] if e["title"] == "Alpha (2001)")
    assert alpha["can_undo"] is True and alpha["mode"] == "copy" and alpha["bytes"] == 100
    assert alpha["detail"] == "Matched Alpha (2001)" and alpha["kind"] == "movie"
    problem = next(e for e in everything["entries"] if e["type"] == "problem")
    assert problem["can_fix"] is True and problem["detail"] == "Not enough free space to move this file"
    skipped = next(e for e in everything["entries"] if e["type"] == "skipped")
    assert skipped["detail"] == "Bonus clip" and skipped["can_fix"] is False

    # "Show older": page through two at a time without gaps or repeats
    seen: list[str] = []
    before = None
    for _ in range(10):
        params = {"limit": 2, **({"before": before} if before else {})}
        page = env.client.get("/api/history", params=params).json()
        seen += [e["key"] for e in page["entries"]]
        before = page["next_before"]
        if before is None:
            break
    assert seen == [e["key"] for e in everything["entries"]]

    def titles(**params) -> list[str]:
        return sorted(e["title"] for e in env.client.get("/api/history", params=params).json()["entries"])

    assert titles(filter="moved") == ["Alpha (2001)", "Show (2010) · S01E01"]
    assert titles(filter="skipped") == ["Gamma"]
    assert titles(filter="problems") == ["Delta (2004)"]
    assert titles(q="alpha") == ["Alpha (2001)"]
    assert titles(kind="series") == ["Show (2010) · S01E01"]
    assert "Show (2010) · S01E01" not in titles(kind="movie")
    assert env.client.get("/api/history", params={"before": "not-a-cursor"}).status_code == 400
    assert env.client.get("/api/history", params={"filter": "nope"}).status_code == 422


# --------------------------------------------------------------------------- backup / restore


def test_settings_backup_and_restore_round_trip(env):
    current = env.client.get("/api/settings").json()
    current["media_servers"] = [{"id": "s1", "kind": "jellyfin", "name": "JF", "url": "http://jf", "token": "jf-secret-token"}]
    current["min_confidence"] = 0.7
    current["clear_solved_after_hours"] = 168
    assert env.client.put("/api/settings", json=current).status_code == 200
    env.client.put("/api/providers", json={"credentials": {"tmdb_api_key": "tmdb-secret", "anidb_client": "schema"}})
    fmt = env.client.post("/api/formats", json={"name": "Mine", "kind": "movie", "expression": "{n} ({y})"}).json()
    watched = env.incoming / "watched"
    watched.mkdir()
    env.client.post("/api/watch-folders", json={"path": str(watched), "settle_seconds": 120, "format_id": fmt["id"]})

    response = env.client.get("/api/settings/backup")
    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="schema-settings-') and disposition.endswith('.json"')
    backup = response.json()
    assert backup["schema_backup"] == 1 and backup["created_at"]
    assert backup["app"]["media_servers"][0]["token"] == SECRET_MASK
    assert backup["app"]["webhook_api_key"] == SECRET_MASK
    assert backup["app"]["clear_solved_after_hours"] == 168
    assert backup["providers"]["credentials"]["tmdb_api_key"] == SECRET_MASK
    assert backup["providers"]["credentials"]["anidb_client"] == "schema"
    assert "Mine" in [f["name"] for f in backup["formats"]]  # the user's formats (not builtin presets)
    assert all(f["name"] != "Plex (series)" for f in backup["formats"])
    assert backup["watch_folders"][0]["settle_seconds"] == 120  # stored values are kept as they are
    assert "jf-secret-token" not in response.text and "tmdb-secret" not in response.text

    secret = env.client.get("/api/settings/backup", params={"include_secrets": True}).json()
    assert secret["app"]["media_servers"][0]["token"] == "jf-secret-token"
    assert secret["providers"]["credentials"]["tmdb_api_key"] == "tmdb-secret"
    assert secret["app"]["webhook_api_key"] == env.client.get("/api/settings/api-key").json()["api_key"]

    # Change everything, then restore.
    env.settings(min_confidence=0.9, clear_solved_after_hours=0)
    env.client.put("/api/providers", json={"language": "de"})
    env.client.delete(f"/api/watch-folders/{env.client.get('/api/watch-folders').json()[0]['id']}")
    env.client.put(f"/api/formats/{fmt['id']}", json={"name": "Mine", "kind": "movie", "expression": "{n}"})
    restored = env.client.post("/api/settings/restore", json=backup)
    assert restored.status_code == 200, restored.text
    assert restored.json() == {"app": True, "providers": True, "watch_folders": 1, "formats": len(backup["formats"])}
    app = env.client.get("/api/settings").json()
    assert app["min_confidence"] == 0.7 and app["clear_solved_after_hours"] == 168
    assert env.ctx.store.get_app().media_servers[0].token == "jf-secret-token"  # masked: stored value kept
    assert env.ctx.store.get_providers().credentials.tmdb_api_key == "tmdb-secret"
    assert env.client.get("/api/providers").json()["language"] == "en"
    [folder] = env.client.get("/api/watch-folders").json()
    assert folder["path"] == str(watched) and folder["settle_seconds"] == 120 and folder["format_id"] == fmt["id"]
    mine = [f for f in env.client.get("/api/formats").json() if f["name"] == "Mine"]
    assert len(mine) == 1 and mine[0]["expression"] == "{n} ({y})"

    # Restoring twice updates in place (watch folders matched by path, formats by name + kind).
    assert env.client.post("/api/settings/restore", json=backup).status_code == 200
    assert len(env.client.get("/api/watch-folders").json()) == 1

    # Secrets from an include_secrets backup are restored too.
    secret["app"]["media_servers"][0]["token"] = "new-token"
    assert env.client.post("/api/settings/restore", json=secret).status_code == 200
    assert env.ctx.store.get_app().media_servers[0].token == "new-token"


def test_settings_restore_rejects_bad_input(env):
    backup = env.client.get("/api/settings/backup").json()
    assert env.client.post("/api/settings/restore", json={"app": backup["app"]}).status_code == 400
    bad_app = dict(backup, app=dict(backup["app"], min_confidence=7))
    response = env.client.post("/api/settings/restore", json=bad_app)
    assert response.status_code == 400 and "min_confidence" in response.json()["detail"]
    bad_format = dict(backup, formats=[{"name": "Broken", "kind": "movie", "expression": "{n"}])
    assert env.client.post("/api/settings/restore", json=bad_format).status_code == 400
    outside = dict(backup, watch_folders=[{"path": str(Path(env.root).parent / "elsewhere")}])
    assert env.client.post("/api/settings/restore", json=outside).status_code == 400
    bad_creds = dict(backup, providers=dict(backup["providers"], credentials={"anidb_client_version": "abc"}))
    assert env.client.post("/api/settings/restore", json=bad_creds).status_code == 400
    bad_roots = dict(backup, app=dict(backup["app"], allowed_roots=["relative/path"]))
    assert env.client.post("/api/settings/restore", json=bad_roots).status_code == 400
    # nothing was written by the rejected restores
    assert env.client.get("/api/settings").json()["min_confidence"] == backup["app"]["min_confidence"]
    assert [f["name"] for f in env.client.get("/api/formats").json() if f["name"] == "Broken"] == []
