"""Config, database, settings store, event bus, auth primitives and the demo builder."""

from __future__ import annotations

import asyncio
import zlib
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, text

from schema_app.api_models import SECRET_MASK, AppSettings, ProvidersIn
from schema_app.auth import SessionSigner, hash_password, verify_password
from schema_app.config import Config, ensure_data_dir, load_secret_key
from schema_app.db import SCHEMA_VERSION, Database, utcnow
from schema_app.demo import builder
from schema_app.domain import MediaKind, MediaServerConfig, default_priority
from schema_app.models import Event, Job, JobItem, Operation
from schema_app.services.events import EventBus
from schema_app.services.settings_store import SettingsError, SettingsStore

# --------------------------------------------------------------------------- config


def test_config_reads_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHEMA_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("SCHEMA_PORT", "9000")
    monkeypatch.setenv("SCHEMA_AUTH", "disabled")
    monkeypatch.setenv("SCHEMA_DEMO", "1")
    monkeypatch.setenv("SCHEMA_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("TMDB_API_KEY", "env-tmdb")
    monkeypatch.setenv("ANIDB_CLIENT_VERSION", "2")
    monkeypatch.setenv("TVDB_PIN", "")
    config = Config()
    assert config.data_dir == tmp_path / "d"
    assert config.port == 9000 and config.auth_enabled is False and config.demo is True
    assert config.log_level == "debug"
    creds = config.env_credentials()
    assert creds.tmdb_api_key == "env-tmdb" and creds.anidb_client_version == 2 and creds.tvdb_pin is None


@pytest.mark.parametrize(("raw", "expected"), [("required", True), ("disabled", False), ("off", False), ("1", True)])
def test_config_auth_aliases(raw, expected):
    assert Config(auth=raw).auth_enabled is expected


def test_config_defaults():
    config = Config(_env_file=None)
    assert config.host == "0.0.0.0"
    assert config.static_dir.name == "dist" and config.static_dir.parent.name == "frontend"


def test_data_dir_and_secret_key(tmp_path):
    config = Config(data_dir=tmp_path / "nested" / "data")
    ensure_data_dir(config)
    assert config.data_dir.is_absolute() and (config.data_dir / "trash").is_dir()
    key = load_secret_key(config)
    assert len(key) > 40
    assert (config.data_dir / "secret.key").read_text().strip() == key
    assert load_secret_key(config) == key  # stable across restarts
    assert load_secret_key(Config(data_dir=config.data_dir, secret_key="explicit")) == "explicit"


# --------------------------------------------------------------------------- database


def test_database_pragmas_and_user_version(tmp_path):
    db = Database(tmp_path / "schema.db")
    db.init()
    with db.engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar().lower() == "wal"
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
    assert db.user_version() == SCHEMA_VERSION
    db.init()  # idempotent
    db.dispose()


def test_database_v1_upgrade_adds_indexes(tmp_path):
    db = Database(tmp_path / "schema.db")
    db.init()
    with db.engine.begin() as conn:
        conn.execute(text("DROP INDEX ix_operations_item"))
        conn.execute(text("DROP INDEX ix_job_items_status_updated"))
        conn.execute(text("ALTER TABLE users DROP COLUMN session_epoch"))  # v3 column
        conn.execute(text("PRAGMA user_version = 1"))
    db.init()
    with db.engine.connect() as conn:
        names = {row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'index'"))}
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(users)"))}
    assert {"ix_operations_item", "ix_job_items_status_updated", "ix_operations_status_mode", "ix_events_job",
            "ix_operations_history"} <= names
    assert "session_epoch" in columns
    assert db.user_version() == SCHEMA_VERSION == 6
    db.dispose()


def test_database_v3_upgrade_adds_item_folder_rename(tmp_path):
    db = Database(tmp_path / "schema.db")
    db.init()
    with db.engine.begin() as conn:
        conn.execute(text("ALTER TABLE job_items DROP COLUMN folder_rename"))  # v4 column
        conn.execute(text("PRAGMA user_version = 3"))
    db.init()
    with db.engine.connect() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(job_items)"))}
    assert "folder_rename" in columns
    assert db.user_version() == SCHEMA_VERSION
    db.init()  # idempotent
    db.dispose()


def test_job_delete_cascades_items_and_keeps_history(tmp_path):
    db = Database(tmp_path / "schema.db")
    db.init()
    with db.session() as session:
        job = Job(label="j", source="manual", status="review", mode="move", input_paths=[])
        session.add(job)
        session.flush()
        item = JobItem(job_id=job.id, source_path="/x.mkv", status="done")
        session.add(item)
        session.flush()
        session.add(Operation(job_id=job.id, item_id=item.id, mode="move", source="/x.mkv", destination="/y.mkv",
                              status="done"))  # fmt: skip
    with db.session() as session:
        session.delete(session.scalars(select(Job)).one())
    with db.session() as session:
        assert session.scalar(select(func.count(JobItem.id))) == 0
        op = session.scalars(select(Operation)).one()
        assert op.job_id is None and op.item_id is None
        assert op.created_at.tzinfo is not None
    db.dispose()


def test_session_rolls_back_on_error(tmp_path):
    db = Database(tmp_path / "schema.db")
    db.init()
    with pytest.raises(RuntimeError), db.session() as session:
        session.add(Job(label="j", source="manual", status="queued", mode="move", input_paths=[]))
        session.flush()
        raise RuntimeError("boom")
    with db.session() as session:
        assert session.scalar(select(func.count(Job.id))) == 0
    db.dispose()


# --------------------------------------------------------------------------- settings store


@pytest.fixture
def store(tmp_path):
    db = Database(tmp_path / "schema.db")
    db.init()
    config = Config(data_dir=tmp_path, tmdb_api_key="from-env")
    store = SettingsStore(db, config)
    store.seed()
    yield store
    db.dispose()


def test_store_seeds_defaults(store):
    app = store.get_app()
    assert app == AppSettings(webhook_api_key=SECRET_MASK)
    providers = store.get_providers()
    assert providers.priority == default_priority()
    assert providers.credentials.tmdb_api_key == "from-env"  # environment overlay
    assert store.env_overridden() == {"tmdb_api_key"}
    key = store.get_api_key()
    store.seed()  # re-seeding keeps existing values
    assert store.get_api_key() == key


def test_store_masks_and_keeps_secrets(store):
    server = MediaServerConfig(id="a", kind="plex", name="Plex", url="http://p", token="secret")
    store.put_app(AppSettings(media_servers=[server], allowed_roots=["/media"]))
    masked = store.get_app_masked()
    assert masked.media_servers[0].token == SECRET_MASK and masked.webhook_api_key == SECRET_MASK
    store.put_app(masked)
    assert store.get_app().media_servers[0].token == "secret"
    with pytest.raises(SettingsError):
        store.put_app(AppSettings(media_servers=[server.model_copy(update={"kind": "kodi"})]))
    with pytest.raises(SettingsError):
        store.put_app(AppSettings(library_roots={MediaKind.MOVIE: "movies"}))


def test_store_normalizes_extensions_and_generates_server_ids(store):
    saved = store.put_app(AppSettings(video_extensions=[".MKV", "mp4", " "],
                                      media_servers=[MediaServerConfig(id="", kind="emby", name="E", url="u", token="t")]))  # fmt: skip
    assert saved.video_extensions == ["mkv", "mp4"]
    assert len(store.get_app().media_servers[0].id) == 32


def test_store_provider_updates(store):
    store.put_providers(ProvidersIn(credentials={"tvdb_api_key": "tv", "anidb_client_version": "4", "tmdb_api_key": "db"}))
    creds = store.get_providers().credentials
    assert creds.tvdb_api_key == "tv" and creds.anidb_client_version == 4
    assert creds.tmdb_api_key == "from-env"  # env still wins over the stored value
    masked = store.masked_credentials()
    assert masked["tvdb_api_key"] == SECRET_MASK and masked["anidb_client_version"] == "4"
    assert masked["trakt_client_id"] is None
    store.put_providers(ProvidersIn(credentials={"tvdb_api_key": SECRET_MASK}))
    assert store.get_providers().credentials.tvdb_api_key == "tv"
    store.put_providers(ProvidersIn(credentials={"tvdb_api_key": None}))
    assert store.get_providers().credentials.tvdb_api_key is None
    store.put_providers(ProvidersIn(priority={MediaKind.SERIES: ["TVmaze", "tvmaze", "tmdb"]}, disabled=["Trakt"]))
    providers = store.get_providers()
    assert providers.priority[MediaKind.SERIES] == ["tvmaze", "tmdb"]
    assert providers.priority[MediaKind.MOVIE] == default_priority()[MediaKind.MOVIE]
    assert providers.disabled == ["trakt"]
    with pytest.raises(SettingsError):
        store.put_providers(ProvidersIn(language=" "))


def test_store_rotates_api_key(store):
    first = store.get_api_key()
    assert store.rotate_api_key() != first


# --------------------------------------------------------------------------- event bus


def test_event_bus_persists_and_fans_out(tmp_path):
    db = Database(tmp_path / "schema.db")
    db.init()
    bus = EventBus(db)

    async def scenario() -> list[dict]:
        bus.bind()
        queue = bus.subscribe()
        bus.publish("job.updated", {"id": 1})
        await asyncio.to_thread(bus.log, "warning", "watcher", "from a thread", job_id=3, api_key="hide-me")
        await asyncio.sleep(0.01)
        messages = [queue.get_nowait(), queue.get_nowait()]
        bus.unsubscribe(queue)
        return messages

    first, second = asyncio.run(scenario())
    assert first == {"type": "job.updated", "data": {"id": 1}}
    assert second["type"] == "event" and second["data"]["message"] == "from a thread"
    assert second["data"]["data"] == {}
    with db.session() as session:
        assert session.scalars(select(Event)).one().job_id == 3
    db.dispose()


def test_event_bus_prunes_old_and_excess_rows(tmp_path, monkeypatch):
    from schema_app.services import events as events_mod

    db = Database(tmp_path / "schema.db")
    db.init()
    with db.session() as session:
        session.add(Event(ts=utcnow() - timedelta(days=40), level="info", category="system", message="old"))
        for i in range(5):
            session.add(Event(level="info", category="system", message=f"m{i}"))
    monkeypatch.setattr(events_mod, "MAX_EVENTS", 3)
    deleted = EventBus(db).prune()
    assert deleted == 3
    with db.session() as session:
        assert [e.message for e in session.scalars(select(Event).order_by(Event.id))] == ["m2", "m3", "m4"]
    db.dispose()


def test_slow_subscriber_gets_resync_instead_of_silent_drops():
    from schema_app.services import events as events_mod

    async def scenario() -> list[dict]:
        bus = EventBus()
        bus.bind()
        queue = bus.subscribe()
        for i in range(events_mod.QUEUE_SIZE + 5):
            bus.publish("job.updated", {"id": i})
        return [queue.get_nowait() for _ in range(queue.qsize())]

    messages = asyncio.run(scenario())
    size = events_mod.QUEUE_SIZE
    assert messages[0] == {"type": "resync", "data": {}}
    assert [m["data"]["id"] for m in messages[1:]] == [size + 1, size + 2, size + 3, size + 4]


# --------------------------------------------------------------------------- auth primitives


@pytest.mark.real_scrypt
def test_password_hashing():
    encoded = hash_password("hunter2-hunter2")
    assert encoded.startswith("scrypt$131072$8$1$")
    assert verify_password("hunter2-hunter2", encoded)
    assert not verify_password("wrong", encoded)
    assert not verify_password("x", "garbage")
    assert hash_password("same") != hash_password("same")  # salted


def test_session_tokens_expire_and_bind_to_password(tmp_path):
    from schema_app.models import User

    signer = SessionSigner("k" * 40)
    user = User(id=1, username="a", password_hash=hash_password("pw-123456"))
    token = signer.issue(user)
    assert signer.read(token)["uid"] == 1
    assert signer.read(token, max_age=-1) is None
    assert SessionSigner("other-key" * 5).read(token) is None
    assert signer.read(None) is None


# --------------------------------------------------------------------------- demo builder


def test_demo_manifest_and_build(tmp_path):
    entries = builder.read_manifest()
    assert len(entries) >= 25
    assert all(e.size_kib >= 1 for e in entries)
    layout, created = builder.build(tmp_path / "demo")
    assert len(created) == len(entries)
    assert {p.name for p in layout.library_roots.values()} == {"Movies", "TV", "Anime"}
    crc_files = [p for p in created if p.name.startswith("[SubsPlease]")]
    assert len(crc_files) == 1
    content = crc_files[0].read_bytes()
    assert f"[{zlib.crc32(content) & 0xFFFFFFFF:08X}]" in crc_files[0].name
    sized = next(p for p in created if p.name.startswith("Neon.Requiem.2021.2160p"))
    assert sized.stat().st_size == 1024 * 1024
    assert (layout.incoming / "Starfall Station" / "Season 3" / "03 - Ghosts in the Array.mkv").exists()
    # deterministic
    _, again = builder.build(tmp_path / "demo2")
    assert [p.read_bytes() for p in created[:3]] == [p.read_bytes() for p in again[:3]]
    assert builder.is_built(tmp_path / "demo")


def test_demo_wipe_refuses_foreign_folders(tmp_path):
    foreign = tmp_path / "precious"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("x")
    with pytest.raises(RuntimeError):
        builder.build(foreign, wipe=True)
    assert (foreign / "keep.txt").exists()
    layout, _ = builder.build(tmp_path / "demo")
    (layout.library_roots[MediaKind.MOVIE] / "moved.mkv").write_bytes(b"x")
    builder.build(tmp_path / "demo", wipe=True)
    assert not (layout.library_roots[MediaKind.MOVIE] / "moved.mkv").exists()


def test_demo_mode_bootstrap_and_reset(make_env):
    env = make_env(demo=True, configure=False)
    settings = env.client.get("/api/settings").json()
    demo_dir = Path(env.ctx.config.demo_dir)
    assert settings["library_roots"]["movie"] == str(demo_dir / "library" / "Movies")
    assert settings["allowed_roots"] == [str(demo_dir / "incoming"), str(demo_dir / "library")]
    jobs = env.client.get("/api/jobs").json()
    assert [j["label"] for j in jobs] == ["Demo import"]
    job = env.wait(jobs[0]["id"])
    assert job["auto_execute"] is False and job["total"] >= 25
    assert env.client.get("/api/auth/status").json()["demo"] is True
    response = env.client.post("/api/demo/reset")
    assert response.status_code == 200 and response.json() == {"ok": True}
    jobs = env.client.get("/api/jobs").json()
    assert len(jobs) == 1 and jobs[0]["id"] != job["id"] and jobs[0]["label"] == "Demo import"
