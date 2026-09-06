"""
Easynews API-like client (unofficial) to perform searches and download NZB files.

This client mimics the webapp behavior by calling:
- GET /2.0/search/solr-search for search results (JSON)
- POST /2.0/api/dl-nzb to create/download NZB for selected items

Authentication is cookie-based via username/password POST to the login endpoint.
You'll need a valid Easynews account. Use responsibly and per Easynews TOS.
"""

from __future__ import annotations

import base64
import threading
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests
from requests.exceptions import RequestException


logger = logging.getLogger(__name__)

_DEFAULT_BASE = "https://members.easynews.com"


def _base_url_from_env() -> str:
    """EASYNEWS_BASE_URL (e.g. a proxy or alternate host); must be http(s)."""
    raw = os.environ.get("EASYNEWS_BASE_URL", "").strip().rstrip("/")
    if not raw:
        return _DEFAULT_BASE
    if not raw.lower().startswith(("http://", "https://")):
        logger.warning(
            "Ignoring EASYNEWS_BASE_URL=%r: must start with http:// or https://, using %s",
            raw, _DEFAULT_BASE,
        )
        return _DEFAULT_BASE
    return raw


EASYNEWS_BASE = _base_url_from_env()

_LOGIN_TIMEOUT = 15
_SEARCH_TIMEOUT = 30

# The 3.0 endpoint always returns 100 items per page and ignores page-size params.
_V3_PAGE_SIZE = 100


def _api_version_from_env() -> str:
    """EASYNEWS_API_VERSION: "3.0" (default) or "2.0"; anything else -> 3.0.

    3.0 is preferred: with no sort parameter it ranks by relevance as well as
    2.0 does, returns richer per-file metadata, and allows ~10 concurrent
    searches per account instead of 2. Do NOT send s1=relevance to it.
    """
    raw = (os.environ.get("EASYNEWS_API_VERSION") or "").strip()
    return raw if raw in ("2.0", "3.0") else "3.0"


def _max_concurrent_from_env() -> int:
    """EASYNEWS_MAX_CONCURRENT_SEARCHES: positive int, default 2.

    Easynews allows at most two concurrent searches per account on the 2.0
    endpoint and answers over-cap requests with an empty body; 3.0 allows
    about ten but shares the counter. 2 is safe for either.
    """
    raw = (os.environ.get("EASYNEWS_MAX_CONCURRENT_SEARCHES") or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return 2
    return value if value > 0 else 2


def _make_search_semaphore(n: int) -> threading.BoundedSemaphore:
    return threading.BoundedSemaphore(n)


# Process-wide: caps in-flight Easynews searches for this account. Only
# effective across threads of one process, so run one gunicorn worker with
# threads (see Dockerfile) rather than several worker processes.
_SEARCH_SEMAPHORE = _make_search_semaphore(_max_concurrent_from_env())
_DOWNLOAD_TIMEOUT = 60


class EasynewsError(Exception):
    pass


@dataclass
class SearchItem:
    id: Optional[str]
    hash: str
    filename: str
    ext: str
    sig: Optional[str]
    type: str
    raw: Dict[str, Any]

    @property
    def value_token(self) -> str:
        """
        Build the value string Easynews expects for checkbox selections:
        format: "{hash}|{b64(filename)}:{b64(ext)}"
        As seen in members.js createNZB -> it reads from input[checkbox].value
        """
        fn_b64 = base64.b64encode(self.filename.encode()).decode().replace("=", "")
        ext_b64 = base64.b64encode(self.ext.encode()).decode().replace("=", "")
        return f"{self.hash}|{fn_b64}:{ext_b64}"


class EasynewsClient:
    def __init__(
        self,
        username: str,
        password: str,
        session: Optional[requests.Session] = None,
        api_version: Optional[str] = None,
    ):
        self.username = username
        self.password = password
        self.api_version = api_version if api_version in ("2.0", "3.0") else _api_version_from_env()
        self.s = session or requests.Session()
        # Default headers
        self.s.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) EasynewsClient/1.0",
                "Accept": "application/json, text/javascript, */*; q=0.9",
            }
        )
        # Use HTTP Basic Auth for endpoints that support it
        self.s.auth = (self.username, self.password)

    def login(self) -> None:
        """
        Prime session and validate credentials using a quick authenticated call.
        This relies on HTTP Basic Auth configured on the session.
        """
        try:
            self.s.get(f"{EASYNEWS_BASE}/2.0/", timeout=_LOGIN_TIMEOUT)
            check = self.s.get(
                f"{EASYNEWS_BASE}/2.0/search/solr-search/?fly=2&gps=test&sb=1&pno=1&pby=1&u=1&chxu=1&chxgx=1&st=basic&s1=dtime&s1d=-&sS=3&vv=1&fty%5B%5D=VIDEO",
                allow_redirects=True,
                timeout=_LOGIN_TIMEOUT,
            )
        except RequestException as e:
            logger.exception("Network error during Easynews login")
            raise EasynewsError(f"Network error during Easynews login: {e}") from e
        if check.status_code in (401, 403):
            raise EasynewsError("Unauthorized; check username/password")

    def search(
        self,
        query: str,
        file_type: str = "VIDEO",
        page: int = 1,
        per_page: int = 50,
        sort_field: Optional[str] = "dtime",
        sort_dir: str = "-",
        safe_off: int = 0,
    ) -> Dict[str, Any]:
        """Search Easynews; returns the raw JSON dict (data + pagination fields).

        Uses the 3.0 API by default (fixed 100 items/page, so per_page > 100
        fetches and merges several pages). EASYNEWS_API_VERSION=2.0 selects the
        legacy Solr endpoint. Every request goes through the account-wide
        concurrency semaphore.
        """
        if self.api_version == "3.0":
            return self._search_v3(query, file_type, page, per_page, sort_field, sort_dir, safe_off)
        with _SEARCH_SEMAPHORE:
            return self._search_v2(query, file_type, page, per_page, sort_field, sort_dir, safe_off)

    def _search_v3(
        self,
        query: str,
        file_type: str,
        page: int,
        per_page: int,
        sort_field: Optional[str],
        sort_dir: str,
        safe_off: int,
    ) -> Dict[str, Any]:
        if file_type != "VIDEO":
            file_type = "VIDEO"
        wanted_pages = max(1, -(-max(1, per_page) // _V3_PAGE_SIZE))  # ceil
        merged: Optional[Dict[str, Any]] = None
        seen: set = set()
        pno = max(1, page)
        for _ in range(wanted_pages):
            params = {
                "gps": query,
                "pno": str(pno),
                "u": "1",
                "safeO": str(safe_off),
                "fty[]": file_type,
            }
            # 3.0 ranks by relevance only when no sort is sent; passing
            # s1=relevance makes it fall back to filename order.
            if sort_field and sort_field != "relevance":
                params["s1"] = sort_field
                params["s1d"] = sort_dir
            url = f"{EASYNEWS_BASE}/3.0/api/search"
            try:
                with _SEARCH_SEMAPHORE:
                    r = self.s.get(url, params=params, timeout=_SEARCH_TIMEOUT)
                r.raise_for_status()
                if not r.text:
                    raise EasynewsError("Easynews returned an empty response (over the concurrency cap?)")
                j = r.json()
            except RequestException as e:
                raise EasynewsError(f"Search request failed: {e}") from e
            except ValueError as e:
                raise EasynewsError(f"Invalid JSON from Easynews: {e}") from e
            if merged is None:
                merged = dict(j)
                merged["data"] = []
            for it in j.get("data") or []:
                key = it.get("hash") if isinstance(it, dict) else None
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                merged["data"].append(it)
            num_pages = int(j.get("numPages") or 1)
            if pno >= num_pages:
                break
            pno += 1
        return merged or {"data": []}

    def search_window(
        self,
        query: str,
        d1: str,
        d2: str,
        page: int = 1,
        per_page: int = 250,
    ) -> Dict[str, Any]:
        """2.0 search restricted to a posting-date window (d1..d2 as
        "YYYY-MM-DD HH:MM:SS"), largest files first. Used by the sibling pivot;
        the 2.0 endpoint is the one known to honour d1/d2."""
        params = {
            "fly": "2",
            "sb": "1",
            "pno": str(max(1, page)),
            "pby": str(per_page),
            "u": "1",
            "chxu": "1",
            "chxgx": "1",
            "st": "adv",
            "gps": query,
            "vv": "1",
            "safeO": "0",
            "s1": "dsize",
            "s1d": "-",
            "d1": d1,
            "d2": d2,
        }
        qs = "&".join(f"{k}={requests.utils.quote(str(v))}" for k, v in params.items()) + "&fty%5B%5D=VIDEO"
        url = f"{EASYNEWS_BASE}/2.0/search/solr-search/?{qs}"
        try:
            with _SEARCH_SEMAPHORE:
                r = self.s.get(url, timeout=_SEARCH_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except RequestException as e:
            raise EasynewsError(f"Window search failed: {e}") from e
        except ValueError as e:
            raise EasynewsError(f"Invalid JSON from Easynews: {e}") from e

    def _search_v2(
        self,
        query: str,
        file_type: str = "VIDEO",
        page: int = 1,
        per_page: int = 50,
        sort_field: Optional[str] = "dtime",
        sort_dir: str = "-",
        safe_off: int = 0,
    ) -> Dict[str, Any]:
        """Legacy 2.0 Solr endpoint (the same one the website uses)."""
        if file_type != "VIDEO":
            # Enforce VIDEO only as requested
            file_type = "VIDEO"

        params = {
            # Backend selector, 1 = solr-search
            "fly": "2",
            "sb": "1",
            "pno": str(page),
            "pby": str(per_page),
            "u": "1",
            "chxu": "1",
            "chxgx": "1",
            "st": "basic",
            "gps": query,
            "vv": "1",  # for VIDEO hover/preview data
            "safeO": str(safe_off),
        }
        if sort_field:
            params["s1"] = sort_field
            params["s1d"] = sort_dir

        # fty[] is a repeated parameter
        url = f"{EASYNEWS_BASE}/2.0/search/solr-search/"
        # Manually build query string to include array param
        query_params = (
            "&".join([f"{k}={requests.utils.quote(v)}" for k, v in params.items()])
            + f"&fty%5B%5D={requests.utils.quote(file_type)}"
        )
        full_url = f"{url}?{query_params}"

        try:
            r = self.s.get(full_url, timeout=_SEARCH_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except RequestException as e:
            logger.exception("Search request failed for query '%s'", query)
            raise EasynewsError(f"Search request failed: {e}") from e

    @staticmethod
    def _collect_items(json_data: Dict[str, Any]) -> List[SearchItem]:
        items: List[SearchItem] = []
        for it in json_data.get("data", []):
            hash_id = ""
            filename_no_ext = ""
            ext = ""
            sig: Optional[str] = None
            typ = ""
            item_id: Optional[str] = None

            if isinstance(it, list):
                if len(it) >= 12:
                    hash_id = it[0]
                    filename_no_ext = it[10]
                    ext = it[11]
            elif isinstance(it, dict):
                # Some APIs may return dict with numeric keys as strings
                if "0" in it:
                    hash_id = it.get("0", "")
                if "10" in it:
                    filename_no_ext = it.get("10", "")
                if "11" in it:
                    ext = it.get("11", "")
                sig = it.get("sig")
                typ = it.get("type", "")
                item_id = it.get("id")

            if not hash_id or not ext:
                # Skip malformed entries
                continue

            items.append(
                SearchItem(
                    id=item_id,
                    hash=hash_id,
                    filename=filename_no_ext,
                    ext=ext,
                    sig=sig,
                    type=typ,
                    raw=it if isinstance(it, dict) else {},
                )
            )
        return items

    def build_nzb_payload(
        self,
        items: List[SearchItem],
        name: Optional[str] = None,
    ) -> Dict[str, str]:
        """
        Build the form-encoded payload expected by /2.0/api/dl-nzb.
        Emulates createNZB() from members.js which submits hidden inputs of the checked items.
        Keys look like "{index}&sig={sig}" and value is value_token.
        We'll just use sequential indexes starting at 0.
        """
        data: Dict[str, str] = {"autoNZB": "1"}
        for idx, it in enumerate(items):
            key = str(idx)
            if it.sig:
                key = f"{idx}&sig={it.sig}"
            data[key] = it.value_token
        if name:
            data["nameZipQ0"] = name
        # The site posts to /2.0/api/dl-nzb and returns the NZB file content (application/x-nzb or xml)
        return data

    def download_nzb(self, payload: Dict[str, str], out_path: str) -> str:
        url = f"{EASYNEWS_BASE}/2.0/api/dl-nzb"
        try:
            r = self.s.post(url, data=payload, stream=True, timeout=_DOWNLOAD_TIMEOUT)
        except RequestException as e:
            logger.exception("NZB download request failed")
            raise EasynewsError(f"NZB download request failed: {e}") from e
        if r.status_code != 200:
            raise EasynewsError(f"NZB creation failed: HTTP {r.status_code}")

        content_type = r.headers.get("Content-Type", "")
        if "xml" not in content_type and "nzb" not in content_type:
            # Sometimes returns text/html with a redirect page; still try to save
            pass

        content = r.content.replace(
            b'date=""', b'date="0"'
        )  # normalize empty NZB date fields
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(content)
        return out_path

    def search_and_nzb(
        self,
        query: str,
        file_type: str = "VIDEO",
        max_items: int = 5,
        nzb_name: Optional[str] = None,
        out_path: str = "download.nzb",
    ) -> str:
        data = self.search(query=query, file_type=file_type)
        items = self._collect_items(data)
        if not items:
            raise EasynewsError("No results found for query")
        sel = items[:max_items]
        payload = self.build_nzb_payload(sel, name=nzb_name)
        return self.download_nzb(payload, out_path)


__all__ = ["EasynewsClient", "EasynewsError", "SearchItem"]
