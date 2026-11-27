"""Table-driven tests for core.parser.parse (SPEC §6.1, §12: >= 120 filenames)."""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path, PureWindowsPath

import pytest

from schema_app.core.parser import parse
from schema_app.domain import MediaKind

M, S, A, U = MediaKind.MOVIE, MediaKind.SERIES, MediaKind.ANIME, MediaKind.UNKNOWN

# (path, expected subset). Keys are ParsedMedia fields; "q.<field>" checks QualityInfo.
CASES: list[tuple[str, dict]] = [
    # ------------------------------------------------------------ scene TV
    ("The.Office.US.S05E14.Stress.Relief.720p.HDTV.x264-CTU.mkv",
     {"kind": S, "title": "The Office US", "season": 5, "episodes": [14], "release_group": "CTU",
      "q.resolution": "720p", "q.source": "HDTV", "q.video_codec": "x264"}),
    ("Breaking.Bad.S05E16.Felina.1080p.BluRay.x264-ROVERS.mkv",
     {"kind": S, "title": "Breaking Bad", "season": 5, "episodes": [16], "q.source": "BluRay"}),
    ("Game.of.Thrones.S08E03.The.Long.Night.2160p.HMAX.WEB-DL.DDP5.1.Atmos.DV.HDR10.H.265-FLUX.mkv",
     {"title": "Game of Thrones", "season": 8, "episodes": [3], "q.resolution": "2160p",
      "q.streaming_service": "HMAX", "q.audio_codec": "DDP", "q.audio_channels": "5.1",
      "q.atmos": True, "q.hdr": ["DV", "HDR10"], "q.video_codec": "x265", "release_group": "FLUX"}),
    ("Dark.S01E01-E02.German.1080p.NF.WEBRip.x264-GRP.mkv",
     {"title": "Dark", "episodes": [1, 2], "languages": ["de"], "q.source": "WEBRip",
      "q.streaming_service": "NF"}),
    ("show.s02e05-06.hdtv.mkv", {"title": "Show", "season": 2, "episodes": [5, 6]}),
    ("Show.Name.S01E01E02E03.mkv", {"episodes": [1, 2, 3]}),
    ("Show.Name.S01E01-E04.mkv", {"episodes": [1, 2, 3, 4]}),
    ("Show.Name.S01E05v2.mkv", {"episodes": [5], "version": 2}),
    ("Show.Name.S01.E02.720p.mkv", {"season": 1, "episodes": [2]}),
    ("Show Name S01 E02 720p.mkv", {"title": "Show Name", "season": 1, "episodes": [2]}),
    ("Show_Name_S03E04_720p.mkv", {"title": "Show Name", "season": 3, "episodes": [4]}),
    ("The.100.S03E01.mkv", {"title": "The 100", "season": 3, "episodes": [1]}),
    ("24.S01E01.mkv", {"title": "24", "season": 1, "episodes": [1]}),
    ("S.W.A.T.2017.S04E01.720p.HDTV.x264-SYNCOPY.mkv",
     {"title": "S.W.A.T.", "year": 2017, "season": 4, "episodes": [1]}),
    ("Marvels.Agents.of.S.H.I.E.L.D.S01E01.mkv", {"title": "Marvels Agents of S.H.I.E.L.D."}),
    ("Trailer.Park.Boys.S01E01.mkv", {"title": "Trailer Park Boys", "is_extra": False}),
    ("Show.S01E01.720p.HDTV.x264-GRP[rarbg].mkv", {"release_group": "GRP", "season": 1}),
    ("Show.S01E01.ITA.ENG.1080p.mkv", {"languages": ["it", "en"]}),
    ("Show.Name.S01E01.MULTi.1080p.mkv", {"languages": ["mul"]}),
    ("Show (2019) - S01E01 - Pilot (1080p).mkv", {"title": "Show", "year": 2019, "tokens": []}),
    ("Show.S2024E04.mkv", {"season": 2024, "episodes": [4], "kind": S}),
    # ------------------------------------------------------------ NxNN / words
    ("The Simpsons - 10x05 - When You Dish Upon a Star.avi",
     {"kind": S, "title": "The Simpsons", "season": 10, "episodes": [5]}),
    ("doctor_who_2005.7x01-7x02.hdtv.mkv",
     {"title": "Doctor Who", "year": 2005, "season": 7, "episodes": [1, 2]}),
    ("Show.Name.1x01x02.mkv", {"season": 1, "episodes": [1, 2]}),
    ("Show.Name.2x5.mkv", {"season": 2, "episodes": [5]}),
    ("Doctor.Who.Series.7.Episode.1.mkv", {"title": "Doctor Who", "season": 7, "episodes": [1]}),
    ("Season 2 Episode 4 - Something.mkv", {"kind": S, "title": None, "season": 2, "episodes": [4]}),
    ("Show Season 1 Episode 2-3.mkv", {"season": 1, "episodes": [2, 3]}),
    ("Some Show S01 1080p BluRay x264-GRP.mkv", {"kind": S, "title": "Some Show", "season": 1, "episodes": []}),
    # ------------------------------------------------------------ daily
    ("The.Daily.Show.2024-03-04.Guest.Name.720p.WEB.h264-EDITH.mkv",
     {"kind": S, "title": "The Daily Show", "air_date": date(2024, 3, 4), "year": None}),
    ("Jimmy.Kimmel.Live.04.03.2024.720p.mkv", {"air_date": date(2024, 3, 4)}),
    ("Late.Show.2023.12.31.mkv", {"air_date": date(2023, 12, 31), "title": "Late Show"}),
    ("News 2024 01 15.mkv", {"air_date": date(2024, 1, 15)}),
    ("Show.2024.02.30.mkv", {"air_date": None}),  # impossible date is not a date
    ("Evening.News.20240115.mkv", {"air_date": date(2024, 1, 15), "kind": S}),
    ("Talk Show - 4th March 2024.mkv", {"air_date": date(2024, 3, 4)}),
    ("Talk.Show.Mar.4.2024.mkv", {"air_date": date(2024, 3, 4), "title": "Talk Show"}),
    ("Talk.Show.2024.Mar.04.mkv", {"air_date": date(2024, 3, 4)}),
    ("Planet.Story.Part.2.of.6.720p.mkv", {"title": "Planet Story", "absolute": [2]}),
    ("Planet Story 3of6.mkv", {"absolute": [3]}),
    # ------------------------------------------------------------ anime fansub
    ("[HorribleSubs] One Piece - 1000 [1080p].mkv",
     {"kind": A, "title": "One Piece", "absolute": [1000], "release_group": "HorribleSubs"}),
    ("[Erai-raws] Shingeki no Kyojin - The Final Season - 28 [1080p][Multiple Subtitle][ABCDEF12].mkv",
     {"kind": A, "title": "Shingeki no Kyojin - The Final Season", "absolute": [28], "crc32": "ABCDEF12"}),
    ("[Group] Mob Psycho 100 - 05 [720p].mkv", {"title": "Mob Psycho 100", "absolute": [5]}),
    ("[EMBER] Spy x Family S2 - 05 [1080p].mkv", {"kind": A, "title": "Spy x Family", "season": 2, "episodes": [5]}),
    ("[Coalgirls]_Toradora!_01_(1280x720_Blu-ray_FLAC)_[0FCC2B20].mkv",
     {"kind": A, "title": "Toradora!", "absolute": [1], "crc32": "0FCC2B20", "q.resolution": "720p",
      "q.audio_codec": "FLAC", "q.source": "BluRay"}),
    ("[SubsPlease] Frieren - 01-04 (1080p) [Batch].mkv", {"absolute": [1, 2, 3, 4]}),
    ("[Group] Title - NCOP1 [1080p].mkv", {"is_extra": True}),
    ("[Group] Title - ED2 [1080p].mkv", {"is_extra": True}),
    ("[Group] Title - OVA 2 [720p].mkv", {"kind": A, "special": True, "season": 0, "episodes": [2]}),
    ("[Group] Title (2019) - 05 [1080p].mkv", {"title": "Title", "year": 2019, "absolute": [5]}),
    ("[Group] Title 05 [720p].mkv", {"kind": A, "title": "Title", "absolute": [5]}),
    ("[Group] Title [05][720p].mkv", {"kind": A, "absolute": [5]}),
    ("[Group] Title - 2019 [1080p].mkv", {"absolute": []}),  # year-like numbers are not episodes
    ("[Judas] Title - 1080 [1080p].mkv", {"absolute": [1080]}),
    ("Title SP1.mkv", {"special": True, "season": 0, "episodes": [1]}),
    ("Naruto Shippuden EP15.mkv", {"title": "Naruto Shippuden", "absolute": [15]}),
    ("Bleach #120.mkv", {"title": "Bleach", "absolute": [120]}),
    ("進撃の巨人 第15話.mkv", {"title": "進撃の巨人", "absolute": [15]}),
    ("【Group】 Title - 03 【1080p】.mkv", {"kind": A, "release_group": "Group", "absolute": [3]}),
    ("[Group] Title - 05 [1080p][ABCD1234].mkv", {"crc32": "ABCD1234", "absolute": [5]}),
    ("[SubsPlease] Title - 15 (1080p) [12345678].mkv", {"crc32": "12345678", "absolute": [15], "air_date": None}),
    ("[Group] Title - 07 [deadbeef].mkv", {"crc32": "DEADBEEF"}),
    ("[Group] Title - 12v3 [720p].mkv", {"absolute": [12], "version": 3}),
    ("Title - 05 (BD 1080p HEVC 10bit FLAC).mkv",
     {"absolute": [5], "q.bit_depth": 10, "q.video_codec": "x265", "q.source": "BluRay"}),
    # ------------------------------------------------------------ movies
    ("2001.A.Space.Odyssey.1968.1080p.BluRay.x264.mkv",
     {"kind": M, "title": "2001 A Space Odyssey", "year": 1968}),
    ("Blade.Runner.2049.2017.2160p.UHD.BluRay.REMUX.HDR.HEVC.Atmos-EPSiLON.mkv",
     {"title": "Blade Runner 2049", "year": 2017, "q.remux": True, "q.resolution": "2160p",
      "release_group": "EPSiLON"}),
    ("1917.2019.1080p.mkv", {"kind": M, "title": "1917", "year": 2019}),
    ("2012.2009.1080p.BluRay.mkv", {"title": "2012", "year": 2009}),
    ("Mission Impossible - Fallout (2018).mkv",
     {"kind": M, "title": "Mission Impossible - Fallout", "year": 2018, "tokens": []}),
    ("Spider-Man.Far.From.Home.2019.1080p.WEBRip.x264-RARBG.mp4",
     {"title": "Spider-Man Far From Home", "year": 2019, "release_group": "RARBG"}),
    ("Harry.Potter.and.the.Deathly.Hallows.Part.2.2011.1080p.BluRay.x264.mkv",
     {"title": "Harry Potter and the Deathly Hallows Part 2", "year": 2011, "part": None}),
    ("Kill.Bill.Vol.1.2003.1080p.mkv", {"title": "Kill Bill Vol 1", "year": 2003}),
    ("Title.2019.mkv", {"kind": M, "title": "Title", "year": 2019}),
    ("Ocean's Eleven (2001).mkv", {"title": "Ocean's Eleven", "year": 2001}),
    ("Amélie (2001).mkv", {"title": "Amélie", "year": 2001}),
    ("Charlottes.Web.2006.1080p.WEB.mkv", {"title": "Charlottes Web", "q.source": "WEB-DL"}),
    ("Mr.Hollands.Opus.1995.1080p.mkv", {"title": "Mr Hollands Opus", "q.audio_codec": None}),
    ("The.German.2006.720p.mkv", {"title": "The German", "languages": []}),
    ("Movie Name.mkv", {"title": "Movie Name", "year": None}),
    ("Movie.Title.1080p.BluRay.x264-GRP.mkv", {"title": "Movie Title", "year": None, "release_group": "GRP"}),
    ("www.Torrenting.com - Movie.Name.2016.720p.mkv", {"title": "Movie Name", "year": 2016}),
    ("[ www.Speed.cd ] - The.Movie.2014.1080p.mkv", {"title": "The Movie", "year": 2014}),
    ("Movie.Name.{tmdb-12345}.2019.mkv", {"title": "Movie Name", "year": 2019}),
    # ------------------------------------------------------------ editions
    ("Avatar.2009.EXTENDED.1080p.BluRay.x264-SPARKS.mkv", {"edition": "Extended"}),
    ("Aliens.1986.Special.Edition.1080p.mkv", {"edition": "Special Edition"}),
    ("Blade.Runner.1982.The.Final.Cut.1080p.mkv", {"edition": "Final Cut", "title": "Blade Runner"}),
    ("Apocalypse.Now.1979.Redux.720p.mkv", {"edition": "Redux"}),
    ("Jaws.1975.25th.Anniversary.Edition.DVDRip.mkv", {"edition": "25th Anniversary Edition", "q.source": "DVD"}),
    ("The.Wizard.of.Oz.1939.Open.Matte.1080p.mkv", {"edition": "Open Matte"}),
    ("Tenet.2020.IMAX.2160p.mkv", {"edition": "IMAX"}),
    ("Movie.Title.2010.UNRATED.1080p.mkv", {"edition": "Unrated"}),
    ("Movie.2010.Theatrical.Cut.1080p.mkv", {"edition": "Theatrical"}),
    ("Movie.2010.Ultimate.Edition.1080p.mkv", {"edition": "Ultimate"}),
    ("Movie.2010.Uncut.1080p.mkv", {"edition": "Uncut"}),
    ("Movie.2010.4K.Remastered.2160p.mkv", {"edition": "Remastered", "q.resolution": "2160p"}),
    ("Movie.2010.Criterion.Collection.1080p.mkv", {"edition": "Criterion"}),
    ("The Ultimate Gift (2006).mkv", {"title": "The Ultimate Gift", "edition": None}),
    # ------------------------------------------------------------ quality vocabulary
    ("Movie.2018.1080p.BluRay.DTS-HD.MA.5.1.x264-GRP.mkv", {"q.audio_codec": "DTS-HD MA", "q.audio_channels": "5.1"}),
    ("Movie.2018.1080p.BluRay.DTS-X.7.1-GRP.mkv", {"q.audio_codec": "DTS-X", "q.audio_channels": "7.1"}),
    ("Movie.2018.1080p.BluRay.DTS.mkv", {"q.audio_codec": "DTS"}),
    ("Movie.2018.720p.WEB-DL.AAC2.0.H.264-GRP.mkv", {"q.audio_codec": "AAC", "q.audio_channels": "2.0"}),
    ("Movie.2018.1080p.BluRay.AC3.5.1.x264-GRP.mkv", {"q.audio_codec": "AC3", "q.audio_channels": "5.1"}),
    ("Movie.2018.1080p.WEB-DL.DD5.1.H.264-GRP.mkv", {"q.audio_codec": "AC3", "q.audio_channels": "5.1"}),
    ("Movie.2018.1080p.WEB-DL.DD+5.1.H.264-GRP.mkv", {"q.audio_codec": "DDP"}),
    ("Movie.2018.1080p.WEB-DL.EAC3.6ch.mkv", {"q.audio_codec": "DDP", "q.audio_channels": "5.1"}),
    ("Movie.2018.DVDRip.XviD.MP3-GRP.avi", {"q.video_codec": "XviD", "q.audio_codec": "MP3", "q.source": "DVD"}),
    ("Movie.2018.DivX.mkv", {"q.video_codec": "DivX"}),
    ("Movie.2018.1080p.BluRay.VC-1.mkv", {"q.video_codec": "VC-1"}),
    ("Movie.2018.MPEG-2.mkv", {"q.video_codec": "MPEG-2"}),
    ("Movie.2018.1080p.AV1.Opus.mkv", {"q.video_codec": "AV1", "q.audio_codec": "Opus"}),
    ("Movie.2018.1080p.WEB.VP9.mkv", {"q.video_codec": "VP9"}),
    ("Movie.2018.1080p.BluRay.FLAC.2.0.x264.mkv", {"q.audio_codec": "FLAC", "q.audio_channels": "2.0"}),
    ("Movie.2018.1080p.BluRay.TrueHD.Atmos.7.1.mkv", {"q.audio_codec": "TrueHD", "q.atmos": True, "q.audio_channels": "7.1"}),
    ("Movie.2018.HDTS.x264.mkv", {"q.source": "Telesync"}),
    ("Movie.2018.TS.mkv", {"q.source": "Telesync"}),
    ("Movie.2018.CAMRip.mkv", {"q.source": "CAM"}),
    ("Movie.2018.SDTV.mkv", {"q.source": "SDTV"}),
    ("Movie.2018.PDTV.mkv", {"q.source": "SDTV"}),
    ("Movie.2018.VHSRip.mkv", {"q.source": "VHS"}),
    ("Movie.2018.BDRip.mkv", {"q.source": "BluRay"}),
    ("Movie.2018.BRRip.mkv", {"q.source": "BluRay"}),
    ("Movie.2018.Blu-ray.mkv", {"q.source": "BluRay"}),
    ("Movie.2018.WEB-Rip.mkv", {"q.source": "WEBRip"}),
    ("Movie.2018.WEBDL.mkv", {"q.source": "WEB-DL"}),
    ("Movie.2018.REPACK.1080p.BluRay.x264-GRP.mkv", {"q.repack": True, "q.proper": False}),
    ("Movie.2018.RERIP.mkv", {"q.repack": True}),
    ("Movie.2018.PROPER.mkv", {"q.proper": True}),
    ("Movie.2018.HDR10Plus.2160p.mkv", {"q.hdr": ["HDR10+"]}),
    ("Movie.2018.2160p.WEB-DL.DoVi.HDR10+.HEVC.mkv", {"q.hdr": ["DV", "HDR10+"]}),
    ("Movie.2018.2160p.Dolby.Vision.mkv", {"q.hdr": ["DV"]}),
    ("Movie.2018.2160p.HLG.mkv", {"q.hdr": ["HLG"]}),
    ("Movie.2018.1080p.Hi10P.mkv", {"q.bit_depth": 10}),
    ("Movie.2018.2160p.12bit.mkv", {"q.bit_depth": 12}),
    ("Movie.2018.1920x1080.mkv", {"q.resolution": "1080p"}),
    ("Movie.2018.4K.mkv", {"q.resolution": "2160p"}),
    ("Movie.2018.576p.mkv", {"q.resolution": "576p"}),
    ("Movie.2018.1080i.HDTV.mkv", {"q.resolution": "1080p", "q.source": "HDTV"}),
    ("Movie.3D.2012.1080p.BluRay.Half-SBS.x264.mkv", {"q.s3d": "SBS"}),
    ("Movie.2012.1080p.BluRay.3D.HOU.x264.mkv", {"q.s3d": "TAB"}),
    ("Movie.2019.1080p.AMZN.WEB-DL.DDP5.1.H.264-NTG.mkv", {"q.streaming_service": "AMZN"}),
    ("Movie.2019.1080p.DSNP.WEB-DL.mkv", {"q.streaming_service": "DSNP"}),
    ("Movie.2019.1080p.ATVP.WEB-DL.mkv", {"q.streaming_service": "ATVP"}),
    ("Movie.2019.1080p.PCOK.WEB-DL.mkv", {"q.streaming_service": "PCOK"}),
    ("Movie.2019.1080p.HULU.WEB-DL.mkv", {"q.streaming_service": "HULU"}),
    ("Mad.Max.Fury.Road.2015.1080p.mkv", {"title": "Mad Max Fury Road", "q.streaming_service": None}),
    ("Les.Miserables.2012.MULTi.TRUEFRENCH.1080p.BluRay.x264-LOST.mkv", {"languages": ["mul", "fr"]}),
    ("Movie.2015.VOSTFR.720p.mkv", {"languages": ["fr"]}),
    ("Movie.2015.GERMAN.DL.1080p.mkv", {"languages": ["de"]}),
    # ------------------------------------------------------------ parts
    ("Movie.Title.2019.CD2.mkv", {"part": 2, "kind": M}),
    ("Movie.Title.2019.Disc.1.mkv", {"part": 1}),
    ("Movie.Title.2019.pt2.mkv", {"part": 2}),
    ("Movie.2019.Part.2.1080p.mkv", {"part": 2, "title": "Movie"}),
    ("Movie.2019.Part.II.1080p.mkv", {"part": 2}),
    # ------------------------------------------------------------ specials
    ("Show.Name.S00E05.Behind.the.Story.mkv",
     {"special": True, "season": 0, "episodes": [5], "special_title": "Behind the Story", "is_extra": False}),
    ("Show - Christmas Special.mkv", {"special": True, "special_title": "Christmas Special", "title": "Show"}),
    ("Kaze no Tsubasa - OVA [BD 1080p].mkv", {"kind": A, "special": True, "episodes": []}),
    ("Title - OAD.mkv", {"special": True}),
    ("Title - ONA 3.mkv", {"special": True, "season": 0, "episodes": [3]}),
    # ------------------------------------------------------------ clutter
    ("Movie.2019.Trailer.1080p.mkv", {"is_extra": True}),
    ("Movie (2019)-trailer.mkv", {"is_extra": True, "title": "Movie", "year": 2019}),
    ("Movie.2019.Deleted.Scenes.mkv", {"is_extra": True}),
    ("Movie.2019.Featurette.mkv", {"is_extra": True}),
    ("Movie.2019.Interview.mkv", {"is_extra": True}),
    # bonus clips that used to be matched to the feature and collide with it (bug 2)
    ("Blade Runner 2049 Environments Reel by DNEG.mkv", {"is_extra": True, "title": "Blade Runner"}),
    ("Alien (1979) Making Of.mkv", {"is_extra": True, "title": "Alien", "year": 1979}),
    ("Movie.2010.Gag.Reel.mkv", {"is_extra": True}),
    ("Movie.2010.Blooper.mkv", {"is_extra": True}),
    ("Movie.2010.VFX.Breakdown.mkv", {"is_extra": True}),
    # ... but the words never count inside a title or as an episode title
    ("Reel Steel 2011.mkv", {"is_extra": False, "title": "Reel Steel"}),
    ("The Interview 2014 1080p.mkv", {"is_extra": False, "title": "The Interview"}),
    ("The Office S02E03 The Interview.mkv", {"is_extra": False, "episodes": [3]}),
    ("Show.S01E02.The.Reel.Deal.720p.mkv", {"is_extra": False, "episodes": [2]}),
    ("Movie.2019.1080p.BluRay.x264-GRP-sample.mkv", {"is_sample": True, "title": "Movie"}),
    ("sample-movie.2019.mkv", {"is_sample": True, "title": "Movie", "year": 2019}),
    ("Movie.2019.SAMPLE.mkv", {"is_sample": True}),
    # ------------------------------------------------------------ sidecars / extensions
    ("Show.Name.S01E01.1080p.WEB.H264-GLHF.en.forced.srt",
     {"extension": "srt", "languages": ["en"], "release_group": "GLHF", "episodes": [1]}),
    ("Show.Name.S01E01.1080p.WEB.H264-GLHF.eng.sdh.srt", {"languages": ["en"]}),
    ("Movie.2019.1080p.de.ass", {"languages": ["de"], "extension": "ass"}),
    ("Movie.2019.mp4", {"extension": "mp4", "year": 2019}),
    ("Movie.2019.MKV", {"extension": "mkv"}),
    ("Show.Name.S01E01", {"extension": "", "season": 1, "episodes": [1]}),
    ("notes.txt", {"extension": "txt"}),
    # ------------------------------------------------------------ folder context
    ("/tv/Show Name/Season 2/Show Name - 05.mkv", {"kind": S, "season": 2, "episodes": [5]}),
    ("/tv/Show Name/Season 2/205.mkv", {"title": "Show Name", "season": 2, "episodes": [5]}),
    ("/tv/Show Name/Season 2/0205 - Title.mkv", {"season": 2, "episodes": [5]}),
    ("/tv/Show Name/Specials/Show Name - Christmas.mkv",
     {"season": 0, "special": True, "special_title": "Christmas"}),
    ("/tv/Show Name/S01/E05.mkv", {"title": "Show Name", "season": 1, "episodes": [5]}),
    ("/tv/Show Name/Season 1/Extras/Bloopers.mkv", {"is_extra": True}),
    ("/movies/The Quiet Meridian (2019)/tqm-1080p.mkv",
     {"year": 2019, "alt_titles": ["The Quiet Meridian"], "folder_title": "The Quiet Meridian"}),
    ("/movies/Movie Name (2010)/VIDEO_TS/VTS_01_1.VOB", {"title": "Movie Name", "year": 2010, "kind": M}),
    ("/movies/Movie Name (2010)/BDMV/STREAM/00001.m2ts", {"title": "Movie Name", "year": 2010}),
    ("/movies/Movie Name (2010)/CD1/movie.avi", {"part": 1, "year": 2010}),
    ("/downloads/complete/Some.Movie.2019.1080p.BluRay.x264-GRP/a8f9e0c1b2d3e4f5a6b7.mkv",
     {"title": "Some Movie", "year": 2019, "kind": M}),
    ("/downloads/Some.Movie.2019.1080p.BluRay.x264-GRP/Sample/some.movie.sample.mkv",
     {"is_sample": True, "title": "Some Movie"}),
    ("/anime/Kaze no Tsubasa/Season 2/[Group] Kaze no Tsubasa - 03.mkv",
     {"kind": A, "season": 2, "episodes": [3], "absolute": []}),
    (r"C:\Media\TV\Harbor Lights (2008)\Season 02\Harbor Lights - S02E05.mkv",
     {"title": "Harbor Lights", "year": 2008, "season": 2, "episodes": [5]}),
    ("/storage/Anime/Tensei Robo Gaiden/Tensei Robo Gaiden - 07.mkv", {"kind": A, "absolute": [7]}),
    ("/tv/Show.Name.S03.1080p.BluRay.x264-GRP/Show.Name.S03E02.1080p.BluRay.x264-GRP.mkv",
     {"title": "Show Name", "season": 3, "episodes": [2], "alt_titles": []}),
    ("/tv/Some Show/Season 1/01 Pilot.mkv", {"title": "Some Show", "season": 1, "episodes": [1]}),
    ("/downloads/incoming/Movie.2019.1080p.mkv", {"folder_title": None}),
    ("/tv/Show/Season 1 (2016)/Show.S01E02.mkv", {"season": 1, "episodes": [2]}),
    ("/media/Staffel 3/Show.E05.mkv", {"season": 3, "episodes": [5]}),
]


def _check(parsed, expected: dict) -> None:
    for key, want in expected.items():
        got = getattr(parsed.quality, key[2:]) if key.startswith("q.") else getattr(parsed, key)
        assert got == want, f"{parsed.filename}: {key} = {got!r}, expected {want!r}"


@pytest.mark.parametrize(("path", "expected"), CASES, ids=[c[0][-60:] for c in CASES])
def test_parse_table(path: str, expected: dict) -> None:
    _check(parse(path), expected)


def test_case_count_meets_spec_bar() -> None:
    assert len(CASES) >= 120


# ------------------------------------------------------------ demo manifest

DEMO = Path(__file__).resolve().parents[2] / "schema_app" / "demo" / "demo_files.txt"


def _demo_lines() -> list[tuple[str, str]]:
    rows = []
    for line in DEMO.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        rel, _size, expectation = (part.strip() for part in line.split("|", 2))
        rows.append((rel.replace("{crc32}", "1A2B3C4D"), expectation))
    return rows


DEMO_EXPECT: dict[str, dict] = {
    "Starfall.Station.S01E01.1080p.WEB-DL.DDP5.1.H.264-NTb.mkv":
        {"kind": S, "title": "Starfall Station", "season": 1, "episodes": [1], "release_group": "NTb",
         "q.resolution": "1080p", "q.source": "WEB-DL", "q.audio_codec": "DDP", "q.audio_channels": "5.1",
         "q.video_codec": "x264"},
    "Starfall.Station.S01E01.1080p.WEB-DL.DDP5.1.H.264-NTb.en.srt":
        {"kind": S, "season": 1, "episodes": [1], "languages": ["en"], "extension": "srt"},
    "Starfall.Station.S01E02E03.720p.HDTV.x264-KILLERS.mkv":
        {"season": 1, "episodes": [2, 3], "q.source": "HDTV", "release_group": "KILLERS"},
    "starfall_station_1x04_hdtv.avi": {"title": "Starfall Station", "season": 1, "episodes": [4]},
    "Starfall Station - S00E02 - Christmas Special.mkv":
        {"season": 0, "episodes": [2], "special": True, "special_title": "Christmas Special"},
    "Starfall Station - The Making of Starfall.mkv":
        {"title": "Starfall Station", "special_title": "The Making of Starfall", "kind": U, "episodes": []},
    "Starfall.Station.S02E10.PROPER.1080p.BluRay.x265.10bit-GROUP.mkv":
        {"season": 2, "episodes": [10], "q.proper": True, "q.video_codec": "x265", "q.bit_depth": 10},
    "Starfall.Station.S03E01.Sample.mkv": {"is_sample": True, "season": 3, "episodes": [1]},
    "Starfall Station/Season 3/03 - Ghosts in the Array.mkv":
        {"title": "Starfall Station", "season": 3, "episodes": [3], "kind": S},
    "Harbor.Lights.2021.S01E03.2160p.AMZN.WEB-DL.DV.HDR.H.265-FLUX.mkv":
        {"title": "Harbor Lights", "year": 2021, "season": 1, "episodes": [3], "q.hdr": ["DV", "HDR10"],
         "q.streaming_service": "AMZN", "q.resolution": "2160p"},
    "Harbor Lights S02E05.mkv": {"title": "Harbor Lights", "year": None, "season": 2, "episodes": [5]},
    "The.Late.Hour.with.Sam.Reyes.2024.03.04.720p.WEB.h264-SPAMnEGGS.mkv":
        {"title": "The Late Hour with Sam Reyes", "air_date": date(2024, 3, 4), "year": None},
    "[SubsPlease] Kaze no Tsubasa - 15 (1080p) [1A2B3C4D].mkv":
        {"kind": A, "title": "Kaze no Tsubasa", "absolute": [15], "crc32": "1A2B3C4D",
         "release_group": "SubsPlease"},
    "[Erai-raws] Wings of the Wind - 26 [1080p][Multiple Subtitle].mkv":
        {"kind": A, "title": "Wings of the Wind", "absolute": [26]},
    "[Judas] Tensei Robo Gaiden - S01E07 [1080p][HEVC x265 10bit].mkv":
        {"kind": A, "season": 1, "episodes": [7], "q.bit_depth": 10, "q.video_codec": "x265"},
    "[Hatsuyuki] Kaze no Tsubasa - 07v2 [720p][DEADBEEF].mkv":
        {"kind": A, "absolute": [7], "version": 2, "crc32": "DEADBEEF"},
    "Kaze no Tsubasa - OVA [BD 1080p].mkv": {"kind": A, "special": True, "title": "Kaze no Tsubasa"},
    "The.Quiet.Meridian.2019.Directors.Cut.1080p.BluRay.x264-SPARKS.mkv":
        {"kind": M, "title": "The Quiet Meridian", "year": 2019, "edition": "Director's Cut"},
    "Iron.Orchard.1987.REMASTERED.720p.BluRay.x264-AMIABLE.mkv":
        {"kind": M, "title": "Iron Orchard", "year": 1987, "edition": "Remastered"},
    "Iron Orchard 2023 2160p WEB-DL DDP5.1 Atmos HDR10 HEVC-CMRG.mkv":
        {"kind": M, "title": "Iron Orchard", "year": 2023, "q.atmos": True, "q.hdr": ["HDR10"],
         "release_group": "CMRG"},
    "Paper.Moons.2004.CD1.DVDRip.XviD-FiCO.avi": {"kind": M, "part": 1, "year": 2004},
    "Paper.Moons.2004.CD2.DVDRip.XviD-FiCO.avi": {"kind": M, "part": 2},
    "Neon.Requiem.2021.1080p.BluRay.x264-GROUP.mkv": {"kind": M, "title": "Neon Requiem", "year": 2021},
    "Neon.Requiem.2021.2160p.UHD.BluRay.REMUX.HDR.HEVC.TrueHD.7.1.Atmos-FGT.mkv":
        {"kind": M, "q.remux": True, "q.resolution": "2160p", "q.audio_codec": "TrueHD",
         "q.audio_channels": "7.1", "q.atmos": True},
    "Neon Requiem (2021)/Featurettes/Neon.Requiem.2021.Behind.The.Scenes.1080p.mkv":
        {"is_extra": True, "title": "Neon Requiem", "year": 2021},
    "completely.unknown.title.2011.1080p.mkv": {"kind": M, "title": "Completely Unknown Title", "year": 2011},
    "notes.txt": {"extension": "txt"},
}


@pytest.mark.parametrize(("rel", "expectation"), _demo_lines(), ids=[r for r, _ in _demo_lines()])
def test_demo_manifest_parses(rel: str, expectation: str) -> None:
    parsed = parse("/config/demo/incoming/" + rel)
    assert rel in DEMO_EXPECT, f"demo line without a parser expectation: {rel} ({expectation})"
    _check(parsed, DEMO_EXPECT[rel])


# ------------------------------------------------------------ properties


def test_parse_accepts_purepath_and_keeps_path() -> None:
    p = PureWindowsPath(r"D:\TV\Show\Season 1\Show.S01E02.mkv")
    parsed = parse(p)
    assert parsed.path == str(p)
    assert parsed.filename == "Show.S01E02.mkv"
    assert (parsed.season, parsed.episodes) == (1, [2])


def test_confidence_orders_by_pattern_strength() -> None:
    strict = parse("Show.S01E02.mkv").confidence
    weak = parse("Show - 05 - Title.mkv").confidence
    unknown = parse("Something Else 7.mkv").confidence
    assert strict > weak > unknown


def test_suspicious_weak_numbers_lower_confidence() -> None:
    normal = parse("/tv/Show/Season 2/Show - 05.mkv").confidence
    odd = parse("/tv/Show/Season 2/Show - 75.mkv").confidence
    assert odd < normal


def test_parse_is_fast() -> None:
    names = [r for r, _ in _demo_lines()] + [c[0] for c in CASES]
    for n in names:  # warm caches
        parse(n)
    start = time.perf_counter()
    rounds = 5
    for _ in range(rounds):
        for n in names:
            parse(n)
    avg_ms = (time.perf_counter() - start) * 1000 / (rounds * len(names))
    assert avg_ms < 1.0, f"parse averaged {avg_ms:.3f} ms"


def test_parse_never_raises_on_odd_input() -> None:
    for name in ["", ".", "..", "[]", "()", "-", "   ", "S01E", "[Group]", "x" * 400, "🎬.mkv", "a/b/c/"]:
        parse(name)
