"""Release-tag vocabulary for the filename parser (clean-room tables, SPEC §6.1 stage 3).

Every rule is a compiled regex plus a handler that records what it saw into a
``TagState``. Rules are *strong* (may appear anywhere and end the title) or *weak*
(only honoured after the title has already ended, because the word is also common in
real titles: "Charlotte's Web", "Mr. Holland's Opus", "The German", "Final Cut").

Input text has underscores already turned into spaces, so ``S`` (the in-tag separator)
only needs space, dot and dash.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

B0 = r"(?<![0-9A-Za-z])"
B1 = r"(?![0-9A-Za-z])"
S = r"[ .\-]"


@dataclass
class TagState:
    resolution: str | None = None
    source: str | None = None
    remux: bool = False
    video_codec: str | None = None
    bit_depth: int | None = None
    hdr: list[str] = field(default_factory=list)
    audio_codec: str | None = None
    audio_channels: str | None = None
    atmos: bool = False
    streaming_service: str | None = None
    proper: bool = False
    repack: bool = False
    s3d: str | None = None
    editions: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    is_extra: bool = False
    extra_word: bool = False  # bonus-material word after the title (only counts for non-episodic names)
    quality_tags: int = 0  # how many technical tags were recognised (kind vote evidence)


Handler = Callable[[TagState, re.Match[str]], None]


@dataclass(frozen=True)
class TagRule:
    name: str
    regex: re.Pattern[str]
    handler: Handler
    strong: bool = True


def _rx(body: str, *, ci: bool = True) -> re.Pattern[str]:
    return re.compile(B0 + "(?:" + body + ")" + B1, re.IGNORECASE if ci else 0)


def _first(attr: str, value: object, *, technical: bool = True) -> Handler:
    def handler(st: TagState, _m: re.Match[str]) -> None:
        if getattr(st, attr) in (None, False):
            setattr(st, attr, value)
        if technical:
            st.quality_tags += 1

    return handler


_NON_DIGITS = re.compile(r"[^0-9]")


def _channels_from(raw: str | None) -> str | None:
    if not raw:
        return None
    digits = _NON_DIGITS.sub("", raw)
    if len(digits) != 2:
        return None
    ch = f"{digits[0]}.{digits[1]}"
    return ch if ch in _VALID_CHANNELS else None


_VALID_CHANNELS = {"1.0", "2.0", "2.1", "3.0", "4.0", "4.1", "5.0", "5.1", "6.1", "7.1", "9.1"}

# optional channel layout glued to an audio codec: DDP5.1, AAC2.0, TrueHD.7.1, DTS-HD.MA.5.1
CH = r"(?:[ .]?(?P<ch>[1-9][ .]?[01]))?"


def _audio(codec: str) -> Handler:
    def handler(st: TagState, m: re.Match[str]) -> None:
        if st.audio_codec is None:
            st.audio_codec = codec
        ch = _channels_from(m.groupdict().get("ch"))
        if ch and st.audio_channels is None:
            st.audio_channels = ch
        st.quality_tags += 1

    return handler


def _channels(st: TagState, m: re.Match[str]) -> None:
    gd = m.groupdict()
    ch = _channels_from(gd.get("ch"))
    if gd.get("n"):
        ch = {"2": "2.0", "6": "5.1", "8": "7.1"}.get(gd["n"])
    if ch and st.audio_channels is None:
        st.audio_channels = ch
        st.quality_tags += 1


def _resolution(st: TagState, m: re.Match[str]) -> None:
    if st.resolution is None:
        st.resolution = m.group("r") + "p"
    st.quality_tags += 1


_HEIGHTS = ((4320, "4320p"), (2160, "2160p"), (1440, "1440p"), (1080, "1080p"), (720, "720p"), (576, "576p"), (480, "480p"))


def _dimensions(st: TagState, m: re.Match[str]) -> None:
    w, h = int(m.group("w")), int(m.group("h"))
    if not (200 <= h <= 4400 and w >= h):
        return
    if st.resolution is None:
        for limit, label in _HEIGHTS:
            if h >= limit * 0.9:
                st.resolution = label
                break
        else:
            st.resolution = "480p"
    st.quality_tags += 1


def _hdr(label: str) -> Handler:
    def handler(st: TagState, _m: re.Match[str]) -> None:
        if label not in st.hdr:
            st.hdr.append(label)
        st.quality_tags += 1

    return handler


def _bitdepth(st: TagState, m: re.Match[str]) -> None:
    raw = m.groupdict().get("b")
    if st.bit_depth is None:
        st.bit_depth = int(raw) if raw else 10
    st.quality_tags += 1


def _remux(st: TagState, m: re.Match[str]) -> None:
    st.remux = True
    if m.group(0).lower().startswith("bd") and st.source is None:
        st.source = "BluRay"
    st.quality_tags += 1


def _edition(label: str | None = None) -> Handler:
    def handler(st: TagState, m: re.Match[str]) -> None:
        name = label
        if name is None:  # anniversary with an ordinal
            n = m.groupdict().get("n")
            name = f"{n}{_ordinal(int(n))} Anniversary Edition" if n else "Anniversary Edition"
        if name not in st.editions:
            st.editions.append(name)

    return handler


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def _language(code: str) -> Handler:
    def handler(st: TagState, _m: re.Match[str]) -> None:
        if code not in st.languages:
            st.languages.append(code)

    return handler


def _service(code: str) -> Handler:
    def handler(st: TagState, _m: re.Match[str]) -> None:
        if st.streaming_service is None:
            st.streaming_service = code

    return handler


def _extra(st: TagState, _m: re.Match[str]) -> None:
    st.is_extra = True


def _extra_word(st: TagState, _m: re.Match[str]) -> None:
    st.extra_word = True


def _noop(st: TagState, _m: re.Match[str]) -> None:
    st.quality_tags += 1


# --------------------------------------------------------------------------- vocab tables

# Long language words are matched case-insensitively; short codes only in upper case
# (so "ita" inside a title or "Eng" as a name do not trigger).
_LANG_WORDS: dict[str, str] = {
    "multi": "mul", "multilang": "mul", "multilanguage": "mul",
    "german": "de", "deutsch": "de",
    "french": "fr", "truefrench": "fr", "vostfr": "fr", "subfrench": "fr",
    "italian": "it", "spanish": "es", "castellano": "es", "latino": "es",
    "japanese": "ja", "english": "en", "korean": "ko", "chinese": "zh", "mandarin": "zh",
    "cantonese": "zh", "russian": "ru", "polish": "pl", "dutch": "nl", "flemish": "nl",
    "swedish": "sv", "norwegian": "no", "danish": "da", "finnish": "fi",
    "portuguese": "pt", "hindi": "hi", "turkish": "tr", "hungarian": "hu", "czech": "cs",
    "arabic": "ar", "hebrew": "he", "thai": "th", "greek": "el", "ukrainian": "uk",
    "vietnamese": "vi", "indonesian": "id", "romanian": "ro", "tamil": "ta", "telugu": "te",
}
_LANG_CODES: dict[str, str] = {
    "GER": "de", "DEU": "de", "FRE": "fr", "FRA": "fr", "VFF": "fr", "VFQ": "fr", "VF2": "fr",
    "ITA": "it", "SPA": "es", "ESP": "es", "JPN": "ja", "JAP": "ja", "ENG": "en", "KOR": "ko",
    "CHI": "zh", "CHS": "zh", "CHT": "zh", "RUS": "ru", "PL": "pl", "PLDUB": "pl", "NL": "nl",
    "SWE": "sv", "NOR": "no", "DAN": "da", "FIN": "fi", "POR": "pt", "HIN": "hi", "TUR": "tr",
    "HUN": "hu", "CZ": "cs", "ARA": "ar", "HEB": "he", "UKR": "uk", "VIE": "vi",
}

# streaming-service release tags (case-sensitive; canonical spelling on the right)
_SERVICES: dict[str, str] = {
    "AMZN": "AMZN", "NF": "NF", "NFLX": "NF", "DSNP": "DSNP", "DSNY": "DSNP", "HMAX": "HMAX",
    "MAX": "MAX", "ATVP": "ATVP", "ATV": "ATVP", "HULU": "HULU", "PCOK": "PCOK", "PMTP": "PMTP",
    "iT": "iT", "CRAV": "CRAV", "STAN": "STAN", "HBO": "HBO", "CR": "CR", "FUNI": "FUNI",
    "VRV": "VRV", "iP": "iP", "NOW": "NOW", "RED": "RED", "YT": "YT", "VUDU": "VUDU",
    "BCORE": "BCORE", "CRIT": "CRIT", "SHO": "SHO", "STZ": "STZ", "AMC": "AMC", "HTSR": "HTSR",
    "MUBI": "MUBI", "ROKU": "ROKU", "TVNZ": "TVNZ", "SKST": "SKST", "PLUZ": "PLUZ", "ADN": "ADN",
}

# Known anime fansub / raw groups (lower-cased) - an anime signal for the kind vote.
FANSUB_GROUPS = frozenset(
    g.lower()
    for g in (
        "SubsPlease", "Erai-raws", "HorribleSubs", "Judas", "EMBER", "ASW", "Commie", "GJM",
        "Kametsu", "Kaleido", "Coalgirls", "Doki", "FFF", "Underwater", "gg", "UTW", "Vivid",
        "DeadFish", "Cleo", "Golumpa", "Hatsuyuki", "Ohys-Raws", "Leopard-Raws", "NC-Raws",
        "Tsundere-Raws", "LostYears", "Yameii", "SallySubs", "MTBB", "Beatrice-Raws", "VCB-Studio",
        "Moozzi2", "Kawaiika-Raws", "DmonHiro", "CBM", "Chihiro", "Mezashite", "Mazui", "Arid",
        "iAHD", "Anime Time", "AnimeKaizoku", "Tenrai-Sensei", "SCY", "Exiled-Destiny", "a4e",
        "Nyanpasu", "Asenshi", "Some-Stuffs", "Reinforce", "Snow-Raws", "Lilith-Raws", "NanDesuKa",
        "ToonsHub", "Anime-Land", "Aergia", "Vodes", "smol", "Okay-Subs", "Kawaii", "SEV",
    )
)


def _build_rules() -> list[TagRule]:
    r: list[TagRule] = []
    add = r.append

    # editions first: "4K.Remastered" must not be eaten by the 4K resolution rule
    add(TagRule("edition", _rx(rf"director'?s{S}?cut"), _edition("Director's Cut")))
    add(TagRule("edition", _rx(rf"extended{S}(?:cut|edition|version)"), _edition("Extended")))
    add(TagRule("edition", _rx(rf"theatrical{S}(?:cut|edition|version)"), _edition("Theatrical")))
    add(TagRule("edition", _rx(rf"ultimate{S}(?:cut|edition)"), _edition("Ultimate")))
    add(TagRule("edition", _rx(rf"special{S}edition"), _edition("Special Edition")))
    add(TagRule("edition", _rx(rf"collector'?s{S}edition"), _edition("Collector's Edition")))
    add(TagRule("edition", _rx(rf"open{S}matte"), _edition("Open Matte")))
    add(TagRule("edition", _rx(rf"(?:(?P<n>\d{{1,3}})(?:th|st|nd|rd){S})?anniversary{S}edition"), _edition()))
    add(TagRule("edition", _rx(rf"4k{S}remaster(?:ed)?"), _edition("Remastered")))
    add(TagRule("edition", _rx(rf"imax{S}edition"), _edition("IMAX")))
    add(TagRule("edition", _rx(rf"(?:alternate|alternative){S}cut"), _edition("Alternate Cut")))
    add(TagRule("edition", _rx(rf"producer'?s{S}cut"), _edition("Producer's Cut")))
    add(TagRule("edition", _rx(rf"rogue{S}cut"), _edition("Rogue Cut")))
    add(TagRule("edition", _rx(rf"criterion{S}collection"), _edition("Criterion")))
    add(TagRule("edition", _rx(rf"black{S}and{S}chrome"), _edition("Black and Chrome")))

    # resolution
    add(TagRule("resolution", _rx(r"(?P<r>480|576|720|1080|1440|2160|4320)[pi]"), _resolution))
    add(TagRule("resolution", _rx(r"(?P<w>\d{3,4})x(?P<h>\d{3,4})"), _dimensions))
    add(TagRule("resolution", _rx(r"4k"), _first("resolution", "2160p")))
    add(TagRule("resolution", _rx(r"8k"), _first("resolution", "4320p")))

    # source (+ remux)
    add(TagRule("remux", _rx(r"(?:bd)?remux"), _remux))
    add(TagRule("source", _rx(rf"blu{S}?ray(?:{S}?rip)?|b[dr]{S}?rip|bd(?:25|50|66|100)?|bdmv|uhd{S}bd"), _first("source", "BluRay")))
    add(TagRule("source", _rx(rf"web{S}?rip"), _first("source", "WEBRip")))
    add(TagRule("source", _rx(rf"web{S}?dl(?:{S}?rip)?"), _first("source", "WEB-DL")))
    add(TagRule("source", _rx(r"hdtv(?:rip)?"), _first("source", "HDTV")))
    add(TagRule("source", _rx(rf"sdtv|pdtv|dsr(?:ip)?|sat{S}?rip|tv{S}?rip|dvb{S}?rip"), _first("source", "SDTV")))
    add(TagRule("source", _rx(rf"dvd(?:{S}?rip|scr|r|5|9)?"), _first("source", "DVD")))
    add(TagRule("source", _rx(rf"vhs(?:{S}?rip)?"), _first("source", "VHS")))
    add(TagRule("source", _rx(rf"telesync|hd{S}?ts|ts{S}?rip|pdvd|telecine|hd{S}?tc"), _first("source", "Telesync")))
    add(TagRule("source", _rx(rf"cam{S}?rip|hd{S}?cam"), _first("source", "CAM")))

    # video codec / bit depth
    add(TagRule("vcodec", _rx(rf"[xh]{S}?264|avc1?"), _first("video_codec", "x264")))
    add(TagRule("vcodec", _rx(rf"[xh]{S}?265|hevc"), _first("video_codec", "x265")))
    add(TagRule("vcodec", _rx(r"av1"), _first("video_codec", "AV1")))
    add(TagRule("vcodec", _rx(r"vp9"), _first("video_codec", "VP9")))
    add(TagRule("vcodec", _rx(r"xvid"), _first("video_codec", "XviD")))
    add(TagRule("vcodec", _rx(r"divx"), _first("video_codec", "DivX")))
    add(TagRule("vcodec", _rx(rf"vc{S}?1"), _first("video_codec", "VC-1")))
    add(TagRule("vcodec", _rx(rf"mpeg{S}?2"), _first("video_codec", "MPEG-2")))
    add(TagRule("bitdepth", _rx(rf"(?P<b>8|10|12){S}?bits?"), _bitdepth))
    add(TagRule("bitdepth", _rx(r"hi10p?"), _bitdepth))

    # HDR
    add(TagRule("hdr", _rx(r"hdr10(?:\+|plus)"), _hdr("HDR10+")))
    add(TagRule("hdr", _rx(r"hdr10"), _hdr("HDR10")))
    add(TagRule("hdr", _rx(r"hdr"), _hdr("HDR10")))
    add(TagRule("hdr", _rx(rf"dolby{S}?vision|dovi"), _hdr("DV")))
    add(TagRule("hdr", _rx(r"hlg"), _hdr("HLG")))

    # audio (most specific first)
    add(TagRule("audio", _rx(rf"dts{S}?hd{S}?ma{CH}"), _audio("DTS-HD MA")))
    add(TagRule("audio", _rx(rf"dts{S}?x{CH}"), _audio("DTS-X")))
    add(TagRule("audio", _rx(rf"dts{S}?hd(?:{S}?hra)?{CH}"), _audio("DTS-HD MA")))
    add(TagRule("audio", _rx(rf"dts(?:{S}?es)?{CH}"), _audio("DTS")))
    add(TagRule("audio", _rx(rf"true{S}?hd{CH}"), _audio("TrueHD")))
    add(TagRule("audio", _rx(rf"(?:ddp|dd\+|e{S}?ac{S}?3|dolby{S}?digital{S}?plus){CH}"), _audio("DDP")))
    add(TagRule("audio", _rx(rf"(?:ac{S}?3|dolby{S}?digital){CH}"), _audio("AC3")))
    add(TagRule("audio", _rx(r"dd(?P<ch>[1-9][ .]?[01])"), _audio("AC3")))
    add(TagRule("audio", _rx(rf"aac(?:{S}?lc)?{CH}"), _audio("AAC")))
    add(TagRule("audio", _rx(rf"flac{CH}"), _audio("FLAC")))
    add(TagRule("audio", _rx(r"mp3"), _audio("MP3")))
    add(TagRule("audio", _rx(rf"l?pcm{CH}"), _audio("PCM")))

    # 3D layouts
    add(TagRule("3d", _rx(rf"h(?:alf)?{S}?sbs|sbs"), _first("s3d", "SBS")))

    # release flags (upper case = scene convention, safe anywhere)
    add(TagRule("proper", _rx(rf"(?:REAL{S})?PROPER", ci=False), _first("proper", True, technical=False)))
    add(TagRule("repack", _rx(r"REPACK\d?|RERIP", ci=False), _first("repack", True, technical=False)))

    # creditless openings / endings are unambiguous extras
    add(TagRule("extra", _rx(rf"nc(?:op|ed)\d{{0,2}}|creditless(?:{S}(?:opening|ending|op|ed))?"), _extra))

    # ---------------------------------------------------------------- weak rules
    add(TagRule("source", _rx(r"web"), _first("source", "WEB-DL"), strong=False))
    add(TagRule("source", _rx(r"TS|TC", ci=False), _first("source", "Telesync"), strong=False))
    add(TagRule("source", _rx(r"CAM", ci=False), _first("source", "CAM"), strong=False))
    add(TagRule("resolution", _rx(r"uhd"), _first("resolution", "2160p"), strong=False))
    add(TagRule("hdr", _rx(r"dv"), _hdr("DV"), strong=False))
    add(TagRule("audio", _rx(rf"opus{CH}"), _audio("Opus"), strong=False))
    add(TagRule("audio", _rx(r"dd"), _audio("AC3"), strong=False))
    add(TagRule("atmos", _rx(r"atmos"), _first("atmos", True), strong=False))
    add(TagRule("channels", _rx(r"(?P<ch>[1-9]\.[01])"), _channels, strong=False))
    add(TagRule("channels", _rx(r"(?P<n>[268])ch"), _channels, strong=False))
    add(TagRule("3d", _rx(rf"h(?:alf)?{S}?(?:tab|ou)|tab|ou"), _first("s3d", "TAB"), strong=False))
    add(TagRule("3d", _rx(r"3d"), _noop, strong=False))
    add(TagRule("proper", _rx(r"proper"), _first("proper", True, technical=False), strong=False))
    add(TagRule("repack", _rx(r"repack\d?|rerip"), _first("repack", True, technical=False), strong=False))
    for word, label in (
        ("extended", "Extended"), ("theatrical", "Theatrical"), ("ultimate", "Ultimate"),
        ("unrated", "Unrated"), ("uncut", "Uncut"), ("uncensored", "Uncensored"),
        ("remaster(?:ed)?", "Remastered"), ("imax", "IMAX"), ("criterion", "Criterion"),
        ("redux", "Redux"), ("restored", "Restored"), ("despecialized", "Despecialized"),
        (rf"final{S}cut", "Final Cut"),
    ):
        add(TagRule("edition", _rx(word), _edition(label), strong=False))
    add(TagRule("edition", _rx(rf"(?:(?P<n>\d{{1,3}})(?:th|st|nd|rd){S})?anniversary"), _edition(), strong=False))
    # Weak: only after the title ("Blade Runner 2049 Environments Reel by DNEG"), never inside it
    # ("Reel Steel", "The Interview 2014").
    add(TagRule("extra", _rx(
        rf"trailers?|teasers?|featurettes?|behind{S}the{S}scenes|deleted{S}scenes?|interviews?"
        rf"|bloopers?|gag{S}reels?|outtakes?|reels?|making{S}of|vfx{S}breakdowns?|b{S}?roll"
        rf"|bonus{S}(?:clips?|features?|material)|alternate{S}(?:endings?|openings?)|screen{S}tests?"
        rf"|storyboards?|promo(?:{S}clips?)?"
    ), _extra_word, strong=False))
    for word, code in _LANG_WORDS.items():
        add(TagRule("language", _rx(word), _language(code), strong=False))
    for word, code in _LANG_CODES.items():
        add(TagRule("language", _rx(word, ci=False), _language(code), strong=False))
    for word, code in _SERVICES.items():
        add(TagRule("service", _rx(word, ci=False), _service(code), strong=False))
    return r


RULES: list[TagRule] = _build_rules()
STRONG_RULES = [rule for rule in RULES if rule.strong]
WEAK_RULES = [rule for rule in RULES if not rule.strong]

# A token that on its own is a tag (used to reject "-DL" of "WEB-DL" as a release group).
TAG_WORDS = frozenset(
    w.lower()
    for w in (
        "dl", "rip", "hd", "ma", "x264", "x265", "h264", "h265", "hevc", "avc", "web", "webrip",
        "webdl", "bluray", "hdtv", "dvdrip", "xvid", "divx", "aac", "ac3", "dts", "ddp", "truehd",
        "atmos", "remux", "proper", "repack", "hdr", "hdr10", "dv", "1080p", "720p", "2160p",
        "480p", "sample", "multi", "subbed", "dubbed", "internal", "limited", "es", "x", "hra",
        "ray", "sbs", "tab", "ou", "1", "2", "3", "4", "5", "6", "7", "8", "9",
    )
)

# Sidecar suffixes (subtitle "flags") that may follow a language code: ".eng.forced.srt"
SUBTITLE_FLAGS = frozenset({"forced", "sdh", "cc", "hi", "default", "full", "signs", "songs", "commentary"})
SIDECAR_EXTENSIONS = frozenset({"srt", "ass", "ssa", "sub", "idx", "sup", "vtt", "smi", "txt", "nfo"})

# 2- and 3-letter subtitle language suffixes -> ISO 639-1
SUBTITLE_LANGS: dict[str, str] = {
    "en": "en", "eng": "en", "de": "de", "ger": "de", "deu": "de", "fr": "fr", "fre": "fr",
    "fra": "fr", "es": "es", "spa": "es", "it": "it", "ita": "it", "ja": "ja", "jpn": "ja",
    "pt": "pt", "por": "pt", "nl": "nl", "dut": "nl", "nld": "nl", "ru": "ru", "rus": "ru",
    "pl": "pl", "pol": "pl", "sv": "sv", "swe": "sv", "no": "no", "nor": "no", "nb": "no",
    "da": "da", "dan": "da", "fi": "fi", "fin": "fi", "ko": "ko", "kor": "ko", "zh": "zh",
    "chi": "zh", "zho": "zh", "ar": "ar", "ara": "ar", "he": "he", "heb": "he", "tr": "tr",
    "tur": "tr", "cs": "cs", "cze": "cs", "ces": "cs", "hu": "hu", "hun": "hu", "el": "el",
    "gre": "el", "ell": "el", "ro": "ro", "rum": "ro", "ron": "ro", "uk": "uk", "ukr": "uk",
    "vi": "vi", "vie": "vi", "id": "id", "ind": "id", "th": "th", "tha": "th", "hi": "hi",
    "hin": "hi", "pt-br": "pt", "zh-cn": "zh", "zh-tw": "zh",
}

VIDEO_EXTENSIONS = frozenset(
    {"mkv", "mp4", "m4v", "avi", "mov", "wmv", "ts", "m2ts", "mts", "webm", "mpg", "mpeg", "flv",
     "ogm", "ogv", "divx", "vob", "iso", "rmvb", "rm", "3gp", "asf", "f4v", "m2v", "wtv"}
)
KNOWN_EXTENSIONS = VIDEO_EXTENSIONS | SIDECAR_EXTENSIONS | frozenset(
    {"jpg", "jpeg", "png", "nfo", "sfv", "md5", "par2", "rar", "zip", "7z", "url", "log", "txt",
     "xml", "json", "mka", "flac", "mp3", "aac", "ac3", "dts", "nzb", "torrent", "part"}
)
