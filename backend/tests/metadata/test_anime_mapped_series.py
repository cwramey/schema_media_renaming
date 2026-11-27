"""AniDB season entries mapped through Anime-Lists render into one series folder.

AniDB keeps one entry per season/cour ("Sora no Kaizoku", "Sora no Kaizoku 2nd Season"), while
Anime-Lists renumbers their episodes onto a single TVDB (or TMDb) series. The series-level
bindings ({n}, {y}, the id tag) must follow that mapped series, or Jellyfin sees two shows.
"""

from __future__ import annotations

import gzip
from datetime import date

import httpx
import pytest
import respx

from schema_app.core.format import build_binding, compile_format
from schema_app.core.matcher import Matcher
from schema_app.core.parser import parse
from schema_app.domain import MediaKind, ProviderCredentials, ProviderRef, ProviderSettings, SeriesInfo
from schema_app.providers.anidb import API_URL, TITLES_URL, AnidbProvider
from schema_app.providers.animelists import ANIME_LISTS_URL
from schema_app.providers.base import ProviderError
from schema_app.providers.cache import ResponseCache
from schema_app.providers.registry import ProviderRegistry

from .conftest import named, payload

CREDS = ProviderCredentials(anidb_client="schemaclient", anidb_client_version=1)
ANIME = {aid: payload(f"anidb_{aid}.xml") for aid in ("61001", "61002", "61003")}
S1 = "[Grp] Sora no Kaizoku - 05 [720p].mkv"
S2 = "[Grp] Sora no Kaizoku 2nd Season - 03 [720p].mkv"

TVDB_SERIES = SeriesInfo(ref=ProviderRef(provider="tvdb", id="99100"), title="Sky Pirates", year=2017,
                         first_aired=date(2017, 4, 6), ids={"tvdb": "99100", "imdb": "tt9910000"}, season_count=3)
TMDB_SERIES = SeriesInfo(ref=ProviderRef(provider="tmdb", id="4400"), title="Sky Pirates", year=2017,
                         first_aired=date(2017, 4, 6), ids={"tmdb": "4400"})


def _anidb_handler(request: httpx.Request) -> httpx.Response:
    aid = request.url.params.get("aid")
    if aid not in ANIME:
        return httpx.Response(200, content=b"<error>No such anime</error>")
    return httpx.Response(200, content=gzip.compress(ANIME[aid]), headers={"Content-Encoding": "gzip"})


@pytest.fixture
def routes():
    with respx.mock(assert_all_called=False) as router:
        router.get(API_URL).mock(side_effect=_anidb_handler)
        router.get(TITLES_URL).mock(return_value=httpx.Response(200, content=gzip.compress(payload("anidb_titles.xml"))))
        router.get(ANIME_LISTS_URL).mock(return_value=httpx.Response(200, content=payload("anime_list.xml")))
        yield router


def _matcher(clock, *, target: str = "tvdb", extra=()) -> Matcher:  # type: ignore[no-untyped-def]
    cache = ResponseCache(None)
    settings = ProviderSettings(credentials=CREDS, priority={MediaKind.ANIME: ["anidb"]})
    reg = ProviderRegistry(settings, cache=cache, sleep=clock.sleep)
    reg.providers["anidb"] = AnidbProvider(CREDS, cache=cache, sleep=clock.sleep, clock=clock,
                                           animelists=reg.animelists, mapping_target=target)
    for p in extra:
        reg.providers[p.name] = p
    return Matcher(reg)


def _render(expr: str, candidate, filename: str) -> str:  # type: ignore[no-untyped-def]
    return compile_format(expr).render(build_binding(parse(filename), candidate, None))


async def test_mapped_season_entries_share_one_jellyfin_series_folder(routes, clock) -> None:
    tvdb = named("tvdb", series=[TVDB_SERIES])
    results = await _matcher(clock, extra=[tvdb]).match_many([parse(S1), parse(S2)])
    first, second = (r.best for r in results)
    assert first is not None and second is not None, [r.warnings for r in results]
    assert (first.id, second.id) == ("anidb:anime:61001", "anidb:anime:61002")  # two AniDB entries
    paths = [_render("{jellyfin.id}", c, f) for c, f in ((first, S1), (second, S2))]
    assert paths == [
        "Sky Pirates (2017) [tvdbid-99100]/Season 01/Sky Pirates (2017) - S01E05 - Sky Market.mkv",
        "Sky Pirates (2017) [tvdbid-99100]/Season 02/Sky Pirates (2017) - S02E03 - " + second.episodes[0].title + ".mkv",
    ]
    assert second.series is not None and second.series.ref.id == "61002", "the AniDB ref (episodes) is kept"
    assert "Sora no Kaizoku 2nd Season" in second.series.aliases
    assert second.series.ids["anidb"] == "61002" and second.series.ids["imdb"] == "tt9910000"
    assert any("Anime-Lists mapping" in reason for reason in second.reasons)
    assert tvdb.calls.count(("get_series", "99100")) == 1, "one lookup per mapped series per batch"


async def test_tmdb_target_uses_tmdb_name_and_tmdb_id_tag(routes, clock) -> None:
    tmdb = named("tmdb", series=[TMDB_SERIES])
    r = await _matcher(clock, target="tmdb", extra=[tmdb]).match(parse(S2))
    assert r.best is not None, r.warnings
    assert [(e.season, e.episode, e.ids["mapped"]) for e in r.best.episodes] == [(1, 15, "tmdb")]
    path = _render("{jellyfin.id}", r.best, S2)
    assert path.startswith("Sky Pirates (2017) [tmdbid-4400]/Season 01/Sky Pirates (2017) - S01E15")
    assert _render("{plex.id}", r.best, S2).startswith("Sky Pirates (2017) {tmdb-4400}/Season 01/")
    assert _render("{db} {provider} {tvdbid} {tmdbid}", r.best, S2) == "tmdb anidb 99100 4400.mkv"


async def test_without_mapped_provider_keeps_anidb_name_but_tag_follows_numbering(routes, clock) -> None:
    r = await _matcher(clock, target="tmdb").match(parse(S2))  # no TMDb provider configured
    assert r.best is not None, r.warnings
    assert r.best.title == "Sora no Kaizoku 2nd Season"
    assert _render("{jellyfin.id}", r.best, S2).startswith(
        "Sora no Kaizoku 2nd Season (2018) [tmdbid-4400]/Season 01/")


async def test_mapped_series_lookup_failure_falls_back_to_anidb_name(routes, clock) -> None:
    broken = named("tvdb", fail=ProviderError("tvdb down"))
    r = await _matcher(clock, extra=[broken]).match(parse(S2))
    assert r.best is not None, r.warnings
    assert r.best.title == "Sora no Kaizoku 2nd Season"
    assert _render("{jellyfin.id}", r.best, S2).startswith("Sora no Kaizoku 2nd Season (2018) [tvdbid-99100]/Season 02/")
    assert any("series name unavailable" in reason for reason in r.best.reasons)


async def test_unmapped_anidb_entry_keeps_anidb_name_and_absolute_numbering(routes, clock) -> None:
    tvdb = named("tvdb", series=[TVDB_SERIES])
    m = _matcher(clock, extra=[tvdb])
    m.registry.animelists.load_from_bytes(b"<anime-list/>")  # no Anime-Lists entry for any aid
    r = await m.match(parse(S1))
    assert r.best is not None, r.warnings
    assert r.best.title == "Sora no Kaizoku" and r.best.year == 2017
    assert all("mapped" not in e.ids for e in r.best.episodes)
    assert _render("{jellyfin.id}", r.best, S1) == "Sora no Kaizoku (2017)/Season 01/Sora no Kaizoku (2017) - S01E05 - Sky Market.mkv"
    assert _render("{db}", r.best, S1) == "anidb.mkv"
    assert tvdb.calls == []
