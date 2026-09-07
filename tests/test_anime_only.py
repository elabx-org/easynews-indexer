"""ANIME_ONLY mode: advertise and return anime only, so FusionHA/Prowlarr scope
the indexer to anime automatically (no app tags needed)."""
import re

import pytest

import server


@pytest.fixture
def anime_only(monkeypatch):
    monkeypatch.setattr(server, "ANIME_ONLY", True)
    monkeypatch.setattr(server, "_cached_search", lambda **kw: {"data": []})


def caps():
    return server.APP.test_client().get("/api?t=caps&apikey=testkey").data.decode()


# --- capabilities ------------------------------------------------------------------

def test_caps_default_advertises_movies_tv_and_anime():
    x = caps()
    assert 'id="2000"' in x and 'id="5000"' in x and 'id="5070"' in x


def test_caps_anime_only_advertises_only_anime(anime_only):
    x = caps()
    assert 'id="5070"' in x
    assert 'id="2000"' not in x and 'id="2030"' not in x and 'id="2040"' not in x
    assert 'id="5030"' not in x and 'id="5040"' not in x
    # no generic TV parent that Prowlarr would map to tv_categories
    assert 'name="TV"' not in x
    # movie search is not offered
    assert "movie-search" not in x
    assert "tv-search" in x


# --- search filtering --------------------------------------------------------------

def _raw(name, size=800_000_000, runtime=1400):
    return {"hash": name, "fn": name, "extension": ".mkv", "size": size, "runtime": runtime,
            "timestamp": 1700000000, "type": "VIDEO"}


def titles(resp):
    return re.findall(r"<title>(.*?)</title>", resp.data.decode())[1:]


def cats(resp):
    return re.findall(r'name="category" value="(\d+)"', resp.data.decode())


def test_anime_only_drops_non_anime_results(anime_only, monkeypatch):
    data = {"data": [
        _raw("[SubsPlease].Dandadan.S02E01.1080p"),           # anime fansub (SxxEyy) -> keep
        _raw("Dandadan.15.[1080p]"),                          # anime absolute-ep -> keep
        _raw("Dandadan.2024.1080p.BluRay.x264-GRP"),          # movie-shaped -> drop
        _raw("Dandadan.Live.Action.S01E01.1080p.WEB-GRP"),    # standard TV -> drop
    ]}
    monkeypatch.setattr(server, "_cached_search", lambda **kw: data)
    r = server.APP.test_client().get("/api?t=tvsearch&q=dandadan&cat=5070&apikey=testkey")
    t = titles(r)
    assert any("Dandadan.S02E01" in x for x in t)
    assert any("Dandadan.15" in x for x in t)
    assert not any("BluRay" in x for x in t)
    assert not any("Live.Action" in x for x in t)
    assert set(cats(r)) <= {"5070"}


def test_anime_only_empty_query_returns_anime_sample_for_any_category(anime_only):
    # Sonarr add-time test still passes: fallback yields an anime sample even if
    # the test happens to send non-anime cats.
    r = server.APP.test_client().get("/api?t=tvsearch&cat=5000,5030,5040&apikey=testkey")
    assert r.status_code == 200
    assert r.data.count(b"<item>") == 1
    assert cats(r) == ["5070"]


def test_default_mode_still_returns_non_anime(monkeypatch):
    monkeypatch.setattr(server, "ANIME_ONLY", False)
    monkeypatch.setattr(server, "_cached_search",
                        lambda **kw: {"data": [_raw("The.Matrix.1999.2160p.BluRay.x265-GRP", size=9_000_000_000, runtime=8000)]})
    r = server.APP.test_client().get("/api?t=movie&q=the+matrix&year=1999&cat=2000,2030,2040&apikey=testkey")
    assert any("Matrix" in x for x in titles(r))


def test_anime_only_flag_reads_env(monkeypatch):
    monkeypatch.setenv("ANIME_ONLY", "1")
    assert server._env_bool("ANIME_ONLY", False) is True
    monkeypatch.setenv("ANIME_ONLY", "off")
    assert server._env_bool("ANIME_ONLY", False) is False
