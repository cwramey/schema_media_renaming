"""Poster URLs handed to the browser: small sizes, https only, known image hosts only."""

from __future__ import annotations

import pytest

from schema_app.domain import ProviderCredentials
from schema_app.providers.common import IMAGE_HOSTS, safe_image_url, tvdb_thumbnail
from schema_app.providers.tmdb import TmdbProvider
from schema_app.providers.tvdb import TvdbProvider
from schema_app.providers.tvmaze import TvmazeProvider

CREDS = ProviderCredentials(tmdb_api_key="k", tvdb_api_key="k")


def test_image_hosts_are_the_three_providers() -> None:
    assert {"image.tmdb.org", "artworks.thetvdb.com", "static.tvmaze.com"} == IMAGE_HOSTS


@pytest.mark.parametrize("url", [
    "https://image.tmdb.org/t/p/w154/abc.jpg",
    "https://artworks.thetvdb.com/banners/posters/81189-10_t.jpg",
    "https://static.tvmaze.com/uploads/images/medium_portrait/1/4600.jpg",
    "https://IMAGE.tmdb.org:443/t/p/w154/abc.jpg",
])  # fmt: skip
def test_safe_image_url_accepts_provider_images(url: str) -> None:
    assert safe_image_url(f"  {url} ") == url


@pytest.mark.parametrize("url", [
    None, 42, "", "/t/p/w154/abc.jpg",
    "http://image.tmdb.org/t/p/w154/abc.jpg",  # not https
    "https://evil.example/abc.jpg",
    "https://image.tmdb.org.evil.example/abc.jpg",
    "https://user:pw@image.tmdb.org/abc.jpg",
    "https://image.tmdb.org:8443/abc.jpg",
    "https://image.tmdb.org:99999/abc.jpg",
    "javascript:alert(1)",
    "data:image/png;base64,AAAA",
    'https://image.tmdb.org/a.jpg" onerror="x',
    "https://image.tmdb.org/a b.jpg",
    "//image.tmdb.org/a.jpg",
])  # fmt: skip
def test_safe_image_url_drops_everything_else(url: object) -> None:
    assert safe_image_url(url) is None


@pytest.mark.parametrize(("url", "thumb"), [
    ("https://artworks.thetvdb.com/banners/posters/81189-10.jpg",
     "https://artworks.thetvdb.com/banners/posters/81189-10_t.jpg"),
    ("https://artworks.thetvdb.com/banners/v4/series/1/posters/abc.jpg",
     "https://artworks.thetvdb.com/banners/v4/series/1/posters/abc_t.jpg"),
    ("https://artworks.thetvdb.com/banners/v4/series/1/posters/abc_t.jpg",
     "https://artworks.thetvdb.com/banners/v4/series/1/posters/abc_t.jpg"),
    ("https://artworks.thetvdb.com/banners/_cache/posters/81189-10.jpg",  # retired path (403)
     "https://artworks.thetvdb.com/banners/posters/81189-10_t.jpg"),
    ("https://artworks.thetvdb.com/other/x.jpg", "https://artworks.thetvdb.com/other/x.jpg"),
    ("https://evil.example/banners/posters/1.jpg", None),
    ("http://artworks.thetvdb.com/banners/posters/1.jpg", None),
])  # fmt: skip
def test_tvdb_thumbnail(url: str, thumb: str | None) -> None:
    assert tvdb_thumbnail(url) == thumb


def test_tmdb_posters_are_small_and_checked() -> None:
    tmdb = TmdbProvider(CREDS)
    movie = tmdb._movie_from({"id": 1, "title": "A", "poster_path": "/ggFHVNu6YYI5L9pCfOacjizRGt.jpg"})
    assert movie.poster_url == "https://image.tmdb.org/t/p/w154/ggFHVNu6YYI5L9pCfOacjizRGt.jpg"
    for bad in ("//evil.example/x.jpg", "/../../x.jpg", "x.jpg", "/a b.jpg", None):
        assert tmdb._movie_from({"id": 1, "title": "A", "poster_path": bad}).poster_url is None
    series = tmdb._series_from({"id": 2, "name": "S", "poster_path": "/abc.png"})
    assert series.poster_url == "https://image.tmdb.org/t/p/w154/abc.png"


def test_tvdb_posters_use_thumbnails_and_drop_foreign_hosts() -> None:
    tvdb = TvdbProvider(CREDS)
    hit = {"tvdb_id": "81189", "name": "Breaking Bad",
           "image_url": "https://artworks.thetvdb.com/banners/posters/81189-10.jpg",
           "thumbnail": "https://artworks.thetvdb.com/banners/_cache/posters/81189-10.jpg"}  # fmt: skip
    assert tvdb._search_series(hit).poster_url == "https://artworks.thetvdb.com/banners/posters/81189-10_t.jpg"
    only_thumb = {"tvdb_id": "1", "name": "X", "thumbnail": "https://artworks.thetvdb.com/banners/v4/a/b_t.jpg"}
    assert tvdb._search_series(only_thumb).poster_url == "https://artworks.thetvdb.com/banners/v4/a/b_t.jpg"
    foreign = {"tvdb_id": "1", "name": "X", "image_url": "https://tracker.example/pixel.gif"}
    assert tvdb._search_series(foreign).poster_url is None


def test_tvmaze_posters_are_checked() -> None:
    maze = TvmazeProvider(CREDS)
    show = {"id": 1, "name": "S", "image": {"medium": "https://static.tvmaze.com/uploads/images/medium_portrait/1/1.jpg"}}
    assert maze._series_from(show).poster_url == show["image"]["medium"]
    assert maze._series_from({"id": 1, "name": "S", "image": {"medium": "http://static.tvmaze.com/x.jpg"}}).poster_url is None
    assert maze._series_from({"id": 1, "name": "S", "image": {"medium": "https://x.example/x.jpg"}}).poster_url is None
