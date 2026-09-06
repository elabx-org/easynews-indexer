"""Sibling pivot: recover obfuscated full releases from their readable sample clip."""
import struct

import pytest

import pivot


# --- fingerprint normalisation (2.0 and 3.0 item shapes) -----------------------------

V2 = {"0": "h2", "10": "lanterns.2026.s01e01.internal.hdr.2160p.web.h265-edith.sample", "11": ".mkv", "sig": "s",
      "rawSize": 148922527, "ts": 1786991013, "14": "1m:0s", "runtime": 60, "width": "3840", "height": "1920",
      "12": "HEVC", "18": "EAC3", "17": 23.98, "alangs": ["eng"], "slangs": ["eng", "bul", "cze"], "fullres": "3840 x 1920"}
V3 = {"hash": "h3", "fn": "lanterns.2026.s01e01.internal.hdr.2160p.web.h265-edith.sample", "extension": ".mkv", "sig": "s",
      "size": 148922527, "timestamp": 1786991013, "runtime": 60, "xres": 3840, "yres": 1920, "vcodec": "HEVC",
      "acodec": "EAC3", "fps": 23.98, "audio_tracks": "eng", "subtitle_tracks": "eng,bul,cze"}


@pytest.mark.parametrize("raw", [V2, V3])
def test_fingerprint_is_the_same_from_either_api_shape(raw):
    fp = pivot.fingerprint(raw)
    assert fp == pivot.Fingerprint(width=3840, height=1920, vcodec="HEVC", acodec="EAC3", fps=23.98,
                                   alangs=("eng",), slangs=("eng", "bul", "cze"))


def test_normalise_item_exposes_hash_name_ext_size_ts_runtime():
    for raw, h in ((V2, "h2"), (V3, "h3")):
        n = pivot.normalise(raw)
        assert (n.hash, n.ext, n.size, n.ts, n.runtime) == (h, ".mkv", 148922527, 1786991013, 60)
        assert n.name.endswith("-edith.sample")


# --- seed -> synthesized release name ---------------------------------------------------

@pytest.mark.parametrize("sample,expected", [
    ("lanterns.2026.s01e01.internal.hdr.2160p.web.h265-edith.sample", "lanterns.2026.s01e01.internal.hdr.2160p.web.h265-edith"),
    ("lanterns.s01e01.german.dl.2160p.web.h265-rile-sample", "lanterns.s01e01.german.dl.2160p.web.h265-rile"),
    ("sample-green.lantern.s01e03.720p.hdtv.x264-2hd", "green.lantern.s01e03.720p.hdtv.x264-2hd"),
    ("Show.S01E01.1080p.WEB-GRP.Sample", "Show.S01E01.1080p.WEB-GRP"),
])
def test_release_name_from_sample(sample, expected):
    assert pivot.release_name_from_sample(sample) == expected


# --- candidate selection inside the posting window ---------------------------------------

def mk(hash_, name, size, ts, **fp):
    d = {"0": hash_, "10": name, "11": ".mkv", "sig": "s", "rawSize": size, "ts": ts, "14": "56m:28s",
         "width": "3840", "height": "1920", "12": "HEVC", "18": "EAC3", "17": 23.98,
         "alangs": ["eng"], "slangs": ["eng", "bul", "cze"]}
    d.update(fp)
    return d


SEED = pivot.normalise(V2)


def test_candidates_match_fingerprint_and_are_full_size_non_samples():
    window = [
        mk("full", "Z4dFB2qGhFf8k1SlOaBWifbFg", 8_100_000_000, 1786991500),
        mk("other-res", "abc", 8_000_000_000, 1786991500, height="2160"),
        mk("other-audio", "abd", 8_000_000_000, 1786991500, **{"18": "AAC"}),
        mk("other-subs", "abe", 8_000_000_000, 1786991500, slangs=["eng"]),
        mk("too-small", "abf", 200_000_000, 1786991500),
        mk("a-sample", "lanterns.s01e01.x-sample", 900_000_000, 1786991500),
        mk("h2", V2["10"], 148922527, 1786991013),  # the seed itself
    ]
    out = pivot.candidates(SEED, window)
    assert [c.hash for c in out] == ["full"]


def test_candidates_tolerate_fps_rounding():
    window = [mk("full", "obf", 8_000_000_000, 1786991500, **{"17": 23.976})]
    assert [c.hash for c in pivot.candidates(SEED, window)] == ["full"]


# --- Matroska header comparison ---------------------------------------------------------

def ebml(eid, payload):
    """Element id bytes + 8-byte size vint + payload."""
    size = len(payload)
    return eid + bytes([0x01]) + size.to_bytes(7, "big") + payload


def mkv(tracks, writing="mkvmerge v98.0", duration_ms=60000.0):
    info = ebml(b"\x15\x49\xa9\x66", ebml(b"\x57\x41", writing.encode()) + ebml(b"\x44\x89", struct.pack(">d", duration_ms)))
    entries = b""
    for ttype, codec, lang in tracks:
        entries += ebml(b"\xae", ebml(b"\x83", bytes([ttype])) + ebml(b"\x86", codec.encode()) + ebml(b"\x22\xb5\x9c", lang.encode()))
    tracks_el = ebml(b"\x16\x54\xae\x6b", entries)
    segment = ebml(b"\x18\x53\x80\x67", info + tracks_el)
    header = ebml(b"\x1a\x45\xdf\xa3", ebml(b"\x42\x82", b"matroska"))
    return header + segment


TRACKS = [(1, "V_MPEGH/ISO/HEVC", "und"), (2, "A_EAC3", "en"), (17, "S_TEXT/UTF8", "en"), (17, "S_TEXT/UTF8", "bg")]


def test_parse_matroska_header_reads_tracks_and_info():
    h = pivot.parse_matroska(mkv(TRACKS, duration_ms=3389000.0))
    assert h.track_signature == "V:V_MPEGH/ISO/HEVC:und|A:A_EAC3:en|S:S_TEXT/UTF8:en|S:S_TEXT/UTF8:bg"
    assert h.writing_app == "mkvmerge v98.0"
    assert h.duration_s == 3389


def test_same_track_table_and_muxer_confirms():
    seed = pivot.parse_matroska(mkv(TRACKS))
    assert pivot.headers_match(seed, pivot.parse_matroska(mkv(TRACKS, duration_ms=3389000.0)))
    assert not pivot.headers_match(seed, pivot.parse_matroska(mkv(TRACKS[:-1], duration_ms=3389000.0)))
    assert not pivot.headers_match(seed, pivot.parse_matroska(mkv(TRACKS, writing="mkvmerge v90.0", duration_ms=3389000.0)))


def test_parse_matroska_handles_garbage_without_raising():
    h = pivot.parse_matroska(b"\x00" * 100)
    assert h.track_signature == "" and h.writing_app == ""


# --- end-to-end with fakes -----------------------------------------------------------------

class FakeBackend:
    def __init__(self, window, headers):
        self.window = window            # list of raw items returned for any window scan
        self.headers = headers          # hash -> bytes served for a range fetch
        self.scans = []
        self.fetches = []

    def scan_window(self, d1, d2):
        self.scans.append((d1, d2))
        return self.window

    def fetch_head(self, item):
        self.fetches.append(item.hash)
        return self.headers[item.hash]


def test_pivot_returns_confirmed_full_release_named_after_the_sample():
    full = mk("fullhash", "Z4dFB2qGhFf8k1SlOaBWifbFg", 8_100_000_000, 1786991500)
    full["sig"] = "fullsig"
    imposter = mk("imposter", "OqbVTDIRxUHdmUR", 6_300_000_000, 1786990000)
    backend = FakeBackend([full, imposter, V2], {
        "h2": mkv(TRACKS), "fullhash": mkv(TRACKS, duration_ms=3389000.0),
        "imposter": mkv(TRACKS[:-1], duration_ms=2700000.0),
    })
    out = pivot.pivot_from_samples([V2], backend, budget_s=30)
    assert len(out) == 1
    r = out[0]
    assert r["hash"] == "fullhash" and r["sig"] == "fullsig" and r["ext"] == ".mkv"
    assert r["filename"] == "lanterns.2026.s01e01.internal.hdr.2160p.web.h265-edith"
    assert r["size"] == 8_100_000_000
    assert r["pivot_seed"] == "h2"
    assert backend.fetches.count("h2") == 1          # seed header fetched once
    assert "imposter" in backend.fetches              # candidates are checked, not assumed


def test_pivot_dedupes_seeds_that_describe_the_same_release():
    dup = dict(V2, **{"0": "h2b"})
    full = mk("fullhash", "obf", 8_100_000_000, 1786991500)
    backend = FakeBackend([full], {"h2": mkv(TRACKS), "h2b": mkv(TRACKS), "fullhash": mkv(TRACKS, duration_ms=3389000.0)})
    out = pivot.pivot_from_samples([V2, dup], backend, budget_s=30)
    assert len(out) == 1 and len(backend.scans) == 1


def test_pivot_respects_time_budget():
    full = mk("fullhash", "obf", 8_100_000_000, 1786991500)
    backend = FakeBackend([full], {"h2": mkv(TRACKS), "fullhash": mkv(TRACKS, duration_ms=3389000.0)})
    assert pivot.pivot_from_samples([V2], backend, budget_s=0) == []
    assert backend.scans == []
