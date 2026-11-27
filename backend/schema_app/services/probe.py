"""Media probing via the ``mediainfo`` CLI (JSON) with ``ffprobe`` as fallback.

Contract: docs/SPEC.md §7.5.

The probe is optional: every feature works from filenames alone. Values are normalized to the
same canonical names the filename parser uses (``QualityInfo``): video ``x264``/``x265``/``AV1``,
audio ``AAC``/``AC3``/``DDP``/``TrueHD``/``DTS-HD MA``, HDR ``HDR10``/``HDR10+``/``DV``/``HLG``,
channels ``5.1``, languages as ISO 639-1 where known.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

from schema_app.domain import ProbeInfo

log = logging.getLogger(__name__)

_ISO639_2_TO_1 = {
    "eng": "en", "fre": "fr", "fra": "fr", "ger": "de", "deu": "de", "spa": "es", "ita": "it",
    "jpn": "ja", "chi": "zh", "zho": "zh", "kor": "ko", "rus": "ru", "por": "pt", "dut": "nl",
    "nld": "nl", "swe": "sv", "nor": "no", "nob": "nb", "dan": "da", "fin": "fi", "pol": "pl",
    "cze": "cs", "ces": "cs", "hun": "hu", "gre": "el", "ell": "el", "tur": "tr", "ara": "ar",
    "heb": "he", "hin": "hi", "tha": "th", "vie": "vi", "ind": "id", "ukr": "uk", "rum": "ro",
    "ron": "ro", "bul": "bg", "hrv": "hr", "srp": "sr", "slv": "sl", "slo": "sk", "slk": "sk",
    "cat": "ca", "may": "ms", "msa": "ms", "fil": "tl", "tgl": "tl", "per": "fa", "fas": "fa",
    "ice": "is", "isl": "is", "est": "et", "lav": "lv", "lit": "lt",
}  # fmt: skip

_CONTAINERS = {
    "matroska": "mkv", "webm": "webm", "mpeg-4": "mp4", "quicktime": "mov", "avi": "avi",
    "mpeg-ts": "ts", "bdav": "m2ts", "mpeg-ps": "mpg", "windows media": "wmv", "flash video": "flv",
    "ogg": "ogm", "realmedia": "rm",
}  # fmt: skip


def available() -> str | None:
    """Name of the usable backend ("mediainfo" / "ffprobe") or None."""
    if shutil.which("mediainfo"):
        return "mediainfo"
    if shutil.which("ffprobe"):
        return "ffprobe"
    return None


def _regular_file(path: str) -> bool:
    """Only regular files are probed: a FIFO, /dev/zero or a socket (directly or through a symlink)
    would keep mediainfo/ffprobe reading until the timeout, a whole worker slot each time."""
    try:
        return stat.S_ISREG(os.stat(path).st_mode)
    except (OSError, ValueError):
        return False


def probe(path: str | Path, *, timeout: float = 20.0) -> ProbeInfo | None:
    """Blocking. Returns None when no backend is installed or the file is unreadable/not media
    (anything but a regular file is never handed to the tools)."""
    target = os.fspath(path)
    if not _regular_file(target):
        return None
    backends = [b for b in ("mediainfo", "ffprobe") if shutil.which(b)]
    for backend in backends:
        try:
            info = (
                _run_mediainfo(target, timeout) if backend == "mediainfo" else _run_ffprobe(target, timeout)
            )
        except subprocess.TimeoutExpired:
            log.warning("%s timed out after %.0f s on %s", backend, timeout, target)
            return None  # a hung network read would hang the next tool too
        except (OSError, ValueError) as exc:
            log.info("%s could not read %s: %s", backend, target, exc)
            continue
        if info is not None:
            return info
    return None


# --------------------------------------------------------------------------- mediainfo


def _run_mediainfo(path: str, timeout: float) -> ProbeInfo | None:
    exe = shutil.which("mediainfo") or "mediainfo"
    proc = subprocess.run([exe, "--Output=JSON", path], capture_output=True, timeout=timeout, check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return parse_mediainfo_json(json.loads(proc.stdout.decode("utf-8", errors="replace")))


def parse_mediainfo_json(data: dict[str, Any]) -> ProbeInfo | None:
    media = data.get("media") if isinstance(data, dict) else None
    tracks = media.get("track") if isinstance(media, dict) else None
    if not isinstance(tracks, list):
        return None
    general = next((t for t in tracks if t.get("@type") == "General"), {})
    videos = [t for t in tracks if t.get("@type") == "Video"]
    audios = [t for t in tracks if t.get("@type") == "Audio"]
    texts = [t for t in tracks if t.get("@type") == "Text"]
    if not videos and not audios:
        return None
    video = next((v for v in videos if _yes(v.get("Default"))), videos[0] if videos else {})
    audio = next((a for a in audios if _yes(a.get("Default"))), audios[0] if audios else {})

    container = general.get("Format")
    duration = _float(general.get("Duration")) or _float(video.get("Duration"))
    bitrate = _int(general.get("OverallBitRate"))
    return ProbeInfo(
        container=_CONTAINERS.get(str(container).lower(), str(container).lower()) if container else None,
        duration_seconds=duration,
        width=_int(video.get("Width")),
        height=_int(video.get("Height")),
        video_codec=_mediainfo_video_codec(video) if video else None,
        bit_depth=_int(video.get("BitDepth")),
        frame_rate=_float(video.get("FrameRate")) or _float(general.get("FrameRate")),
        hdr=_mediainfo_hdr(video) if video else [],
        audio_codec=_mediainfo_audio_codec(audio) if audio else None,
        audio_channels=_channels(_int(audio.get("Channels")), str(audio.get("ChannelLayout") or ""))
        if audio
        else None,
        audio_languages=_languages(a.get("Language") for a in audios),
        subtitle_languages=_languages(t.get("Language") for t in texts),
        bitrate_kbps=bitrate // 1000 if bitrate else None,
    )


def _mediainfo_video_codec(track: dict[str, Any]) -> str | None:
    fmt = str(track.get("Format") or "").upper()
    codec_id = str(track.get("CodecID") or "").upper()
    if fmt == "AVC":
        return "x264"
    if fmt == "HEVC":
        return "x265"
    if fmt == "AV1":
        return "AV1"
    if fmt == "VP9":
        return "VP9"
    if fmt == "VC-1":
        return "VC-1"
    if fmt == "MPEG VIDEO":
        return (
            "MPEG-2"
            if str(track.get("Format_Version", "")).endswith("2") or not track.get("Format_Version")
            else "MPEG-1"
        )
    if fmt == "MPEG-4 VISUAL":
        if "XVID" in codec_id:
            return "XviD"
        if "DIV" in codec_id or "DX50" in codec_id:
            return "DivX"
        return "XviD"
    return str(track.get("Format")) if track.get("Format") else None


def _mediainfo_hdr(track: dict[str, Any]) -> list[str]:
    blob = " ".join(
        str(track.get(k) or "")
        for k in ("HDR_Format", "HDR_Format_Compatibility", "HDR_Format_Commercial", "HDR_Format_String")
    ).upper()
    transfer = str(
        track.get("transfer_characteristics") or track.get("TransferCharacteristics") or ""
    ).upper()
    found: list[str] = []
    if "DOLBY VISION" in blob:
        found.append("DV")
    if "HDR10+" in blob or "2094 APP 4" in blob or "2094-40" in blob:
        found.append("HDR10+")
    if "HDR10" in blob.replace("HDR10+", "") or "2086" in blob or "PQ" in transfer or "2084" in transfer:
        found.append("HDR10")
    if "HLG" in blob or "HLG" in transfer or "B67" in transfer:
        found.append("HLG")
    return found


def _mediainfo_audio_codec(track: dict[str, Any]) -> str | None:
    fmt = str(track.get("Format") or "").upper()
    commercial = str(track.get("Format_Commercial_IfAny") or track.get("Format_Commercial") or "").upper()
    features = str(track.get("Format_AdditionalFeatures") or "").upper()
    profile = str(track.get("Format_Profile") or "").upper()
    if fmt == "E-AC-3":
        return "DDP"
    if fmt == "AC-3":
        return "AC3"
    if fmt == "MLP FBA" or "TRUEHD" in commercial:
        return "TrueHD"
    if fmt == "DTS":
        if "DTS:X" in commercial or "XLL X" in features or "DTS:X" in profile:
            return "DTS-X"
        if "XLL" in features or "MA" in profile or "MASTER AUDIO" in commercial:
            return "DTS-HD MA"
        return "DTS"
    if fmt == "AAC":
        return "AAC"
    if fmt == "FLAC":
        return "FLAC"
    if fmt == "OPUS":
        return "Opus"
    if fmt == "MPEG AUDIO":
        return "MP3" if "3" in str(track.get("Format_Profile") or "Layer 3") else "MP2"
    if fmt == "PCM":
        return "PCM"
    if fmt == "VORBIS":
        return "Vorbis"
    return str(track.get("Format")) if track.get("Format") else None


# --------------------------------------------------------------------------- ffprobe


def _run_ffprobe(path: str, timeout: float) -> ProbeInfo | None:
    exe = shutil.which("ffprobe") or "ffprobe"
    proc = subprocess.run(
        [exe, "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return parse_ffprobe_json(json.loads(proc.stdout.decode("utf-8", errors="replace")))


_FF_VIDEO = {
    "h264": "x264", "hevc": "x265", "av1": "AV1", "vp9": "VP9", "vc1": "VC-1", "mpeg2video": "MPEG-2",
    "mpeg1video": "MPEG-1", "mpeg4": "XviD", "msmpeg4v3": "DivX",
}  # fmt: skip
_FF_AUDIO = {
    "aac": "AAC", "ac3": "AC3", "eac3": "DDP", "truehd": "TrueHD", "flac": "FLAC", "opus": "Opus",
    "mp3": "MP3", "mp2": "MP2", "vorbis": "Vorbis", "pcm_s16le": "PCM", "pcm_s24le": "PCM",
}  # fmt: skip


def parse_ffprobe_json(data: dict[str, Any]) -> ProbeInfo | None:
    streams = data.get("streams") if isinstance(data, dict) else None
    if not isinstance(streams, list):
        return None
    fmt = data.get("format") or {}
    videos = [
        s for s in streams
        if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
    ]  # fmt: skip
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]
    if not videos and not audios:
        return None
    video = next(
        (v for v in videos if (v.get("disposition") or {}).get("default")), videos[0] if videos else {}
    )
    audio = next(
        (a for a in audios if (a.get("disposition") or {}).get("default")), audios[0] if audios else {}
    )

    format_name = str(fmt.get("format_name") or "")
    container = None
    if format_name:
        first = format_name.split(",")[0]
        container = {"matroska": "mkv", "mov": "mp4", "mpegts": "ts", "mpeg": "mpg", "asf": "wmv"}.get(
            first, first
        )
        if first == "mov" and str(fmt.get("filename", "")).lower().endswith(".mov"):
            container = "mov"
    bitrate = _int(fmt.get("bit_rate"))
    return ProbeInfo(
        container=container,
        duration_seconds=_float(fmt.get("duration")) or _float(video.get("duration")),
        width=_int(video.get("width")),
        height=_int(video.get("height")),
        video_codec=_FF_VIDEO.get(str(video.get("codec_name") or ""), video.get("codec_name"))
        if video
        else None,
        bit_depth=_ff_bit_depth(video) if video else None,
        frame_rate=_ratio(video.get("avg_frame_rate")) or _ratio(video.get("r_frame_rate"))
        if video
        else None,
        hdr=_ff_hdr(video) if video else [],
        audio_codec=_ff_audio_codec(audio) if audio else None,
        audio_channels=_channels(_int(audio.get("channels")), str(audio.get("channel_layout") or ""))
        if audio
        else None,
        audio_languages=_languages((a.get("tags") or {}).get("language") for a in audios),
        subtitle_languages=_languages((s.get("tags") or {}).get("language") for s in subs),
        bitrate_kbps=bitrate // 1000 if bitrate else None,
    )


def _ff_bit_depth(stream: dict[str, Any]) -> int | None:
    raw = _int(stream.get("bits_per_raw_sample"))
    if raw:
        return raw
    pix = str(stream.get("pix_fmt") or "")
    for depth in (12, 10):
        if f"{depth}le" in pix or f"{depth}be" in pix:
            return depth
    return 8 if pix else None


def _ff_hdr(stream: dict[str, Any]) -> list[str]:
    found: list[str] = []
    side = stream.get("side_data_list") or []
    kinds = " ".join(str(s.get("side_data_type") or "") for s in side if isinstance(s, dict)).upper()
    transfer = str(stream.get("color_transfer") or "").lower()
    if "DOVI" in kinds or "DOLBY VISION" in kinds:
        found.append("DV")
    if "HDR DYNAMIC METADATA SMPTE2094-40" in kinds or "HDR10+" in kinds:
        found.append("HDR10+")
    if transfer == "smpte2084" or "MASTERING DISPLAY" in kinds:
        found.append("HDR10")
    if transfer == "arib-std-b67":
        found.append("HLG")
    return found


def _ff_audio_codec(stream: dict[str, Any]) -> str | None:
    name = str(stream.get("codec_name") or "")
    profile = str(stream.get("profile") or "").upper()
    if name == "dts":
        if "DTS:X" in profile:
            return "DTS-X"
        if "MA" in profile:
            return "DTS-HD MA"
        return "DTS"
    if name.startswith("pcm_"):
        return "PCM"
    return _FF_AUDIO.get(name, name or None)


# --------------------------------------------------------------------------- shared helpers


def _yes(value: Any) -> bool:
    return str(value).strip().lower() in {"yes", "true", "1"}


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value).split("/")[0].split(" ")[0]))
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).split(" ")[0])
    except (TypeError, ValueError):
        return None


def _ratio(value: Any) -> float | None:
    if not value:
        return None
    text = str(value)
    if "/" in text:
        num, _, den = text.partition("/")
        try:
            n, d = float(num), float(den)
        except ValueError:
            return None
        return round(n / d, 3) if d else None
    return _float(text)


def _channels(count: int | None, layout: str) -> str | None:
    if not count:
        return None
    layout = layout.strip()
    # mediainfo: "L R C LFE Ls Rs"; ffprobe: "5.1(side)", "7.1", "stereo". Without a layout, assume
    # the usual 5.1/7.1 shape for six or more channels.
    has_lfe = ("LFE" in layout.upper() or ".1" in layout) if layout else count >= 6
    return f"{count - 1}.1" if has_lfe and count > 1 else f"{count}.0"


def _languages(values: Any) -> list[str]:
    out: list[str] = []
    for value in values:
        if not value:
            continue
        code = str(value).strip().lower().replace("_", "-").split("-")[0]
        if code in {"und", "zxx", "mis", "mul", ""}:
            continue
        code = _ISO639_2_TO_1.get(code, code)
        if code not in out:
            out.append(code)
    return out
