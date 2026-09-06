"""Sibling pivot wired into /api search and the client's windowed search."""
import re
from urllib.parse import parse_qs, urlparse

import pytest

import easynews_client
import pivot
import server
from tests.test_pivot import TRACKS, V2, mkv, mk


# --- client: windowed 2.0 search used for the pivot scan --------------------------------

class Sess:
    def __init__(self):
        self.urls = []; self.headers = {}; self.auth = None
    def get(self, url, params=None, timeout=None, **kw):
        self.urls.append(url)
        class R:
            status_code = 200; text = '{"data": []}'
            def raise_for_status(self): pass
            def json(self): return {"data": []}
        return R()


def test_search_window_uses_2_0_date_filters_sorted_by_size():
    s = Sess()
    c = easynews_client.EasynewsClient("u", "p", session=s, api_version="3.0")
    c.search_window(query="*", d1="2026-08-17 17:38:33", d2="2026-08-17 19:08:33", page=2, per_page=250)
    u = s.urls[-1]; q = {k: v[0] for k, v in parse_qs(urlparse(u).query).items()}
    assert "/2.0/search/solr-search/" in u
    assert q["gps"] == "*" and q["d1"] == "2026-08-17 17:38:33" and q["d2"] == "2026-08-17 19:08:33"
    assert q["s1"] == "dsize" and q["s1d"] == "-" and q["pno"] == "2" and q["pby"] == "250" and q["st"] == "adv"


# --- server integration -------------------------------------------------------------------

SEED_V3 = {"hash": "seedhash", "id": "0ca6", "sig": "seedsig", "fn": "lanterns.2026.s01e01.internal.hdr.2160p.web.h265-edith.sample",
           "extension": ".mkv", "size": 148922527, "runtime": 60, "timestamp": 1786991013, "xres": 3840, "yres": 1920,
           "vcodec": "HEVC", "acodec": "EAC3", "fps": 23.98, "audio_tracks": "eng", "subtitle_tracks": "eng,bul,cze", "type": "VIDEO"}
GERMAN = {"hash": "dehash", "sig": "s", "fn": "lanterns.s01e01.german.dl.1080p.web.h264-wvf", "extension": ".mkv",
          "size": 1_900_000_000, "runtime": 3389, "timestamp": 1786991013, "type": "VIDEO"}


class FakeBackend:
    def __init__(self):
        full = mk("fullhash", "Z4dFB2qGhFf8k1SlOaBWifbFg", 8_100_000_000, 1786991500); full["sig"] = "fullsig"
        self.window = [full]
        self.headers = {"seedhash": mkv(TRACKS), "fullhash": mkv(TRACKS, duration_ms=3389000.0)}
        self.scans = 0
    def scan_window(self, d1, d2):
        self.scans += 1; return self.window
    def fetch_head(self, item):
        return self.headers[item.hash]


@pytest.fixture
def wired(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(server, "_cached_search", lambda **kw: {"data": [SEED_V3, GERMAN]})
    monkeypatch.setattr(server, "_pivot_backend", lambda: backend)
    monkeypatch.setattr(server, "SIBLING_PIVOT", True)
    server._pivot_cache_clear()
    return backend


def titles(resp):
    return re.findall(r"<title>(.*?)</title>", resp.data.decode())[1:]


def test_search_appends_confirmed_sibling_named_after_the_sample(wired):
    resp = server.APP.test_client().get("/api?t=tvsearch&q=lanterns&season=1&ep=1&cat=5000,5030,5040,5070&apikey=testkey")
    assert resp.status_code == 200
    t = titles(resp)
    assert "lanterns.2026.s01e01.internal.hdr.2160p.web.h265-edith.mkv" in t
    assert "lanterns.s01e01.german.dl.1080p.web.h264-wvf.mkv" in t
    assert not any("sample" in x.lower() for x in t)
    body = resp.data.decode()
    assert 'name="size" value="8100000000"' in body
    enc = re.search(r't=get&amp;id=([A-Za-z0-9_=-]+)', body.split("edith.mkv</title>")[1]).group(1)
    d = server.decode_id(enc)
    assert d["hash"] == "fullhash" and d["sig"] == "fullsig" and d["filename"].endswith("-edith")
    assert wired.scans == 1


def test_pivot_results_are_cached_per_seed(wired):
    c = server.APP.test_client()
    c.get("/api?t=tvsearch&q=lanterns&season=1&ep=1&cat=5000&apikey=testkey")
    c.get("/api?t=tvsearch&q=lanterns&season=1&ep=1&cat=5000&apikey=testkey")
    assert wired.scans == 1


def test_pivot_can_be_disabled(wired, monkeypatch):
    monkeypatch.setattr(server, "SIBLING_PIVOT", False)
    resp = server.APP.test_client().get("/api?t=tvsearch&q=lanterns&season=1&ep=1&cat=5000&apikey=testkey")
    assert "edith.mkv" not in resp.data.decode() and wired.scans == 0


def test_pivot_not_used_for_the_empty_query_fallback(wired):
    resp = server.APP.test_client().get("/api?t=tvsearch&cat=5000,5030,5040,5070&apikey=testkey")
    assert resp.status_code == 200 and wired.scans == 0


def test_pivot_failure_does_not_break_the_search(wired, monkeypatch):
    def boom(*a, **k): raise RuntimeError("easynews down")
    monkeypatch.setattr(wired, "scan_window", boom)
    resp = server.APP.test_client().get("/api?t=tvsearch&q=lanterns&season=1&ep=1&cat=5000&apikey=testkey")
    assert resp.status_code == 200 and "german.dl.1080p" in resp.data.decode()
