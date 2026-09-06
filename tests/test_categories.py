"""Category detection for real Easynews titles, as seen by Sonarr/Radarr via Prowlarr.

anime_hint=True means the incoming request asked for category 5070 (every Sonarr
request does; Radarr requests never do).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("EASYNEWS_USER", "test")
os.environ.setdefault("EASYNEWS_PASS", "test")

import pytest  # noqa: E402

import server  # noqa: E402


def cat(title, anime_hint=False):
    meta = server._extract_release_markers(title)
    return server._detect_category(title, meta, anime_hint=anime_hint)


# --- fansub-style titles (group in brackets) ---------------------------------

def test_whitelisted_group_episode_at_end_of_title_is_anime():
    assert cat("[Judas] One Piece - 1165") == server.CATEGORY_ANIME


def test_unlisted_fansub_group_is_still_anime():
    assert cat("[AnimeSakura] Solo Leveling - 03 - 2160p ESub") == server.CATEGORY_ANIME


def test_known_good_fansub_title_stays_anime():
    assert cat("[HorribleSubs] One Piece - 744 [720p]") == server.CATEGORY_ANIME


@pytest.mark.parametrize("title", ["[BBC] Planet Earth - 01", "[REPACK] Some Show - 01"])
def test_non_fansub_bracket_tags_are_not_anime_without_hint(title):
    assert cat(title) != server.CATEGORY_ANIME


# --- bare "Title NNN" absolute-episode titles --------------------------------

@pytest.mark.parametrize("title", ["One Piece 485", "One Piece - 582"])
def test_bare_absolute_episode_is_anime_when_anime_requested(title):
    assert cat(title, anime_hint=True) == server.CATEGORY_ANIME


def test_bare_absolute_episode_is_not_anime_for_movie_requests():
    assert cat("One Piece 485") != server.CATEGORY_ANIME


def test_trailing_year_is_not_treated_as_an_episode():
    assert cat("The Matrix 1999", anime_hint=True) != server.CATEGORY_ANIME


def test_resolution_suffix_is_not_an_episode():
    assert cat("Frieren_4K", anime_hint=True) != server.CATEGORY_ANIME


# --- regressions ---------------------------------------------------------------

def test_sxxeyy_titles_stay_tv_even_with_anime_hint():
    assert cat("Frieren - Beyond Journey's End - S02E02", anime_hint=True) == server.CATEGORY_TV


def test_sxxeyy_hd_title_stays_tv_hd():
    assert cat("the.bear.s03e01.1080p.webrip.x264-avtomat.mkv") == server.CATEGORY_TV_HD


# --- dotted release names (what the 3.0 API and most scene posts look like) --------

@pytest.mark.parametrize("title", [
    "[Sick-Fansubs].One.Piece.1176.[1080p][1DBD9AB7].mp4",
    "[HatSubs].One.Piece.1012.(BD.1080p.10-bit.Opus).[551F705F].mkv",
    "[Feibanyama].One.Piece.EP1160.[IQIYI.WebRip.2160p.HEVC.AAC.Multi-Subs].mkv",
])
def test_dotted_fansub_group_titles_are_anime_without_hint(title):
    assert cat(title) == server.CATEGORY_ANIME


@pytest.mark.parametrize("title", [
    "One.Piece.485.mkv",
    "One.Piece.EP1177.1080p.TVER.WEB-DL.JPN.AAC2.0.H.264-ToonsHub.mkv",
    "one.piece.e1177.subfrench.1080p.web.x264-amb3r.mkv",
    "one.piece.e0083.remastered.multi.1080p.web.h264-d4kid.mkv",
    "One.Piece.EP1177.Episode.1177.1080p.CR.WEB-DL.JPN.AAC2.0.H.264.ESub-ToonsHub.mkv",
])
def test_dotted_bare_absolute_episode_is_anime_when_anime_requested(title):
    assert cat(title, anime_hint=True) == server.CATEGORY_ANIME


@pytest.mark.parametrize("title", [
    "The.Matrix.1999.1080p.BluRay.x264-GRP.mkv",
    "Alien.Romulus.2024.2160p.WEB-DL.DDP5.1.Atmos.H.265-FLUX.mkv",
    "Movie.Title.2.2019.1080p.WEB.H264-GRP.mkv",
    "Some.Documentary.1080p.WEB.H.264-GRP.mkv",
])
def test_dotted_movie_names_are_not_anime_even_with_hint(title):
    assert cat(title, anime_hint=True) != server.CATEGORY_ANIME


def test_dotted_season_episode_with_separator_is_tv():
    assert cat("@AnimesHunt-Solo.Leveling.S02.E02.[1080p].MULTI.~.VyxoR.mkv", anime_hint=True) == server.CATEGORY_TV_HD
