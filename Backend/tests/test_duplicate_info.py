"""Unit tests for on-the-fly duplicate title detection (core.duplicate_info)."""

import pytest

from core.duplicate_info import (
    attach_duplicate_info,
    _tags_from_title_payload,
    _comparative_metrics,
    _comparative_diff_tags,
    _parse_mkv_stream_bitrate_bps,
)


def test_two_titles_same_segment_map_both_get_duplicate_info():
    """Two titles with same segment_map get duplicate_info with group_size=2 and metrics."""
    titles_by_id = {
        "t1": {"title_id": "t1", "src": "t1", "segment_map": "1,2,3", "title": "Title A"},
        "t2": {"title_id": "t2", "src": "t2", "segment_map": "1,2,3", "title": "Title B"},
    }
    attach_duplicate_info(titles_by_id, "disc-123")
    assert "duplicate_info" in titles_by_id["t1"]
    assert "duplicate_info" in titles_by_id["t2"]
    di1 = titles_by_id["t1"]["duplicate_info"]
    di2 = titles_by_id["t2"]["duplicate_info"]
    assert di1["group_size"] == 2
    assert di2["group_size"] == 2
    assert di1["same_as"] == ["t2"]
    assert di2["same_as"] == ["t1"]
    assert di1["group_id"] == di2["group_id"]
    assert di1["group_id"].startswith("disc:disc-123:")
    assert di1["confidence"] == "high"
    assert di1["tags"] == []
    assert di1["diff_tags"] == []
    assert "metrics" in di1
    assert di1["metrics"]["scan_usable"] is False


def test_singleton_gets_duplicate_info_with_absolute_tags_only():
    """Single title with segment_map gets duplicate_info (tags only; diff_tags empty)."""
    titles_by_id = {
        "t1": {
            "title_id": "t1",
            "src": "t1",
            "segment_map": "1,2,3",
            "title": "Title A",
            "streams": [{"type": "Video", "resolution": "1920 x 1080"}],
        },
    }
    attach_duplicate_info(titles_by_id, "disc-123")
    di = titles_by_id["t1"]["duplicate_info"]
    assert di["group_size"] == 1
    assert di["same_as"] == []
    assert di["diff_tags"] == []
    assert "quality:1080p" in di["tags"]


def test_empty_or_none_segment_map_no_duplicate_info():
    """Titles with empty or None segment_map do not get duplicate_info."""
    titles_by_id = {
        "t1": {"title_id": "t1", "src": "t1", "segment_map": None, "title": "A"},
        "t2": {"title_id": "t2", "src": "t2", "segment_map": "", "title": "B"},
    }
    attach_duplicate_info(titles_by_id, "disc-123")
    assert "duplicate_info" not in titles_by_id["t1"]
    assert "duplicate_info" not in titles_by_id["t2"]


def test_three_titles_two_same_segment_third_singleton():
    """Pair shares group; third title has its own singleton duplicate_info."""
    titles_by_id = {
        "t1": {"title_id": "t1", "src": "t1", "segment_map": "1,2,3", "title": "A"},
        "t2": {"title_id": "t2", "src": "t2", "segment_map": "1,2,3", "title": "B"},
        "t3": {"title_id": "t3", "src": "t3", "segment_map": "4,5,6", "title": "C"},
    }
    attach_duplicate_info(titles_by_id, "disc-123")
    assert titles_by_id["t1"]["duplicate_info"]["group_size"] == 2
    assert titles_by_id["t2"]["duplicate_info"]["group_size"] == 2
    assert titles_by_id["t3"]["duplicate_info"]["group_size"] == 1
    assert titles_by_id["t3"]["duplicate_info"]["same_as"] == []


def test_disc_id_empty_no_duplicate_info():
    """When disc_id is empty, no duplicate_info is attached."""
    titles_by_id = {
        "t1": {"title_id": "t1", "src": "t1", "segment_map": "1,2,3", "title": "A"},
        "t2": {"title_id": "t2", "src": "t2", "segment_map": "1,2,3", "title": "B"},
    }
    attach_duplicate_info(titles_by_id, "")
    assert "duplicate_info" not in titles_by_id["t1"]
    assert "duplicate_info" not in titles_by_id["t2"]


def test_titles_by_id_empty_no_op():
    """Empty titles_by_id is a no-op."""
    titles_by_id = {}
    attach_duplicate_info(titles_by_id, "disc-123")
    assert titles_by_id == {}


def test_segment_map_normalization_same_group():
    """Slight segment_map variations normalize to same group."""
    titles_by_id = {
        "t1": {"title_id": "t1", "src": "t1", "segment_map": "1,2,3", "title": "A"},
        "t2": {"title_id": "t2", "src": "t2", "segment_map": "1, 2, 3", "title": "B"},
    }
    attach_duplicate_info(titles_by_id, "disc-123")
    assert "duplicate_info" in titles_by_id["t1"]
    assert "duplicate_info" in titles_by_id["t2"]


def test_comparative_metrics_from_metadata_scan():
    """_comparative_metrics extracts chapters, subtitle langs, audio_score, video_bitrate, pixels."""
    payload = {
        "metadata_scan": {
            "chapters_count": 40,
            "subtitle_summary": [{"language": "eng"}, {"language": "fra"}],
            "audio_summary": [
                {"codec_name": "dts", "channels": 6, "channel_layout": "5.1(side)"},
            ],
            "format": {"bit_rate": 21089667},
            "video_hints": {"width": 1920, "height": 1080},
        },
    }
    m = _comparative_metrics(payload)
    assert m["chapters_count"] == 40
    assert m["subtitle_count"] == 2
    assert m["subtitle_language_count"] == 2
    assert m["audio_score"] >= 2
    assert m["video_bitrate"] == 21089667
    assert m["video_pixels"] == 1920 * 1080
    assert m["scan_usable"] is True


def test_comparative_diff_tags_group_max_chapters_and_subs():
    """Group-max: both at max get the tag; not at max do not."""
    my_m = {
        "chapters_count": 40,
        "subtitle_language_count": 4,
        "audio_score": 2,
        "audio_language_count": 1,
        "video_bitrate": 20_000_000,
        "video_pixels": 1920 * 1080,
    }
    others = [
        {
            "chapters_count": 40,
            "subtitle_language_count": 6,
            "audio_score": 2,
            "audio_language_count": 1,
            "video_bitrate": 22_773_535,
            "video_pixels": 1920 * 1080,
        },
    ]
    diff = _comparative_diff_tags(my_m, others)
    assert "chapters:more" in diff  # tied at 40
    assert "subs:more-languages" not in diff
    assert "video:best" not in diff  # other has higher bitrate at same res

    my_m2 = others[0]
    others2 = [my_m]
    diff2 = _comparative_diff_tags(my_m2, others2)
    assert "chapters:more" in diff2
    assert "subs:more-languages" in diff2
    assert "video:best" in diff2


def test_comparative_diff_tags_no_emission_when_scan_metrics_all_zero():
    """No comparative tags when all scan-only metrics are zero (failed / empty scan)."""
    z = {
        "chapters_count": 0,
        "subtitle_language_count": 0,
        "audio_score": 0,
        "audio_language_count": 0,
        "video_bitrate": None,
        "video_pixels": 0,
    }
    assert _comparative_diff_tags(z, [dict(z)]) == []


def test_parse_mkv_stream_bitrate_bps():
    assert _parse_mkv_stream_bitrate_bps("25.3 Mb/s") == 25_300_000
    assert _parse_mkv_stream_bitrate_bps("8000 kbps") == 8_000_000
    assert _parse_mkv_stream_bitrate_bps(21_000_000) == 21_000_000


def test_comparative_metrics_streams_fallback_when_scan_unusable():
    """Failed ffprobe + MakeMKV streams still yield merged metrics for auto-primary."""
    payload = {
        "metadata_scan": {"warning": "ffprobe timed out"},
        "streams": [
            {"type": "Video", "bitrate": "20000 kbps"},
            {"type": "Subtitles"},
            {"type": "Subtitles"},
        ],
    }
    m = _comparative_metrics(payload)
    assert m["scan_usable"] is False
    assert m["subtitle_count"] == 2
    assert m["video_bitrate"] == 20_000_000


def test_comparative_metrics_usable_scan_does_not_take_streams_subtitles():
    """When scan is usable, subtitle counts come from ffprobe summary, not streams."""
    payload = {
        "metadata_scan": {
            "chapters_count": 1,
            "subtitle_summary": [{"language": "eng"}],
            "audio_summary": [{"codec_name": "aac", "channels": 2}],
            "format": {"bit_rate": 5_000_000, "duration": 100},
        },
        "streams": [{"type": "Subtitles"}, {"type": "Subtitles"}, {"type": "Subtitles"}],
    }
    m = _comparative_metrics(payload)
    assert m["scan_usable"] is True
    assert m["subtitle_count"] == 1


def test_diff_tags_scan_only_ignores_streams_when_scan_present():
    """diff_tags use scan-only metrics: stream subtitle rows do not affect comparative tags."""
    titles_by_id = {
        "t1": {
            "title_id": "t1",
            "segment_map": "75",
            "metadata_scan": {
                "subtitle_summary": [{"language": "eng"}],
                "format": {"bit_rate": 1_000_000, "duration": 1},
            },
            "streams": [
                {"type": "Video"},
                {"type": "Subtitles"},
                {"type": "Subtitles"},
                {"type": "Subtitles"},
            ],
        },
        "t2": {
            "title_id": "t2",
            "segment_map": "75",
            "metadata_scan": {
                "subtitle_summary": [{"language": "eng"}, {"language": "fra"}],
                "format": {"bit_rate": 1_000_000, "duration": 1},
            },
            "streams": [{"type": "Video"}, {"type": "Subtitles"}],
        },
    }
    attach_duplicate_info(titles_by_id, "disc-xyz")
    d1 = titles_by_id["t1"]["duplicate_info"]
    d2 = titles_by_id["t2"]["duplicate_info"]
    assert "subs:more-languages" in d2["diff_tags"]
    assert "subs:more-languages" not in d1["diff_tags"]


def test_tags_from_metadata_summary():
    """metadata_summary hints become tags (overlay)."""
    payload = {
        "title_id": "t1",
        "metadata_summary": {
            "quality_hints": ["4K", "10-bit", "HDR"],
            "audio_hints": ["7.1", "truehd"],
            "subtitle_hints": ["forced", "multiple languages"],
        },
    }
    tags = _tags_from_title_payload(payload)
    assert "quality:4k" in tags
    assert "quality:10-bit" in tags
    assert "quality:hdr" in tags
    assert "audio:7.1" in tags
    assert "audio:truehd" in tags
    assert "subs:forced" in tags
    assert "subs:multiple-languages" in tags


def test_tags_streams_first_with_summary_overlay():
    """Streams provide base tags; metadata_summary adds hints."""
    payload = {
        "streams": [{"type": "Video", "resolution": "3840 x 2160"}],
        "metadata_summary": {"quality_hints": ["HDR"], "audio_hints": [], "subtitle_hints": []},
    }
    tags = _tags_from_title_payload(payload)
    assert "quality:4k" in tags
    assert "quality:hdr" in tags


def test_duplicate_info_group_max_and_metrics():
    """Duplicate pair: absolute tags from summary; diff_tags group-max; metrics scan-only."""
    titles_by_id = {
        "t1": {
            "title_id": "t1",
            "src": "t1",
            "segment_map": "1,2,3",
            "metadata_summary": {"quality_hints": ["4K", "HDR"], "audio_hints": ["7.1"], "subtitle_hints": []},
            "metadata_scan": {
                "chapters_count": 40,
                "subtitle_summary": [
                    {"language": "eng"},
                    {"language": "fra"},
                    {"language": "spa"},
                    {"language": "por"},
                ],
                "audio_summary": [
                    {"codec_name": "dts", "channels": 6, "channel_layout": "5.1(side)"},
                    {"codec_name": "dts", "channels": 6, "channel_layout": "5.1(side)"},
                ],
                "format": {"bit_rate": 21089667},
                "video_hints": {"width": 1920, "height": 1080},
            },
        },
        "t2": {
            "title_id": "t2",
            "src": "t2",
            "segment_map": "1,2,3",
            "metadata_summary": {"quality_hints": ["1080p"], "audio_hints": ["5.1"], "subtitle_hints": ["forced"]},
            "metadata_scan": {
                "chapters_count": 37,
                "subtitle_summary": [
                    {"language": "eng"},
                    {"language": "fra"},
                    {"language": "spa"},
                    {"language": "por"},
                    {"language": "spa"},
                    {"language": "por"},
                ],
                "audio_summary": [
                    {"codec_name": "dts", "channels": 6, "channel_layout": "5.1(side)"},
                    {"codec_name": "dts", "channels": 6, "channel_layout": "5.1(side)"},
                    {"codec_name": "ac3", "channels": 6, "channel_layout": "5.1(side)"},
                    {"codec_name": "ac3", "channels": 6, "channel_layout": "5.1(side)"},
                    {"codec_name": "ac3", "channels": 6, "channel_layout": "5.1(side)"},
                ],
                "format": {"bit_rate": 22773535},
                "video_hints": {"width": 1920, "height": 1080},
            },
        },
    }
    attach_duplicate_info(titles_by_id, "disc-123")
    di1 = titles_by_id["t1"]["duplicate_info"]
    di2 = titles_by_id["t2"]["duplicate_info"]
    assert "quality:4k" in di1["tags"] and "quality:hdr" in di1["tags"]
    assert "audio:7.1" in di1["tags"]
    assert "quality:1080p" in di2["tags"] and "subs:forced" in di2["tags"]
    assert "chapters:more" in di1["diff_tags"]
    assert "chapters:more" not in di2["diff_tags"]
    assert "subs:more-languages" in di1["diff_tags"] and "subs:more-languages" in di2["diff_tags"]
    assert "audio:best" in di1["diff_tags"] and "audio:best" in di2["diff_tags"]
    assert "video:best" in di2["diff_tags"]
    assert "video:best" not in di1["diff_tags"]
    assert di1["metrics"]["chapters_count"] == 40
    assert di2["metrics"]["video_bitrate"] == 22773535


# ────────────────────────────────────────────────────────────────
# Auto-select primary tests
# ────────────────────────────────────────────────────────────────


def test_auto_select_primary_picks_largest_file():
    """Auto-selection picks the title with the largest size when audio/chapters are equal."""
    titles = {
        "t1": {"segment_map": "1,2,3", "size": 5_000_000_000, "metadata_scan": {}},
        "t2": {"segment_map": "1,2,3", "size": 8_000_000_000, "metadata_scan": {}},
    }
    attach_duplicate_info(titles, "disc-test")
    assert titles["t1"].get("active") is False
    assert titles["t2"].get("active") is True


def test_auto_select_primary_respects_existing_active():
    """If a title is already active=True, auto-selection does not override."""
    titles = {
        "t1": {"segment_map": "1,2,3", "size": 5_000_000_000, "active": True, "metadata_scan": {}},
        "t2": {"segment_map": "1,2,3", "size": 8_000_000_000, "metadata_scan": {}},
    }
    attach_duplicate_info(titles, "disc-test")
    assert titles["t1"].get("active") is True
    assert titles["t2"].get("active") is not True


def test_auto_select_primary_prefers_better_audio():
    """Audio score (weight 3) outweighs raw file size."""
    titles = {
        "t1": {
            "segment_map": "1,2,3",
            "size": 10_000_000_000,
            "metadata_scan": {
                "audio_summary": [{"codec_name": "ac3", "channels": 2, "channel_layout": "stereo"}],
            },
        },
        "t2": {
            "segment_map": "1,2,3",
            "size": 5_000_000_000,
            "metadata_scan": {
                "audio_summary": [{"codec_name": "truehd", "channels": 8, "channel_layout": "7.1"}],
            },
        },
    }
    attach_duplicate_info(titles, "disc-test")
    assert titles["t2"].get("active") is True
    assert titles["t1"].get("active") is False


# ────────────────────────────────────────────────────────────────
# compute_expected_path tests
# ────────────────────────────────────────────────────────────────


def test_compute_expected_path_movie_plex():
    from core.disc import compute_expected_path

    result = compute_expected_path(
        {"title": "The Matrix", "type": "MainMovie", "edition": "", "season": None, "episode": None},
        {"release_type": "movie", "release_name": ""},
        {"movie_name": "The Matrix", "production_year": 1999},
        media_server="plex",
        resolution="1080p",
    )
    assert result == "Movies/The Matrix (1999)/The Matrix.1080p.mkv"


def test_compute_expected_path_movie_with_edition_plex():
    from core.disc import compute_expected_path

    result = compute_expected_path(
        {"title": "The Matrix", "type": "MainMovie", "edition": "Director's Cut"},
        {"release_type": "movie"},
        {"movie_name": "The Matrix", "production_year": 1999},
        media_server="plex",
        resolution="4k",
    )
    assert "edition-Director" in result
    assert ".4k" in result


def test_compute_expected_path_series_plex():
    from core.disc import compute_expected_path

    result = compute_expected_path(
        {"title": "Pilot", "type": "Episode", "season": 1, "episode": 1},
        {"release_type": "series"},
        {"movie_name": "Lost"},
        media_server="plex",
        resolution="1080p",
    )
    assert "Series/Lost/Season 01/" in result
    assert "Lost - s01e01 - Pilot" in result


def test_compute_expected_path_movie_jellyfin():
    from core.disc import compute_expected_path

    result = compute_expected_path(
        {"title": "The Matrix", "type": "MainMovie", "edition": "Extended"},
        {"release_type": "movie"},
        {"movie_name": "The Matrix", "production_year": 1999},
        media_server="jellyfin",
        resolution="4k",
    )
    assert "- [Extended]" in result
    assert "[2160p]" in result


def test_compute_expected_path_extras_subfolder():
    from core.disc import compute_expected_path

    result = compute_expected_path(
        {"title": "Making Of", "type": "Extra"},
        {"release_type": "movie"},
        {"movie_name": "The Matrix", "production_year": 1999},
        media_server="plex",
    )
    assert "/Other/" in result
    assert result.endswith("Making Of.mkv")


def test_compute_expected_path_trailer_plex_vs_jellyfin():
    from core.disc import compute_expected_path

    meta = {
        "title": "Theatrical Trailer",
        "type": "Trailer",
        "edition": "",
        "season": None,
        "episode": None,
    }
    rel = {"release_type": "movie", "release_name": ""}
    movie = {"movie_name": "The Matrix", "production_year": 1999}
    plex_path = compute_expected_path(meta, rel, movie, media_server="plex")
    jf_path = compute_expected_path(meta, rel, movie, media_server="jellyfin")
    assert "/Trailers/" in plex_path
    assert "/trailers/" in jf_path


# ── Subsumed-m2ts → wrapper group absorption (PR 2 of titles-step plan) ──────


def test_subsumed_m2ts_joins_wrapper_mpls_group():
    """The Midway 4K case: an m2ts wrapped by an mpls picks up the wrapper's
    duplicate_info.group_id so the left-rail collapse renders them as one row."""
    titles_by_id = {
        # Wrapper mpls with two segment-set sibling permutations.
        "mpls-1": {"title_id": "mpls-1", "src": "mpls-1",
                   "segment_map": "504,510,501", "title": "Canonical"},
        "mpls-2": {"title_id": "mpls-2", "src": "mpls-2",
                   "segment_map": "501,510,504", "title": "Decoy"},
        # m2ts subsumed by mpls-1.
        "m2ts-7": {"title_id": "m2ts-7", "src": "m2ts-7",
                   "segment_map": "504", "title": "Clip 504",
                   "subsumed_by_title_id": "mpls-1"},
    }
    attach_duplicate_info(titles_by_id, "disc-abc")
    wrapper_gid = titles_by_id["mpls-1"]["duplicate_info"]["group_id"]
    sibling_gid = titles_by_id["mpls-2"]["duplicate_info"]["group_id"]
    m2ts_gid = titles_by_id["m2ts-7"]["duplicate_info"]["group_id"]
    # All three share the wrapper's group_id (sorted-segment-set sibling +
    # subsumed m2ts both collapse into the wrapper's row).
    assert wrapper_gid == sibling_gid == m2ts_gid


def test_subsumed_m2ts_with_singleton_wrapper_creates_synthetic_group():
    """If the wrapper has no sorted-segment-set siblings (it's an mpls on
    its own), the m2ts still gets absorbed via a synthetic group keyed on
    the wrapper's title_id."""
    titles_by_id = {
        "mpls-1": {"title_id": "mpls-1", "src": "mpls-1",
                   "segment_map": "504", "title": "Lone mpls"},
        "m2ts-7": {"title_id": "m2ts-7", "src": "m2ts-7",
                   "segment_map": "504", "title": "Clip 504",
                   "subsumed_by_title_id": "mpls-1"},
    }
    attach_duplicate_info(titles_by_id, "disc-abc")
    assert "duplicate_info" in titles_by_id["mpls-1"]
    assert "duplicate_info" in titles_by_id["m2ts-7"]
    # Both share the same group_id even though only the wrapper had a
    # segment_map and the m2ts was a singleton initially.
    assert (
        titles_by_id["mpls-1"]["duplicate_info"]["group_id"]
        == titles_by_id["m2ts-7"]["duplicate_info"]["group_id"]
    )


def test_subsumed_m2ts_with_no_segment_map_wrapper_synthesized():
    """Wrapper has NO segment_map at all (edge case). The helper creates a
    synthetic group anyway so the m2ts still has a parent to collapse
    under — without this branch, the m2ts would render as a standalone row."""
    titles_by_id = {
        "mpls-1": {"title_id": "mpls-1", "src": "mpls-1",
                   "segment_map": None, "title": "Wrapper no seg"},
        "m2ts-7": {"title_id": "m2ts-7", "src": "m2ts-7",
                   "segment_map": "999", "title": "Clip 999",
                   "subsumed_by_title_id": "mpls-1"},
    }
    attach_duplicate_info(titles_by_id, "disc-abc")
    # m2ts gets a group identity that points back to the wrapper.
    assert "duplicate_info" in titles_by_id["m2ts-7"]
    assert "duplicate_info" in titles_by_id["mpls-1"]
    assert (
        titles_by_id["m2ts-7"]["duplicate_info"]["group_id"]
        == titles_by_id["mpls-1"]["duplicate_info"]["group_id"]
    )


def test_subsumed_m2ts_evicted_from_own_singleton_group():
    """An m2ts that USED to render as its own singleton row (because it had
    a segment_map) gets evicted from that group when subsumed_by points
    elsewhere — otherwise the user sees both the wrapper row AND a
    duplicate-of-itself row in the left rail."""
    titles_by_id = {
        "mpls-1": {"title_id": "mpls-1", "src": "mpls-1",
                   "segment_map": "1,2,3", "title": "Wrapper"},
        # m2ts has its own (singleton) segment_map = "5" — would naturally
        # form a singleton group; absorption MUST clear that.
        "m2ts-5": {"title_id": "m2ts-5", "src": "m2ts-5",
                   "segment_map": "5", "title": "Clip 5",
                   "subsumed_by_title_id": "mpls-1"},
    }
    attach_duplicate_info(titles_by_id, "disc-abc")
    # m2ts shares the wrapper's group_id (the singleton "5" group was
    # discarded, the m2ts is in mpls-1's group instead).
    assert (
        titles_by_id["m2ts-5"]["duplicate_info"]["group_id"]
        == titles_by_id["mpls-1"]["duplicate_info"]["group_id"]
    )


def test_orphan_m2ts_with_no_subsumed_by_still_gets_singleton_group():
    """Conservative-subsumption guard: an m2ts WITHOUT subsumed_by_title_id
    (real example: Rick and Morty TV-extra m2ts that no mpls references)
    keeps its own singleton duplicate_info. The collapse rule only fires
    when the subsumption pipeline positively confirms a wrapper."""
    titles_by_id = {
        "mpls-1": {"title_id": "mpls-1", "src": "mpls-1",
                   "segment_map": "1,2,3", "title": "Wrapper"},
        "m2ts-orphan": {"title_id": "m2ts-orphan", "src": "m2ts-orphan",
                        "segment_map": "99", "title": "Orphan Extra",
                        "subsumed_by_title_id": None},
    }
    attach_duplicate_info(titles_by_id, "disc-abc")
    # Orphan still gets duplicate_info, but with its own group_id —
    # different from the wrapper's. The left rail will render it as its
    # own row, exactly what we want for the Rick and Morty case.
    assert (
        titles_by_id["m2ts-orphan"]["duplicate_info"]["group_id"]
        != titles_by_id["mpls-1"]["duplicate_info"]["group_id"]
    )


# ── Ungroup escape hatch (PR 5: force_independent_group flag) ────────────────


def test_force_independent_group_excludes_from_grouping():
    """When `force_independent_group=True`, the title is excluded from
    sibling grouping AND from subsumed-m2ts absorption — it renders as
    its own row. Reversible via the same flag (frontend toggles)."""
    titles_by_id = {
        "mpls-1": {"title_id": "mpls-1", "src": "mpls-1",
                   "segment_map": "1,2,3", "title": "Canonical"},
        "mpls-2": {"title_id": "mpls-2", "src": "mpls-2",
                   "segment_map": "3,1,2", "title": "Should be sibling but is split off",
                   "force_independent_group": True},
        "mpls-3": {"title_id": "mpls-3", "src": "mpls-3",
                   "segment_map": "2,3,1", "title": "Stays with canonical"},
    }
    attach_duplicate_info(titles_by_id, "disc-abc")
    # mpls-1 and mpls-3 share a group; mpls-2 is force-split.
    g1 = titles_by_id["mpls-1"]["duplicate_info"]["group_id"]
    g3 = titles_by_id["mpls-3"]["duplicate_info"]["group_id"]
    assert g1 == g3
    # mpls-2 has no duplicate_info at all (skipped before grouping).
    assert "duplicate_info" not in titles_by_id["mpls-2"]


def test_force_independent_group_off_falls_back_to_normal_grouping():
    """Defensive: flag=False is identical to flag-absent — siblings
    collapse as usual."""
    titles_by_id = {
        "mpls-1": {"title_id": "mpls-1", "src": "mpls-1",
                   "segment_map": "1,2,3"},
        "mpls-2": {"title_id": "mpls-2", "src": "mpls-2",
                   "segment_map": "3,1,2",
                   "force_independent_group": False},
    }
    attach_duplicate_info(titles_by_id, "disc-abc")
    g1 = titles_by_id["mpls-1"]["duplicate_info"]["group_id"]
    g2 = titles_by_id["mpls-2"]["duplicate_info"]["group_id"]
    assert g1 == g2
