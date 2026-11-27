"""Big jobs: chunked matching that saves as it goes and resumes, narrow item rows (candidates in a side
table), cheap single-item actions, coalesced pushes, lanes and priorities, and the v6 migration."""

from __future__ import annotations

import asyncio
import functools
import json
import os
import threading
import time
import zlib
from pathlib import Path

from conftest import ADMIN, CLIENT_HEADERS, make_config
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from systems_fakes import movie_candidate

from schema_app.api_models import JobCreate, JobSource
from schema_app.core import pipeline as pipeline_mod
from schema_app.db import SCHEMA_VERSION, Database
from schema_app.models import Job, JobItem, JobItemCandidates
from schema_app.services import hashing
from schema_app.services import probe as probe_mod
from schema_app.services.jobs import open_item_paths, slim_raw


def by_name(job: dict) -> dict[str, dict]:
    return {item["filename"]: item for item in job["items"]}


def record_pushes(env) -> list[tuple[str, dict]]:
    seen: list[tuple[str, dict]] = []
    publish = env.ctx.bus.publish

    def spy(type_: str, data):  # noqa: ANN001
        seen.append((type_, data if isinstance(data, dict) else data.model_dump(mode="json")))
        publish(type_, data)

    env.ctx.bus.publish = spy
    return seen


def make_batch(env) -> None:
    for n in (1, 2, 3):
        env.touch(f"Show/Show.S01E0{n}.1080p.mkv")
    env.touch("a/Film.2010.1080p.mkv")  # same title, different quality: " - 1080p" / " - 720p"
    env.touch("b/Film.2010.720p.mkv")
    env.touch("c/Other.2011.mkv")  # same title, same quality: both duplicates
    env.touch("d/Other.2011.mkv")
    env.touch("Clip.sample.2012.mkv")  # clutter


def outcome(job: dict) -> dict[str, tuple[str, str | None, bool]]:
    return {f"{Path(i['source_path']).parent.name}/{i['filename']}":
            (i["status"], i["proposed_path"], "duplicate_in_batch" in i["warnings"]) for i in job["items"]}  # fmt: skip


# --------------------------------------------------------------------------- chunked matching


def test_chunked_match_gives_the_same_result_as_one_batch(make_env, monkeypatch, tmp_path):
    whole = make_env()
    make_batch(whole)
    expected = outcome(whole.create_job(whole.incoming))
    assert expected["a/Film.2010.1080p.mkv"][1].endswith("Film (2010) - 1080p.mkv")
    assert expected["c/Other.2011.mkv"][0] == "review" and expected["c/Other.2011.mkv"][2]

    monkeypatch.setattr(pipeline_mod, "SAVE_CHUNK", 2)
    monkeypatch.setattr(pipeline_mod, "MATCH_CHUNK", 2)
    for path in whole.incoming.rglob("*.mkv"):
        path.unlink()
    chunked = make_env(data_dir=tmp_path / "chunked")  # same media folders, its own database
    make_batch(chunked)
    pushes = record_pushes(chunked)
    job = chunked.create_job(chunked.incoming)
    assert outcome(job) == expected
    # every saved chunk is pushed as one job.items_changed with its ids, never one message per file
    changed = [d for t, d in pushes if t == "job.items_changed"]
    assert len(changed) >= 4 and all(d["job_id"] == job["id"] and len(d["item_ids"]) <= 2 for d in changed)
    assert not [t for t, _d in pushes if t == "item.updated"]
    with chunked.ctx.db.session() as session:
        assert session.scalar(select(func.count()).where(JobItem.matched.is_(False))) == 0


def test_interrupted_match_resumes_from_the_saved_chunks(tmp_path, fakes):
    from schema_app.main import create_app

    media = tmp_path / "media"
    (media / "in").mkdir(parents=True)
    for n in range(1, 5):
        (media / "in" / f"Show.S01E0{n}.mkv").write_bytes(b"x")
    config = make_config(tmp_path)
    with TestClient(create_app(config), headers=CLIENT_HEADERS) as client:
        client.post("/api/auth/setup", json=ADMIN)
        settings = client.get("/api/settings").json()
        settings.update(allowed_roots=[str(media)], library_roots={"series": str(media / "lib")})
        client.put("/api/settings", json=settings)
        job_id = client.post("/api/jobs", json={"paths": [str(media / "in")]}).json()["id"]
        for _ in range(500):
            if client.get(f"/api/jobs/{job_id}", params={"limit": 0}).json()["status"] == "ready":
                break
            time.sleep(0.02)
        ctx = client.app.state.ctx
        with ctx.db.session() as session:  # as if the container died after the first saved chunk
            session.get(Job, job_id).status = "matching"
            items = session.scalars(select(JobItem).where(JobItem.job_id == job_id).order_by(JobItem.id)).all()
            for item in items[:2]:
                item.status = "pending"  # matched and saved, not decided yet
            for item in items[2:]:
                item.status, item.matched, item.selected_candidate_id = "pending", False, None
        ctx.pipeline._stopping = True
    before = len(fakes.matcher.calls)
    with TestClient(create_app(config), headers=CLIENT_HEADERS) as client:
        client.post("/api/auth/login", json=ADMIN)
        for _ in range(500):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] not in {"queued", "matching"}:
                break
            time.sleep(0.02)
    assert job["status"] == "ready" and {i["status"] for i in job["items"]} == {"matched"}
    looked_up = [name for kind, names in fakes.matcher.calls[before:] if kind == "match_many" for name in names]
    assert sorted(looked_up) == ["Show.S01E03.mkv", "Show.S01E04.mkv"]  # only the unsaved half again


# --------------------------------------------------------------------------- what an item stores


def test_items_keep_three_candidates_and_only_the_pick_once_done(env, monkeypatch):
    many = env.fakes.matcher._result

    def five(parsed, hint):  # noqa: ANN001
        result = many(parsed, hint)
        alts = [movie_candidate(f"Alt {n}", 2010, score=0.5 - n / 100, mid=f"9{n}") for n in range(3)]
        return result.model_copy(update={"candidates": [*result.candidates, *alts]})

    monkeypatch.setattr(env.fakes.matcher, "_result", five)
    env.touch("Film.2010.mkv")
    env.touch("Other.2011.mkv")
    job = env.create_job(env.incoming)
    film, other = (by_name(job)[n] for n in ("Film.2010.mkv", "Other.2011.mkv"))
    full = env.client.get(f"/api/items/{film['id']}").json()
    assert len(full["candidates"]) == 3 and full["candidate_count"] == 3 and not full["slim"]
    page = env.client.get(f"/api/jobs/{job['id']}/items").json()["items"]
    assert {i["candidate_count"] for i in page} == {3} and all(len(i["candidates"]) == 1 for i in page)

    assert env.client.post(f"/api/jobs/{job['id']}/execute", json={"item_ids": [film["id"]]}).status_code == 200
    env.wait(job["id"])
    done = env.client.get(f"/api/items/{film['id']}").json()
    assert done["status"] == "done" and [c["id"] for c in done["candidates"]] == [done["selected_candidate_id"]]
    skipped = env.client.post("/api/items/bulk", json={"action": "skip", "item_ids": [other["id"]]}).json()[0]
    assert skipped["status"] == "skipped" and skipped["candidate_count"] == 1
    with env.ctx.db.session() as session:
        for item_id in (film["id"], other["id"]):
            assert len(session.get(JobItemCandidates, item_id).candidates) == 1


def test_deleting_a_job_deletes_its_stored_candidates(env):
    env.touch("Film.2010.mkv")
    job = env.create_job(env.incoming)
    assert env.client.delete(f"/api/jobs/{job['id']}").status_code == 204
    with env.ctx.db.session() as session:
        assert session.scalar(select(func.count()).select_from(JobItemCandidates)) == 0


def test_slim_candidate_keeps_every_episode_key_of_a_long_multi_episode_file():
    episodes = [{"series_ref": {"provider": "x", "id": "1"}, "season": 1, "episode": n, "title": f"E{n}",
                 "overview": "long"} for n in range(1, 13)]  # fmt: skip
    slim = slim_raw({"id": "x:series:1", "kind": "series", "provider": "x", "title": "T", "score": 0.9,
                     "episodes": episodes, "reasons": ["r"], "part": 2})  # fmt: skip
    assert len(slim["episodes"]) == 8 and "overview" not in slim["episodes"][0] and "reasons" not in slim
    light = pipeline_mod.Work(item_id=1, source_path="/a.mkv", parsed=None, light=True, selected_id="x:series:1",
                              chosen_raw=json.dumps(slim))  # fmt: skip
    assert light.match_key() == ("x:series:1", tuple((1, n, None) for n in range(1, 13)), 2)


# --------------------------------------------------------------------------- single-item actions


def test_status_changes_read_only_the_item(env, monkeypatch):
    env.touch("Film.2010.mkv")
    env.touch("Vague.2011.mkv")
    job = env.create_job(env.incoming)
    vague = by_name(job)["Vague.2011.mkv"]

    def whole_job(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("a status change must not read the rest of the job")

    original = pipeline_mod.Pipeline.__dict__["_light_works"]  # the staticmethod itself
    monkeypatch.setattr(pipeline_mod.Pipeline, "_light_works", staticmethod(whole_job))
    assert env.client.patch(f"/api/items/{vague['id']}", json={"status": "approved"}).json()["status"] == "approved"
    assert env.client.patch(f"/api/items/{vague['id']}", json={"status": "skipped"}).json()["status"] == "skipped"
    monkeypatch.setattr(pipeline_mod.Pipeline, "_light_works", original)
    picked = env.client.patch(f"/api/items/{vague['id']}", json={"selected_candidate_id": vague["candidates"][0]["id"]})
    assert picked.status_code == 200 and picked.json()["status"] == "approved"


def test_bulk_filter_form_answers_empty_above_the_cap_and_pushes_once(env, monkeypatch):
    from schema_app.api import jobs as jobs_api

    for n in range(1, 5):
        env.touch(f"Show.S01E0{n}.mkv")
    job = env.create_job(env.incoming)
    monkeypatch.setattr(jobs_api, "BULK_RESPONSE_MAX", 2)
    pushes = record_pushes(env)
    response = env.client.post("/api/items/bulk", json={"action": "approve", "job_id": job["id"]})
    assert response.status_code == 200 and response.json() == []
    assert env.client.get(f"/api/jobs/{job['id']}", params={"limit": 0}).json()["counts"] == {"approved": 4}
    changed = [d for t, d in pushes if t == "job.items_changed"]
    assert changed == [{"job_id": job["id"], "item_ids": sorted(i["id"] for i in job["items"])}]


def test_items_changed_leaves_the_ids_out_above_the_cap(env, monkeypatch):
    pushes = record_pushes(env)
    monkeypatch.setattr(pipeline_mod, "CHANGED_IDS_MAX", 2)
    env.ctx.pipeline._publish_changed(7, [3, 1])
    env.ctx.pipeline._publish_changed(7, [1, 2, 3])
    env.ctx.pipeline._publish_changed(7, [])
    assert [d for t, d in pushes if t == "job.items_changed"] == [{"job_id": 7, "item_ids": [1, 3]}, {"job_id": 7}]


def test_search_matches_path_destination_and_title_case_insensitively(env):
    env.touch("Folder.With.100%/Film.2010.mkv")
    env.touch("Other.2011.mkv")
    job = env.create_job(env.incoming)

    def found(q: str) -> list[str]:
        return [i["filename"] for i in env.client.get(f"/api/jobs/{job['id']}/items", params={"q": q}).json()["items"]]

    assert found("FOLDER.with") == ["Film.2010.mkv"]  # source path
    assert found("film (2010)") == ["Film.2010.mkv"]  # proposed path
    assert found("other") == ["Other.2011.mkv"]  # title
    assert found("100%") == ["Film.2010.mkv"] and found("film_2010") == []  # LIKE wildcards are literal


def test_open_item_paths_looks_up_only_the_batch(env):
    env.touch("Vague.2011.mkv")
    env.touch("Film.2010.mkv")
    job = env.create_job(env.incoming)  # both still open (review / matched)
    vague = by_name(job)["Vague.2011.mkv"]["source_path"]
    with env.ctx.db.session() as session:
        everything = open_item_paths(session)
        batch = open_item_paths(session, [vague, str(env.incoming / "nope.mkv")])
    assert batch == {os.path.normcase(os.path.normpath(vague))}
    assert batch < everything


# --------------------------------------------------------------------------- lanes, priorities, file threads


def test_watch_jobs_are_not_held_up_by_a_running_import(env, monkeypatch):
    gate = threading.Event()
    match_many = env.fakes.matcher.match_many

    async def slow_for_imports(items, hint=None):  # noqa: ANN001
        if any("Slow" in p.filename for p in items):
            while not gate.is_set():
                await asyncio.sleep(0.01)
        return await match_many(items, hint)

    monkeypatch.setattr(env.fakes.matcher, "match_many", slow_for_imports)
    big = env.touch("import/Slow.2010.mkv")
    fresh = env.touch("downloads/Fresh.2011.mkv")
    manual = env.client.post("/api/jobs", json={"paths": [str(big.parent)]}).json()
    watch = env.client.portal.call(functools.partial(
        env.ctx.pipeline.create_job, JobCreate(paths=[str(fresh)]), source=JobSource.WATCH, validate=False))  # fmt: skip
    try:
        assert env.wait(watch.id)["status"] == "ready"  # matched while the import is still busy
        assert env.client.get(f"/api/jobs/{manual['id']}", params={"limit": 0}).json()["status"] == "matching"
    finally:
        gate.set()
    assert env.wait(manual["id"])["status"] == "ready"


def test_automation_runs_go_first_and_slip_in_between_files():
    pipe = pipeline_mod.Pipeline(config=None, db=None, store=None, bus=None, matcher=None)  # type: ignore[arg-type]
    ran: list[int] = []

    async def scenario() -> None:
        pipe._exec_queue, pipe._exec_auto, pipe._exec_wake = asyncio.Queue(), asyncio.Queue(), asyncio.Event()
        pipe._exec_queue.put_nowait((1, [], None))
        pipe._exec_auto.put_nowait((2, [], None))
        assert await pipe._next_exec() == (2, [], None)
        assert await pipe._next_exec() == (1, [], None)

        async def run(job_id: int, ids: list[int], mode: object) -> None:
            ran.append(job_id)

        pipe._run_execute = run  # type: ignore[method-assign]
        pipe._exec_auto.put_nowait((3, [], None))
        pipe._exec_auto.put_nowait((4, [], None))
        await pipe._run_waiting_automation()  # what a manual run does between two files

    asyncio.run(scenario())
    assert ran == [3, 4]


def test_a_download_executes_between_the_files_of_a_manual_run(env, monkeypatch):
    for n in (1, 2, 3):
        env.touch(f"import/Show.S01E0{n}.mkv")
    fresh = env.touch("downloads/Fresh.2011.mkv")
    manual = env.create_job(env.incoming / "import")
    watch = env.client.portal.call(functools.partial(
        env.ctx.pipeline.create_job, JobCreate(paths=[str(fresh)]), source=JobSource.WATCH, validate=False))  # fmt: skip
    assert env.wait(watch.id)["status"] == "ready"
    execute = env.fakes.fileops.execute
    loop = env.client.portal.call(asyncio.get_running_loop)

    def first_file_queues_the_download(plan, **kwargs):  # noqa: ANN001, ANN003
        if not env.fakes.fileops.executed:  # the manual run's first file: the download arrives now
            asyncio.run_coroutine_threadsafe(
                env.ctx.pipeline.execute_job(watch.id, pipeline_mod.ExecuteRequest()), loop).result(5)  # fmt: skip
        return execute(plan, **kwargs)

    monkeypatch.setattr(pipeline_mod.fileops, "execute", first_file_queues_the_download)
    env.client.post(f"/api/jobs/{manual['id']}/execute", json={"mode": "copy"})
    env.wait(manual["id"])
    env.wait(watch.id)
    order = [Path(plan.source).name for plan in env.fakes.fileops.executed]
    assert order == ["Show.S01E01.mkv", "Fresh.2011.mkv", "Show.S01E02.mkv", "Show.S01E03.mkv"]


def test_crc_is_verified_while_the_lookups_run(env, monkeypatch):
    content = b"anime-bytes"
    crc = f"{zlib.crc32(content) & 0xFFFFFFFF:08X}"
    env.touch(f"[Grp] Anime Show S01E03 [{crc}].mkv", content)
    looking_up = threading.Event()
    match_many = env.fakes.matcher.match_many

    async def lookup(items, hint=None):  # noqa: ANN001
        looking_up.set()
        return await match_many(items, hint)

    def crc_after_lookup_started(path, **kwargs):  # noqa: ANN001, ANN003
        assert looking_up.wait(5), "the CRC read blocked the metadata lookup"
        return f"{zlib.crc32(Path(path).read_bytes()) & 0xFFFFFFFF:08X}"

    monkeypatch.setattr(env.fakes.matcher, "match_many", lookup)
    monkeypatch.setattr(hashing, "crc32_file", crc_after_lookup_started)
    [item] = env.create_job(env.incoming)["items"]
    assert item["crc_ok"] is True and "crc_mismatch" not in item["warnings"]


def test_probes_run_on_a_few_file_threads(env, monkeypatch):
    from schema_app.domain import ProbeInfo

    lock = threading.Lock()
    state = {"now": 0, "most": 0, "threads": set()}

    def probe(path, **kwargs):  # noqa: ANN001, ANN003
        with lock:
            state["now"] += 1
            state["most"] = max(state["most"], state["now"])
            state["threads"].add(threading.current_thread().name)
        time.sleep(0.03)
        with lock:
            state["now"] -= 1
        return ProbeInfo(width=1920, height=1080, duration_seconds=1500.0)

    monkeypatch.setattr(probe_mod, "available", lambda: "ffprobe")
    monkeypatch.setattr(probe_mod, "probe", probe)
    for n in range(1, 9):
        env.touch(f"Show.S01E0{n}.mkv")
    job = env.create_job(env.incoming)
    assert 1 < state["most"] <= pipeline_mod.PROBE_CONCURRENCY
    assert all(name.startswith("schema-files") for name in state["threads"])
    item = env.client.get(f"/api/items/{job['items'][0]['id']}").json()
    assert item["probe"]["width"] == 1920  # the full item reads it from job_item_candidates
    rows = env.client.get(f"/api/jobs/{job['id']}/items").json()["items"]
    assert rows and all(r["slim"] and r["probe"] is None for r in rows)  # list rows never read the side table


# --------------------------------------------------------------------------- Home cache


def test_home_board_is_reused_until_something_changes(env, monkeypatch):
    from schema_app.api import home as home_api

    builds: list[int] = []
    build = home_api.build_home

    def counting(*args, **kwargs):  # noqa: ANN002, ANN003
        builds.append(1)
        return build(*args, **kwargs)

    monkeypatch.setattr(home_api, "build_home", counting)
    home_api.forget_home(env.ctx)
    env.client.get("/api/home")
    env.client.get("/api/home")
    assert len(builds) == 1  # several tabs polling: one build
    env.ctx.bus.log("error", "system", "something broke")  # a push: the board changed
    board = env.client.get("/api/home").json()
    assert len(builds) == 2 and any(c["kind"] == "event" for c in board["needs_you"])
    key = next(c["key"] for c in board["needs_you"] if c["kind"] == "event")
    assert env.client.post("/api/home/actions", json={"key": key, "action": "dismiss"}).status_code == 200
    assert not [c for c in env.client.get("/api/home").json()["needs_you"] if c["kind"] == "event"]
    monkeypatch.setattr(home_api, "HOME_TTL", 0.0)
    env.client.get("/api/home")
    assert len(builds) == 4


# --------------------------------------------------------------------------- SQLite


def test_connections_get_a_bigger_cache_and_mmap(tmp_path):
    db = Database(tmp_path / "schema.db")
    db.init()
    with db.engine.connect() as conn:
        assert conn.execute(text("PRAGMA cache_size")).scalar() == -8192
        assert conn.execute(text("PRAGMA mmap_size")).scalar() == 256 * 1024 * 1024
    db.optimize()
    db.dispose()


_V5_JOB_ITEMS = """
CREATE TABLE job_items (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    status VARCHAR(16) NOT NULL, source_path TEXT NOT NULL, parsed JSON, candidates JSON NOT NULL,
    selected_candidate_id VARCHAR(255), confidence FLOAT NOT NULL, proposed_relative TEXT, proposed_path TEXT,
    path_overridden BOOLEAN NOT NULL, final_path TEXT, sidecars JSON NOT NULL, warnings JSON NOT NULL, error TEXT,
    crc_ok BOOLEAN, probe JSON, updated_at DATETIME NOT NULL, folder_rename JSON
)"""


def _candidate(n: int, kind: str = "movie") -> dict:
    return {"id": f"tmdb:{kind}:{n}", "kind": kind, "provider": "tmdb", "title": f"Title {n}", "year": 2000 + n,
            "score": 0.9 - n / 100, "overview": "long text " * 50, "reasons": ["x"],
            "movie": {"ref": {"provider": "tmdb", "id": str(n)}, "title": f"Title {n}", "year": 2000 + n,
                      "overview": "o", "aliases": ["a"]}}  # fmt: skip


def test_v5_database_upgrade_moves_candidates_out_of_job_items(tmp_path):
    path = tmp_path / "schema.db"
    db = Database(path)
    db.init()
    with db.engine.begin() as conn:  # the v5 job_items table, with its blobs
        conn.execute(text("DROP TABLE job_item_candidates"))
        conn.execute(text("DROP TABLE job_items"))
        conn.execute(text("DROP INDEX ix_operations_history"))
        conn.execute(text(_V5_JOB_ITEMS))
        conn.execute(text("CREATE INDEX ix_job_items_status ON job_items (status)"))
        conn.execute(text("CREATE INDEX ix_job_items_source_path ON job_items (source_path)"))
        conn.execute(text("INSERT INTO jobs (id, label, source, status, mode, auto_execute, input_paths, progress, "
                          "created_at, updated_at) VALUES (1, 'j', 'manual', 'review', 'move', 0, '[]', 1, "
                          "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"))  # fmt: skip
        rows = [
            # review: 5 suggestions, the pick ranks 4th -> top 2 + the pick
            (1, "review", "/in/Pick.Me.mkv", [_candidate(n) for n in range(5)], "tmdb:movie:3", None, '["low_confidence"]'),
            # done: only the pick stays
            (2, "done", "/in/Done.mkv", [_candidate(n) for n in range(4)], "tmdb:movie:1", '{"width": 1920}', "[]"),
            (3, "pending", "/in/New.mkv", [], None, None, "[]"),
            (4, "review", "/in/Undone.mkv", [_candidate(7)], "tmdb:movie:7", None, '["undone"]'),
        ]  # fmt: skip
        for item_id, status, source, candidates, selected, probe, warnings in rows:
            conn.execute(text(
                "INSERT INTO job_items (id, job_id, status, source_path, parsed, candidates, selected_candidate_id, "
                "confidence, proposed_path, path_overridden, sidecars, warnings, probe, updated_at) VALUES "
                "(:id, 1, :status, :source, :parsed, :candidates, :selected, 0.5, :proposed, 0, '[]', :warnings, "
                ":probe, '2026-01-01 00:00:00')"
            ), {"id": item_id, "status": status, "source": source, "candidates": json.dumps(candidates),
                "parsed": json.dumps({"title": f"Parsed {item_id}", "kind": "movie"}), "selected": selected,
                "proposed": f"/lib/Title {item_id}.mkv", "warnings": warnings, "probe": probe})  # fmt: skip
        conn.execute(text("PRAGMA user_version = 5"))
    db.dispose()

    db = Database(path)
    db.init()
    # The v5 database was copied before the upgrade touched it, once.
    backup = db.backup_path()
    assert backup is not None and backup.name.endswith(".pre-v6.bak") and backup.exists()
    import sqlite3

    old = sqlite3.connect(str(backup))
    try:
        assert old.execute("PRAGMA user_version").fetchone()[0] == 5
        assert "candidates" in {row[1] for row in old.execute("PRAGMA table_xinfo(job_items)")}
        assert old.execute("SELECT count(*) FROM job_items").fetchone()[0] == len(rows)
    finally:
        old.close()
    if os.name != "nt":
        assert (backup.stat().st_mode & 0o777) == 0o600
    stamp = backup.stat().st_mtime_ns
    db.init()  # already current: no new backup
    assert backup.stat().st_mtime_ns == stamp
    with db.engine.connect() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_xinfo(job_items)"))}
        indexes = {row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'index'"))}
    assert "candidates" not in columns and "probe" not in columns and {"chosen", "search_text", "undone"} <= columns
    assert "ix_job_items_status" not in indexes and "ix_job_items_source_path" not in indexes
    assert {"ix_job_items_attention", "ix_job_items_search", "ix_operations_history"} <= indexes
    assert db.user_version() == SCHEMA_VERSION == 6
    with db.session() as session:
        side = {d.item_id: d for d in session.scalars(select(JobItemCandidates))}
        items = {i.id: i for i in session.scalars(select(JobItem))}
        undone = dict(session.execute(select(JobItem.id, JobItem.undone)).all())
    assert [c["id"] for c in side[1].candidates] == ["tmdb:movie:0", "tmdb:movie:1", "tmdb:movie:3"]
    assert [c["id"] for c in side[2].candidates] == ["tmdb:movie:1"] and side[2].probe == {"width": 1920}
    assert 3 not in side and items[3].candidate_count == 0 and items[3].matched is False
    pick = items[1]
    assert pick.candidate_count == 3 and pick.chosen["id"] == "tmdb:movie:3" and "overview" not in pick.chosen
    assert (pick.match_title, pick.match_year, pick.match_kind, pick.kind) == ("Title 3", 2003, "movie", "movie")
    assert pick.search_text == "/in/pick.me.mkv\x1f/lib/title 1.mkv\x1fparsed 1" and pick.matched is True
    assert undone == {1: False, 2: False, 3: False, 4: True}
    db.init()  # idempotent
    db.dispose()
