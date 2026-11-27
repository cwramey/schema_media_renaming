"""Pipeline state machine (SPEC §3.2-§3.3) driven through the API with fake siblings."""

from __future__ import annotations

import zlib
from pathlib import Path

import pytest
from conftest import ADMIN, CLIENT_HEADERS, make_config
from fastapi.testclient import TestClient
from sqlalchemy import select

from schema_app.domain import WatchFolderSpec
from schema_app.models import Job, JobItem, JobItemCandidates


def items_by_name(job: dict) -> dict[str, dict]:
    return {item["filename"]: item for item in job["items"]}


# --------------------------------------------------------------------------- matching


def test_job_matches_into_review_and_matched(env):
    env.touch("Show.S01E02.1080p.mkv")
    env.touch("unknown.thing.2011.mkv")
    job = env.create_job(env.incoming)
    assert job["status"] == "review"
    assert job["source"] == "manual"
    assert job["counts"] == {"matched": 1, "review": 1}
    assert job["total"] == 2
    items = items_by_name(job)
    show = items["Show.S01E02.1080p.mkv"]
    assert show["status"] == "matched"
    assert show["proposed_relative"] == "Show (2010)/Season 01/Show - S01E02 - Episode 2.mkv"
    assert Path(show["proposed_path"]) == env.library / "TV" / "Show (2010)" / "Season 01" / "Show - S01E02 - Episode 2.mkv"
    assert show["selected_candidate_id"].startswith("fake:series:")
    assert len(show["candidates"]) == 2
    unknown = items["unknown.thing.2011.mkv"]
    assert unknown["status"] == "review"
    assert "no_match" in unknown["warnings"]
    assert unknown["proposed_path"] is None


def test_all_matched_job_is_ready(env):
    env.touch("Movie.Title.2001.1080p.mkv")
    job = env.create_job(env.incoming)
    assert job["status"] == "ready"
    item = job["items"][0]
    assert item["proposed_relative"] == "Movie Title (2001)/Movie Title (2001).mkv"


def test_non_video_files_are_ignored_and_empty_job_fails(env):
    env.touch("notes.txt")
    env.touch("download.mkv.part")
    job = env.create_job(env.incoming)
    assert job["status"] == "failed"
    assert "No video files" in job["error"]


def test_ignore_patterns_and_hidden_files(env):
    env.touch("Good.Movie.2010.mkv")
    env.touch(".hidden.Movie.2010.mkv")
    env.touch("Partial.Movie.2010.mkv.crdownload")
    job = env.create_job(env.incoming)
    assert [i["filename"] for i in job["items"]] == ["Good.Movie.2010.mkv"]


def test_samples_and_extras_are_skipped(env):
    env.touch("Show.S01E01.sample.mkv")
    env.touch("Film.2010.trailer.mkv")
    env.touch("Film.2010.mkv")
    job = env.create_job(env.incoming)
    items = items_by_name(job)
    assert items["Show.S01E01.sample.mkv"]["status"] == "skipped"
    assert "sample" in items["Show.S01E01.sample.mkv"]["warnings"]
    assert items["Film.2010.trailer.mkv"]["status"] == "skipped"
    assert "extra" in items["Film.2010.trailer.mkv"]["warnings"]
    assert items["Film.2010.mkv"]["status"] == "matched"
    assert job["status"] == "ready"


def test_samples_kept_when_setting_disabled(env):
    env.settings(skip_samples=False)
    env.touch("Show.S01E01.sample.mkv")
    job = env.create_job(env.incoming)
    assert job["items"][0]["status"] == "matched"


def test_crc_verified_and_mismatch(env):
    content = b"anime-bytes"
    crc = f"{zlib.crc32(content) & 0xFFFFFFFF:08X}"
    env.touch(f"[Grp] Anime Show S01E03 [{crc}].mkv", content)
    env.touch("[Grp] Anime Show S01E04 [DEADBEEF].mkv", content)
    job = env.create_job(env.incoming)
    items = {i["parsed"]["episodes"][0]: i for i in job["items"]}
    assert items[3]["crc_ok"] is True
    assert items[3]["status"] == "matched"
    assert items[4]["crc_ok"] is False
    assert items[4]["status"] == "review"
    assert "crc_mismatch" in items[4]["warnings"]


def test_crc_check_can_be_disabled(env):
    env.settings(verify_crc=False)
    env.touch("[Grp] Anime Show S01E04 [DEADBEEF].mkv")
    job = env.create_job(env.incoming)
    assert job["items"][0]["crc_ok"] is None
    assert job["items"][0]["status"] == "matched"


def test_low_confidence_goes_to_review(env):
    env.touch("Vague.Show.S01E01.mkv")
    job = env.create_job(env.incoming)
    item = job["items"][0]
    assert item["status"] == "review"
    assert "low_confidence" in item["warnings"]
    assert item["proposed_path"]  # still rendered for the reviewer


def test_min_confidence_setting_is_respected(env):
    env.settings(min_confidence=0.4)
    env.touch("Vague.Show.S01E01.mkv")
    job = env.create_job(env.incoming)
    assert job["items"][0]["status"] == "matched"


def test_existing_destination_with_skip_policy_needs_review(env):
    src = env.touch("Film.2010.mkv")
    dest = env.library / "Movies" / "Film (2010)" / "Film (2010).mkv"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"old")
    job = env.create_job(src)
    item = job["items"][0]
    assert item["status"] == "review"
    assert "destination_exists" in item["warnings"]


def test_existing_destination_with_overwrite_policy_is_matched(env):
    env.settings(conflict_policy="overwrite")
    src = env.touch("Film.2010.mkv")
    dest = env.library / "Movies" / "Film (2010)" / "Film (2010).mkv"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"old")
    job = env.create_job(src)
    assert job["items"][0]["status"] == "matched"
    assert "replaces_existing" in job["items"][0]["warnings"]  # visible in the preview


def test_multi_version_suffix(env):
    env.touch("a/Film.2020.1080p.mkv")
    env.touch("b/Film.2020.2160p.REMUX.HDR.mkv")
    job = env.create_job(env.incoming)
    rels = sorted(i["proposed_relative"] for i in job["items"])
    assert rels == ["Film (2020)/Film (2020) - 1080p.mkv", "Film (2020)/Film (2020) - 2160p Remux HDR.mkv"]
    assert job["status"] == "ready"


def test_unresolvable_collision_goes_to_review(env):
    env.touch("a/Film.2020.1080p.mkv")
    env.touch("b/Film.2020.1080p.mkv")
    job = env.create_job(env.incoming)
    assert all(i["status"] == "review" for i in job["items"])
    assert all("duplicate_in_batch" in i["warnings"] for i in job["items"])
    # the same quality can't tell them apart, so no " - 1080p" is added
    assert all(i["proposed_relative"] == "Film (2020)/Film (2020).mkv" for i in job["items"])


def test_version_suffix_goes_away_when_the_collision_does(env):
    env.touch("a/Film.2020.1080p.mkv")
    env.touch("b/Film.2020.720p.mkv")
    job = env.create_job(env.incoming)
    by_rel = {i["proposed_relative"]: i for i in job["items"]}
    assert set(by_rel) == {"Film (2020)/Film (2020) - 1080p.mkv", "Film (2020)/Film (2020) - 720p.mkv"}
    moved = by_rel["Film (2020)/Film (2020) - 720p.mkv"]
    other = by_rel["Film (2020)/Film (2020) - 1080p.mkv"]
    picked = env.client.post(f"/api/items/{moved['id']}/rematch", json={"hint": {"query": "Other Film", "year": 2020}})
    assert picked.status_code == 200, picked.text
    assert picked.json()["proposed_relative"] == "Other Film (2020)/Other Film (2020).mkv"
    assert env.client.get(f"/api/items/{other['id']}").json()["proposed_relative"] == "Film (2020)/Film (2020).mkv"


def test_two_part_release_of_one_episode_gets_part_suffixes(env, monkeypatch):
    original = env.fakes.matcher._result

    def result(parsed, hint):
        out = original(parsed, hint)
        if out.best is not None and parsed.episodes and parsed.episodes[0] in (1, 2):
            part = parsed.episodes[0]
            first = out.best.model_copy(update={
                "part": part,
                "episodes": [e.model_copy(update={"episode": 1, "title": "Just Say Yes"}) for e in out.best.episodes]})
            out = out.model_copy(update={"best": first, "candidates": [first, *out.candidates[1:]]})
        return out

    monkeypatch.setattr(env.fakes.matcher, "_result", result)
    env.touch("Ted.S01E01.Just.Say.Yes.Part.1.mkv")
    env.touch("Ted.S01E02.Just.Say.Yes.Part.2.mkv")
    job = env.create_job(env.incoming, kind="series")
    rels = sorted(i["proposed_relative"] for i in job["items"])
    assert [r.rsplit(" - ", 1)[-1] for r in rels] == ["part1.mkv", "part2.mkv"]
    assert rels[0].rsplit(" - ", 1)[0] == rels[1].rsplit(" - ", 1)[0]  # same episode name otherwise
    assert all(i["status"] == "matched" for i in job["items"])


def test_patch_accepts_a_candidate_id_from_search(env):
    env.touch("Featurette.2010.mkv")
    job = env.create_job(env.incoming)
    item = job["items"][0]
    found = env.client.post(f"/api/items/{item['id']}/search", json={"query": "Some Show", "kind": "series"}).json()
    assert found[0]["id"] not in {c["id"] for c in item["candidates"]}
    updated = env.client.patch(f"/api/items/{item['id']}", json={"selected_candidate_id": found[0]["id"]})
    assert updated.status_code == 200, updated.text
    assert updated.json()["selected_candidate_id"] == found[0]["id"]
    bogus = env.client.patch(f"/api/items/{item['id']}", json={"selected_candidate_id": "nonsense"})
    assert bogus.status_code == 400


def test_job_kind_hint_and_format(env):
    fmt = env.client.post("/api/formats", json={"name": "flat", "kind": "series", "expression": "{n}/{sxe}"}).json()
    env.touch("Show.S02E05.mkv")
    job = env.create_job(env.incoming, kind="series", format_id=fmt["id"])
    assert job["kind"] == "series"
    assert job["format_id"] == fmt["id"]
    assert job["items"][0]["proposed_relative"] == "Show/2x05.mkv"


def test_custom_dest_root(env):
    target = env.root / "elsewhere"
    target.mkdir()
    env.touch("Film.2010.mkv")
    job = env.create_job(env.incoming, dest_root=str(target))
    assert Path(job["items"][0]["proposed_path"]).parent.parent == target


def test_missing_library_root_needs_review(env):
    env.settings(library_roots={})
    env.touch("Film.2010.mkv")
    job = env.create_job(env.incoming)
    item = job["items"][0]
    assert item["status"] == "review"
    assert "no_library_root" in item["warnings"]


def test_format_block_errors_become_warnings(env):
    fmt = env.client.post(
        "/api/formats", json={"name": "bad", "kind": "movie", "expression": "{n}{' - ' + n.lower(1, 2, 3)}"}
    ).json()
    env.touch("Film.2010.mkv")
    job = env.create_job(env.incoming, format_id=fmt["id"])
    item = job["items"][0]
    assert item["proposed_relative"] == "Film.mkv"
    assert any(w.startswith("format") for w in item["warnings"])


def test_matcher_exception_sends_items_to_review(env):
    env.fakes.matcher.fail = True
    env.touch("Film.2010.mkv")
    job = env.create_job(env.incoming)
    item = job["items"][0]
    assert item["status"] == "review"
    assert any(w.startswith("match_error") for w in item["warnings"])


def test_sidecars_are_attached(env):
    env.touch("Show.S01E01.mkv")
    env.touch("Show.S01E01.en.srt", b"subs")
    job = env.create_job(env.incoming)
    item = job["items"][0]
    assert [Path(p).name for p in item["sidecars"]] == ["Show.S01E01.en.srt"]


# --------------------------------------------------------------------------- execution


def test_auto_execute_completes_job_and_refreshes_media_servers(env):
    env.settings(media_servers=[{"id": "srv1", "kind": "plex", "name": "Plex", "url": "http://plex", "token": "t"}])
    src = env.touch("Show.S01E01.mkv")
    env.touch("Show.S01E01.en.srt", b"subs")
    job = env.create_job(src.parent, auto_execute=True)
    job = env.wait(job["id"])
    assert job["status"] == "completed"
    assert job["finished_at"] is not None
    item = job["items"][0]
    assert item["status"] == "done"
    final = Path(item["final_path"])
    assert final.exists() and not src.exists()
    assert final.with_name("Show - S01E01 - Episode 1.en.srt").exists()
    ops = env.client.get("/api/operations", params={"job_id": job["id"]}).json()
    assert len(ops) == 1 and ops[0]["status"] == "done"
    assert ops[0]["sidecars"][0]["destination"].endswith(".en.srt")
    assert env.fakes.media.refreshed and env.fakes.media.refreshed[0][0] == "Plex"


def test_auto_execute_leaves_review_items(env):
    env.touch("Film.2010.mkv")
    env.touch("unknown.thing.mkv")
    job = env.create_job(env.incoming, auto_execute=True)
    job = env.wait(job["id"])
    assert job["status"] == "review"
    assert job["counts"] == {"done": 1, "review": 1}


def test_execute_copy_then_undo(env):
    src = env.touch("Film.2010.mkv")
    job = env.create_job(src)
    response = env.client.post(f"/api/jobs/{job['id']}/execute", json={"mode": "copy"})
    assert response.status_code == 200
    assert response.json()["status"] == "executing"
    job = env.wait(job["id"])
    assert job["status"] == "completed"
    item = job["items"][0]
    assert src.exists() and Path(item["final_path"]).exists()
    op = env.client.get("/api/operations").json()[0]
    assert op["mode"] == "copy" and op["item_id"] == item["id"]

    undone = env.client.post(f"/api/operations/{op['id']}/undo")
    assert undone.status_code == 200
    assert undone.json()["status"] == "undone"
    assert undone.json()["undone_at"] is not None
    assert not Path(item["final_path"]).exists()
    job = env.client.get(f"/api/jobs/{job['id']}").json()
    assert job["status"] == "review"
    assert job["finished_at"] is None
    assert job["items"][0]["status"] == "review"
    assert "undone" in job["items"][0]["warnings"]
    assert env.client.post(f"/api/operations/{op['id']}/undo").status_code == 409


def test_execute_selected_items_only(env):
    env.touch("Film.2010.mkv")
    env.touch("Other.Film.2011.mkv")
    job = env.create_job(env.incoming)
    first = job["items"][0]["id"]
    env.client.post(f"/api/jobs/{job['id']}/execute", json={"item_ids": [first]})
    job = env.wait(job["id"])
    statuses = {i["id"]: i["status"] for i in job["items"]}
    assert statuses[first] == "done"
    assert sorted(statuses.values()) == ["done", "matched"]
    assert job["status"] == "ready"


def test_execute_test_mode_touches_nothing(env):
    src = env.touch("Film.2010.mkv")
    job = env.create_job(src)
    env.client.post(f"/api/jobs/{job['id']}/execute", json={"mode": "test"})
    job = env.wait(job["id"])
    item = job["items"][0]
    # A dry run reports but never completes the item: it stays runnable and the job stays ready.
    assert item["status"] == "matched"
    assert item["final_path"] is None
    assert f"dry_run_ok: {item['proposed_path']}" in item["warnings"]
    assert job["status"] == "ready"
    assert src.exists()
    assert env.client.get("/api/operations").json()[0]["mode"] == "test"
    assert env.fakes.media.refreshed == []


def test_execute_failure_marks_item_error_and_job_failed(env):
    env.fakes.fileops.fail_with = "disk full"
    env.touch("Film.2010.mkv")
    job = env.create_job(env.incoming)
    env.client.post(f"/api/jobs/{job['id']}/execute")
    job = env.wait(job["id"])
    assert job["status"] == "failed"
    assert job["items"][0]["status"] == "error"
    assert job["items"][0]["error"] == "disk full"
    op = env.client.get("/api/operations").json()[0]
    assert op["status"] == "failed" and op["error"] == "disk full"
    assert env.client.post(f"/api/operations/{op['id']}/undo").status_code == 409
    errors = env.client.get("/api/events", params={"level": "error"}).json()
    assert any("disk full" in e["message"] for e in errors)


def test_conflict_at_execution_time_returns_item_to_review(env):
    src = env.touch("Film.2010.mkv")
    job = env.create_job(src)
    dest = Path(job["items"][0]["proposed_path"])
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"appeared meanwhile")
    env.client.post(f"/api/jobs/{job['id']}/execute")
    job = env.wait(job["id"])
    assert job["items"][0]["status"] == "review"
    assert "destination_exists" in job["items"][0]["warnings"]


def test_execute_with_nothing_runnable_is_409(env):
    env.touch("unknown.thing.mkv")
    job = env.create_job(env.incoming)
    assert env.client.post(f"/api/jobs/{job['id']}/execute").status_code == 409
    assert env.client.post("/api/jobs/999/execute").status_code == 404


# --------------------------------------------------------------------------- job actions


def test_cancel_settled_job_and_terminal_conflicts(env):
    env.touch("Film.2010.mkv")
    job = env.create_job(env.incoming)
    response = env.client.post(f"/api/jobs/{job['id']}/cancel")
    assert response.status_code == 200 and response.json()["status"] == "cancelled"
    assert env.client.post(f"/api/jobs/{job['id']}/cancel").status_code == 409
    assert env.client.post(f"/api/jobs/{job['id']}/execute").status_code == 409


def test_rematch_job_requeues_and_rematches(env):
    env.touch("Film.2010.mkv")
    job = env.create_job(env.incoming)
    calls = len(env.fakes.matcher.calls)
    response = env.client.post(f"/api/jobs/{job['id']}/rematch")
    assert response.status_code == 200
    job = env.wait(job["id"])
    assert job["status"] == "ready"
    assert len(env.fakes.matcher.calls) > calls


def test_delete_job(env):
    env.touch("Film.2010.mkv")
    job = env.create_job(env.incoming)
    assert env.client.delete(f"/api/jobs/{job['id']}").status_code == 204
    assert env.client.get(f"/api/jobs/{job['id']}").status_code == 404
    assert env.client.delete(f"/api/jobs/{job['id']}").status_code == 404


def test_create_job_validation(env, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.mkv").write_bytes(b"x")
    assert env.client.post("/api/jobs", json={"paths": [str(outside / "x.mkv")]}).status_code == 403
    assert env.client.post("/api/jobs", json={"paths": [str(env.incoming / "nope.mkv")]}).status_code == 400
    assert env.client.post("/api/jobs", json={"paths": ["relative/path.mkv"]}).status_code == 400
    assert env.client.post("/api/jobs", json={"paths": []}).status_code == 422
    env.touch("Film.2010.mkv")
    assert env.client.post("/api/jobs", json={"paths": [str(env.incoming)], "dest_root": str(outside)}).status_code == 403
    assert env.client.post("/api/jobs", json={"paths": [str(env.incoming)], "format_id": 9999}).status_code == 400


def test_job_list_filters(env):
    env.touch("Film.2010.mkv")
    ready = env.create_job(env.incoming)
    env.touch("x/unknown.mkv")
    review = env.create_job(env.incoming / "x")
    listed = env.client.get("/api/jobs").json()
    assert [j["id"] for j in listed] == [review["id"], ready["id"]]
    assert [j["id"] for j in env.client.get("/api/jobs", params={"status": "ready"}).json()] == [ready["id"]]
    assert len(env.client.get("/api/jobs", params={"limit": 1}).json()) == 1
    assert [j["id"] for j in env.client.get("/api/jobs", params={"limit": 1, "offset": 1}).json()] == [ready["id"]]
    assert env.client.get("/api/jobs", params={"limit": 0}).status_code == 422


# --------------------------------------------------------------------------- item actions


def test_patch_item_status_transitions(env):
    env.touch("unknown.thing.mkv")
    env.touch("Film.2010.mkv")
    job = env.create_job(env.incoming)
    items = items_by_name(job)
    unknown, film = items["unknown.thing.mkv"], items["Film.2010.mkv"]
    # approving without a destination is refused
    assert env.client.patch(f"/api/items/{unknown['id']}", json={"status": "approved"}).status_code == 409
    skipped = env.client.patch(f"/api/items/{unknown['id']}", json={"status": "skipped"}).json()
    assert skipped["status"] == "skipped"
    assert env.client.get(f"/api/jobs/{job['id']}").json()["status"] == "ready"
    approved = env.client.patch(f"/api/items/{film['id']}", json={"status": "approved"}).json()
    assert approved["status"] == "approved"
    back = env.client.patch(f"/api/items/{film['id']}", json={"status": "review"}).json()
    assert back["status"] == "review"
    assert env.client.get(f"/api/jobs/{job['id']}").json()["status"] == "review"
    assert env.client.patch("/api/items/9999", json={"status": "skipped"}).status_code == 404
    assert env.client.patch(f"/api/items/{film['id']}", json={"status": "done"}).status_code == 422


def test_patch_item_candidate_rerenders_and_approves(env):
    env.touch("Vague.Show.S01E01.mkv")
    job = env.create_job(env.incoming)
    item = job["items"][0]
    alt = item["candidates"][1]
    updated = env.client.patch(f"/api/items/{item['id']}", json={"selected_candidate_id": alt["id"]}).json()
    assert updated["selected_candidate_id"] == alt["id"]
    assert updated["status"] == "approved"
    assert updated["proposed_relative"].startswith("Vague Show Alt (2010)/")
    bad = env.client.patch(f"/api/items/{item['id']}", json={"selected_candidate_id": "nope"})
    assert bad.status_code == 400


def test_patch_item_path_override_and_reset(env):
    env.touch("Film.2010.mkv")
    job = env.create_job(env.incoming)
    item = job["items"][0]
    custom = env.library / "Movies" / "Custom" / "Name.mkv"
    updated = env.client.patch(f"/api/items/{item['id']}", json={"proposed_path": str(custom)}).json()
    assert updated["path_overridden"] is True
    assert Path(updated["proposed_path"]) == custom
    assert updated["status"] == "matched"  # a path edit is not an approval
    assert updated["proposed_relative"] == "Custom/Name.mkv"  # follows the override, not the old render
    relative = env.client.patch(f"/api/items/{item['id']}", json={"proposed_path": "Rel/Other.mkv"}).json()
    assert Path(relative["proposed_path"]) == env.library / "Movies" / "Rel" / "Other.mkv"
    assert relative["proposed_relative"] == "Rel/Other.mkv"
    outside = env.client.patch(f"/api/items/{item['id']}", json={"proposed_path": str(env.root.parent / "x.mkv")})
    assert outside.status_code == 403
    reset = env.client.patch(f"/api/items/{item['id']}", json={"reset_path": True}).json()
    assert reset["path_overridden"] is False
    assert reset["proposed_relative"] == "Film (2010)/Film (2010).mkv"
    assert Path(reset["proposed_path"]).name == "Film (2010).mkv"


def test_item_rematch_with_ref_prepends_candidate_and_approves(env):
    env.touch("unknown.Show.S01E05.mkv")
    job = env.create_job(env.incoming)
    item = job["items"][0]
    hint = {"ref": {"provider": "tvdb", "id": "4242"}, "kind": "series", "provider": "tvdb", "season": 1, "episodes": [5]}
    updated = env.client.post(f"/api/items/{item['id']}/rematch", json={"hint": hint}).json()
    assert updated["selected_candidate_id"] == "tvdb:series:4242"
    assert updated["candidates"][0]["id"] == "tvdb:series:4242"
    assert updated["status"] == "approved"
    assert updated["proposed_relative"] == "Picked Show (2010)/Season 01/Picked Show - S01E05 - Episode 5.mkv"
    assert ("resolve", "tvdb:4242") in env.fakes.matcher.calls
    assert env.client.get(f"/api/jobs/{job['id']}").json()["status"] == "ready"


def test_item_rematch_with_query_hint(env):
    env.touch("unknown.thing.mkv")
    job = env.create_job(env.incoming)
    item = job["items"][0]
    updated = env.client.post(f"/api/items/{item['id']}/rematch",
                              json={"hint": {"query": "Real Movie", "kind": "movie"}}).json()  # fmt: skip
    assert updated["status"] == "matched"
    assert updated["proposed_relative"].startswith("Real Movie")


def test_item_search(env):
    env.touch("unknown.thing.mkv")
    job = env.create_job(env.incoming)
    item_id = job["items"][0]["id"]
    results = env.client.post(f"/api/items/{item_id}/search", json={"query": "Thing", "kind": "movie"}).json()
    assert results[0]["title"] == "Thing" and results[0]["kind"] == "movie"
    assert env.client.post("/api/items/999/search", json={"query": "x", "kind": "movie"}).status_code == 404
    assert env.client.post(f"/api/items/{item_id}/search", json={"query": "", "kind": "movie"}).status_code == 422


def test_bulk_actions(env):
    env.touch("Film.2010.mkv")
    env.touch("Vague.Movie.2011.mkv")
    job = env.create_job(env.incoming)
    ids = [i["id"] for i in job["items"]]
    approved = env.client.post("/api/items/bulk", json={"item_ids": ids, "action": "approve"}).json()
    assert {i["status"] for i in approved} == {"approved"}
    assert env.client.get(f"/api/jobs/{job['id']}").json()["status"] == "ready"
    skipped = env.client.post("/api/items/bulk", json={"item_ids": ids[:1], "action": "skip"}).json()
    assert skipped[0]["status"] == "skipped"
    rematched = env.client.post("/api/items/bulk", json={"item_ids": ids, "action": "rematch"}).json()
    assert {i["status"] for i in rematched} == {"matched", "review"}
    assert env.client.post("/api/items/bulk", json={"item_ids": [999], "action": "skip"}).status_code == 404
    assert env.client.post("/api/items/bulk", json={"item_ids": ids, "action": "explode"}).status_code == 422


def test_items_of_running_jobs_are_locked(env):
    env.touch("Film.2010.mkv")
    job = env.create_job(env.incoming)
    item_id = job["items"][0]["id"]
    with env.ctx.db.session() as session:
        session.get(Job, job["id"]).status = "executing"
    assert env.client.patch(f"/api/items/{item_id}", json={"status": "skipped"}).status_code == 409
    assert env.client.delete(f"/api/jobs/{job['id']}").status_code == 409
    assert env.client.post(f"/api/jobs/{job['id']}/rematch").status_code == 409


def test_preview_is_stateless(env):
    response = env.client.post("/api/preview", json={"names": ["Show.S01E02.mkv", "unknown.thing.mkv"]})
    assert response.status_code == 200
    first, second = response.json()
    assert first["rendered"] == "Show (2010)/Season 01/Show - S01E02 - Episode 2.mkv"
    assert first["best"]["kind"] == "series"
    assert second["rendered"] is None and second["error"]
    custom = env.client.post("/api/preview", json={"names": ["Film.2010.mkv"], "expression": "{n.upper()} [{y}]"}).json()
    assert custom[0]["rendered"] == "FILM [2010].mkv"
    assert env.client.post("/api/preview", json={"names": ["x.mkv"], "expression": "{n"}).status_code == 400
    assert env.client.get("/api/jobs").json() == []


# --------------------------------------------------------------------------- watch folders


def test_watch_ready_batch_creates_watch_job_and_dedupes(env):
    folder = env.client.post("/api/watch-folders", json={"path": str(env.incoming), "auto_execute": False}).json()
    watcher = env.fakes.watchers[-1]
    assert [spec.id for spec in watcher.specs] == [folder["id"]]
    assert folder["status"] == "watching"
    src = env.touch("Film.2010.mkv")
    spec = WatchFolderSpec(id=folder["id"], path=str(env.incoming))
    env.client.portal.call(watcher.on_ready, spec, [src])
    jobs = env.client.get("/api/jobs").json()
    assert len(jobs) == 1 and jobs[0]["source"] == "watch"
    job = env.wait(jobs[0]["id"])
    assert job["label"].startswith("Watch: ")
    # the same file offered again while its job is still open: no new job
    env.client.portal.call(watcher.on_ready, spec, [src])
    assert len(env.client.get("/api/jobs").json()) == 1
    listed = env.client.get("/api/watch-folders").json()[0]
    assert listed["files_seen"] == 1 and listed["last_event_at"] is not None


def test_watch_folder_auto_execute(env):
    folder = env.client.post("/api/watch-folders", json={"path": str(env.incoming), "mode": "copy"}).json()
    src = env.touch("Film.2010.mkv")
    watcher = env.fakes.watchers[-1]
    env.client.portal.call(watcher.on_ready, WatchFolderSpec(id=folder["id"], path=str(env.incoming)), [src])
    job = env.wait(env.client.get("/api/jobs").json()[0]["id"])
    job = env.wait(job["id"])
    assert job["status"] == "completed" and job["mode"] == "copy"


def test_watch_status_callback_updates_folder(env):
    folder = env.client.post("/api/watch-folders", json={"path": str(env.incoming / "missing")}).json()
    watcher = env.fakes.watchers[-1]
    env.client.portal.call(watcher.on_status, folder["id"], "missing", "folder not found")
    listed = env.client.get("/api/watch-folders").json()[0]
    assert listed["last_error"] == "folder not found"


def test_watch_scan_creates_job(env):
    folder = env.client.post("/api/watch-folders", json={"path": str(env.incoming), "auto_execute": False}).json()
    env.touch("Film.2010.mkv")
    response = env.client.post(f"/api/watch-folders/{folder['id']}/scan")
    assert response.status_code == 202
    job = env.wait(response.json()["id"])
    assert job["source"] == "watch" and job["total"] == 1
    assert env.client.post("/api/watch-folders/999/scan").status_code == 404


# --------------------------------------------------------------------------- startup


def test_startup_recovery(tmp_path, fakes):
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
            crashed = Job(label="crashed", source="manual", status="executing", mode="move", input_paths=[str(film)])
            stuck = Job(label="stuck", source="manual", status="matching", mode="move", input_paths=[str(film)])
            session.add_all([crashed, stuck])
            session.flush()
            session.add(JobItem(job_id=crashed.id, source_path=str(film), status="executing",
                                proposed_path=str(media / "lib" / "x.mkv")))  # fmt: skip
            ids = crashed.id, stuck.id
        # Make sure the workers don't pick the fake rows up before the restart.
        client.app.state.ctx.pipeline._stopping = True
    with TestClient(create_app(config), headers=CLIENT_HEADERS) as client:
        client.post("/api/auth/login", json=ADMIN)
        crashed = client.get(f"/api/jobs/{ids[0]}").json()
        assert crashed["status"] == "review"
        assert crashed["items"][0]["status"] == "review"
        assert "interrupted" in crashed["items"][0]["warnings"]
        import time

        for _ in range(200):
            stuck = client.get(f"/api/jobs/{ids[1]}").json()
            if stuck["status"] not in {"queued", "matching"}:
                break
            time.sleep(0.02)
        assert stuck["status"] == "ready"
        events = client.get("/api/events", params={"category": "job"}).json()
        assert any("interrupted" in e["message"] for e in events)


def test_matcher_unavailable_keeps_app_running(tmp_path, fakes, monkeypatch):
    from schema_app.core import matcher as matcher_mod
    from schema_app.main import create_app

    def broken(*args, **kwargs):
        raise RuntimeError("no providers")

    monkeypatch.setattr(matcher_mod, "create_matcher", broken)
    with TestClient(create_app(make_config(tmp_path)), headers=CLIENT_HEADERS) as client:
        client.post("/api/auth/setup", json=ADMIN)
        assert client.get("/api/providers").json()["statuses"] == []
        assert client.post("/api/providers/tmdb/test").status_code == 503
        events = client.get("/api/events", params={"level": "error"}).json()
        assert any("no providers" in e["message"] for e in events)


def test_shutdown_closes_matcher(make_env):
    env = make_env()
    matcher = env.fakes.matcher
    env.client.__exit__(None, None, None)
    assert matcher.closed is True
    env.client.__enter__()  # re-open so the fixture teardown stays symmetric


@pytest.mark.parametrize("mode", ["move", "copy"])
def test_default_mode_from_settings(env, mode):
    env.settings(default_mode=mode)
    env.touch("Film.2010.mkv")
    job = env.create_job(env.incoming)
    assert job["mode"] == mode


def test_db_rows_match_api(env):
    env.touch("Film.2010.mkv")
    job = env.create_job(env.incoming)
    with env.ctx.db.session() as session:
        row = session.scalars(select(JobItem).where(JobItem.job_id == job["id"])).one()
        assert row.status == "matched"
        assert row.parsed["title"] == "Film"
        detail = session.get(JobItemCandidates, row.id)
        assert detail.candidates[0]["id"] == row.selected_candidate_id
        # the derived columns lists, Home and History read instead of the candidates
        assert row.chosen["id"] == row.selected_candidate_id and row.candidate_count == len(detail.candidates)
        assert row.match_title == detail.candidates[0]["title"] and row.kind == "movie" and row.matched
        assert row.search_text.startswith(str(env.incoming).lower())


def test_upgrade_rank_of_untagged_existing_file_comes_from_probe(tmp_path, monkeypatch):
    """Schema's own names carry no quality tags: they must not rank 0 (any 720p would "upgrade" them)."""
    from schema_app.core import pipeline as pipeline_mod
    from schema_app.domain import ProbeInfo

    existing = tmp_path / "The Matrix (1999).mkv"
    existing.write_bytes(b"x")
    rank = pipeline_mod.Pipeline._existing_rank

    monkeypatch.setattr(pipeline_mod.probe_mod, "available", lambda: None)
    assert rank(None, str(existing)) is None  # can't tell -> upgrade skips

    monkeypatch.setattr(pipeline_mod.probe_mod, "available", lambda: "mediainfo")
    monkeypatch.setattr(pipeline_mod.probe_mod, "probe", lambda path: ProbeInfo(width=3840, height=1600))
    uhd = rank(None, str(existing))
    tagged_720p = tmp_path / "The.Matrix.1999.720p.HDTV.mkv"
    tagged_720p.write_bytes(b"x")
    assert uhd is not None and uhd > rank(None, str(tagged_720p))


# --------------------------------------------------------------------------- dry runs (mode "test")


def test_dry_run_then_real_execute(env):
    src = env.touch("Film.2010.mkv")
    job = env.create_job(src)
    env.client.post(f"/api/jobs/{job['id']}/execute", json={"mode": "test"})
    job = env.wait(job["id"])
    assert job["status"] == "ready"
    assert job["counts"] == {"matched": 1}
    # the dry run left something to execute: the real run works without a rematch
    response = env.client.post(f"/api/jobs/{job['id']}/execute", json={"mode": "move"})
    assert response.status_code == 200
    job = env.wait(job["id"])
    assert job["status"] == "completed"
    item = job["items"][0]
    assert item["status"] == "done"
    assert Path(item["final_path"]).exists() and not src.exists()
    assert not any(w.startswith("dry_run") for w in item["warnings"])
    ops = env.client.get("/api/operations", params={"job_id": job["id"]}).json()
    assert sorted(op["mode"] for op in ops) == ["move", "test"]


def test_dry_run_keeps_approved_status(env):
    env.touch("Film.2010.mkv")
    job = env.create_job(env.incoming)
    item_id = job["items"][0]["id"]
    env.client.patch(f"/api/items/{item_id}", json={"status": "approved"})
    env.client.post(f"/api/jobs/{job['id']}/execute", json={"mode": "test"})
    job = env.wait(job["id"])
    assert job["items"][0]["status"] == "approved"
    assert job["status"] == "ready"


def test_dry_run_failure_is_reported_without_failing_the_item(env):
    env.fakes.fileops.fail_with = "Destination folder is not writable"
    src = env.touch("Film.2010.mkv")
    job = env.create_job(src)
    env.client.post(f"/api/jobs/{job['id']}/execute", json={"mode": "test"})
    job = env.wait(job["id"])
    item = job["items"][0]
    assert item["status"] == "matched"
    assert "dry_run_failed" in item["warnings"]
    assert item["error"] == "Dry run: Destination folder is not writable"
    assert job["status"] == "ready"
    op = env.client.get("/api/operations").json()[0]
    assert op["mode"] == "test" and op["status"] == "failed"

    # once fixed, a second dry run clears the report and a real run completes
    env.fakes.fileops.fail_with = None
    env.client.post(f"/api/jobs/{job['id']}/execute", json={"mode": "test"})
    item = env.wait(job["id"])["items"][0]
    assert item["error"] is None
    assert "dry_run_failed" not in item["warnings"]
    assert any(w.startswith("dry_run_ok: ") for w in item["warnings"])
    env.client.post(f"/api/jobs/{job['id']}/execute", json={"mode": "move"})
    assert env.wait(job["id"])["items"][0]["status"] == "done"


def test_dry_run_conflict_is_reported_and_item_stays_runnable(env):
    src = env.touch("Film.2010.mkv")
    job = env.create_job(src)
    dest = Path(job["items"][0]["proposed_path"])
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"appeared meanwhile")
    env.client.post(f"/api/jobs/{job['id']}/execute", json={"mode": "test"})
    job = env.wait(job["id"])
    item = job["items"][0]
    assert item["status"] == "matched"
    assert {"dry_run_failed", "destination_exists"} <= set(item["warnings"])
    assert item["error"].startswith("Dry run: ")
    assert dest.read_bytes() == b"appeared meanwhile" and src.exists()


def test_undo_of_dry_run_does_not_touch_the_item(env):
    from schema_app.services import fileops as real_fileops

    env.fakes.fileops.undo = real_fileops.undo  # the real undo is a no-op for mode "test"
    src = env.touch("Film.2010.mkv")
    job = env.create_job(src)
    env.client.post(f"/api/jobs/{job['id']}/execute", json={"mode": "test"})
    env.wait(job["id"])
    env.client.post(f"/api/jobs/{job['id']}/execute", json={"mode": "move"})
    job = env.wait(job["id"])
    assert job["items"][0]["status"] == "done"
    test_op = next(op for op in env.client.get("/api/operations").json() if op["mode"] == "test")
    undone = env.client.post(f"/api/operations/{test_op['id']}/undo")
    assert undone.status_code == 200
    job = env.client.get(f"/api/jobs/{job['id']}").json()
    assert job["items"][0]["status"] == "done"
    assert "undone" not in job["items"][0]["warnings"]
    assert job["status"] == "completed"
    assert Path(job["items"][0]["final_path"]).exists()
