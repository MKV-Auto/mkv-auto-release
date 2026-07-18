"""
Unit tests for workers.tasks pure helpers: _safe_track_folder, _backfill_preview_title_ids,
_parse_makemkv_titles_saved_failed.
"""
import pytest

from workers.tasks import (
    _safe_track_folder,
    _backfill_preview_title_ids,
    _parse_makemkv_titles_saved_failed,
)


class TestSafeTrackFolder:
    """Tests for _safe_track_folder(name: str) -> str."""

    def test_normal_name(self):
        assert _safe_track_folder("title-1") == "title-1"
        assert _safe_track_folder("title_1") == "title_1"
        assert _safe_track_folder("00001.mpls") == "00001"

    def test_empty_after_sanitize_returns_track(self):
        assert _safe_track_folder("") == "track"

    def test_special_chars_replaced_with_underscore(self):
        assert _safe_track_folder("a b") == "a_b"
        assert " " not in _safe_track_folder("a b c")
        assert _safe_track_folder("a.b") == "a"

    def test_path_like_uses_stem(self):
        assert _safe_track_folder("subdir/movie_t01.mkv") == "movie_t01"
        assert _safe_track_folder("previews/t1/preview.m3u8") == "preview"


class TestBackfillPreviewTitleIds:
    """Tests for _backfill_preview_title_ids(disc_payload: dict) -> dict."""

    def test_returns_empty_dict_when_payload_not_dict(self):
        # Implementation uses payload={} when not a dict; does not pass through.
        result = _backfill_preview_title_ids(None)
        assert isinstance(result, dict) and len(result) == 0
        result = _backfill_preview_title_ids("x")
        assert isinstance(result, dict) and len(result) == 0

    def test_returns_unchanged_when_previews_not_dict(self):
        payload = {"previews": "x", "title_filename_map": {}}
        assert _backfill_preview_title_ids(payload) == payload

    def test_returns_unchanged_when_tracks_not_dict(self):
        payload = {"previews": {"tracks": []}, "title_filename_map": {}}
        assert _backfill_preview_title_ids(payload) == payload

    def test_returns_unchanged_when_title_filename_map_not_dict(self):
        payload = {"previews": {"tracks": {}}, "title_filename_map": "x"}
        assert _backfill_preview_title_ids(payload) == payload

    def test_returns_unchanged_when_filename_to_id_empty(self):
        payload = {"previews": {"tracks": {"t1": {"source": "a.mkv"}}}, "title_filename_map": {}}
        assert _backfill_preview_title_ids(payload) == payload

    def test_backfills_title_id_from_source_via_filename_map(self):
        payload = {
            "title_filename_map": {"tid-1": "movie_t01.mkv"},
            "previews": {
                "tracks": {
                    "t1": {"source": "movie_t01.mkv"},
                }
            },
        }
        result = _backfill_preview_title_ids(payload)
        assert result["previews"]["tracks"]["t1"]["title_id"] == "tid-1"

    def test_backfills_using_rel_base_when_full_path_not_in_map(self):
        payload = {
            "title_filename_map": {"tid-2": "movie_t02.mkv"},
            "previews": {
                "tracks": {
                    "t2": {"source": "finalize/movie_t02.mkv"},
                }
            },
        }
        result = _backfill_preview_title_ids(payload)
        assert result["previews"]["tracks"]["t2"]["title_id"] == "tid-2"

    def test_skips_entry_with_existing_different_title_id(self):
        payload = {
            "title_filename_map": {"tid-new": "a.mkv"},
            "previews": {
                "tracks": {
                    "t1": {"source": "a.mkv", "title_id": "tid-old"},
                }
            },
        }
        result = _backfill_preview_title_ids(payload)
        assert result["previews"]["tracks"]["t1"]["title_id"] == "tid-old"

    def test_skips_entry_without_source(self):
        payload = {
            "title_filename_map": {"tid-1": "a.mkv"},
            "previews": {
                "tracks": {
                    "t1": {},
                }
            },
        }
        result = _backfill_preview_title_ids(payload)
        assert "title_id" not in result["previews"]["tracks"]["t1"]

    def test_skips_non_dict_entry(self):
        payload = {
            "title_filename_map": {"tid-1": "a.mkv"},
            "previews": {
                "tracks": {
                    "t1": "not-a-dict",
                }
            },
        }
        result = _backfill_preview_title_ids(payload)
        assert result["previews"]["tracks"]["t1"] == "not-a-dict"


class TestParseMakemkvTitlesSavedFailed:
    """Tests for _parse_makemkv_titles_saved_failed (#313)."""

    def test_copy_complete_titles_saved_only(self):
        log = 'MSG:5036,0,0,"Copy complete. 2 titles saved."'
        saved, failed = _parse_makemkv_titles_saved_failed(log)
        assert saved == 2
        assert failed == 0

    def test_titles_saved_and_failed(self):
        log = "Copy complete. 1 titles saved, 2 failed."
        saved, failed = _parse_makemkv_titles_saved_failed(log)
        assert saved == 1
        assert failed == 2

    def test_no_match_returns_none_zero(self):
        log = "Some other log line"
        saved, failed = _parse_makemkv_titles_saved_failed(log)
        assert saved is None
        assert failed == 0

    def test_robot_msg_5037_line_parses_saved_failed(self):
        log = (
            'MSG:5037,516,2,"Copy complete. 5 titles saved, 11 failed.",'
            '"Copy complete. %1 titles saved, %2 failed.","5","11"'
        )
        saved, failed = _parse_makemkv_titles_saved_failed(log)
        assert saved == 5
        assert failed == 11
