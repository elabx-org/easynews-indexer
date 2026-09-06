"""Strict matching: contiguous title, season/episode marker anywhere after it, year optional."""
import pytest

import server


def strict(title, phrase):
    return server._matches_strict(title, server._sanitize_phrase(phrase))


@pytest.mark.parametrize("title,phrase,expected", [
    ("lanterns.s01e01.german.dl.1080p.web.h264-wvf", "lanterns S01E01", True),
    ("lanterns.2026.s01e01.internal.hdr.2160p.web.h265-edith.sample", "lanterns S01E01", True),   # year between title and SxxEyy
    ("Lanterns.2026.S01E01.1080p.WEB.H264-SuccessfulCrab", "lanterns S01E01", True),
    ("green.lanterns.light-rmxtras", "lanterns S01E01", False),                                   # no episode marker
    ("lanterns.2026.s01e02.1080p.web", "lanterns S01E01", False),                                  # wrong episode
    ("the.bear.s03e01.1080p.web.h264", "the bear S03E01", True),
    ("the.chosen.in.the.wild.with.bear.grylls.s03e01", "the bear S03E01", False),                  # title not contiguous
    ("The.Matrix.1999.1080p.BluRay.x264", "the matrix 1999", True),
    ("The.Matrix.1080p.BluRay.x264", "the matrix 1999", True),                                     # year missing from name is tolerated...
    ("The.Matrix.Reloaded.2003.1080p", "the matrix 1999", True),                                   # ...year conflicts are query_meta's job
    ("Lanterns.2026.S01.1080p.WEB.H264-GRP", "lanterns S01", True),
])
def test_matches_strict(title, phrase, expected):
    assert strict(title, phrase) is expected


def test_year_conflict_is_still_rejected_end_to_end():
    items = [
        {"hash": "a", "fn": "The.Matrix.Reloaded.2003.1080p.BluRay.x264-GRP", "extension": ".mkv", "size": 9_000_000_000, "runtime": 8000, "type": "VIDEO"},
        {"hash": "b", "fn": "The.Matrix.1999.1080p.BluRay.x264-GRP", "extension": ".mkv", "size": 9_000_000_000, "runtime": 8000, "type": "VIDEO"},
    ]
    out = server.filter_and_map({"data": items}, min_bytes=1, strict_phrase=server._sanitize_phrase("the matrix 1999"),
                                strict_match=True, query_meta={"year": 1999})
    assert [i["hash"] for i in out] == ["b"]
