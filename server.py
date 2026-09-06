import base64
import html
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote

import requests
from flask import Flask, Response, request


def _load_dotenv():
    path = os.path.join(os.getcwd(), ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    os.environ.setdefault(k, v)
    except Exception:
        pass


# Must run before importing easynews_client: it reads EASYNEWS_BASE_URL at import.
_load_dotenv()

import easynews_client  # noqa: E402
import pivot  # noqa: E402
from easynews_client import EasynewsClient, EasynewsError, SearchItem  # noqa: E402


logger = logging.getLogger(__name__)

APP = Flask(__name__)
_CLIENT: Optional[EasynewsClient] = None
_CLIENT_LOCK = threading.Lock()
_CLIENT_LOGIN_TTL = 600  # seconds
_CLIENT_LAST_LOGIN: float = 0.0

API_KEY = os.environ.get("NEWZNAB_APIKEY", "testkey")
EZ_USER = os.environ.get("EASYNEWS_USER")
EZ_PASS = os.environ.get("EASYNEWS_PASS")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    """Integer env var; unset, unparseable or < minimum falls back to default."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        logger.warning("Ignoring %s=%r: not an integer, using %d", name, raw, default)
        return default
    if value < minimum:
        logger.warning("Ignoring %s=%d: below minimum %d, using %d", name, value, minimum, default)
        return default
    return value


# Sibling pivot: recover obfuscated full releases from readable sample clips
# (see pivot.py). Bounded per search by PIVOT_BUDGET_SECONDS; results are
# cached per sample so repeated arr searches do not rescan.
SIBLING_PIVOT = _env_bool("SIBLING_PIVOT", True)
PIVOT_BUDGET_SECONDS = _env_int("PIVOT_BUDGET_SECONDS", 20, minimum=0)
PIVOT_CACHE_TTL_SECONDS = _env_int("PIVOT_CACHE_TTL_SECONDS", 6 * 3600, minimum=60)
PIVOT_SEED_MIN_BYTES = 5 * 1024 * 1024
_PIVOT_CACHE: Dict[str, Tuple[float, List[dict]]] = {}
_PIVOT_CACHE_LOCK = threading.Lock()


def _pivot_cache_clear() -> None:
    with _PIVOT_CACHE_LOCK:
        _PIVOT_CACHE.clear()


def _pivot_backend() -> Any:
    return pivot.EasynewsPivotBackend(client(), easynews_client.EASYNEWS_BASE)


def _run_pivot(seed_items: List[dict]) -> List[dict]:
    """seed_items are raw Easynews sample records. Returns confirmed siblings as
    filter_and_map-shaped result dicts, served from the per-seed cache when fresh."""
    now = _now()
    todo: List[dict] = []
    found: List[dict] = []
    with _PIVOT_CACHE_LOCK:
        for raw in seed_items:
            h = str(raw.get("hash") or raw.get("0") or "")
            entry = _PIVOT_CACHE.get(h)
            if entry is not None and now < entry[0]:
                found.extend(entry[1])
            else:
                todo.append(raw)
    if todo and PIVOT_BUDGET_SECONDS > 0:
        results = pivot.pivot_from_samples(todo, _pivot_backend(), budget_s=PIVOT_BUDGET_SECONDS)
        by_seed: Dict[str, List[dict]] = {}
        for r in results:
            by_seed.setdefault(r["pivot_seed"], []).append(r)
        with _PIVOT_CACHE_LOCK:
            for raw in todo:
                h = str(raw.get("hash") or raw.get("0") or "")
                _PIVOT_CACHE[h] = (now + PIVOT_CACHE_TTL_SECONDS, by_seed.get(h, []))
        found.extend(results)
    return [_pivot_result_to_item(r) for r in found]


def _pivot_result_to_item(r: dict) -> dict:
    title = r["title"]
    raw = r.get("raw") or {}
    fullres = raw.get("fullres")
    if not fullres and raw.get("width") and raw.get("height"):
        fullres = f"{raw.get('width')} x {raw.get('height')}"
    quality = _extract_quality(title, fullres)
    meta = _extract_release_markers(title, quality)
    if not quality and meta.get("quality"):
        quality = meta.get("quality")
    return {
        "hash": r["hash"],
        "filename": r["filename"],
        "ext": r["ext"],
        "sig": r.get("sig"),
        "size": r["size"],
        "title": title,
        "poster": raw.get("poster") or raw.get("7"),
        "posted": r.get("posted"),
        "duration": r.get("runtime"),
        "duration_hms": _format_duration(r.get("runtime")),
        "quality": quality,
        "thumbnail": None,
        "year": meta.get("year"),
        "season": meta.get("season"),
        "episode": meta.get("episode"),
        "pivot": True,
    }


# Default strictness for t=movie / t=tvsearch (plain t=search is never strict
# by default). Per-request ?strict=0|1 always wins.
STRICT_MATCHING_DEFAULT = _env_bool("STRICT_MATCHING", True)

# Minimum file size in MB: the default when ?minsize= is absent and the floor
# applied to any ?minsize= value (a request can never go below it).
DEFAULT_MIN_SIZE_MB = _env_int("DEFAULT_MIN_SIZE_MB", 100)

# Results requested from Easynews per search; nothing beyond this can be returned.
# Results fetched from Easynews per search and the hard ceiling for ?limit=.
# On 3.0 (100 items/page) this fetches ceil(MAX_RESULTS/100) pages, so larger
# values cost proportionally more upstream page requests. Bounded to [100,1000].
UPSTREAM_PAGE_SIZE = min(1000, max(100, _env_int("MAX_RESULTS", 250, minimum=1)))

# Result count when ?limit= is absent and the hard maximum for ?limit=; also
# advertised as max/default in caps <limits>. Capped at UPSTREAM_PAGE_SIZE so
# caps never promise more than a search can deliver.
DEFAULT_LIMIT = _env_int("DEFAULT_LIMIT", 100, minimum=1)
if DEFAULT_LIMIT > UPSTREAM_PAGE_SIZE:
    logger.warning(
        "DEFAULT_LIMIT=%d exceeds the %d results Easynews returns per search; using %d",
        DEFAULT_LIMIT, UPSTREAM_PAGE_SIZE, UPSTREAM_PAGE_SIZE,
    )
    DEFAULT_LIMIT = UPSTREAM_PAGE_SIZE


def _int_param(raw: Optional[str], default: int, minimum: int) -> int:
    """Query-string integer; blank, unparseable or < minimum falls back to default."""
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return value if value >= minimum else default


def _resolve_min_size_mb(min_size_param: Optional[str]) -> int:
    return _int_param(min_size_param, DEFAULT_MIN_SIZE_MB, minimum=DEFAULT_MIN_SIZE_MB)


def _resolve_limit(limit_param: Optional[str]) -> int:
    return min(_int_param(limit_param, DEFAULT_LIMIT, minimum=1), DEFAULT_LIMIT)


def _resolve_offset(offset_param: Optional[str]) -> int:
    return _int_param(offset_param, 0, minimum=0)


def _strict_requested(t: str, strict_param: Optional[str]) -> bool:
    if strict_param is not None:
        return strict_param.strip().lower() not in {"0", "false", "no", "off"}
    return STRICT_MATCHING_DEFAULT and t in {"movie", "tvsearch"}


# --- Search-result cache -------------------------------------------------------
#
# Sonarr/Radarr (several instances, all via Prowlarr) fire the same search
# repeatedly within minutes. Cache the raw Easynews search response for a short
# TTL so identical searches don't trigger duplicate Easynews round trips.
#
# Keyed by the exact kwargs passed to EasynewsClient.search(). Only real
# searches are cached: the empty-query sample fallback never reaches Easynews,
# and t=get NZB downloads are never cached. Errors are never cached.
#
# The cache is per-process: gunicorn runs sync workers as separate processes,
# so each of the N workers holds its own cache and a repeated search may hit
# Easynews up to N times before every worker is warm. The lock only guards
# the threads within one process.
CACHE_TTL_SECONDS = _env_int("CACHE_TTL_SECONDS", 120, minimum=0)  # 0 disables
SEARCH_CACHE_MAX_ENTRIES = 256

_SearchCacheKey = Tuple[Tuple[str, Any], ...]
_SEARCH_CACHE: "OrderedDict[_SearchCacheKey, Tuple[float, Dict[str, Any]]]" = OrderedDict()
_SEARCH_CACHE_LOCK = threading.Lock()


def _now() -> float:
    # Indirection so tests can drive the clock without sleeping.
    return time.time()


def _search_cache_clear() -> None:
    with _SEARCH_CACHE_LOCK:
        _SEARCH_CACHE.clear()


def _search_cache_size() -> int:
    with _SEARCH_CACHE_LOCK:
        return len(_SEARCH_CACHE)


def _cached_search(**search_kwargs: Any) -> Dict[str, Any]:
    """client().search(**search_kwargs), served from the TTL cache when fresh."""
    ttl = CACHE_TTL_SECONDS
    if ttl <= 0:
        return client().search(**search_kwargs)

    key: _SearchCacheKey = tuple(sorted(search_kwargs.items()))
    now = _now()
    with _SEARCH_CACHE_LOCK:
        entry = _SEARCH_CACHE.get(key)
        if entry is not None and now < entry[0]:
            return entry[1]

    # Miss or expired: fetch outside the lock so unrelated searches don't
    # serialize behind one Easynews round trip. Exceptions propagate and
    # leave the cache untouched, so the next request retries.
    data = client().search(**search_kwargs)

    with _SEARCH_CACHE_LOCK:
        _SEARCH_CACHE.pop(key, None)
        _SEARCH_CACHE[key] = (now + ttl, data)
        while len(_SEARCH_CACHE) > SEARCH_CACHE_MAX_ENTRIES:
            _SEARCH_CACHE.popitem(last=False)  # evict oldest insertion
    return data


def require_apikey() -> bool:
    key = request.args.get("apikey") or request.headers.get("X-Api-Key")
    return (API_KEY is None) or (key == API_KEY)


def client() -> EasynewsClient:
    if not EZ_USER or not EZ_PASS:
        raise RuntimeError("Set EASYNEWS_USER and EASYNEWS_PASS environment variables")
    global _CLIENT, _CLIENT_LAST_LOGIN
    with _CLIENT_LOCK:
        now = time.time()
        if _CLIENT is None:
            _CLIENT = EasynewsClient(EZ_USER, EZ_PASS)
            _CLIENT.login()
            _CLIENT_LAST_LOGIN = now
        elif now - _CLIENT_LAST_LOGIN > _CLIENT_LOGIN_TTL:
            try:
                _CLIENT.login()
            except EasynewsError:
                _CLIENT = EasynewsClient(EZ_USER, EZ_PASS)
                _CLIENT.login()
            _CLIENT_LAST_LOGIN = time.time()
        return _CLIENT


def xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def encode_id(item: dict) -> str:
    # Pack info needed to build NZB for a single selection and preserve title for filename
    payload = {
        "hash": item.get("hash"),
        "filename": item.get("filename"),
        "ext": item.get("ext"),
        "sig": item.get("sig"),
        "title": item.get("title"),
    }
    if item.get("sample"):
        payload["sample"] = True
    raw = (
        base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False).encode())
        .decode()
        .rstrip("=")
    )
    return raw


def decode_id(enc: str) -> dict:
    pad = "=" * (-len(enc) % 4)
    raw = base64.urlsafe_b64decode(enc + pad).decode()
    return json.loads(raw)


def to_search_item(d: dict) -> SearchItem:
    return SearchItem(
        id=None,
        hash=d["hash"],
        filename=d["filename"],
        ext=d["ext"],
        sig=d.get("sig"),
        type="VIDEO",
        raw={},
    )


_TITLE_PARENS_RE = re.compile(r"\(([^()]*)\)")


def _normalize_title(raw: str) -> str:
    text = html.unescape(raw or "").strip()
    if not text:
        return text
    matches = _TITLE_PARENS_RE.findall(text)
    for candidate in reversed(matches):
        cleaned = candidate.strip()
        if cleaned:
            return cleaned
    return text


def _coerce_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            try:
                return datetime.fromtimestamp(int(text), tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                dt = datetime.strptime(text.replace("Z", "+0000"), fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except ValueError:
                continue
    return None


_ALLOWED_VIDEO_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".m4v",
    ".avi",
    ".ts",
    ".mov",
    ".wmv",
    ".mpg",
    ".mpeg",
    ".flv",
    ".webm",
}

_STOPWORDS = {
    "the",
    "a",
    "an",
    "and",
    "of",
    "in",
    "for",
    "on",
}

_MIN_DURATION_SECONDS = 60
_TOKEN_SPLIT_RE = re.compile(r"[^\w]+", re.UNICODE)
_QUALITY_RE = re.compile(r"(2160|1440|1080|720|480|360)\s*(p|i)?", re.IGNORECASE)
_YEAR_RE = re.compile(r"(19|20)\d{2}")
_SEASON_EP_RE = re.compile(
    r"(?:s(?P<season>\d{1,2})[._ -]?e(?P<episode>\d{1,4})(?!\d)|(?<!\d)(?P<season2>\d{1,2})x(?P<episode2>\d{1,2})(?!\d))",
    re.IGNORECASE,
)
# Anime detection patterns
_ANIME_BRACKET_GROUP_RE = re.compile(r"^\[([^\]]+)\]", re.IGNORECASE)

# Bracketed prefixes that are release tags or broadcasters, not fansub groups.
# Any other [Group] prefix followed by an episode number is treated as anime.
_NON_FANSUB_BRACKET_TAGS = {
    "bbc", "pbs", "itv", "hbo", "amzn", "nf", "dsnp", "atvp",
    "repack", "proper", "real", "rerip", "internal", "readnfo",
    "4k", "uhd", "hdr", "hdr10", "dv", "dubbed", "subbed", "multi",
    "2160p", "1080p", "720p", "480p", "x264", "x265", "hevc", "h264", "h265",
}
# Episode-number candidates. Names are usually dotted ("One.Piece.1176.[1080p]"),
# sometimes spaced ("[Judas] One Piece - 1165"), and absolute episodes may be
# prefixed ("EP1177", "e0083", "Episode 1045").
_EP_CANDIDATE_RE = re.compile(
    r"(?:^|[\s._-])(?P<prefix>(?:ep|e|episode)\.?\s*)?(?P<ep>\d{1,4})(?:v\d)?(?P<after>$|[\s._-].*)",
    re.IGNORECASE,
)
_EP_FOLLOWED_BY_RE = re.compile(
    r"^(?:[\s._-]+(?:\[|\(|\d{3,4}[pi](?![a-z0-9]))|\.(?:mkv|mp4|avi|ts|m4v|webm)$|$)",
    re.IGNORECASE,
)


def _has_absolute_episode(text: str) -> bool:
    """A 1-4 digit number (not a year) that reads as an absolute episode:
    "EP1177"/"e0083"/"Episode 12" anywhere, or a bare number that ends the
    name or is followed by a bracket/paren tag or a resolution token."""
    for m in _EP_CANDIDATE_RE.finditer(text):
        ep = m.group("ep")
        if _YEAR_RE.fullmatch(ep):
            continue
        if m.group("prefix"):
            return True
        if _EP_FOLLOWED_BY_RE.match(m.group("after")):
            return True
    return False


_SANITIZE_SYMBOLS_RE = re.compile(r"[\.\-_:\s]+")
_NON_ALNUM_RE = re.compile(r"[^\w\sÀ-ÿ]")

# Newznab category constants
CATEGORY_MOVIES = 2000
CATEGORY_MOVIES_HD = 2030
CATEGORY_MOVIES_UHD = 2040
CATEGORY_TV = 5000
CATEGORY_TV_HD = 5030
CATEGORY_TV_UHD = 5040
CATEGORY_ANIME = 5070  # Anime as TV subcategory
CATEGORY_OTHER = 7000


def _parse_duration_seconds(raw: Any) -> Optional[int]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        if raw <= 0:
            return None
        return int(raw)
    text = str(raw).strip().lower()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    total = 0
    matched = False
    for label, multiplier in (("h", 3600), ("m", 60), ("s", 1)):
        for part in re.findall(rf"(\d+)\s*{label}", text):
            total += int(part) * multiplier
            matched = True
    if matched:
        return total
    if ":" in text:
        try:
            pieces = [int(p) for p in text.split(":")]
            if len(pieces) == 3:
                h, m, s = pieces
            elif len(pieces) == 2:
                h = 0
                m, s = pieces
            else:
                return None
            return h * 3600 + m * 60 + s
        except ValueError:
            return None
    return None


def _as_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _tokenize(text: str) -> List[str]:
    if not text:
        return []
    normalized = _TOKEN_SPLIT_RE.sub(" ", text.lower())
    tokens = [
        tok for tok in normalized.split() if len(tok) > 1 and tok not in _STOPWORDS
    ]
    return tokens


def _sanitize_phrase(text: str) -> str:
    if not text:
        return ""
    working = text.replace("&", " and ")
    working = _SANITIZE_SYMBOLS_RE.sub(" ", working)
    working = _NON_ALNUM_RE.sub("", working)
    return working.lower().strip()


_SAMPLE_TOKEN_RE = re.compile(r"(?:^|[\s._\-\[(])sample(?=$|[\s._\-\])])", re.IGNORECASE)


def _is_sample_name(name: str) -> bool:
    """True for sample clips: a "sample" token that prefixes the name or sits in
    its second half ("...-sample", "...edith.sample", "sample-<release>").
    A title that merely contains the word early on (Free.Sample.Kings) is kept."""
    if not name:
        return False
    m = _SAMPLE_TOKEN_RE.search(name)
    if not m:
        return False
    start = m.start() + (0 if m.start() == 0 else 1)
    return start == 0 or start >= len(name) // 2


def _is_flagged_item(item: Any, ext: str, duration_seconds: Optional[int]) -> bool:
    passwd = False
    virus = False
    file_type = ""
    if isinstance(item, dict):
        passwd = bool(item.get("passwd") or item.get("password"))
        virus = bool(item.get("virus"))
        file_type = str(item.get("type") or item.get("file_type") or "").upper()
    if passwd or virus:
        return True
    if file_type and file_type != "VIDEO":
        return True
    if ext and ext.lower() not in _ALLOWED_VIDEO_EXTENSIONS:
        return True
    if duration_seconds is not None and duration_seconds < _MIN_DURATION_SECONDS:
        return True
    return False


def _format_duration(seconds: Optional[int]) -> Optional[str]:
    if seconds is None:
        return None
    if seconds <= 0:
        return None
    td = timedelta(seconds=seconds)
    total_seconds = int(td.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{secs:02}"


def _extract_quality(*texts: Optional[str]) -> Optional[str]:
    for text in texts:
        if not text:
            continue
        lowered = text.lower()
        if "4k" in lowered:
            return "2160p"
        match = _QUALITY_RE.search(lowered)
        if match:
            value = match.group(1)
            suffix = match.group(2) or "p"
            return f"{value}{suffix.lower()}"
        if "uhd" in lowered:
            return "2160p"
        if "fhd" in lowered:
            return "1080p"
    return None


def _build_thumbnail_url(
    base: Optional[str], hash_id: Optional[str], slug: Optional[str]
) -> Optional[str]:
    if not base or not hash_id:
        return None
    base = base.rstrip("/") + "/"
    prefix = hash_id[:3]
    safe_slug = quote((slug or hash_id).replace("/", "_"))
    return f"{base}{prefix}/pr-{hash_id}.jpg/th-{safe_slug}.jpg"


def _extract_release_markers(
    text: str, quality_hint: Optional[str] = None
) -> Dict[str, Optional[Any]]:
    info: Dict[str, Optional[Any]] = {}
    if not text:
        return info
    season_match = _SEASON_EP_RE.search(text)
    if season_match:
        season = season_match.group("season") or season_match.group("season2")
        episode = season_match.group("episode") or season_match.group("episode2")
        if season:
            info["season"] = int(season)
        if episode:
            info["episode"] = int(episode)
    year_match = _YEAR_RE.search(text)
    if year_match:
        info["year"] = int(year_match.group(0))
    quality = quality_hint or _extract_quality(text)
    if quality:
        info["quality"] = quality
    return info


def _detect_anime(title: str, anime_hint: bool = False) -> bool:
    """
    Detect anime releases.

    Always anime: "[Group] Title NN" / "[Group].Title.NN.[tags]" where [Group]
    is not a known release tag/broadcaster and NN reads as an episode number.

    Only when the request asked for the anime category (anime_hint): bare
    absolute-episode titles such as "One.Piece.485.mkv" or "One.Piece.EP1177".
    Radarr never asks for 5070, so movie searches are unaffected.

    Never anime: titles with SxxEyy / NxNN patterns (those are TV), or where
    the only number is a year.
    """
    if _SEASON_EP_RE.search(title):
        return False

    bracket_match = _ANIME_BRACKET_GROUP_RE.search(title)
    if bracket_match:
        group_name = bracket_match.group(1).strip().lower()
        if group_name in _NON_FANSUB_BRACKET_TAGS:
            return False
        rest = title[bracket_match.end() :].strip()
        return _has_absolute_episode(rest)

    if not anime_hint:
        return False
    stripped = title.strip()
    if not stripped or not stripped[0].isalpha():
        return False
    return _has_absolute_episode(stripped)


def _detect_category(
    title: str, metadata: Dict[str, Optional[Any]], anime_hint: bool = False
) -> int:
    """
    Detect Newznab category based on filename and extracted metadata.

    Detection logic:
    1. Anime: bracketed fansub groups + episode-only patterns (PRIORITY)
    2. TV shows: presence of season/episode patterns (SxxExx or xxyy)
    3. Movies: presence of year, absence of TV patterns
    4. Resolution subcategories: 720p+ = HD, 2160p/4K/UHD = UHD (TV/Movies only)
    5. Default to generic categories if uncertain

    Args:
        title: The filename/title to analyze
        metadata: Dict with season, episode, year, quality keys

    Returns:
        Newznab category ID (int)
    """
    # Check for anime FIRST (priority detection)
    if _detect_anime(title, anime_hint=anime_hint):
        return CATEGORY_ANIME  # 5070 - No quality subcategories

    season = metadata.get("season")
    episode = metadata.get("episode")
    quality = metadata.get("quality")
    year = metadata.get("year")

    quality_lower = (quality or "").lower()
    is_uhd = False
    is_hd = False

    if quality_lower:
        # UHD: 2160p or higher, or contains 4k/uhd keywords
        if "2160" in quality_lower or "4k" in quality_lower or "uhd" in quality_lower:
            is_uhd = True
        # HD: 720p or 1080p
        elif "720" in quality_lower or "1080" in quality_lower:
            is_hd = True

    has_tv_pattern = season is not None or episode is not None

    if not has_tv_pattern:
        if _SEASON_EP_RE.search(title):
            has_tv_pattern = True

    if has_tv_pattern:
        if is_uhd:
            return CATEGORY_TV_UHD  # 5040
        elif is_hd:
            return CATEGORY_TV_HD  # 5030
        else:
            return CATEGORY_TV  # 5000

    # Movies typically have a year but no season/episode
    if year or (not has_tv_pattern):
        if is_uhd:
            return CATEGORY_MOVIES_UHD  # 2040
        elif is_hd:
            return CATEGORY_MOVIES_HD  # 2030
        else:
            return CATEGORY_MOVIES  # 2000

    # Default fallback to generic Movies
    return CATEGORY_MOVIES  # 2000


_STRICT_MARKER_RE = re.compile(r"^(?:s\d{1,2}(?:e\d{1,4})?|(?:19|20)\d{2})$", re.IGNORECASE)


def _matches_strict(title: str, strict_phrase: Optional[str]) -> bool:
    """The query's title words must appear contiguously in the release name.
    Trailing markers the bridge appends to the query (SxxEyy / Sxx / year)
    must appear anywhere after the title; a year marker is optional because
    many names omit it (year conflicts are handled by query_meta)."""
    if not strict_phrase:
        return True
    candidate = _sanitize_phrase(title)
    if not candidate:
        return False
    if candidate == strict_phrase:
        return True
    candidate_tokens = candidate.split()
    phrase_tokens = strict_phrase.split()
    if not phrase_tokens:
        return True

    markers: List[str] = []
    while phrase_tokens and _STRICT_MARKER_RE.match(phrase_tokens[-1]) and len(phrase_tokens) > 1:
        markers.insert(0, phrase_tokens.pop())

    title_end = -1
    for idx in range(0, max(1, len(candidate_tokens) - len(phrase_tokens) + 1)):
        if candidate_tokens[idx : idx + len(phrase_tokens)] == phrase_tokens:
            title_end = idx + len(phrase_tokens)
            break
    if title_end < 0:
        return False
    tail = candidate_tokens[title_end:]
    for marker in markers:
        if _YEAR_RE.fullmatch(marker):
            continue  # optional
        if marker not in tail:
            return False
    return True


def filter_and_map(
    json_data: dict,
    min_bytes: int,
    query_tokens: Optional[List[str]] = None,
    query_meta: Optional[Dict[str, Optional[Any]]] = None,
    strict_phrase: Optional[str] = None,
    strict_match: bool = False,
    sample_mode: str = "drop",
) -> List[dict]:
    """sample_mode: "drop" (default) removes sample clips; "only" returns the raw
    records of sample clips that pass every other filter (pivot seeds)."""
    token_set: Set[str] = set(query_tokens or [])
    thumb_base = json_data.get("thumbURL") or json_data.get("thumbUrl")
    out: List[dict] = []
    for it in json_data.get("data", []):
        hash_id: Optional[str] = None
        subject: Optional[str] = None
        filename_no_ext: Optional[str] = None
        ext: Optional[str] = None
        size: Any = 0
        poster: Optional[str] = None
        posted_raw: Any = None
        sig: Optional[str] = None
        display_fn: Optional[str] = None
        extension_field: Optional[str] = None
        duration_raw: Any = None
        fullres: Optional[str] = None

        if isinstance(it, list):
            if len(it) >= 12:
                hash_id = it[0]
                subject = it[6]
                filename_no_ext = it[10]
                ext = it[11]
            if len(it) > 7:
                poster = it[7]
            if len(it) > 8:
                posted_raw = it[8]
            if len(it) > 14:
                duration_raw = it[14]
        elif isinstance(it, dict):
            hash_id = it.get("hash") or it.get("0") or it.get("id")
            subject = it.get("subject") or it.get("6")
            filename_no_ext = it.get("filename") or it.get("10") or it.get("fn")
            ext = it.get("ext") or it.get("11") or it.get("extension")
            size = it.get("size", 0)
            poster = it.get("poster") or it.get("7")
            posted_raw = it.get("timestamp") or it.get("ts") or it.get("dtime") or it.get("date") or it.get("12")
            sig = it.get("sig")
            display_fn = it.get("fn") or it.get("filename")
            extension_field = it.get("extension") or it.get("ext")
            duration_raw = it.get("14") or it.get("runtime") or it.get("duration") or it.get("len")
            fullres = it.get("fullres") or it.get("resolution")
            if not fullres and it.get("xres") and it.get("yres"):
                fullres = f"{it.get('xres')} x {it.get('yres')}"

        if not hash_id or not ext:
            continue

        filename_no_ext = filename_no_ext or ""
        ext = ext or ""
        if extension_field and not ext:
            ext = extension_field

        # Try to use numeric size if present; otherwise skip (can't verify <100MB rule)
        if not isinstance(size, int):
            try:
                size = int(size)
            except Exception:
                size = 0

        if size < min_bytes:
            continue

        duration_seconds = _parse_duration_seconds(duration_raw)

        if _is_flagged_item(it, ext, duration_seconds):
            continue
        is_sample = _is_sample_name(display_fn or filename_no_ext or "")
        if sample_mode == "only":
            if not is_sample:
                continue
        elif is_sample:
            continue

        title: Optional[str] = None
        if display_fn:
            cleaned = display_fn.strip()
            if cleaned:
                normalized = cleaned.replace(" - ", "-")
                parts = [segment for segment in normalized.split(" ") if segment]
                sanitized = ".".join(parts)
                ext_component = extension_field or ext or ""
                if ext_component and not ext_component.startswith("."):
                    ext_component = f".{ext_component}"
                title = f"{sanitized}{ext_component}" if ext_component else sanitized

        if not title:
            fallback = subject or f"{filename_no_ext}{ext}"
            title = _normalize_title(fallback)

        quality = _extract_quality(title, fullres)
        title_meta = _extract_release_markers(title, quality)
        if not quality and title_meta.get("quality"):
            quality = title_meta.get("quality")

        if strict_match and not _matches_strict(title, strict_phrase):
            continue

        if query_meta:
            q_year = query_meta.get("year")
            q_season = query_meta.get("season")
            q_episode = query_meta.get("episode")
            q_quality = query_meta.get("quality")
            t_year = title_meta.get("year")
            t_season = title_meta.get("season")
            t_episode = title_meta.get("episode")
            t_quality = quality or title_meta.get("quality")
            if q_year and t_year and q_year != t_year:
                continue
            if q_season and t_season and q_season != t_season:
                continue
            if q_episode and t_episode and q_episode != t_episode:
                continue
            if q_quality and t_quality and q_quality.lower() != t_quality.lower():
                continue

        if token_set:
            title_tokens = set(_tokenize(title))
            if not title_tokens or not token_set.issubset(title_tokens):
                continue

        if sample_mode == "only":
            out.append(it)
            continue

        duration_formatted = _format_duration(duration_seconds)
        thumbnail_url = _build_thumbnail_url(thumb_base, hash_id, filename_no_ext)
        year = title_meta.get("year")

        out.append(
            {
                "hash": hash_id,
                "filename": filename_no_ext,
                "ext": ext,
                "sig": sig,
                "size": size,
                "title": title,
                "poster": poster,
                "posted": posted_raw,
                "duration": duration_seconds,
                "duration_hms": duration_formatted,
                "quality": quality,
                "thumbnail": thumbnail_url,
                "year": year,
                "season": title_meta.get("season"),
                "episode": title_meta.get("episode"),
            }
        )
    return out


@APP.route("/health")
def health():
    # Liveness only: no Easynews call, no API key, safe for container healthchecks.
    return {"status": "ok"}, 200


@APP.route("/api")
def api():
    if not require_apikey():
        return Response("Unauthorized", status=401)

    t = request.args.get("t", "caps")
    if t == "caps":
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<caps>"
            '<server version="0.1" title="Easynews Bridge"/>'
            f'<limits max="{DEFAULT_LIMIT}" default="{DEFAULT_LIMIT}"/>'
            '<registration available="no" open="no"/>'
            "<searching>"
            '<search available="yes" supportedParams="q"/>'
            '<movie-search available="yes" supportedParams="q,year"/>'
            '<tv-search available="yes" supportedParams="q,season,ep"/>'
            "</searching>"
            "<categories>"
            '<category id="2000" name="Movies">'
            '<subcat id="2030" name="Movies/HD"/>'
            '<subcat id="2040" name="Movies/UHD"/>'
            "</category>"
            '<category id="5000" name="TV">'
            '<subcat id="5030" name="TV/HD"/>'
            '<subcat id="5040" name="TV/UHD"/>'
            '<subcat id="5070" name="TV/Anime"/>'
            "</category>"
            '<category id="7000" name="Other"/>'
            "</categories>"
            "</caps>"
        )
        return Response(xml, mimetype="application/xml")

    if t in ("search", "movie", "tvsearch"):
        base_query = (request.args.get("q") or "").strip()
        cat_param = request.args.get("cat") or ""
        # Sonarr requests always include 5070; Radarr requests never do.
        anime_hint = "5070" in {c.strip() for c in cat_param.split(",") if c.strip()}
        season_param = request.args.get("season") or request.args.get("seasonnum")
        episode_param = (
            request.args.get("ep")
            or request.args.get("epnum")
            or request.args.get("episode")
        )
        year_param = request.args.get("year") or request.args.get("yr")
        season_int = _as_int(season_param)
        episode_int = _as_int(episode_param)
        year_int = _as_int(year_param)

        search_components: List[str] = []
        if base_query:
            search_components.append(base_query)

        if t == "movie":
            if year_int and str(year_int) not in base_query:
                search_components.append(str(year_int))
        elif t == "tvsearch":
            if season_int is not None and episode_int is not None:
                search_components.append(f"S{season_int:02}E{episode_int:02}")
            elif season_int is not None:
                search_components.append(f"S{season_int:02}")
            if year_int and str(year_int) not in base_query:
                search_components.append(str(year_int))

        search_label = " ".join(part for part in search_components if part).strip()
        raw_query = search_label or base_query
        q = raw_query.strip()
        fallback_query = False
        if (
            not q or q.lower() == "test"
        ):  # allow Prowlarr validation calls to receive data
            # Check if TV/Anime categories are requested
            tv_categories = {"5000", "5030", "5040"}
            anime_categories = {"5070"}
            requested_categories = set(cat_param.split(",")) if cat_param else set()
            wants_tv = t == "tvsearch" or bool(requested_categories & tv_categories)
            wants_anime = bool(requested_categories & anime_categories) and not wants_tv
            # Use appropriate fallback query
            if wants_anime:
                q = "one piece"  # Anime fallback
            elif wants_tv:
                q = "breaking bad"  # TV fallback
            else:
                q = "matrix"  # Movie fallback
            fallback_query = True
        query_tokens = _tokenize(raw_query)
        query_meta = _extract_release_markers(raw_query)
        if year_int:
            query_meta["year"] = year_int
        if season_int is not None:
            query_meta["season"] = season_int
        if episode_int is not None:
            query_meta["episode"] = episode_int
        strict_requested = _strict_requested(t, request.args.get("strict"))
        strict_phrase = _sanitize_phrase(raw_query) if strict_requested else None
        limit = _resolve_limit(request.args.get("limit"))
        offset = _resolve_offset(request.args.get("offset"))
        min_bytes = _resolve_min_size_mb(request.args.get("minsize")) * 1024 * 1024

        if fallback_query:
            # Check if TV/Anime categories are requested
            tv_categories = {"5000", "5030", "5040"}
            anime_categories = {"5070"}
            requested_categories = set(cat_param.split(",")) if cat_param else set()
            wants_tv = t == "tvsearch" or bool(requested_categories & tv_categories)
            wants_anime = bool(requested_categories & anime_categories) and not wants_tv

            if wants_anime:
                # Anime-appropriate fallback
                items = [
                    {
                        "hash": "SAMPLEHASH_ANIME123",
                        "filename": "sample.anime.series.01.720p.mkv",
                        "ext": ".mkv",
                        "sig": None,
                        "size": 350 * 1024 * 1024,
                        "title": "[SampleSubs] Sample Anime Series - 01 [720p]",
                        "sample": True,
                        "category": CATEGORY_ANIME,
                        "poster": "sample@example.com",
                        "posted": int(time.time()),
                    }
                ]
            elif wants_tv:
                # TV-appropriate fallback for Sonarr
                items = [
                    {
                        "hash": "SAMPLEHASH_TV123456",
                        "filename": "sample.tv.show.s01e01.1080p.mkv",
                        "ext": ".mkv",
                        "sig": None,
                        "size": 800 * 1024 * 1024,
                        "title": "Sample TV Show S01E01 1080p",
                        "sample": True,
                        "category": CATEGORY_TV_HD,
                        "poster": "sample@example.com",
                        "posted": int(time.time()),
                    }
                ]
            else:
                # Movie fallback for Radarr
                items = [
                    {
                        "hash": "SAMPLEHASH1234567890",
                        "filename": "sample.matrix.clip",
                        "ext": ".mkv",
                        "sig": None,
                        "size": 700 * 1024 * 1024,
                        "title": "Sample Matrix Clip",
                        "sample": True,
                        "category": CATEGORY_MOVIES_HD,
                        "poster": "sample@example.com",
                        "posted": int(time.time()),
                    }
                ]
        else:
            # aim for maximum results per page
            data = _cached_search(
                query=q,
                file_type="VIDEO",
                per_page=UPSTREAM_PAGE_SIZE,
                sort_field="relevance",
                sort_dir="-",
            )
            if fallback_query:
                items = filter_and_map(data, min_bytes=min_bytes)
            else:
                items = filter_and_map(
                    data,
                    min_bytes=min_bytes,
                    query_tokens=query_tokens,
                    query_meta=query_meta,
                    strict_phrase=strict_phrase,
                    strict_match=strict_requested,
                )
                if SIBLING_PIVOT:
                    try:
                        seeds = filter_and_map(
                            data,
                            min_bytes=PIVOT_SEED_MIN_BYTES,
                            query_tokens=query_tokens,
                            query_meta=query_meta,
                            strict_phrase=strict_phrase,
                            strict_match=strict_requested,
                            sample_mode="only",
                        )
                        if seeds:
                            have = {it["hash"] for it in items}
                            for extra in _run_pivot(seeds):
                                if extra["hash"] not in have:
                                    have.add(extra["hash"])
                                    items.append(extra)
                    except Exception:
                        logger.warning("sibling pivot failed; returning direct results only", exc_info=True)

        # Trim by limit (handles fallback and real queries)
        items = items[offset : offset + limit]

        display_q = raw_query if raw_query else q
        chan_title = f"Results for {display_q}"
        now_dt = datetime.now(timezone.utc)
        channel_pub = now_dt.strftime("%a, %d %b %Y %H:%M:%S %z")

        header = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">'
            "<channel>"
            f"<title>{xml_escape(chan_title)}</title>"
            f"<description>{xml_escape(chan_title)}</description>"
            f"<link>{request.url_root.rstrip('/')}/api</link>"
            f"<pubDate>{channel_pub}</pubDate>"
        )

        body_parts: List[str] = []
        for it in items:
            enc_id = encode_id(it)
            title = xml_escape(it["title"]) if it["title"] else "Untitled"
            link = f"{request.url_root.rstrip('/')}/api?t=get&id={enc_id}&apikey={request.args.get('apikey')}"
            safe_link = xml_escape(link)
            size = it["size"]
            guid = enc_id
            poster = it.get("poster")
            posted_dt = _coerce_datetime(it.get("posted")) or now_dt
            posted_str = posted_dt.strftime("%a, %d %b %Y %H:%M:%S %z")
            posted_epoch = str(int(posted_dt.timestamp()))
            duration_hms = it.get("duration_hms")
            quality = it.get("quality")
            thumb = it.get("thumbnail")
            year = it.get("year")
            season = it.get("season")
            episode = it.get("episode")

            title_text = it.get("title", "")
            title_metadata = {
                "season": season,
                "episode": episode,
                "year": year,
                "quality": quality,
            }
            category_id = it.get("category") or _detect_category(
                title_text, title_metadata, anime_hint=anime_hint
            )

            attr_parts = [
                f'<newznab:attr name="size" value="{size}"/>',
                f'<newznab:attr name="category" value="{category_id}"/>',
                f'<newznab:attr name="usenetdate" value="{posted_str}"/>',
                f'<newznab:attr name="posted" value="{posted_epoch}"/>',
            ]
            if poster:
                attr_parts.append(
                    f'<newznab:attr name="poster" value="{xml_escape(poster)}"/>'
                )
            if quality:
                attr_parts.append(
                    f'<newznab:attr name="quality" value="{xml_escape(quality)}"/>'
                )
            if duration_hms:
                attr_parts.append(
                    f'<newznab:attr name="duration" value="{duration_hms}"/>'
                )
            if thumb:
                attr_parts.append(
                    f'<newznab:attr name="thumb" value="{xml_escape(thumb)}"/>'
                )
            if year:
                attr_parts.append(f'<newznab:attr name="year" value="{year}"/>')
            if season:
                attr_parts.append(f'<newznab:attr name="season" value="{season}"/>')
            if episode:
                attr_parts.append(f'<newznab:attr name="episode" value="{episode}"/>')
            attr_xml = "".join(attr_parts)
            item_xml = (
                f"<item>"
                f"<title>{title}</title>"
                f'<guid isPermaLink="false">{guid}</guid>'
                f"<link>{safe_link}</link>"
                f"<category>{category_id}</category>"
                f"<pubDate>{posted_str}</pubDate>"
                f"{attr_xml}"
                f'<enclosure url="{safe_link}" length="{size}" type="application/x-nzb"/>'
                f"</item>"
            )
            body_parts.append(item_xml)

        footer = "</channel></rss>"
        xml = header + "".join(body_parts) + footer
        return Response(xml, mimetype="application/rss+xml")

    if t in ("get", "getnzb"):
        enc_id = request.args.get("id")
        if not enc_id:
            return Response("Missing id", status=400)
        d = decode_id(enc_id)
        if d.get("sample"):
            title = d.get("title", "Sample Item")
            safe_title = "sample"
            nzb_content = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">'
                '<file subject="Sample Matrix Clip" date="0" poster="sample@example.com">'
                "<groups><group>alt.binaries.sample</group></groups>"
                '<segments><segment bytes="1024" number="1">sample</segment></segments>'
                "</file></nzb>"
            ).encode("utf-8")
            resp = Response(nzb_content, mimetype="application/x-nzb")
            resp.headers["Content-Disposition"] = (
                f'attachment; filename="{safe_title}.nzb"'
            )
            return resp
        si = to_search_item(d)
        try:
            c = client()
            payload = c.build_nzb_payload([si], name=d.get("title"))
            # fetch content
            url = f"{easynews_client.EASYNEWS_BASE}/2.0/api/dl-nzb"
            r = c.s.post(url, data=payload, timeout=60)
        except EasynewsError as e:
            return Response(f"Upstream error: {e}", status=502)
        except requests.exceptions.RequestException as e:
            return Response(f"Upstream network error: {e}", status=502)
        if r.status_code != 200:
            return Response(f"Upstream error {r.status_code}", status=502)
        # Name file as title.nzb
        title = d.get("title") or (d.get("filename", "download") + d.get("ext", ""))
        safe_title = (
            "".join(ch for ch in title if ch.isalnum() or ch in (" ", "-", "_", "."))[
                :200
            ].strip()
            or "download"
        )
        resp = Response(r.content, mimetype="application/x-nzb")
        resp.headers["Content-Disposition"] = f'attachment; filename="{safe_title}.nzb"'
        return resp

    return Response("Unsupported 't' parameter", status=400)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8081))
    APP.run(host="0.0.0.0", port=port)
