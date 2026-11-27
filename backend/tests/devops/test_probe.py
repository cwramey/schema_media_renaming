"""Media probing with ``shutil.which`` and ``subprocess.run`` patched (no real binaries)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from schema_app.services import probe

MEDIAINFO_JSON: dict[str, Any] = {
    "media": {
        "@ref": "movie.mkv",
        "track": [
            {"@type": "General", "Format": "Matroska", "Duration": "7265.123", "OverallBitRate": "18534000",
             "FrameRate": "23.976"},
            {"@type": "Video", "Format": "HEVC", "Width": "3840", "Height": "2160", "BitDepth": "10",
             "FrameRate": "23.976", "HDR_Format": "Dolby Vision / SMPTE ST 2086",
             "HDR_Format_Compatibility": "HDR10", "transfer_characteristics": "PQ", "Default": "Yes"},
            {"@type": "Audio", "Format": "MLP FBA", "Format_Commercial_IfAny": "Dolby TrueHD with Dolby Atmos",
             "Channels": "8", "ChannelLayout": "L R C LFE Ls Rs Lb Rb", "Language": "en", "Default": "Yes"},
            {"@type": "Audio", "Format": "AC-3", "Channels": "6", "Language": "de", "Default": "No"},
            {"@type": "Audio", "Format": "E-AC-3", "Channels": "6", "Language": "en", "Default": "No"},
            {"@type": "Text", "Language": "en", "Format": "PGS"},
            {"@type": "Text", "Language": "fr-CA", "Format": "UTF-8"},
            {"@type": "Text", "Language": "und", "Format": "UTF-8"},
        ],
    }
}  # fmt: skip

FFPROBE_JSON: dict[str, Any] = {
    "streams": [
        {"codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}},
        {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "pix_fmt": "yuv420p",
         "avg_frame_rate": "24000/1001", "disposition": {"default": 1, "attached_pic": 0}},
        {"codec_type": "audio", "codec_name": "dts", "profile": "DTS-HD MA", "channels": 6,
         "channel_layout": "5.1(side)", "tags": {"language": "eng"}, "disposition": {"default": 1}},
        {"codec_type": "audio", "codec_name": "aac", "channels": 2, "channel_layout": "stereo",
         "tags": {"language": "jpn"}, "disposition": {"default": 0}},
        {"codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "ger"}},
    ],
    "format": {"filename": "x.mkv", "format_name": "matroska,webm", "duration": "1422.250000",
               "bit_rate": "5200000"},
}  # fmt: skip


@pytest.fixture
def media_file(tmp_path: Path) -> Path:
    path = tmp_path / "movie.mkv"
    path.write_bytes(b"\x1a\x45\xdf\xa3 not really a matroska file")
    return path


def _which(*available: str):  # type: ignore[no-untyped-def]
    return lambda name: f"/usr/bin/{name}" if name in available else None


def _completed(payload: Any, code: int = 0) -> subprocess.CompletedProcess[bytes]:
    out = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=out, stderr=b"")


def test_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe.shutil, "which", _which("mediainfo", "ffprobe"))
    assert probe.available() == "mediainfo"
    monkeypatch.setattr(probe.shutil, "which", _which("ffprobe"))
    assert probe.available() == "ffprobe"
    monkeypatch.setattr(probe.shutil, "which", _which())
    assert probe.available() is None


def test_no_backend_returns_none(monkeypatch: pytest.MonkeyPatch, media_file: Path) -> None:
    monkeypatch.setattr(probe.shutil, "which", _which())

    def never(*_a: object, **_k: object) -> None:
        raise AssertionError("must not run anything")

    monkeypatch.setattr(probe.subprocess, "run", never)
    assert probe.probe(media_file) is None


def test_missing_file_returns_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(probe.shutil, "which", _which("mediainfo"))
    assert probe.probe(tmp_path / "nope.mkv") is None


def test_mediainfo_mapping(monkeypatch: pytest.MonkeyPatch, media_file: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        assert kwargs["timeout"] == 5 and kwargs["capture_output"]
        return _completed(MEDIAINFO_JSON)

    monkeypatch.setattr(probe.shutil, "which", _which("mediainfo", "ffprobe"))
    monkeypatch.setattr(probe.subprocess, "run", fake_run)
    info = probe.probe(media_file, timeout=5)
    assert info is not None
    assert calls == [["/usr/bin/mediainfo", "--Output=JSON", str(media_file)]]
    assert info.container == "mkv"
    assert info.duration_seconds == pytest.approx(7265.123)
    assert (info.width, info.height, info.bit_depth) == (3840, 2160, 10)
    assert info.video_codec == "x265"
    assert info.frame_rate == pytest.approx(23.976)
    assert info.hdr == ["DV", "HDR10"]
    assert info.audio_codec == "TrueHD" and info.audio_channels == "7.1"
    assert info.audio_languages == ["en", "de"]
    assert info.subtitle_languages == ["en", "fr"]
    assert info.bitrate_kbps == 18534


def test_ffprobe_fallback_when_mediainfo_missing(monkeypatch: pytest.MonkeyPatch, media_file: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        return _completed(FFPROBE_JSON)

    monkeypatch.setattr(probe.shutil, "which", _which("ffprobe"))
    monkeypatch.setattr(probe.subprocess, "run", fake_run)
    info = probe.probe(media_file)
    assert info is not None
    assert calls[0][:2] == ["/usr/bin/ffprobe", "-v"] and "-show_streams" in calls[0]
    assert info.container == "mkv"
    assert info.video_codec == "x264" and (info.width, info.height) == (1920, 1080)
    assert info.bit_depth == 8
    assert info.frame_rate == pytest.approx(23.976)
    assert info.audio_codec == "DTS-HD MA" and info.audio_channels == "5.1"
    assert info.audio_languages == ["en", "ja"]
    assert info.subtitle_languages == ["de"]
    assert info.duration_seconds == pytest.approx(1422.25) and info.bitrate_kbps == 5200


def test_ffprobe_used_when_mediainfo_fails(monkeypatch: pytest.MonkeyPatch, media_file: Path) -> None:
    def fake_run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if "mediainfo" in args[0]:
            return _completed(b"", code=1)
        return _completed(FFPROBE_JSON)

    monkeypatch.setattr(probe.shutil, "which", _which("mediainfo", "ffprobe"))
    monkeypatch.setattr(probe.subprocess, "run", fake_run)
    info = probe.probe(media_file)
    assert info is not None and info.video_codec == "x264"


def test_garbage_output_returns_none(monkeypatch: pytest.MonkeyPatch, media_file: Path) -> None:
    monkeypatch.setattr(probe.shutil, "which", _which("mediainfo"))
    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **k: _completed(b"<html>not json</html>"))
    assert probe.probe(media_file) is None
    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **k: _completed({"media": {"track": [
        {"@type": "General", "Format": "Text"}]}}))  # fmt: skip
    assert probe.probe(media_file) is None  # no audio or video track: not media


def test_timeout_returns_none_without_retrying(monkeypatch: pytest.MonkeyPatch, media_file: Path) -> None:
    calls: list[str] = []

    def slow(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(args[0])
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(probe.shutil, "which", _which("mediainfo", "ffprobe"))
    monkeypatch.setattr(probe.subprocess, "run", slow)
    assert probe.probe(media_file, timeout=0.1) is None
    assert len(calls) == 1


def test_binary_vanished_between_checks(monkeypatch: pytest.MonkeyPatch, media_file: Path) -> None:
    def gone(*_a: object, **_k: object) -> None:
        raise FileNotFoundError("mediainfo")

    monkeypatch.setattr(probe.shutil, "which", _which("mediainfo"))
    monkeypatch.setattr(probe.subprocess, "run", gone)
    assert probe.probe(media_file) is None


@pytest.mark.parametrize(
    ("track", "expected"),
    [
        ({"Format": "AVC"}, "x264"),
        ({"Format": "MPEG-4 Visual", "CodecID": "XVID"}, "XviD"),
        ({"Format": "MPEG-4 Visual", "CodecID": "DX50"}, "DivX"),
        ({"Format": "MPEG Video", "Format_Version": "2"}, "MPEG-2"),
        ({"Format": "AV1"}, "AV1"),
        ({"Format": "VC-1"}, "VC-1"),
    ],
)
def test_mediainfo_video_codecs(track: dict[str, str], expected: str) -> None:
    assert probe._mediainfo_video_codec(track) == expected


@pytest.mark.parametrize(
    ("track", "expected"),
    [
        ({"Format": "E-AC-3"}, "DDP"),
        ({"Format": "AC-3"}, "AC3"),
        ({"Format": "DTS", "Format_AdditionalFeatures": "XLL"}, "DTS-HD MA"),
        ({"Format": "DTS", "Format_AdditionalFeatures": "XLL X"}, "DTS-X"),
        ({"Format": "DTS"}, "DTS"),
        ({"Format": "AAC"}, "AAC"),
        ({"Format": "Opus"}, "Opus"),
        ({"Format": "FLAC"}, "FLAC"),
    ],
)
def test_mediainfo_audio_codecs(track: dict[str, str], expected: str) -> None:
    assert probe._mediainfo_audio_codec(track) == expected


@pytest.mark.parametrize(
    ("count", "layout", "expected"),
    [(2, "L R", "2.0"), (6, "", "5.1"), (6, "L R C Ls Rs Cs", "6.0"), (5, "5.0(side)", "5.0"),
     (8, "7.1", "7.1"), (1, "", "1.0"), (None, "", None), (2, "stereo", "2.0"), (7, "6.1", "6.1")],
)  # fmt: skip
def test_channels(count: int | None, layout: str, expected: str | None) -> None:
    assert probe._channels(count, layout) == expected


def test_hdr_variants() -> None:
    hdr10plus = {"HDR_Format": "SMPTE ST 2094 App 4", "HDR_Format_Compatibility": "HDR10+ Profile A"}
    assert probe._mediainfo_hdr(hdr10plus) == ["HDR10+"]
    assert probe._mediainfo_hdr({**hdr10plus, "transfer_characteristics": "PQ"}) == ["HDR10+", "HDR10"]
    assert probe._mediainfo_hdr({"transfer_characteristics": "HLG"}) == ["HLG"]
    assert probe._mediainfo_hdr({}) == []
    assert probe._ff_hdr({"color_transfer": "smpte2084", "side_data_list": [
        {"side_data_type": "DOVI configuration record"}]}) == ["DV", "HDR10"]  # fmt: skip
    assert probe._ff_hdr({"color_transfer": "arib-std-b67"}) == ["HLG"]


# --------------------------------------------------------------------------- non-regular files


def _never_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe.shutil, "which", _which("mediainfo", "ffprobe"))

    def never(*_a: object, **_k: object) -> None:
        raise AssertionError("a probe tool must not be started on this path")

    monkeypatch.setattr(probe.subprocess, "run", never)


def test_devices_and_folders_are_not_probed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _never_run(monkeypatch)
    assert probe.probe(os.devnull) is None
    assert probe.probe(tmp_path) is None


def test_symlink_to_a_device_is_not_probed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, need_symlinks: None
) -> None:
    _never_run(monkeypatch)
    link = tmp_path / "zero.mkv"
    os.symlink(os.devnull, link)
    assert probe.probe(link) is None


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX-only")
def test_fifo_is_not_probed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _never_run(monkeypatch)
    fifo = tmp_path / "video.mkv"
    os.mkfifo(fifo)
    assert probe.probe(fifo) is None
