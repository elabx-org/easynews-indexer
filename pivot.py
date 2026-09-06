"""Sibling pivot: recover obfuscated full releases from a readable sample clip.

Scene uploads to Easynews often post the main file under an obfuscated name
while the sample clip keeps the real release name. Easynews indexes a media
fingerprint for every video (resolution, codecs, fps, audio/subtitle
languages) and allows date-window searches, so a sample can be used as a seed:

1. scan the posting window around the sample for large files with the same
   fingerprint,
2. confirm each candidate by reading the first bytes of the file and comparing
   the Matroska track table and muxer with the sample's,
3. emit confirmed files as releases named after the sample.

The Easynews-specific I/O lives in EasynewsPivotBackend; everything else is
pure and unit-tested with fakes.
"""
from __future__ import annotations

import re
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# --- normalisation ---------------------------------------------------------------


@dataclass(frozen=True)
class Fingerprint:
    width: Optional[int]
    height: Optional[int]
    vcodec: str
    acodec: str
    fps: Optional[float]
    alangs: Tuple[str, ...]
    slangs: Tuple[str, ...]


@dataclass
class Item:
    hash: str
    name: str
    ext: str
    sig: Optional[str]
    size: int
    ts: Optional[int]
    runtime: Optional[int]
    fp: Fingerprint
    raw: Dict[str, Any]


def _int(v: Any) -> Optional[int]:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _langs(*vals: Any) -> Tuple[str, ...]:
    for v in vals:
        if isinstance(v, (list, tuple)) and v:
            return tuple(str(x).strip() for x in v if str(x).strip())
        if isinstance(v, str) and v.strip():
            return tuple(s.strip() for s in v.split(",") if s.strip())
    return ()


_HMS_RE = re.compile(r"(?:(\d+)h)?:?(?:(\d+)m)?:?(?:(\d+)s)?")


def _runtime(raw: Dict[str, Any]) -> Optional[int]:
    for key in ("runtime", "duration"):
        n = _int(raw.get(key))
        if n:
            return n
    text = str(raw.get("14") or "").strip().lower()
    if text:
        m = _HMS_RE.fullmatch(text)
        if m and any(m.groups()):
            h, mi, s = (int(x) if x else 0 for x in m.groups())
            return h * 3600 + mi * 60 + s
    return None


def fingerprint(raw: Dict[str, Any]) -> Fingerprint:
    width = _int(raw.get("xres") or raw.get("width"))
    height = _int(raw.get("yres") or raw.get("height"))
    if (width is None or height is None) and raw.get("fullres"):
        m = re.match(r"\s*(\d+)\s*x\s*(\d+)", str(raw["fullres"]))
        if m:
            width, height = int(m.group(1)), int(m.group(2))
    return Fingerprint(
        width=width,
        height=height,
        vcodec=str(raw.get("vcodec") or raw.get("12") or "").upper(),
        acodec=str(raw.get("acodec") or raw.get("18") or "").upper(),
        fps=_float(raw.get("fps") if raw.get("fps") is not None else raw.get("17")),
        alangs=_langs(raw.get("audio_tracks"), raw.get("alangs"), raw.get("alang")),
        slangs=_langs(raw.get("subtitle_tracks"), raw.get("slangs"), raw.get("slang")),
    )


def normalise(raw: Dict[str, Any]) -> Item:
    return Item(
        hash=str(raw.get("hash") or raw.get("0") or ""),
        name=str(raw.get("fn") or raw.get("10") or raw.get("filename") or ""),
        ext=str(raw.get("extension") or raw.get("11") or raw.get("ext") or ""),
        sig=raw.get("sig"),
        size=_int(raw.get("rawSize") if raw.get("rawSize") is not None else raw.get("size")) or 0,
        ts=_int(raw.get("timestamp") if raw.get("timestamp") is not None else raw.get("ts")),
        runtime=_runtime(raw),
        fp=fingerprint(raw),
        raw=raw,
    )


# --- names ------------------------------------------------------------------------

_SAMPLE_SUFFIX_RE = re.compile(r"[\s._-]+sample$", re.IGNORECASE)
_SAMPLE_PREFIX_RE = re.compile(r"^sample[\s._-]+", re.IGNORECASE)
_SAMPLE_TOKEN_RE = re.compile(r"(?:^|[\s._\-\[(])sample(?=$|[\s._\-\])])", re.IGNORECASE)


def is_sample_name(name: str) -> bool:
    return bool(name) and bool(_SAMPLE_TOKEN_RE.search(name))


def release_name_from_sample(sample_name: str) -> str:
    name = _SAMPLE_SUFFIX_RE.sub("", sample_name.strip())
    name = _SAMPLE_PREFIX_RE.sub("", name)
    return name


# --- candidate selection -------------------------------------------------------------

MIN_FULL_BYTES = 800 * 1024 * 1024
MIN_FULL_RUNTIME_S = 15 * 60
MAX_FULL_RUNTIME_S = 4 * 3600
WINDOW_BEFORE_S = 45 * 60
WINDOW_AFTER_S = 45 * 60


def _fps_close(a: Optional[float], b: Optional[float]) -> bool:
    if a is None or b is None:
        return a == b
    return abs(a - b) <= 0.05


def _fp_match(seed: Fingerprint, cand: Fingerprint) -> bool:
    return (
        seed.width == cand.width
        and seed.height == cand.height
        and seed.vcodec == cand.vcodec
        and seed.acodec == cand.acodec
        and _fps_close(seed.fps, cand.fps)
        and seed.alangs == cand.alangs
        and seed.slangs == cand.slangs
    )


def candidates(seed: Item, window: Iterable[Dict[str, Any]]) -> List[Item]:
    out: List[Item] = []
    for raw in window:
        it = normalise(raw)
        if not it.hash or it.hash == seed.hash:
            continue
        if it.size < MIN_FULL_BYTES:
            continue
        if is_sample_name(it.name):
            continue
        if it.runtime is not None and not (MIN_FULL_RUNTIME_S <= it.runtime <= MAX_FULL_RUNTIME_S):
            continue
        if not _fp_match(seed.fp, it.fp):
            continue
        out.append(it)
    out.sort(key=lambda i: (abs((i.ts or 0) - (seed.ts or 0)), -i.size))
    return out


# --- Matroska header ---------------------------------------------------------------------


@dataclass
class MkvHeader:
    track_signature: str = ""
    writing_app: str = ""
    duration_s: Optional[int] = None


_ID_SEGMENT = 0x18538067
_ID_INFO = 0x1549A966
_ID_TRACKS = 0x1654AE6B
_ID_TRACKENTRY = 0xAE
_ID_TRACKTYPE = 0x83
_ID_CODECID = 0x86
_ID_LANG = 0x22B59C
_ID_LANG_IETF = 0x22B59D
_ID_DURATION = 0x4489
_ID_WRITINGAPP = 0x5741
_TRACK_TYPES = {1: "V", 2: "A", 17: "S"}


def _vint(b: bytes, i: int, keep_marker: bool) -> Tuple[Optional[int], int, int]:
    if i >= len(b):
        return None, i, 0
    first = b[i]
    if first == 0:
        return None, i + 1, 0
    length = 1
    while length <= 8 and not (first & (0x80 >> (length - 1))):
        length += 1
    if length > 8 or i + length > len(b):
        return None, i + 1, 0
    value = first if keep_marker else first & (0xFF >> length)
    for k in range(1, length):
        value = (value << 8) | b[i + k]
    return value, i + length, length


def _elements(b: bytes, start: int, end: int):
    i = start
    while i < end and i < len(b) - 1:
        eid, j, _ = _vint(b, i, True)
        size, k, slen = _vint(b, j, False)
        if eid is None or size is None:
            return
        unknown = slen and size == (1 << (7 * slen)) - 1
        elem_end = end if unknown else min(end, k + size)
        yield eid, k, elem_end
        if unknown:
            return
        i = k + size


def parse_matroska(b: bytes) -> MkvHeader:
    out = MkvHeader()
    tracks: List[str] = []
    try:
        for eid, s, e in _elements(b, 0, len(b)):
            if eid != _ID_SEGMENT:
                continue
            for eid2, s2, e2 in _elements(b, s, e):
                if eid2 == _ID_INFO:
                    for t, ts, te in _elements(b, s2, e2):
                        if t == _ID_WRITINGAPP:
                            out.writing_app = b[ts:te].decode("utf-8", "replace")
                        elif t == _ID_DURATION and te - ts in (4, 8):
                            fmt = ">d" if te - ts == 8 else ">f"
                            out.duration_s = int(round(struct.unpack(fmt, b[ts:te])[0] / 1000.0))
                elif eid2 == _ID_TRACKS:
                    for t, ts, te in _elements(b, s2, e2):
                        if t != _ID_TRACKENTRY:
                            continue
                        ttype, codec, lang = "?", "", ""
                        for u, us, ue in _elements(b, ts, te):
                            if u == _ID_TRACKTYPE and ue > us:
                                ttype = _TRACK_TYPES.get(b[us], str(b[us]))
                            elif u == _ID_CODECID:
                                codec = b[us:ue].decode("utf-8", "replace")
                            elif u in (_ID_LANG, _ID_LANG_IETF) and not lang:
                                lang = b[us:ue].decode("utf-8", "replace")
                        tracks.append(f"{ttype}:{codec}:{lang}")
    except Exception:  # corrupt or truncated input: report what we have
        pass
    out.track_signature = "|".join(tracks)
    return out


def headers_match(seed: MkvHeader, cand: MkvHeader) -> bool:
    if not seed.track_signature or not cand.track_signature:
        return False
    return seed.track_signature == cand.track_signature and seed.writing_app == cand.writing_app


# --- orchestration --------------------------------------------------------------------------

MAX_SEEDS_PER_SEARCH = 6
MAX_CONFIRMS_PER_SEED = 6


class _HeaderCache:
    """hash -> parsed header; headers never change for a given post."""

    def __init__(self, max_entries: int = 2048):
        self._d: Dict[str, MkvHeader] = {}
        self._lock = threading.Lock()
        self._max = max_entries

    def get(self, key: str) -> Optional[MkvHeader]:
        with self._lock:
            return self._d.get(key)

    def put(self, key: str, value: MkvHeader) -> None:
        with self._lock:
            if len(self._d) >= self._max:
                self._d.pop(next(iter(self._d)))
            self._d[key] = value


_HEADER_CACHE = _HeaderCache()


def _header_for(item: Item, backend: Any) -> MkvHeader:
    cached = _HEADER_CACHE.get(item.hash)
    if cached is not None:
        return cached
    try:
        head = backend.fetch_head(item)
    except Exception:
        head = b""
    parsed = parse_matroska(head or b"")
    _HEADER_CACHE.put(item.hash, parsed)
    return parsed


def pivot_from_samples(samples: Sequence[Dict[str, Any]], backend: Any, budget_s: float = 20.0) -> List[Dict[str, Any]]:
    """For each sample clip, find and confirm the obfuscated full release(s).

    backend must provide scan_window(d1_ts, d2_ts) -> raw items and
    fetch_head(item) -> bytes. Returns result dicts in the shape filter_and_map
    produces (hash/filename/ext/sig/size/posted/...), plus pivot_seed.
    """
    deadline = time.monotonic() + max(0.0, budget_s)
    results: List[Dict[str, Any]] = []
    seen_seed_names: set = set()
    seen_hashes: set = set()
    window_cache: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}

    seeds = []
    for raw in samples:
        it = normalise(raw)
        if not it.hash or it.ts is None or not it.fp.vcodec:
            continue
        key = release_name_from_sample(it.name).lower()
        if key in seen_seed_names:
            continue
        seen_seed_names.add(key)
        seeds.append(it)
    seeds = seeds[:MAX_SEEDS_PER_SEARCH]

    for seed in seeds:
        if time.monotonic() >= deadline:
            break
        d1 = (seed.ts or 0) - WINDOW_BEFORE_S
        d2 = (seed.ts or 0) + WINDOW_AFTER_S
        wkey = (d1 // 600, d2 // 600)
        if wkey not in window_cache:
            try:
                window_cache[wkey] = list(backend.scan_window(d1, d2))
            except Exception:
                window_cache[wkey] = []
        cands = candidates(seed, window_cache[wkey])
        if not cands:
            continue
        seed_header = _header_for(seed, backend)
        if not seed_header.track_signature:
            continue
        release = release_name_from_sample(seed.name)
        for cand in cands[:MAX_CONFIRMS_PER_SEED]:
            if time.monotonic() >= deadline:
                break
            if cand.hash in seen_hashes:
                continue
            if not headers_match(seed_header, _header_for(cand, backend)):
                continue
            seen_hashes.add(cand.hash)
            results.append(
                {
                    "hash": cand.hash,
                    "filename": release,
                    "ext": cand.ext,
                    "sig": cand.sig,
                    "size": cand.size,
                    "posted": cand.ts,
                    "runtime": cand.runtime,
                    "title": f"{release}{cand.ext}",
                    "pivot_seed": seed.hash,
                    "raw": cand.raw,
                }
            )
    return results


class EasynewsPivotBackend:
    """Real I/O against Easynews using an EasynewsClient (2.0 date filters + range GET)."""

    HEAD_BYTES = 3_000_000

    def __init__(self, client: Any, base_url: str, max_pages: int = 4):
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.max_pages = max_pages

    def scan_window(self, d1_ts: int, d2_ts: int) -> List[Dict[str, Any]]:
        fmt = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t))  # noqa: E731
        out: List[Dict[str, Any]] = []
        for pno in range(1, self.max_pages + 1):
            data = self.client.search_window(query="*", d1=fmt(d1_ts), d2=fmt(d2_ts), page=pno, per_page=250)
            items = data.get("data") or []
            out.extend(items)
            if len(items) < 250:
                break
            smallest = min((normalise(i).size for i in items), default=0)
            if smallest < MIN_FULL_BYTES:
                break
        return out

    def fetch_head(self, item: Item) -> bytes:
        url = f"{self.base_url}/dl/{item.hash}{item.ext}/{item.name}{item.ext}"
        params = {"sig": item.sig} if item.sig else None
        r = self.client.s.get(url, params=params, headers={"Range": f"bytes=0-{self.HEAD_BYTES}"}, timeout=60, stream=True)
        try:
            return r.raw.read(self.HEAD_BYTES + 1)
        finally:
            r.close()
