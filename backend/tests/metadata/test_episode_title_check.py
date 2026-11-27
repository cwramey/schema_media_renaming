"""Episode titles in filenames check (and fix) the episode number (2026-10-31 TV import fixtures).

Releases often number episodes differently from TMDb (a pilot counted as E01, a two-part premiere
split in two). The filename's own episode title decides which episode it really is.
"""

from __future__ import annotations

from schema_app.core.matcher import Matcher
from schema_app.core.parser import parse
from schema_app.core.scoring import episode_title_similarity, split_part
from schema_app.domain import ProviderRef, SeriesInfo

from .conftest import ep, fake_registry, named

P = "tmdb"


def series(sid: str, title: str, year: int, seasons: int = 1) -> SeriesInfo:
    return SeriesInfo(ref=ProviderRef(provider=P, id=sid), title=title, year=year, ids={P: sid},
                      season_count=seasons)


def se(results) -> list[tuple[int | None, int | None]]:
    return [(e.season, e.episode) for e in results.best.episodes] if results.best else []


VENTURE_S1 = ["Dia de los Dangerous!", "Careers in Science", "Home Insecurity", "The Incredible Mr. Brisby",
              "Ghosts of the Sargasso", "Eeny, Meeny, Miney... Magic!", "Ice Station - Impossible!",
              "Mid-Life Chrysalis", "Tag Sale - You're It!", "Are You There God? It's Me, Dean",
              "Past Tense", "The Trial of the Monarch", "Return to Spider-Skull Island"]


def venture() -> object:
    sid = "4000"
    eps = [ep(sid, 0, 1, "The Terrible Secret of Turtle Bay", provider=P),
           ep(sid, 0, 2, "A Very Venture Christmas", provider=P)]
    eps += [ep(sid, 1, n, t, absolute=n, provider=P) for n, t in enumerate(VENTURE_S1, start=1)]
    eps += [ep(sid, 2, 1, "Powerless in the Face of Death", absolute=14, provider=P),
            ep(sid, 2, 2, "Hate Floats", absolute=15, provider=P)]
    eps += [ep(sid, 3, 1, "Shadowman 9: In the Cradle of Destiny", provider=P),
            ep(sid, 3, 12, "The Family That Slays Together (1)", provider=P),
            ep(sid, 3, 13, "The Family That Slays Together (2)", provider=P)]
    return named(P, series=[series(sid, "The Venture Bros.", 2003, seasons=3)], episodes={sid: eps})


TED_S1 = ["Just Say Yes", "My Two Dads", "Ejectile Dysfunction", "Bye Bye Bean", "Desperately Seeking Susan",
          "Loose Lips", "He's Gotta Have It"]


def ted_world() -> object:
    eps = {"10": [ep("10", 1, n, t, provider=P) for n, t in enumerate(TED_S1, start=1)],
           "11": [ep("11", 1, n, f"Wasted {n}", provider=P) for n in range(1, 11)],
           "12": [ep("12", 1, n, f"Lasso {n}", provider=P) for n in range(1, 11)]}
    shows = [series("10", "ted", 2024), series("11", "Wa$ted!", 2016), series("12", "Ted Lasso", 2020)]
    return named(P, series=shows, episodes=eps)


# --------------------------------------------------------------------------- similarity


def test_episode_title_similarity_basics() -> None:
    assert episode_title_similarity("Dia de los Dangerous", "Dia de los Dangerous!") == 1.0
    assert episode_title_similarity("Hes Gotta Have It", "He's Gotta Have It") == 1.0
    assert episode_title_similarity("Strike", "Olympics") == 0.0
    assert episode_title_similarity("Just Say Yes Part 2", "Just Say Yes") == 1.0
    assert episode_title_similarity("The Family That Slays Together, Part 1", "The Family That Slays Together (2)") == 0.0
    assert split_part("The Family That Slays Together (1)") == ("The Family That Slays Together", 1)


# --------------------------------------------------------------------------- fixtures 2204-2218


async def test_venture_s1_offset_is_fixed_by_title() -> None:
    root = "/media/downloads/Venture Brothers/Season 1/Venture Brothers, The - "
    names = ["S1E01 - The Terrible Secret of Turtle Bay.avi", "S1E02 - Dia de los Dangerous.avi",
             "S1E14 - Return to Spider-Skull Island.avi", "S1E15 - A Very Venture Christmas.avi"]
    real_s2 = "/media/downloads/Venture Brothers/Season 2/Venture Brothers, The - S2E01 - Powerless in the Face of Death.avi"
    results = await Matcher(fake_registry([venture()])).match_many([parse(root + n) for n in names] + [parse(real_s2)])
    assert [se(r) for r in results] == [[(0, 1)], [(1, 1)], [(1, 13)], [(0, 2)], [(2, 1)]]
    assert all("episode_title_mismatch" not in r.warnings and "episode_conflict" not in r.warnings for r in results)
    assert "title match: S01E02 → S01E01" in results[1].best.reasons
    assert "title match: S01E14 → S01E13" in results[2].best.reasons
    assert not any("read as absolute" in reason for reason in results[2].best.reasons)


async def test_out_of_range_episode_without_title_never_rolls_into_next_season() -> None:
    r = await Matcher(fake_registry([venture()])).match(parse("/x/Venture Brothers, The - S1E14.avi"))
    assert r.best is None
    assert "episode_not_found" in r.warnings


# --------------------------------------------------------------------------- 2243 / 2255 / 2256


async def test_venture_s3_see_codes() -> None:
    folder = "/media/tv/The Venture Bros/Season 03/The Venture Bros - "
    names = ["301 - Shadowman 9- In the Cradle of Destiny.avi", "312 - The Family That Slays Together, Part 1.avi",
             "313 - The Family That Slays Together, Part 2.avi"]
    results = await Matcher(fake_registry([venture()])).match_many([parse(folder + n) for n in names])
    assert [se(r) for r in results] == [[(3, 1)], [(3, 12)], [(3, 13)]]
    assert all(r.best.part is None for r in results)


async def test_see_code_without_season_folder() -> None:
    r = await Matcher(fake_registry([venture()])).match(
        parse("/media/downloads/The Venture Bros - 301 - Shadowman 9- In the Cradle of Destiny.avi"))
    assert se(r) == [(3, 1)]


# --------------------------------------------------------------------------- Superstore / Ted / IT Crowd / ATHF


async def test_superstore_strike_is_s02e02() -> None:
    sid = "20"
    provider = named(P, series=[series(sid, "Superstore", 2015, seasons=2)],
                     episodes={sid: [ep(sid, 2, 1, "Olympics", provider=P), ep(sid, 2, 2, "Strike", provider=P)]})
    r = await Matcher(fake_registry([provider])).match(
        parse("/media/downloads/Superstore (2015) - S02E01 - Strike (1080p AMZN WEB-DL x265 Silence).mkv"))
    assert se(r) == [(2, 2)]
    assert r.best.episodes[0].title == "Strike"


async def test_ted_two_part_premiere_and_offset() -> None:
    folder = "/media/downloads/Ted/"
    names = ["Ted.S01E01.Just.Say.Yes.Part.1.2160p.PCOK.WEB-DL.DDP5.1.H.265-WADU.mkv",
             "Ted.S01E02.Just.Say.Yes.Part.2.2160p.PCOK.WEB-DL.DDP5.1.H.265-WADU.mkv",
             "Ted.S01E03.My.Two.Dads.2160p.PCOK.WEB-DL.DDP5.1.H.265-WADU.mkv",
             "Ted.S01E08.Hes.Gotta.Have.It.2160p.NOW.WEB-DL.DDP5.1.H.265-WADU.mkv"]
    results = await Matcher(fake_registry([ted_world()])).match_many([parse(folder + n) for n in names])
    assert [r.best.title for r in results] == ["ted"] * 4
    assert [se(r) for r in results] == [[(1, 1)], [(1, 1)], [(1, 2)], [(1, 7)]]
    assert [r.best.part for r in results] == [1, 2, None, None]
    assert all("episode_conflict" not in r.warnings for r in results)


async def test_it_crowd_title_only_special() -> None:
    sid = "30"
    provider = named(P, series=[series(sid, "The IT Crowd", 2006, seasons=4)],
                     episodes={sid: [ep(sid, 0, 1, "The Internet Is Coming", provider=P),
                                     ep(sid, 4, 1, "Jen the Fredo", provider=P)]})
    r = await Matcher(fake_registry([provider])).match(
        parse("/media/downloads/The IT Crowd 2006 S05 The Internet is Coming 1080p WEB-DL HEVC x265 BONE.mkv"))
    assert se(r) == [(0, 1)]


async def test_athf_unknown_special_goes_to_review() -> None:
    sid = "40"
    provider = named(P, series=[series(sid, "Aqua Teen Hunger Force", 2000, seasons=1)],
                     episodes={sid: [ep(sid, 0, 1, "Rabbot", provider=P), ep(sid, 1, 1, "Rabbot", provider=P)]})
    r = await Matcher(fake_registry([provider])).match(
        parse("/media/downloads/Aqua Teen Hunger Force (2000) - S00E01 - Baffler Meal (480p DVD x265 r00t).mkv"))
    assert "episode_title_mismatch" in r.warnings


async def test_matching_title_keeps_number() -> None:
    r = await Matcher(fake_registry([venture()])).match(
        parse("/x/The Venture Bros - S02E01 - Powerless in the Face of Death.avi"))
    assert se(r) == [(2, 1)]
    assert "episode_title_mismatch" not in r.warnings


async def test_numbers_typed_by_the_user_are_not_second_guessed() -> None:
    from schema_app.domain import MatchHint

    r = await Matcher(fake_registry([venture()])).match(
        parse("/x/Venture Brothers, The - S1E02 - Dia de los Dangerous.avi"), MatchHint(season=1, episodes=[2]))
    assert se(r) == [(1, 2)]
    assert "episode_title_mismatch" not in r.warnings
