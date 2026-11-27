"""End-to-end tests with the REAL sibling modules (parser, matcher + fixtures provider, fileops,
paths, hashing). Marked ``integration``: run once every module is in."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from conftest import ADMIN, CLIENT_HEADERS, make_config
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

# demo file -> expected relative destination with the seeded default formats (SPEC §6.5, §8.6)
EXPECTED = {
    "Starfall.Station.S01E01.1080p.WEB-DL.DDP5.1.H.264-NTb.mkv":
        "TV/Starfall Station (2016)/Season 01/Starfall Station - S01E01",
    "Starfall.Station.S01E02E03.720p.HDTV.x264-KILLERS.mkv":
        "TV/Starfall Station (2016)/Season 01/Starfall Station - S01E02-E03",
    "starfall_station_1x04_hdtv.avi": "TV/Starfall Station (2016)/Season 01/Starfall Station - S01E04",
    "Starfall Station - S00E02 - Christmas Special.mkv": "TV/Starfall Station (2016)/Specials/Starfall Station - S00E02",
    "Starfall Station - The Making of Starfall.mkv": "TV/Starfall Station (2016)/Specials/Starfall Station - S00E01",
    "03 - Ghosts in the Array.mkv": "TV/Starfall Station (2016)/Season 03/Starfall Station - S03E03",
    "Harbor.Lights.2021.S01E03.2160p.AMZN.WEB-DL.DV.HDR.H.265-FLUX.mkv": "TV/Harbor Lights (2021)/Season 01/",
    "Harbor Lights S02E05.mkv": "TV/Harbor Lights (2008)/Season 02/",
    "The.Quiet.Meridian.2019.Directors.Cut.1080p.BluRay.x264-SPARKS.mkv":
        "Movies/The Quiet Meridian (2019)/The Quiet Meridian (2019) - Director's Cut.mkv",
    "Iron.Orchard.1987.REMASTERED.720p.BluRay.x264-AMIABLE.mkv": "Movies/Iron Orchard (1987)/",
    "Iron Orchard 2023 2160p WEB-DL DDP5.1 Atmos HDR10 HEVC-CMRG.mkv": "Movies/Iron Orchard (2023)/",
    "Paper.Moons.2004.CD1.DVDRip.XviD-FiCO.avi": "Movies/Paper Moons (2004)/Paper Moons (2004) - pt1.avi",
    "Paper.Moons.2004.CD2.DVDRip.XviD-FiCO.avi": "Movies/Paper Moons (2004)/Paper Moons (2004) - pt2.avi",
    "Neon.Requiem.2021.1080p.BluRay.x264-GROUP.mkv": "Movies/Neon Requiem (2021)/Neon Requiem (2021) - 1080p.mkv",
    "Neon.Requiem.2021.2160p.UHD.BluRay.REMUX.HDR.HEVC.TrueHD.7.1.Atmos-FGT.mkv":
        "Movies/Neon Requiem (2021)/Neon Requiem (2021) - 2160p Remux HDR.mkv",
    "[Erai-raws] Wings of the Wind - 26 [1080p][Multiple Subtitle].mkv":
        "Anime/Kaze no Tsubasa/Season 03/Kaze no Tsubasa - S03E01",
    "Kaze no Tsubasa - OVA [BD 1080p].mkv": "Anime/Kaze no Tsubasa/Specials/Kaze no Tsubasa - S00E01",
}
REVIEW = {"completely.unknown.title.2011.1080p.mkv", "[Hatsuyuki] Kaze no Tsubasa - 07v2 [720p][DEADBEEF].mkv"}
SKIPPED = {"Starfall.Station.S03E01.Sample.mkv", "Neon.Requiem.2021.Behind.The.Scenes.1080p.mkv"}


def wait(client: TestClient, job_id: int, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in {"queued", "matching", "executing"} or time.monotonic() > deadline:
            return job
        time.sleep(0.05)


def test_demo_import_execute_copy_and_undo(tmp_path):
    from schema_app.main import create_app

    config = make_config(tmp_path, demo=True)
    with TestClient(create_app(config), headers=CLIENT_HEADERS) as client:
        assert client.post("/api/auth/setup", json=ADMIN).status_code == 200
        job_id = client.get("/api/jobs").json()[0]["id"]
        job = wait(client, job_id)
        assert job["status"] == "review"
        items = {i["filename"]: i for i in job["items"]}
        library = Path(config.data_dir) / "demo" / "library"
        for name, expected in EXPECTED.items():
            item = items[name]
            assert item["status"] == "matched", (name, item["warnings"])
            relative = Path(item["proposed_path"]).relative_to(library).as_posix()
            assert relative.startswith(expected), (name, relative)
        for name in REVIEW:
            assert items[name]["status"] == "review", name
        assert "crc_mismatch" in items["[Hatsuyuki] Kaze no Tsubasa - 07v2 [720p][DEADBEEF].mkv"]["warnings"]
        for name in SKIPPED:
            assert items[name]["status"] == "skipped", name
        crc_item = next(i for n, i in items.items() if n.startswith("[SubsPlease]"))
        assert crc_item["crc_ok"] is True and crc_item["status"] == "matched"
        sidecar_item = items["Starfall.Station.S01E01.1080p.WEB-DL.DDP5.1.H.264-NTb.mkv"]
        assert [Path(s).name for s in sidecar_item["sidecars"]] == [
            "Starfall.Station.S01E01.1080p.WEB-DL.DDP5.1.H.264-NTb.en.srt"
        ]

        matched = [i for i in job["items"] if i["status"] == "matched"]
        assert len(matched) == len(job["items"]) - len(REVIEW) - len(SKIPPED)
        assert client.post(f"/api/jobs/{job_id}/execute", json={"mode": "copy"}).status_code == 200
        job = wait(client, job_id)
        done = [i for i in job["items"] if i["status"] == "done"]
        assert len(done) == len(matched)
        for item in done:
            assert Path(item["final_path"]).exists()
            assert Path(item["source_path"]).exists()  # copy mode keeps the source
        final = Path(next(i for i in done if i["id"] == sidecar_item["id"])["final_path"])
        assert final.with_name(final.stem + ".en.srt").exists()

        ops = client.get("/api/operations", params={"job_id": job_id, "limit": 500}).json()
        assert len(ops) == len(done) and {op["status"] for op in ops} == {"done"}
        undone = client.post(f"/api/operations/{ops[0]['id']}/undo").json()
        assert undone["status"] == "undone"
        assert not Path(ops[0]["destination"]).exists()
        assert client.get(f"/api/jobs/{job_id}").json()["status"] == "review"


def test_real_paths_sanitize_titles(tmp_path):
    """The format engine with DevOps' real sanitizer: '/' and ':' never create folders."""
    from schema_app.core.format import Bindings, compile_format

    bindings = Bindings.from_dict({"n": "Face/Off: Redux", "y": 1997}, extension_suffix=".mkv")
    assert compile_format("{n} ({y})/{n}").render(bindings) == "Face-Off - Redux (1997)/Face-Off - Redux.mkv"
