"""Sample-file filtering: never hand a sample clip to Sonarr/Radarr as a release."""
import pytest

import server

V3_ITEM = {  # real 3.0-shaped Easynews item (trimmed) for a 2160p sample clip
    "hash": "5b49e35d190733518f49ffa5a79f75c107aeda3e1", "id": "0ca6", "sig": "sig123",
    "fn": "lanterns.2026.s01e01.internal.dv.2160p.web.h265-edith.sample", "extension": ".mkv",
    "size": 155468287, "runtime": 63, "xres": 3840, "yres": 2160, "type": "VIDEO",
    "subject": "57ff2d14 (lanterns.2026.s01e01.internal.dv.2160p.web.h265-edith.sample.mkv AutoUnRAR)",
    "poster": "ghp1AkwR1JQqU@YWx0aHVi.com", "timestamp": 1786990902, "password": False, "virus": False,
}


@pytest.mark.parametrize("name,expected", [
    ("lanterns.2026.s01e01.2160p.web.h265-ggwp-sample", True),
    ("lanterns.s01e01.german.dl.dv.2160p.web.h265.internal-rile-sample", True),
    ("lanterns.2026.s01e02.internal.dv.2160p.web.h265-edith.sample", True),
    ("sample-green.lantern.the.animated.series.s01e03.720p.hdtv.x264-2hd", True),
    ("Green.Lantern.The.Animated.Series.S01E01.Beware.My.Power.720p.WEB-DL", False),
    ("The.Samples.S01E01.1080p.WEB.h264-GRP", False),          # not the token "sample"
    ("Free.Sample.Kings.S01E01.1080p.WEB.h264-GRP", False),    # token in the first half of a title
    ("", False),
])
def test_is_sample_name(name, expected):
    assert server._is_sample_name(name) is expected


def _items_for(data, **kw):
    return server.filter_and_map({"data": data}, min_bytes=100 * 1024 * 1024, **kw)


def test_filter_and_map_drops_sample_clips_even_above_min_size():
    assert _items_for([V3_ITEM]) == []


def test_filter_and_map_keeps_full_release_with_same_shape():
    full = dict(V3_ITEM, fn="lanterns.2026.s01e01.internal.dv.2160p.web.h265-edith", size=8_100_000_000, runtime=3389)
    items = _items_for([full])
    assert len(items) == 1
    assert items[0]["title"].startswith("lanterns.2026.s01e01")


def test_empty_query_fallback_sample_item_still_served(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "client", lambda: calls.append(1) or (_ for _ in ()).throw(AssertionError("client must not be called")))
    resp = server.APP.test_client().get("/api?t=tvsearch&cat=5000,5030,5040,5070&apikey=testkey")
    assert resp.status_code == 200
    assert resp.data.count(b"<item>") == 1
