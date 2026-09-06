"""Health endpoint and STRICT_MATCHING env var."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("EASYNEWS_USER", "test")
os.environ.setdefault("EASYNEWS_PASS", "test")

import pytest  # noqa: E402

import server  # noqa: E402


# --- /health --------------------------------------------------------------------

def test_health_needs_no_apikey():
    resp = server.APP.test_client().get("/health")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


def test_api_still_requires_apikey():
    resp = server.APP.test_client().get("/api?t=caps")
    assert resp.status_code == 401


# --- STRICT_MATCHING ------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("1", True), ("true", True), ("yes", True), ("on", True),
    ("0", False), ("false", False), ("no", False), ("off", False),
])
def test_env_bool_parses_common_spellings(monkeypatch, raw, expected):
    monkeypatch.setenv("STRICT_MATCHING", raw)
    assert server._env_bool("STRICT_MATCHING", True) is expected


def test_env_bool_falls_back_to_default_when_unset(monkeypatch):
    monkeypatch.delenv("STRICT_MATCHING", raising=False)
    assert server._env_bool("STRICT_MATCHING", True) is True
    assert server._env_bool("STRICT_MATCHING", False) is False


def test_strict_default_applies_to_movie_and_tv_search_only():
    assert server._strict_requested("tvsearch", None) is True
    assert server._strict_requested("movie", None) is True
    assert server._strict_requested("search", None) is False


def test_strict_matching_env_off_disables_default(monkeypatch):
    monkeypatch.setattr(server, "STRICT_MATCHING_DEFAULT", False)
    assert server._strict_requested("tvsearch", None) is False
    assert server._strict_requested("movie", None) is False


def test_per_request_strict_param_overrides_env(monkeypatch):
    monkeypatch.setattr(server, "STRICT_MATCHING_DEFAULT", False)
    assert server._strict_requested("tvsearch", "1") is True
    monkeypatch.setattr(server, "STRICT_MATCHING_DEFAULT", True)
    assert server._strict_requested("tvsearch", "0") is False
