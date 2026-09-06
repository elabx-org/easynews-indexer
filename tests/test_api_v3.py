"""Easynews 3.0 endpoint, field mapping, pagination and the per-account concurrency cap."""
import json
import threading
import time
from urllib.parse import parse_qs, urlparse

import pytest

import easynews_client
import server


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.headers = {"content-type": "application/json"}
        self.text = json.dumps(payload)

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeSession:
    """Records requests; returns per-page payloads built by `responder(url, params)`."""

    def __init__(self, responder, delay=0.0):
        self.responder = responder
        self.delay = delay
        self.urls = []
        self.headers = {}
        self.auth = None
        self.inflight = 0
        self.max_inflight = 0
        self._lock = threading.Lock()

    def get(self, url, params=None, timeout=None, **kw):
        with self._lock:
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            if self.delay:
                time.sleep(self.delay)
            full = url if not params else url + ("&" if "?" in url else "?") + "&".join(f"{k}={v}" for k, v in params.items())
            self.urls.append(full)
            return FakeResponse(self.responder(full))
        finally:
            with self._lock:
                self.inflight -= 1


def page_of(n, page, num_pages=1):
    return {"data": [{"hash": f"h{page}_{i}", "fn": f"f{page}_{i}", "extension": ".mkv", "size": 1} for i in range(n)],
            "numPages": num_pages, "results": n * num_pages, "page": page}


def qs(url):
    return {k: v[0] for k, v in parse_qs(urlparse(url).query, keep_blank_values=True).items()}


# --- endpoint selection ---------------------------------------------------------

def test_default_api_version_is_3(monkeypatch):
    monkeypatch.delenv("EASYNEWS_API_VERSION", raising=False)
    assert easynews_client._api_version_from_env() == "3.0"


@pytest.mark.parametrize("raw,expected", [("2.0", "2.0"), ("3.0", "3.0"), ("bogus", "3.0"), ("", "3.0")])
def test_api_version_env_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("EASYNEWS_API_VERSION", raw)
    assert easynews_client._api_version_from_env() == expected


def test_v3_search_url_and_params():
    s = FakeSession(lambda url: page_of(3, 1))
    c = easynews_client.EasynewsClient("u", "p", session=s, api_version="3.0")
    c.search(query="lanterns s01e01", per_page=100, sort_field="relevance", sort_dir="-")
    assert len(s.urls) == 1
    u = s.urls[0]
    assert urlparse(u).path == "/3.0/api/search"
    q = qs(u)
    assert q["gps"] == "lanterns s01e01" and q["pno"] == "1"
    assert q["fty[]"] == "VIDEO" and q["u"] == "1" and q["safeO"] == "0"
    assert "pby" not in q and "fly" not in q
    # 3.0 ranks by relevance only when NO sort is sent; s1=relevance makes it
    # fall back to filename order (measured: 3/100 title matches vs 31/100).
    assert "s1" not in q and "s1d" not in q


def test_v3_sends_explicit_non_relevance_sort():
    s = FakeSession(lambda url: page_of(3, 1))
    c = easynews_client.EasynewsClient("u", "p", session=s, api_version="3.0")
    c.search(query="x", per_page=100, sort_field="dtime", sort_dir="-")
    q = qs(s.urls[0])
    assert q["s1"] == "dtime" and q["s1d"] == "-"


def test_v2_search_url_still_available():
    s = FakeSession(lambda url: page_of(3, 1))
    c = easynews_client.EasynewsClient("u", "p", session=s, api_version="2.0")
    c.search(query="x", per_page=250)
    u = s.urls[0]
    assert "/2.0/search/solr-search/" in u and qs(u)["pby"] == "250" and qs(u)["fly"] == "2"


# --- 3.0 pagination (fixed 100 per page) --------------------------------------------

def test_v3_fetches_enough_pages_to_honour_per_page_and_dedups():
    def responder(url):
        p = int(qs(url)["pno"])
        d = page_of(100, p, num_pages=5)
        if p == 2:  # a repost of a page-1 hash must not be duplicated
            d["data"][0]["hash"] = "h1_0"
        return d
    s = FakeSession(responder)
    c = easynews_client.EasynewsClient("u", "p", session=s, api_version="3.0")
    out = c.search(query="x", per_page=250)
    assert [qs(u)["pno"] for u in s.urls] == ["1", "2", "3"]
    assert len(out["data"]) == 299
    assert out["numPages"] == 5


def test_v3_stops_at_last_page():
    s = FakeSession(lambda url: page_of(40, 1, num_pages=1))
    c = easynews_client.EasynewsClient("u", "p", session=s, api_version="3.0")
    out = c.search(query="x", per_page=250)
    assert len(s.urls) == 1 and len(out["data"]) == 40


# --- concurrency cap (account-wide, per process) ------------------------------------

def test_search_concurrency_is_capped(monkeypatch):
    monkeypatch.setattr(easynews_client, "_SEARCH_SEMAPHORE", easynews_client._make_search_semaphore(2))
    s = FakeSession(lambda url: page_of(1, 1), delay=0.15)
    c = easynews_client.EasynewsClient("u", "p", session=s, api_version="3.0")
    threads = [threading.Thread(target=c.search, kwargs={"query": f"q{i}", "per_page": 100}) for i in range(6)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert len(s.urls) == 6
    assert s.max_inflight <= 2


@pytest.mark.parametrize("raw,expected", [("2", 2), ("4", 4), ("0", 2), ("abc", 2), ("", 2)])
def test_max_concurrent_env_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("EASYNEWS_MAX_CONCURRENT_SEARCHES", raw)
    assert easynews_client._max_concurrent_from_env() == expected


# --- 3.0 item field mapping in the server ---------------------------------------------

V3_FULL = {
    "hash": "abc123", "id": "0ca6", "sig": "sigv3", "fn": "lanterns.2026.s01e01.2160p.web.h265-ggwp",
    "extension": ".mkv", "size": 8_100_000_000, "runtime": 3389, "xres": 3840, "yres": 2160, "type": "VIDEO",
    "subject": "x", "poster": "p@example.com", "timestamp": 1786990902, "password": False, "virus": False,
}


def test_v3_item_maps_filename_duration_and_resolution():
    items = server.filter_and_map({"data": [V3_FULL]}, min_bytes=100 * 1024 * 1024)
    assert len(items) == 1
    it = items[0]
    assert it["filename"] == "lanterns.2026.s01e01.2160p.web.h265-ggwp"
    assert it["ext"] == ".mkv"
    assert it["duration_hms"] == "00:56:29"
    assert it["quality"] == "2160p"
    assert it["sig"] == "sigv3"


def test_v3_item_id_roundtrips_for_nzb_download():
    items = server.filter_and_map({"data": [V3_FULL]}, min_bytes=100 * 1024 * 1024)
    enc = server.encode_id(items[0])
    d = server.decode_id(enc)
    assert d["hash"] == "abc123" and d["filename"] == V3_FULL["fn"] and d["ext"] == ".mkv" and d["sig"] == "sigv3"
