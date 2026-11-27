"""Plex / Jellyfin / Emby refresh against respx-mocked HTTP (no network)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx

from schema_app.domain import MediaServerConfig, PathMapping
from schema_app.services import mediaservers
from schema_app.services.mediaservers import MediaServerError, RefreshDebouncer, refresh_paths, test_server

PLEX = "http://plex.test:32400"
JF = "http://jellyfin.test:8096"


def plex_cfg(**kw: object) -> MediaServerConfig:
    base = {"id": "p1", "kind": "plex", "name": "Plex", "url": PLEX + "/", "token": "plex-secret-token"}
    base.update(kw)
    return MediaServerConfig(**base)  # type: ignore[arg-type]


def jf_cfg(kind: str = "jellyfin", **kw: object) -> MediaServerConfig:
    base = {"id": "j1", "kind": kind, "name": kind.title(), "url": JF, "token": "jf-secret-key"}
    base.update(kw)
    return MediaServerConfig(**base)  # type: ignore[arg-type]


SECTIONS = {
    "MediaContainer": {
        "size": 3,
        "Directory": [
            {"key": "1", "title": "Movies", "type": "movie", "Location": [{"id": 1, "path": "/data/media/Movies"}]},
            {"key": "2", "title": "TV", "type": "show", "Location": [{"id": 2, "path": "/data/media/TV"},
                                                                       {"id": 3, "path": "/data2/TV"}]},
            {"key": "3", "title": "Anime", "type": "show", "Location": [{"id": 4, "path": "/data/media/TV/Anime"}]},
        ],
    }
}  # fmt: skip


# --------------------------------------------------------------------------- test_server


@respx.mock
async def test_plex_test_server_ok() -> None:
    root = respx.get(f"{PLEX}/").respond(
        json={"MediaContainer": {"friendlyName": "Tower", "version": "1.41.2.9200"}}
    )
    respx.get(f"{PLEX}/library/sections").respond(json=SECTIONS)
    ok, message = await test_server(plex_cfg())
    assert ok and message == "Plex 1.41.2.9200 'Tower', 3 libraries"
    assert root.calls.last.request.headers["X-Plex-Token"] == "plex-secret-token"


@respx.mock
async def test_plex_bad_token_message_hides_token() -> None:
    respx.get(f"{PLEX}/").respond(401)
    ok, message = await test_server(plex_cfg())
    assert not ok and "rejected the token (401)" in message
    assert "plex-secret-token" not in message


@respx.mock
async def test_unreachable_server() -> None:
    respx.get(f"{PLEX}/").mock(side_effect=httpx.ConnectError("refused"))
    ok, message = await test_server(plex_cfg())
    assert not ok and message.startswith("Could not connect to Plex at http://plex.test:32400")


@respx.mock
async def test_timeout_and_non_json() -> None:
    respx.get(f"{PLEX}/").mock(side_effect=httpx.ReadTimeout("slow"))
    ok, message = await test_server(plex_cfg())
    assert not ok and "did not answer in time" in message
    respx.get(f"{PLEX}/").respond(200, text="<html>router login</html>")
    ok, message = await test_server(plex_cfg())
    assert not ok and "non-JSON" in message


@respx.mock
async def test_jellyfin_test_server_ok() -> None:
    info = respx.get(f"{JF}/System/Info").respond(
        json={"ServerName": "media", "Version": "10.10.3", "ProductName": "Jellyfin Server"}
    )
    respx.get(f"{JF}/Library/VirtualFolders").respond(json=[{"Name": "Movies"}, {"Name": "Shows"}])
    ok, message = await test_server(jf_cfg())
    assert ok and message == "Jellyfin 10.10.3 'media', 2 libraries"
    headers = info.calls.last.request.headers
    assert headers["X-Emby-Token"] == "jf-secret-key" and 'Token="jf-secret-key"' in headers["Authorization"]


@respx.mock
async def test_emby_detects_product_mismatch() -> None:
    respx.get(f"{JF}/System/Info").respond(
        json={"ServerName": "e", "Version": "4.8", "ProductName": "Jellyfin Server"}
    )
    respx.get(f"{JF}/Library/VirtualFolders").respond(json=[])
    ok, message = await test_server(jf_cfg("emby"))
    assert ok and message.startswith("Jellyfin (configured as Emby) 4.8") and "0 libraries" in message


@respx.mock
async def test_jellyfin_bad_key() -> None:
    respx.get(f"{JF}/System/Info").respond(403)
    ok, message = await test_server(jf_cfg())
    assert not ok and "rejected the API key (403)" in message and "jf-secret-key" not in message


async def test_input_validation() -> None:
    assert (await test_server(plex_cfg(kind="kodi")))[1].startswith("Unknown media server type 'kodi'")
    assert not (await test_server(plex_cfg(url=" ")))[0]
    assert not (await test_server(plex_cfg(token="")))[0]


# --------------------------------------------------------------------------- refresh


@respx.mock
async def test_plex_partial_refresh_by_section_with_mapping() -> None:
    respx.get(f"{PLEX}/library/sections").respond(json=SECTIONS)
    refresh_tv = respx.get(f"{PLEX}/library/sections/2/refresh").respond(200)
    refresh_anime = respx.get(f"{PLEX}/library/sections/3/refresh").respond(200)
    refresh_all = respx.get(f"{PLEX}/library/sections/all/refresh").respond(200)
    cfg = plex_cfg(path_mappings=[PathMapping(source="/storage/media", target="/data/media")])
    await refresh_paths(cfg, [
        "/storage/media/TV/Show (2016)/Season 01/Show - S01E01.mkv",
        "/storage/media/TV/Show (2016)/Season 01/Show - S01E02.mkv",
        "/storage/media/TV/Anime/Kaze (2018)/Season 01/Kaze - S01E01.mkv",
    ])  # fmt: skip
    assert refresh_tv.call_count == 1
    assert refresh_tv.calls.last.request.url.params["path"] == "/data/media/TV/Show (2016)/Season 01"
    assert refresh_anime.call_count == 1  # deepest matching location wins
    assert refresh_anime.calls.last.request.url.params["path"] == "/data/media/TV/Anime/Kaze (2018)/Season 01"
    assert not refresh_all.called


@respx.mock
async def test_plex_falls_back_to_full_refresh() -> None:
    respx.get(f"{PLEX}/library/sections").respond(json=SECTIONS)
    refresh_all = respx.get(f"{PLEX}/library/sections/all/refresh").respond(200)
    await refresh_paths(plex_cfg(), ["/somewhere/else/Movie (2019)/Movie (2019).mkv"])
    assert refresh_all.call_count == 1


@respx.mock
async def test_plex_windows_server_paths() -> None:
    sections = {"MediaContainer": {"Directory": [{"key": "7", "Location": [{"path": "D:\\Media\\Movies"}]}]}}
    respx.get(f"{PLEX}/library/sections").respond(json=sections)
    refresh = respx.get(f"{PLEX}/library/sections/7/refresh").respond(200)
    cfg = plex_cfg(path_mappings=[PathMapping(source="/storage/movies", target="D:\\Media\\Movies")])
    await refresh_paths(cfg, ["/storage/movies/Paper Moons (2004)/Paper Moons (2004).mkv"])
    assert refresh.calls.last.request.url.params["path"] == "D:\\Media\\Movies\\Paper Moons (2004)"


@respx.mock
async def test_folders_are_deduplicated_and_existing_folders_kept(tmp_path: Path) -> None:
    show = tmp_path / "TV" / "Show"
    (show / "Season 01").mkdir(parents=True)
    body: list[dict[str, object]] = []

    def capture(request: httpx.Request) -> httpx.Response:
        body.append(json.loads(request.content))
        return httpx.Response(204)

    respx.post(f"{JF}/Library/Media/Updated").mock(side_effect=capture)
    await refresh_paths(jf_cfg(), [
        str(show / "Season 01" / "a.mkv"),
        str(show / "Season 01" / "b.mkv"),
        str(show),  # an existing folder is used as is and covers the season folder
    ])  # fmt: skip
    assert body == [{"Updates": [{"Path": str(show), "UpdateType": "Created"}]}]


@respx.mock
async def test_renamed_folder_refreshes_their_parent(tmp_path: Path) -> None:
    movies = tmp_path / "movies"
    (movies / "Alien (1979)").mkdir(parents=True)  # "Alien.1979" was renamed to it
    body: list[dict[str, object]] = []

    def capture(request: httpx.Request) -> httpx.Response:
        body.append(json.loads(request.content))
        return httpx.Response(204)

    respx.post(f"{JF}/Library/Media/Updated").mock(side_effect=capture)
    await refresh_paths(jf_cfg(), [str(movies / "Alien (1979)"), str(movies / "Alien.1979")])
    # The parent covers both, so Jellyfin drops the stale "Alien.1979" entry and finds the new one.
    assert body == [{"Updates": [{"Path": str(movies), "UpdateType": "Created"}]}]


@respx.mock
async def test_jellyfin_media_updated_with_mapping() -> None:
    updated = respx.post(f"{JF}/Library/Media/Updated").respond(204)
    refresh = respx.post(f"{JF}/Library/Refresh").respond(204)
    cfg = jf_cfg(path_mappings=[PathMapping(source="/storage", target="/media")])
    await refresh_paths(cfg, ["/storage/Movies/Neon Requiem (2021)/Neon Requiem (2021).mkv"])
    sent = json.loads(updated.calls.last.request.content)
    assert sent == {"Updates": [{"Path": "/media/Movies/Neon Requiem (2021)", "UpdateType": "Created"}]}
    assert updated.calls.last.request.headers["X-Emby-Token"] == "jf-secret-key"
    assert not refresh.called


@respx.mock
async def test_emby_falls_back_to_full_refresh() -> None:
    respx.post(f"{JF}/Library/Media/Updated").respond(404)
    refresh = respx.post(f"{JF}/Library/Refresh").respond(204)
    await refresh_paths(jf_cfg("emby"), ["/media/x/y.mkv"])
    assert refresh.call_count == 1


@respx.mock
async def test_refresh_errors_are_readable() -> None:
    respx.post(f"{JF}/Library/Media/Updated").respond(401)
    with pytest.raises(MediaServerError, match="rejected the API key"):
        await refresh_paths(jf_cfg(), ["/media/x/y.mkv"])
    respx.get(f"{PLEX}/library/sections").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(MediaServerError, match="Could not connect"):
        await refresh_paths(plex_cfg(), ["/media/x/y.mkv"])


async def test_disabled_or_empty_is_a_noop() -> None:
    with respx.mock(assert_all_called=False) as router:
        await refresh_paths(plex_cfg(enabled=False), ["/a/b.mkv"])
        await refresh_paths(plex_cfg(), [])
        assert not router.calls
    with pytest.raises(MediaServerError):
        await refresh_paths(plex_cfg(kind="kodi"), ["/a/b.mkv"])


# --------------------------------------------------------------------------- debounce


async def test_debouncer_batches_per_server() -> None:
    calls: list[tuple[str, list[str]]] = []

    async def fake_refresh(cfg: MediaServerConfig, paths: list[str]) -> None:
        calls.append((cfg.id, sorted(paths)))

    deb = RefreshDebouncer(delay=0.15, refresh=fake_refresh)
    plex, jf = plex_cfg(), jf_cfg()
    deb.add(plex, ["/m/a/1.mkv"])
    await asyncio.sleep(0.05)
    deb.add(plex, ["/m/a/2.mkv", "/m/a/1.mkv"])
    deb.add(jf, ["/m/b/1.mkv"])
    assert deb.pending == {"p1": ["/m/a/1.mkv", "/m/a/2.mkv"], "j1": ["/m/b/1.mkv"]}
    await asyncio.sleep(0.08)
    assert calls == []  # still inside the quiet period
    await asyncio.sleep(0.2)
    assert sorted(calls) == [("j1", ["/m/b/1.mkv"]), ("p1", ["/m/a/1.mkv", "/m/a/2.mkv"])]
    await deb.aclose()


async def test_debouncer_max_delay_and_flush() -> None:
    calls: list[list[str]] = []

    async def fake_refresh(cfg: MediaServerConfig, paths: list[str]) -> None:
        calls.append(paths)

    deb = RefreshDebouncer(delay=10, max_delay=0.2, refresh=fake_refresh)
    deb.add(plex_cfg(), ["/a.mkv"])
    await asyncio.sleep(0.35)
    assert calls == [["/a.mkv"]]  # capped by max_delay even though delay is 10 s
    deb.add(plex_cfg(), ["/b.mkv"])
    await deb.flush()
    assert calls == [["/a.mkv"], ["/b.mkv"]]
    deb.add(plex_cfg(enabled=False), ["/c.mkv"])
    assert deb.pending == {}
    await deb.aclose()


async def test_debouncer_reports_errors() -> None:
    seen: list[str] = []

    async def failing(cfg: MediaServerConfig, paths: list[str]) -> None:
        raise MediaServerError("Plex rejected the token (401).")

    async def on_error(cfg: MediaServerConfig, exc: Exception) -> None:
        seen.append(f"{cfg.name}: {exc}")

    deb = RefreshDebouncer(delay=0.01, refresh=failing, on_error=on_error)
    deb.add(plex_cfg(), ["/a.mkv"])
    await deb.flush()
    await asyncio.sleep(0.05)
    assert seen == ["Plex: Plex rejected the token (401)."]
    await deb.aclose()


async def test_debouncer_default_uses_refresh_paths() -> None:
    assert RefreshDebouncer()._refresh is mediaservers.refresh_paths
