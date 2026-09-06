"""Configurable defaults: EASYNEWS_BASE_URL, DEFAULT_MIN_SIZE_MB, DEFAULT_LIMIT.

No network: the Easynews client is swapped for a fake only where a request
would otherwise go out; everything else is the real code.
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("EASYNEWS_USER", "test")
os.environ.setdefault("EASYNEWS_PASS", "test")

import pytest  # noqa: E402

import easynews_client  # noqa: E402
import server  # noqa: E402

MB = 1024 * 1024


# --- fakes -----------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code=200, payload=None, content=b"<nzb/>"):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"data": []}
        self.content = content
        self.headers = {"Content-Type": "application/x-nzb"}

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload

    @property
    def text(self):
        return json.dumps(self._payload)


class FakeSession:
    """Records every URL requests.Session.get/post would have hit."""

    def __init__(self, payload=None):
        self.headers = {}
        self.auth = None
        self.calls = []
        self._payload = payload

    def get(self, url, **kwargs):
        self.calls.append(("GET", url))
        return FakeResponse(payload=self._payload)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url))
        return FakeResponse()


class FakeClient:
    """Stands in for server.client(): a logged-in EasynewsClient."""

    def __init__(self, search_payload=None):
        self.s = FakeSession()
        self._search_payload = search_payload or {"data": []}

    def search(self, **kwargs):
        return self._search_payload

    def build_nzb_payload(self, items, name=None):
        return {"autoNZB": "1"}


def _item(name, size_mb):
    return {
        "hash": f"hash-{name}",
        "fn": f"foo {name}",
        "ext": ".mkv",
        "size": size_mb * MB,
        "type": "VIDEO",
    }


def _search(monkeypatch, items, **params):
    monkeypatch.setattr(server, "client", lambda: FakeClient({"data": items}))
    query = {"t": "search", "q": "foo", "apikey": server.API_KEY, **params}
    resp = server.APP.test_client().get("/api", query_string=query)
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def _run_isolated(code, cwd=ROOT, **env):
    """Import a module in a fresh interpreter so import-time env parsing is real.

    A value of None removes the variable from the child's environment.
    """
    full_env = {
        **os.environ, "PYTHONPATH": ROOT,
        "EASYNEWS_USER": "test", "EASYNEWS_PASS": "test", **env,
    }
    full_env = {k: v for k, v in full_env.items() if v is not None}
    return subprocess.run(
        [sys.executable, "-c", code], cwd=cwd, env=full_env,
        capture_output=True, text=True,
    )


# --- _env_int ---------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [("250", 250), (" 7 ", 7), ("0", 0)])
def test_env_int_parses_valid_values(monkeypatch, raw, expected):
    monkeypatch.setenv("SOME_INT", raw)
    assert server._env_int("SOME_INT", 100) == expected


@pytest.mark.parametrize("raw", ["abc", "", "  ", "1.5", "-5", "10MB"])
def test_env_int_falls_back_to_default_on_invalid(monkeypatch, raw):
    monkeypatch.setenv("SOME_INT", raw)
    assert server._env_int("SOME_INT", 100) == 100


def test_env_int_falls_back_to_default_when_unset(monkeypatch):
    monkeypatch.delenv("SOME_INT", raising=False)
    assert server._env_int("SOME_INT", 100) == 100


def test_env_int_enforces_minimum(monkeypatch):
    monkeypatch.setenv("SOME_INT", "0")
    assert server._env_int("SOME_INT", 100, minimum=1) == 100


def test_invalid_int_env_does_not_crash_import():
    result = _run_isolated(
        "import server; print(server.DEFAULT_LIMIT, server.DEFAULT_MIN_SIZE_MB)",
        DEFAULT_LIMIT="lots", DEFAULT_MIN_SIZE_MB="big",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["100", "100"]


# --- EASYNEWS_BASE_URL ---------------------------------------------------------------

def test_base_url_env_is_read_and_trailing_slash_stripped():
    result = _run_isolated(
        "import easynews_client; print(easynews_client.EASYNEWS_BASE)",
        EASYNEWS_BASE_URL="https://example.test/",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "https://example.test"


def test_base_url_defaults_to_members_easynews():
    result = _run_isolated(
        "import easynews_client; print(easynews_client.EASYNEWS_BASE)",
        EASYNEWS_BASE_URL="",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "https://members.easynews.com"


def test_base_url_from_dotenv_is_honoured(tmp_path):
    # server imports easynews_client; the .env must be loaded before that import.
    (tmp_path / ".env").write_text(
        "EASYNEWS_BASE_URL=https://from-dotenv.test/\nDEFAULT_LIMIT=7\n"
    )
    result = _run_isolated(
        "import server, easynews_client; "
        "print(easynews_client.EASYNEWS_BASE, server.DEFAULT_LIMIT)",
        cwd=str(tmp_path), EASYNEWS_BASE_URL=None, DEFAULT_LIMIT=None,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["https://from-dotenv.test", "7"]


@pytest.mark.parametrize("raw", ["members.easynews.com", "ftp://x.test", "://nope"])
def test_base_url_without_http_scheme_falls_back_to_default(raw):
    result = _run_isolated(
        "import easynews_client; print(easynews_client.EASYNEWS_BASE)",
        EASYNEWS_BASE_URL=raw,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "https://members.easynews.com"
    assert "EASYNEWS_BASE_URL" in result.stderr


def test_client_search_url_uses_base_url(monkeypatch):
    monkeypatch.setattr(easynews_client, "EASYNEWS_BASE", "https://example.test")
    session = FakeSession(payload={"data": []})
    client = easynews_client.EasynewsClient("u", "p", session=session, api_version="3.0")
    client.search("foo")
    method, url = session.calls[-1]
    assert method == "GET"
    assert url.startswith("https://example.test/3.0/api/search")
    assert "members.easynews.com" not in url

    legacy = easynews_client.EasynewsClient("u", "p", session=session, api_version="2.0")
    legacy.search("foo")
    _, url = session.calls[-1]
    assert url.startswith("https://example.test/2.0/search/solr-search/?")


def test_client_login_and_download_urls_use_base_url(monkeypatch, tmp_path):
    monkeypatch.setattr(easynews_client, "EASYNEWS_BASE", "https://example.test")
    session = FakeSession()
    client = easynews_client.EasynewsClient("u", "p", session=session)
    client.login()
    client.download_nzb({"autoNZB": "1"}, str(tmp_path / "x.nzb"))
    urls = [u for _, u in session.calls]
    assert urls and all(u.startswith("https://example.test/") for u in urls)
    assert ("POST", "https://example.test/2.0/api/dl-nzb") in session.calls


def test_server_nzb_download_url_uses_base_url(monkeypatch):
    monkeypatch.setattr(easynews_client, "EASYNEWS_BASE", "https://example.test")
    fake = FakeClient()
    monkeypatch.setattr(server, "client", lambda: fake)
    enc = server.encode_id({"hash": "h", "filename": "f", "ext": ".mkv", "title": "T"})
    resp = server.APP.test_client().get(
        "/api", query_string={"t": "get", "id": enc, "apikey": server.API_KEY}
    )
    assert resp.status_code == 200
    assert fake.s.calls == [("POST", "https://example.test/2.0/api/dl-nzb")]


# --- DEFAULT_MIN_SIZE_MB ---------------------------------------------------------------

def test_min_size_default_applies_when_param_absent(monkeypatch):
    monkeypatch.setattr(server, "DEFAULT_MIN_SIZE_MB", 50)
    assert server._resolve_min_size_mb(None) == 50


def test_min_size_floor_is_default_not_hardcoded_100(monkeypatch):
    monkeypatch.setattr(server, "DEFAULT_MIN_SIZE_MB", 50)
    assert server._resolve_min_size_mb("10") == 50
    assert server._resolve_min_size_mb("75") == 75
    assert server._resolve_min_size_mb("garbage") == 50


def test_search_honours_lower_configured_min_size(monkeypatch):
    monkeypatch.setattr(server, "DEFAULT_MIN_SIZE_MB", 50)
    items = [_item("small", 20), _item("mid", 60), _item("big", 200)]
    xml = _search(monkeypatch, items)
    assert "<title>foo.mid.mkv</title>" in xml and "<title>foo.big.mkv</title>" in xml
    assert "foo.small.mkv" not in xml
    xml = _search(monkeypatch, items, minsize="10")
    assert "<title>foo.mid.mkv</title>" in xml and "foo.small.mkv" not in xml


# --- DEFAULT_LIMIT ---------------------------------------------------------------------

def test_caps_limits_reflect_default_limit(monkeypatch):
    monkeypatch.setattr(server, "DEFAULT_LIMIT", 42)
    resp = server.APP.test_client().get(
        "/api", query_string={"t": "caps", "apikey": server.API_KEY}
    )
    assert resp.status_code == 200
    assert '<limits max="42" default="42"/>' in resp.get_data(as_text=True)


def test_search_uses_default_limit_when_param_absent(monkeypatch):
    monkeypatch.setattr(server, "DEFAULT_LIMIT", 3)
    items = [_item(f"n{i}", 500) for i in range(5)]
    xml = _search(monkeypatch, items)
    assert xml.count("<item>") == 3
    xml = _search(monkeypatch, items, limit="2")
    assert xml.count("<item>") == 2


def test_limit_param_is_capped_at_default_limit(monkeypatch):
    # caps advertises max=DEFAULT_LIMIT, so a request can't exceed it.
    monkeypatch.setattr(server, "DEFAULT_LIMIT", 3)
    items = [_item(f"n{i}", 500) for i in range(5)]
    xml = _search(monkeypatch, items, limit="1000")
    assert xml.count("<item>") == 3


@pytest.mark.parametrize("raw", ["", "abc", "0", "-1", "1.5"])
def test_invalid_limit_param_falls_back_to_default(monkeypatch, raw):
    monkeypatch.setattr(server, "DEFAULT_LIMIT", 2)
    items = [_item(f"n{i}", 500) for i in range(5)]
    xml = _search(monkeypatch, items, limit=raw)
    assert xml.count("<item>") == 2


@pytest.mark.parametrize("raw", ["", "abc", "-1"])
def test_invalid_offset_param_falls_back_to_zero(monkeypatch, raw):
    monkeypatch.setattr(server, "DEFAULT_LIMIT", 10)
    items = [_item(f"n{i}", 500) for i in range(3)]
    xml = _search(monkeypatch, items, offset=raw)
    assert xml.count("<item>") == 3
    xml = _search(monkeypatch, items, offset="1")
    assert xml.count("<item>") == 2


def test_default_limit_is_capped_at_upstream_page_size():
    # Easynews is queried for at most UPSTREAM_PAGE_SIZE results, so caps
    # must not advertise more than can ever be returned.
    result = _run_isolated(
        "import server; print(server.DEFAULT_LIMIT, server.UPSTREAM_PAGE_SIZE)",
        DEFAULT_LIMIT="500",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["250", "250"]
    assert "DEFAULT_LIMIT" in result.stderr


# --- GUNICORN_WORKERS (Dockerfile CMD) ---------------------------------------------------

def _docker_cmd():
    import json
    with open(os.path.join(ROOT, "Dockerfile")) as f:
        for line in f:
            if line.startswith("CMD "):
                return json.loads(line[4:])
    raise AssertionError("no CMD in Dockerfile")


@pytest.mark.parametrize("raw,expected", [
    (None, "1"), ("", "1"), ("abc", "1"), ("0", "1"), ("-2", "1"), ("2", "2"), ("12", "12"),
])
def test_dockerfile_cmd_falls_back_on_invalid_worker_count(raw, expected):
    cmd = _docker_cmd()
    assert cmd[:2] == ["sh", "-c"]
    # Run the real CMD shell snippet with gunicorn swapped for echo.
    script = cmd[2].replace("gunicorn ", "echo ")
    env = {"PATH": os.environ["PATH"], "PORT": "8081"}
    if raw is not None:
        env["GUNICORN_WORKERS"] = raw
    result = subprocess.run(["sh", "-c", script], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert f"--workers {expected} " in result.stdout


@pytest.mark.parametrize("raw,expected", [
    (None, "8"), ("", "8"), ("abc", "8"), ("0", "8"), ("4", "4"),
])
def test_dockerfile_cmd_threads_default_and_fallback(raw, expected):
    cmd = _docker_cmd()
    script = cmd[2].replace("gunicorn ", "echo ")
    env = {"PATH": os.environ["PATH"], "PORT": "8081"}
    if raw is not None:
        env["GUNICORN_THREADS"] = raw
    result = subprocess.run(["sh", "-c", script], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert f"--threads {expected} " in result.stdout
