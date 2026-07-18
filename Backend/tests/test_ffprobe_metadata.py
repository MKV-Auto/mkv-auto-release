# Tests for core.ffprobe_metadata
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from core.ffprobe_metadata import (
    MetadataScanResult,
    metadata_scan_to_summary,
    scan_file_metadata,
    is_metadata_scan_disabled,
)


class TestScanFileMetadata:
    def test_file_not_found(self):
        r = scan_file_metadata(Path("/nonexistent/file.mkv"))
        assert r is None

    def test_valid_mkv_returns_result(self, tmp_path):
        f = tmp_path / "x.mkv"
        f.write_bytes(b"x")
        ffprobe_data = {
            "format": {"duration": "120.5", "size": "1000000", "bit_rate": "64000"},
            "streams": [
                {"codec_type": "video", "codec_name": "hevc", "width": 1920, "height": 1080, "pix_fmt": "yuv420p10le", "color_transfer": "smpte2084"},
                {"codec_type": "audio", "codec_name": "eac3", "channels": 6, "channel_layout": "5.1", "disposition": {}, "tags": {}},
                {"codec_type": "subtitle", "codec_name": "ass", "disposition": {}, "tags": {}},
            ],
        }
        with patch("core.ffprobe_metadata._run_ffprobe_json", return_value=ffprobe_data):
            with patch("core.ffprobe_metadata._count_chapters", return_value=5):
                r = scan_file_metadata(f)
        assert r is not None
        assert r.format.get("duration") == 120.5
        assert r.stream_counts["video"] == 1
        assert r.stream_counts["audio"] == 1
        assert r.stream_counts["subtitle"] == 1
        assert r.chapters_count == 5
        assert r.video_hints is not None
        assert r.video_hints.get("pix_fmt") == "yuv420p10le"
        assert len(r.audio_summary) == 1
        assert len(r.subtitle_summary) == 1

    def test_ffprobe_failure_returns_result_with_warning(self, tmp_path):
        f = tmp_path / "x.mkv"
        f.write_bytes(b"x")
        with patch("core.ffprobe_metadata._run_ffprobe_json", return_value=None):
            with patch("core.ffprobe_metadata.FFPROBE_METADATA_RETRY_ATTEMPTS", 1):
                r = scan_file_metadata(f)
        assert r is not None
        assert r.warning == "ffprobe failed or timed out"
        assert r.stream_counts["video"] == 0
        assert r.stream_counts["audio"] == 0

    def test_ffprobe_retries_then_succeeds(self, tmp_path):
        f = tmp_path / "x.mkv"
        f.write_bytes(b"x")
        ffprobe_data = {
            "format": {"duration": "1", "size": "100", "bit_rate": "800000"},
            "streams": [{"codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720}],
        }
        with patch("core.ffprobe_metadata._run_ffprobe_json", side_effect=[None, ffprobe_data]):
            with patch("core.ffprobe_metadata.FFPROBE_METADATA_RETRY_ATTEMPTS", 2):
                with patch("core.ffprobe_metadata.time.sleep"):
                    with patch("core.ffprobe_metadata._count_chapters", return_value=0):
                        r = scan_file_metadata(f)
        assert r is not None
        assert r.warning is None
        assert r.stream_counts["video"] == 1


class TestMetadataScanToSummary:
    def test_none_returns_minimal(self):
        s = metadata_scan_to_summary(None)
        assert s["quality_tier"] == "minimal"
        assert s["subtitle_tier"] == "minimal"
        assert s["audio_tier"] == "minimal"

    def test_empty_dict_returns_minimal(self):
        s = metadata_scan_to_summary({})
        assert s["quality_tier"] == "minimal"

    def test_10bit_hdr_1080p_quality_best(self):
        scan = {
            "video_hints": {"width": 1920, "height": 1080, "pix_fmt": "yuv420p10le", "color_transfer": "smpte2084"},
            "stream_counts": {"video": 1, "audio": 1, "subtitle": 1},
            "audio_summary": [{"channels": 8, "channel_layout": "7.1", "codec_name": "eac3"}],
            "subtitle_summary": [{"forced": True}, {"language": "eng"}],
        }
        s = metadata_scan_to_summary(scan)
        assert s["quality_tier"] == "best"
        assert "10-bit" in s["quality_hints"]
        assert "HDR" in s["quality_hints"]
        assert s["audio_tier"] == "best"
        assert "7.1" in s["audio_hints"]

    def test_8bit_1080p_quality_ok(self):
        scan = {
            "video_hints": {"width": 1920, "height": 1080, "pix_fmt": "yuv420p"},
            "stream_counts": {"video": 1, "audio": 1, "subtitle": 0},
            "audio_summary": [{"channels": 6, "channel_layout": "5.1"}],
            "subtitle_summary": [],
        }
        s = metadata_scan_to_summary(scan)
        assert s["quality_tier"] == "ok"
        assert s["audio_tier"] == "ok"
        assert "5.1" in s["audio_hints"]


class TestIsMetadataScanDisabled:
    def test_env_disables(self):
        with patch("core.ffprobe_metadata.DISABLE_FFPROBE_METADATA_SCAN", True):
            assert is_metadata_scan_disabled() is True

    def test_settings_disables(self):
        with patch("core.ffprobe_metadata.DISABLE_FFPROBE_METADATA_SCAN", False):
            with patch("core.settings.load_settings", return_value={"disable_ffprobe_metadata_scan": True}):
                # is_metadata_scan_disabled does "from core import settings" then settings.load_settings()
                assert is_metadata_scan_disabled() is True
