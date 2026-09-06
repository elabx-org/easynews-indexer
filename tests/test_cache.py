"""Short-TTL cache for Easynews search responses (CACHE_TTL_SECONDS)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("EASYNEWS_USER", "test")
os.environ.setdefault("EASYNEWS_PASS", "test")

import pytest  # noqa: E402

import server  # noqa: E402
from easynews_client import EasynewsError  # noqa: E402


def _item(name: str) -> dict:
    return {
        "hash": f"HASH{name}",
        "fn": f"{name}.s01e01.1080p",
        "ext": ".mkv",
        "size": 900 * 1024 * 1024,
        "type": "VIDEO",
        "sig": None,
    }


class FakeClient:
    """Stands in for EasynewsClient; records every search() call."""

    def __init__(self, fail_first: int = 0):
        self.calls = []
        self.fail_first = fail_first

    def search(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_first > 0:
            self.fail_first -= 1
            raise EasynewsError("simulated upstream failure")
        return {"data": [_item(kwargs["query"].replace(" ", "."))]}


@pytest.fixture
def fake(monkeypatch):
    fc = FakeClient()
    monkeypatch.setattr(server, "client", lambda: fc)
    monkeypatch.setattr(server, "CACHE_TTL_SECONDS", 120)
    server._search_cache_clear()
    yield fc
    server._search_cache_clear()


@pytest.fixture
def clock(monkeypatch):
    state = {"t": 1_000_000.0}
    monkeypatch.setattr(server, "_now", lambda: state["t"])
    return state


def _search(q: str, **extra):
    params = {"t": "search", "q": q, "apikey": "testkey", **extra}
    return server.APP.test_client().get("/api", query_string=params)


# --- hits and misses ------------------------------------------------------------

def test_identical_search_within_ttl_calls_client_once(fake, clock):
    r1 = _search("breaking bad")
    clock["t"] += 30
    r2 = _search("breaking bad")
    assert r1.status_code == r2.status_code == 200
    assert len(fake.calls) == 1
    assert b"breaking.bad" in r2.data


def test_different_query_calls_client_again(fake, clock):
    _search("breaking bad")
    _search("better call saul")
    assert len(fake.calls) == 2
    assert [c["query"] for c in fake.calls] == ["breaking bad", "better call saul"]


def test_expired_entry_is_refreshed_not_served(fake, clock):
    _search("breaking bad")
    clock["t"] += 121
    _search("breaking bad")
    assert len(fake.calls) == 2


def test_entry_just_inside_ttl_is_still_served(fake, clock):
    _search("breaking bad")
    clock["t"] += 119
    _search("breaking bad")
    assert len(fake.calls) == 1


def test_ttl_zero_disables_cache(fake, clock, monkeypatch):
    monkeypatch.setattr(server, "CACHE_TTL_SECONDS", 0)
    _search("breaking bad")
    _search("breaking bad")
    assert len(fake.calls) == 2


def test_client_error_is_not_cached(fake, clock, monkeypatch):
    fake.fail_first = 1
    r1 = _search("breaking bad")
    assert r1.status_code >= 500
    r2 = _search("breaking bad")
    assert r2.status_code == 200
    assert len(fake.calls) == 2
    # And the successful response is now cached.
    _search("breaking bad")
    assert len(fake.calls) == 2


def test_cache_key_covers_every_client_parameter(fake, clock):
    # tvsearch appends SxxEyy to the query that reaches the client, so the two
    # requests are different searches even though ?q= is identical.
    _search("breaking bad", t="tvsearch", season="1", ep="1")
    _search("breaking bad", t="tvsearch", season="1", ep="2")
    assert len(fake.calls) == 2


def test_sample_fallback_never_touches_client_or_cache(fake, clock):
    _search("test")
    _search("")
    assert fake.calls == []
    assert server._search_cache_size() == 0


# --- bounds -----------------------------------------------------------------------

def test_cache_is_bounded_and_evicts_oldest(fake, clock, monkeypatch):
    monkeypatch.setattr(server, "SEARCH_CACHE_MAX_ENTRIES", 3)
    for q in ("q1", "q2", "q3"):
        _search(q)
    assert server._search_cache_size() == 3
    _search("q4")  # evicts q1
    assert server._search_cache_size() == 3
    _search("q4")
    _search("q2")
    assert len(fake.calls) == 4  # q1..q4, q4/q2 were hits
    _search("q1")  # was evicted -> miss
    assert len(fake.calls) == 5


# --- env parsing ------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("60", 60), ("0", 0), (" 5 ", 5), ("", 120), ("abc", 120), ("-1", 120),
])
def test_env_int_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("CACHE_TTL_SECONDS", raw)
    assert server._env_int("CACHE_TTL_SECONDS", 120, minimum=0) == expected


def test_env_int_default_when_unset(monkeypatch):
    monkeypatch.delenv("CACHE_TTL_SECONDS", raising=False)
    assert server._env_int("CACHE_TTL_SECONDS", 120, minimum=0) == 120
