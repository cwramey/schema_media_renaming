"""Deterministic corpus of media paths for the parser golden test and benchmark.

Not a test module: ``test_parser_golden.py`` reads the frozen snapshot built from this corpus,
and ``bench_parser.py`` times the parser over it. The corpus mixes every literal name the
metadata tests already parse, the demo manifest, and generated scene / anime / season-pack /
movie / sidecar / folder-context paths with the awkward cases real libraries contain.
"""

from __future__ import annotations

import ast
import random
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEMO = HERE.parents[1] / "schema_app" / "demo" / "demo_files.txt"

# --------------------------------------------------------------------------- vocabulary

TITLES = [
    "Starfall Station", "Harbor Lights", "The Office US", "Breaking Bad", "Game of Thrones",
    "The Interview", "Reel Steel", "Charlotte's Web", "Final Cut", "Mr. Holland's Opus", "The German",
    "S.W.A.T.", "24", "1917", "2001 A Space Odyssey", "Blade Runner 2049", "The 100", "9-1-1",
    "Room 104", "Catch-22", "Ocean's Eleven", "WALL-E", "Se7en", "Love, Death & Robots",
    "House M.D.", "Marvel's Agents of S.H.I.E.L.D.", "Doctor Who", "Shōgun", "Amélie", "Pokémon",
    "Dark", "Fargo", "Max", "Red", "Web of Lies", "Atmos Fear", "The Extended Family", "Proper Manners",
    "Ultimate Force", "Behind the Scenes", "Trailer Park Boys", "Sample Size", "Part Time Hero",
    "Special Ops", "Season of the Witch", "The Complete Guide", "Episode One", "Disc World",
    "CD Wow", "Cam Girl", "Italian Job", "English Teacher", "Spanish Princess", "Multi Pass",
    "Kaze no Tsubasa", "Shingeki no Kyojin", "One Piece", "Spy x Family", "Mob Psycho 100",
    "Toradora!", "Steins;Gate", "Re:Zero kara Hajimeru Isekai Seikatsu", "Kimi no Na wa",
    "Neon Requiem", "Iron Orchard", "Paper Moons", "The Quiet Meridian", "Night Ferry",
    "İstanbul Hatırası", "Iğdır Günlüğü", "Ålesund", "Straße der Lieder", "Café Society",
    "進撃の巨人", "鬼滅の刃", "Hello (World)", "What If...?", "Who Is America?", "Mission: Impossible",
    "Alien³", "Æon Flux", "The Matrix", "Spider-Man", "X-Men", "4K Wonders", "8K World",
    "HDR Life", "Web Therapy", "DTS Theatre", "Remux City", "The Hdtv Story", "Dolby", "Atmosphere",
]

GROUPS = ["NTb", "FLUX", "KILLERS", "SPARKS", "AMIABLE", "CMRG", "FGT", "FiCO", "GROUP", "CTU",
          "ROVERS", "SYNCOPY", "EDITH", "SPAMnEGGS", "NOGRP", "playWEB", "BTN", "TEPES", "MZABI",
          "QxR", "RARBG", "YTS.MX", "EVO", "TGx", "x0r", "DL", "WEB", "4", "2160p", "D-Z0N3",
          "PSA", "Tigole", "Joy", "iFT", "HONE", "NTG", "SiGMA", "ViSUM", "LOL", "DIMENSION"]
ANIME_GROUPS = ["SubsPlease", "Erai-raws", "HorribleSubs", "Judas", "EMBER", "ASW", "Commie",
                "Coalgirls", "Doki", "FFF", "UTW", "Hatsuyuki", "Ohys-Raws", "Fansub", "Group",
                "Anime Time", "Some-Stuffs", "Kawaii", "Nyanpasu", "SomeRandomGroup", "BlurayDesuYo"]

RES = ["480p", "576p", "720p", "1080p", "1080i", "2160p", "4K", "UHD", "1920x1080", "1280x720",
       "3840x2160", "720x480", "8K", "4320p"]
SOURCES = ["WEB-DL", "WEBRip", "WEB", "BluRay", "Blu-ray", "BDRip", "BRRip", "BD", "HDTV", "PDTV",
           "DVDRip", "DVD", "DVD5", "VHSRip", "HDCAM", "CAM", "TS", "HDTS", "TC", "Remux", "BDRemux",
           "UHD BluRay", "WEB.DL", "DSR", "SATRip", "TVRip", "BD50", "BDMV"]
VCODECS = ["x264", "x265", "H.264", "H 265", "h264", "HEVC", "AVC", "AV1", "VP9", "XviD", "DivX",
           "VC-1", "MPEG2", "10bit", "8bit", "Hi10P", "12 bit"]
AUDIO = ["DDP5.1", "DD5.1", "DD+5.1", "EAC3", "E-AC-3", "AC3", "AAC", "AAC2.0", "AAC 2.0", "TrueHD.7.1",
         "TrueHD Atmos", "DTS", "DTS-HD.MA.5.1", "DTS-HD MA 7.1", "DTS-X", "DTS-ES", "FLAC", "FLAC2.0",
         "MP3", "LPCM", "PCM 2.0", "Opus", "Opus 2.0", "Atmos", "5.1", "7.1", "2.0", "6ch", "2ch",
         "Dolby Digital Plus 5.1", "DD", "DTS-HD HRA"]
HDR = ["HDR", "HDR10", "HDR10+", "HDR10Plus", "DV", "DoVi", "Dolby Vision", "HLG", "DV.HDR10"]
SERVICES = ["AMZN", "NF", "NFLX", "DSNP", "HMAX", "MAX", "ATVP", "HULU", "PCOK", "iT", "CR", "FUNI",
            "iP", "RED", "STAN", "CRAV", "HBO", "VRV", "YT", "SHO", "STZ", "MUBI", "ADN"]
FLAGS = ["PROPER", "REPACK", "REPACK2", "REAL.PROPER", "RERIP", "INTERNAL", "LIMITED", "proper",
         "repack", "iNTERNAL", "READNFO", "DUBBED", "SUBBED", "HC", "COMPLETE"]
LANGS = ["MULTi", "MULTI", "German", "GERMAN", "FRENCH", "TRUEFRENCH", "VOSTFR", "iTALiAN", "ITA",
         "ENG", "SPA", "Spanish", "Latino", "JPN", "Japanese", "KOR", "CHS", "RUS", "PL", "PLDUB",
         "NL", "SWE", "Dutch", "Hindi", "TUR", "VFF", "VF2", "Castellano", "Multilanguage", "ger"]
EDITIONS = ["Directors.Cut", "Director's Cut", "Extended.Cut", "EXTENDED", "Extended Edition",
            "Theatrical.Cut", "THEATRICAL", "UNRATED", "Uncut", "Remastered", "REMASTERED",
            "4K.Remastered", "IMAX", "IMAX.Edition", "Criterion", "Criterion.Collection",
            "Special.Edition", "Collectors.Edition", "Ultimate.Cut", "Open.Matte", "25th.Anniversary.Edition",
            "Anniversary", "Redux", "Despecialized", "Final.Cut", "Alternate.Cut", "Producers.Cut",
            "Rogue.Cut", "Black.and.Chrome", "Uncensored", "Restored"]
S3D = ["3D", "3D.HSBS", "HSBS", "Half-SBS", "SBS", "H-OU", "Half.OU", "TAB", "HTAB"]
EXTRA_WORDS = ["Trailer", "Teaser", "Featurette", "Behind.The.Scenes", "Deleted.Scenes", "Interview",
               "Bloopers", "Gag.Reel", "Outtakes", "Making.Of", "VFX.Breakdown", "B-Roll", "Bonus.Features",
               "Alternate.Ending", "Screen.Test", "Storyboard", "Promo", "Reel", "NCOP", "NCED1",
               "Creditless.Opening", "Sample", "sample"]
EP_TITLES = ["Pilot", "The Long Night", "Felina", "Stress Relief", "Ghosts in the Array", "Christmas Special",
             "The Internet is Coming", "Part 2", "Interview with a Ghost", "The Reel Thing",
             "Deleted Scene", "1080 Degrees", "2049", "Web of Lies", "Season Finale", "Complete",
             "Dia de los Dangerous", "Episode 5", "OVA", "The German", "Trailer Park", "Special",
             "Sample Day", "E.T. Phone Home", "Ça va", "第一話", "Part II", "Chapter Eleven"]
VIDEO_EXT = ["mkv", "mp4", "avi", "m4v", "ts", "m2ts", "wmv", "mov", "webm", "mpg", "iso", "vob",
             "MKV", "Mp4", "rmvb", "flv", "ogm"]
SIDE_EXT = ["srt", "ass", "ssa", "sub", "idx", "sup", "vtt", "nfo", "txt", "jpg", "png", "nzb",
            "torrent", "par2", "rar", "part", "sfv"]
SUB_SUFFIX = ["en", "eng", "de", "ger", "fr", "pt-br", "zh-cn", "es", "ja", "nl", "en.forced",
              "eng.sdh", "forced", "en.hi", "en.cc.forced", "default", "signs", "it.full", "xx",
              "chs", "zh-tw", "ukr", "tr"]
ROOTS = ["", "/media/downloads/", "/config/demo/incoming/", "/data/tv/", "/data/movies/", "/anime/",
         "D:\\TV Shows\\", "D:\\Movies\\", "\\\\nas\\media\\TV\\", "/mnt/user/media/", "C:\\Users\\me\\Downloads\\",
         "/volume1/video/", "/downloads/complete/", "/srv/torrents/seeding/", "/incoming/"]


def _sep_join(rng: random.Random, words: list[str], sep: str | None = None) -> str:
    sep = sep if sep is not None else rng.choice([".", " ", "_", ".", " "])
    out = sep.join(w for w in words if w)
    if sep != " ":
        out = out.replace(" ", sep)
    return out


def _case(rng: random.Random, text: str) -> str:
    roll = rng.random()
    if roll < 0.08:
        return text.lower()
    if roll < 0.12:
        return text.upper()
    return text


def _tags(rng: random.Random, *, k_max: int = 6) -> list[str]:
    pools = [RES, SOURCES, VCODECS, AUDIO, HDR, SERVICES, FLAGS, LANGS, S3D]
    picked: list[str] = []
    for pool in rng.sample(pools, k=rng.randint(0, min(k_max, len(pools)))):
        picked.append(rng.choice(pool))
    if rng.random() < 0.2:
        rng.shuffle(picked)
    return picked


def _group_suffix(rng: random.Random) -> str:
    roll = rng.random()
    if roll < 0.6:
        return "-" + rng.choice(GROUPS)
    if roll < 0.7:
        return " - [" + rng.choice(GROUPS) + "]"
    if roll < 0.8:
        return "[" + rng.choice(["rarbg", "eztv", "ettv", "TGx"]) + "]"
    return ""


def _scene_tv(rng: random.Random) -> str:
    title = _case(rng, rng.choice(TITLES))
    s = rng.randint(0, 25)
    e = rng.randint(0, 120)
    numbering = rng.choice([
        f"S{s:02d}E{e:02d}", f"s{s}e{e}", f"S{s:02d}E{e:02d}E{e + 1:02d}", f"S{s:02d}E{e:02d}-E{e + 2:02d}",
        f"S{s:02d}E{e:02d}-{e + 1:02d}", f"{s}x{e:02d}", f"{s}x{e:02d}-{s}x{e + 1:02d}", f"S{s:02d}.E{e:02d}",
        f"S{s:02d} E{e:02d}", f"Season {s} Episode {e}", f"Series.{s}.Episode.{e}", f"S{s:02d}E{e:02d}v2",
        f"S{s:02d}EP{e:02d}", f"Staffel {s} Folge {e}", f"S{s}", f"Season {s}", f"E{e:02d}", f"Ep.{e}",
        f"Episode {e}", f"{e:02d}", f"{s}{e:02d}", f"Part {rng.randint(1, 6)} of 6", f"{rng.randint(1, 6)}of6",
        f"S{2000 + s}E{e:02d}", f"S{s:02d}E{e:03d}", f"S{s:02d}E{e:02d}+E{e + 1:02d}",
    ])
    words = [title]
    if rng.random() < 0.25:
        words.append(str(rng.choice([2005, 2008, 2016, 2017, 2019, 2021, 2024])))
    if rng.random() < 0.1:
        words.append(rng.choice(["(US)", "(UK)", "US", "UK", "(2019)"]))
    words.append(numbering)
    if rng.random() < 0.45:
        words.append(rng.choice(EP_TITLES))
    if rng.random() < 0.15:
        words.append(rng.choice(EXTRA_WORDS))
    words += _tags(rng)
    sep = rng.choice([".", " ", "_", ".", " - "])
    if sep == " - ":
        name = f"{words[0]} - {numbering}" + (f" - {words[-1]}" if len(words) > 2 else "")
        if rng.random() < 0.5:
            name += " (" + " ".join(_tags(rng, k_max=4)) + ")"
    else:
        name = _sep_join(rng, words, sep)
    return name + _group_suffix(rng) + "." + rng.choice(VIDEO_EXT)


def _crc(rng: random.Random) -> str:
    return "".join(rng.choice("0123456789ABCDEF") for _ in range(8))


def _anime(rng: random.Random) -> str:
    group = rng.choice(ANIME_GROUPS)
    title = rng.choice(TITLES[54:65] + TITLES[73:76] + ["Sora no Kaizoku", "Tensei Robo Gaiden",
                                                        "Wings of the Wind", "Sky Pirates"])
    e = rng.randint(0, 1100)
    numbering = rng.choice([
        f"{e:02d}", f"{e:02d}v2", f"{e:02d}v3", f"S{rng.randint(1, 4)} - {e:02d}", f"S{rng.randint(1, 4)}E{e:02d}",
        f"Season {rng.randint(1, 3)} - {e:02d}", f"{e:02d}-{e + 3:02d}", "OVA", f"OVA {rng.randint(1, 4)}",
        f"OAD{rng.randint(1, 3)}", "ONA", f"SP{rng.randint(1, 3)}", f"NCOP{rng.randint(1, 2)}", "NCED",
        f"OP{rng.randint(1, 3)}", f"ED{rng.randint(1, 3)}v2", f"#{e}", f"第{e}話", f"Ep {e}", f"{e:03d} END",
    ])
    tag_text = rng.choice([
        "[1080p]", "(1080p)", "[720p][Multiple Subtitle]", "[BD 1080p]", "[HEVC x265 10bit]",
        "(BD 1080p HEVC FLAC)", "[1080p][HEVC][AAC]", "(1280x720 Blu-ray FLAC)", "[WEB 1080p AV1 Opus]",
        "[Dual Audio][1080p]", "", "[BDRip 1920x1080 x264 FLAC]", "(TV 720p)", "[MULTi]",
    ])
    crc = rng.choice(["", f" [{_crc(rng)}]", f"[{_crc(rng)}]", f" ({_crc(rng)})", " [12345678]"])
    lead = rng.choice([f"[{group}] ", f"[{group}]_", f"({group}) ", "", f"[{group}][{rng.choice(RES)}] "])
    body = f"{title} - {numbering}" if rng.random() < 0.75 else f"{title} {numbering}"
    if rng.random() < 0.15:
        body += " - " + rng.choice(EP_TITLES)
    name = f"{lead}{body} {tag_text}{crc}".rstrip()
    if lead.endswith("_"):
        name = name.replace(" ", "_")
    if rng.random() < 0.05:
        name = name.replace("[", "【").replace("]", "】")
    return name + "." + rng.choice(["mkv", "mkv", "mp4", "avi"])


def _movie(rng: random.Random) -> str:
    title = _case(rng, rng.choice(TITLES))
    year = rng.choice([1931, 1950, 1968, 1982, 1987, 1999, 2001, 2004, 2012, 2019, 2021, 2023, 2049])
    style = rng.random()
    if style < 0.45:
        words = [title, str(year)]
        if rng.random() < 0.3:
            words.append(rng.choice(EDITIONS))
        if rng.random() < 0.12:
            words.append(rng.choice(["CD1", "CD2", "Disc 1", "Part.2", "pt3", "Part II", "DVD2"]))
        if rng.random() < 0.1:
            words.append(rng.choice(EXTRA_WORDS))
        words += _tags(rng)
        name = _sep_join(rng, words) + _group_suffix(rng)
    elif style < 0.65:
        tags = " ".join(f"[{t}]" for t in _tags(rng, k_max=4))
        name = f"{title} ({year}) {tags}".rstrip()
        if rng.random() < 0.3:
            name += rng.choice([" [YTS.MX]", " - [YTS.AM]", " {tmdb-12345}", " [imdbid-tt0133093]", " {edition-Extended}"])
    elif style < 0.75:
        site = rng.choice(["www.SceneTime.com", "www.Torrenting.com", "[ www.UsaBit.com ]", "ettv.to", "Ink.cc"])
        name = f"{site} - {_sep_join(rng, [title, str(year)] + _tags(rng))}"
    elif style < 0.85:
        name = f"{title}-{rng.choice(['trailer', 'behindthescenes', 'deleted', 'featurette', 'interview', 'scene', 'short', 'other', 'extra'])}"
    else:
        words = [title] + _tags(rng, k_max=3)
        name = _sep_join(rng, words)
    return name + "." + rng.choice(VIDEO_EXT)


def _sidecar(rng: random.Random) -> str:
    base = _scene_tv(rng) if rng.random() < 0.5 else _movie(rng)
    stem = base.rsplit(".", 1)[0]
    ext = rng.choice(SIDE_EXT)
    if ext in {"srt", "ass", "ssa", "sub", "idx", "sup", "vtt"} and rng.random() < 0.7:
        stem += "." + rng.choice(SUB_SUFFIX)
    return stem + "." + ext


def _pack_folder(rng: random.Random) -> str:
    title = rng.choice(TITLES)
    s = rng.randint(1, 12)
    return rng.choice([
        _sep_join(rng, [title, f"S{s:02d}"] + _tags(rng)) + _group_suffix(rng),
        f"{title} Season {s} Complete " + " ".join(_tags(rng, k_max=3)),
        _sep_join(rng, [title, "Complete", "Series"] + _tags(rng)) + _group_suffix(rng),
        f"{title} (2019) Season {s} S{s:02d} (1080p BluRay x265 HEVC 10bit AAC 5.1 Tigole)",
        f"[{rng.choice(ANIME_GROUPS)}] {title} (Season {s}) [BD 1080p]",
        f"{title} Collection",
        _sep_join(rng, [title, f"S{s:02d}E01-E10"] + _tags(rng)),
    ])


def _folder_path(rng: random.Random, filename: str) -> str:
    root = rng.choice(ROOTS)
    win = "\\" in root
    sep = "\\" if win else "/"
    title = rng.choice(TITLES)
    year = rng.choice(["", " (2019)", " (2005)", " [2021]", " 2008"])
    s = rng.randint(0, 15)
    layouts = [
        [f"{title}{year}", f"Season {s}"],
        [f"{title}{year}", f"Season {s:02d}"],
        [f"{title}", f"S{s:02d}"],
        [f"{title}", rng.choice(["Specials", "Season 0", "Season 00", "SP"])],
        [f"{title}{year}"],
        [f"{title}{year}", rng.choice(["Featurettes", "Extras", "Behind The Scenes", "Trailers", "Other"])],
        [f"{title}{year}", rng.choice(["Sample", "samples"])],
        [f"{title}{year}", rng.choice(["VIDEO_TS", "BDMV", "BDMV/STREAM"])],
        [f"{title}{year}", rng.choice(["CD1", "CD 2", "Disc 1", "DVD2"])],
        [_pack_folder(rng)],
        [_pack_folder(rng), f"Season {s}"],
        [rng.choice(["Movies", "TV Shows", "Anime", "tv", "films", "Documentaries", "complete", "Downloads"]),
         f"{title}{year}"],
        [rng.choice(["Movies", "TV", "Anime"]), f"{title}", f"Staffel {s}"],
        [f"{title}", rng.choice(["Saison 2", "Temporada 3", "Series 4", "Season 2 (2019)", "season_3", "Season.1"])],
        [rng.choice(["1080p", "x265", "4K", "tmp", "_unpack", "qbittorrent", "Plex"]), _pack_folder(rng)],
        [_sep_join(rng, [title, "2019"] + _tags(rng)) + _group_suffix(rng)],
        [rng.choice(["0123456789abcdef0123", "VTS_01_1", "12345", "abcdefABCDEF1234567890"])],
        [f"{title} - {rng.choice(EP_TITLES)}", f"Season {s}"],
        [rng.choice(["A", "B", "..", ".", "deep", "deeper", "deepest"]), f"{title}{year}", f"Season {s}", "x"],
    ]
    folders = rng.choice(layouts)
    if win:
        folders = [f.replace("/", "\\") for f in folders]
    return root + sep.join(folders) + sep + filename


def _odd(rng: random.Random) -> str:
    return rng.choice([
        "", ".", "..", "[]", "()", "-", "   ", "S01E", "[Group]", "x" * 300, "🎬.mkv", "a/b/c/",
        "VTS_01_1.VOB", "VIDEO_TS.IFO", "00001.m2ts", "BDMV/STREAM/00001.m2ts", "12345.mkv",
        "0123456789abcdef0123.mkv", "Movie.mkv.part", "Movie.2019.mkv.!qB", "Show.S01E01.mkv.txt",
        "no_extension_name", "trailing.dot.", ".hidden.mkv", "UPPER.CASE.S01E01.MKV", "Episode 5.mkv",
        "01.mkv", "01 - Pilot.mkv", "2019.mkv", "1080p.mkv", "Season 1.mkv", "Show - 2019 - 05.mkv",
        "Show.2019.S2019E05.mkv", "Show (2019) (2020).mkv", "[ABCDEF12].mkv", "[2019] Title.mkv",
        "İSTANBUL.İtalian.ENG.mkv", "ıtalian.job.2003.mkv", "ſpanish.Show.S01E01.mkv", "Kelvin.K.mkv",
        "Title.２０１９.１０８０ｐ.mkv", "Title【1080p】「Group」.mkv", "Ｓ０１Ｅ０２.mkv", "Show.S01E01.٣.mkv",
        "Title ٢٠١٩ ١٠٨٠p.mkv", "Show.S01E01.Ⅱ.mkv", "Movie (US) [UK] (JP).mkv", "Show.US.S01E01.mkv",
        "a.b.c.d.e.f.g.h.mkv", "S.W.A.T.S01E01.mkv", "Mr.Robot.S01E01.mkv", "Movie 3D 2012 HOU.mkv",
        "Movie.2012.Hi10P.mkv", "Show.S01E01.1080p.DD.mkv", "Show.S01E01.DV.mkv", "Show S01E01 TS.mkv",
        "Show.S01E01.WEB.H264-NoGroup.mkv", "Show.S01E01-1080p.mkv", "Show.S01E01-x264.mkv",
        "Show.S01E01-DL.mkv", "Show.S01E01-9.mkv", "Show - 1999.mkv", "Show - 2041.mkv", "Show - 1919.mkv",
        "Show 第12話.mp4", "Show #5.mkv", "Show Ep5.mkv", "Show e05.mkv", "Show.E05.mkv", "Show.S01.mkv",
        "Show Season 2 Episode 3 to 5.mkv", "Show Season 1 Episode 2 and Episode 3.mkv",
        "Show - 12 ~ 14 [1080p].mkv", "Show - 05-06.mkv", "Show 4th of July 2020.mkv",
        "Show.Jan.32.2020.mkv", "Show.2020.13.01.mkv", "Show.31.02.2020.mkv", "Show.19991231.mkv",
        "Show.29.02.2024.mkv", "Show.2023.02.29.mkv", "The.Show.2019.11.12.HDTV.mkv",
    ])


def generated(seed: int, count: int) -> list[str]:
    """``count`` generated paths (deterministic for a given seed)."""
    rng = random.Random(seed)
    makers = [(_scene_tv, 30), (_anime, 18), (_movie, 25), (_sidecar, 8), (_odd, 4)]
    total = sum(w for _f, w in makers)
    out: list[str] = []
    for _ in range(count):
        roll = rng.uniform(0, total)
        chosen = makers[-1][0]
        for maker, weight in makers:
            roll -= weight
            if roll <= 0:
                chosen = maker
                break
        name = chosen(rng)
        if rng.random() < 0.45:
            name = _folder_path(rng, name)
        elif rng.random() < 0.3:
            name = rng.choice(ROOTS) + name
        out.append(name)
    return out


def literal_test_paths() -> list[str]:
    """Every literal path the metadata tests hand to ``parse`` (CASES tables included)."""
    found: list[str] = []
    for path in sorted(HERE.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "parse":
                arg = node.args[0] if node.args else None
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found.append(arg.value)
            if isinstance(node, ast.Tuple) and node.elts and isinstance(node.elts[0], ast.Constant):
                first = node.elts[0].value
                if isinstance(first, str) and len(node.elts) == 2 and isinstance(node.elts[1], ast.Dict):
                    found.append(first)
    return found


def demo_paths() -> list[str]:
    rows = []
    for line in DEMO.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        rel = line.split("|", 1)[0].strip().replace("{crc32}", "1A2B3C4D")
        rows.append("/config/demo/incoming/" + rel)
    return rows


def golden_corpus() -> list[str]:
    """The frozen golden set: test literals + demo + 700 generated paths, de-duplicated in order."""
    names = literal_test_paths() + demo_paths() + generated(seed=20261125, count=700)
    return list(dict.fromkeys(names))


def bench_corpus(count: int = 5000) -> list[str]:
    """A larger mix for timing (includes the golden set)."""
    return list(dict.fromkeys(golden_corpus() + generated(seed=7, count=count)))
