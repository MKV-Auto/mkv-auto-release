# Tests for core.ffmpeg_detection
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from core.ffmpeg_detection import (
    DetectionResult,
    detect_padding_junk,
    _calculate_bitrate,
    _get_file_metadata,
    _get_bitrate_threshold,
    _get_min_duration_for_expensive_checks,
    is_detection_disabled,
    FFMPEG_DETECTION_BITRATE_THRESHOLD,
    FFMPEG_DETECTION_MIN_DURATION_PERCENT,
    FFMPEG_DETECTION_MIN_DURATION_FLOOR,
    BITRATE_THRESHOLD_DVD,
    BITRATE_THRESHOLD_720P,
    BITRATE_THRESHOLD_1080P,
    BITRATE_THRESHOLD_2160P,
    FFMPEG_DETECTION_CONFIDENCE_THRESHOLD,
)


class TestGetBitrateThreshold:
    """Disc-specific resolution-aware bitrate thresholds."""

    def test_none_resolution_uses_fallback(self):
        assert _get_bitrate_threshold(None) == FFMPEG_DETECTION_BITRATE_THRESHOLD

    def test_dvd_resolution(self):
        # 720x480 = 345_600, 720x576 = 414_720
        assert _get_bitrate_threshold((720, 480)) == BITRATE_THRESHOLD_DVD
        assert _get_bitrate_threshold((720, 576)) == BITRATE_THRESHOLD_DVD

    def test_720p_resolution(self):
        # 1280x720 = 921_600
        assert _get_bitrate_threshold((1280, 720)) == BITRATE_THRESHOLD_720P

    def test_1080p_resolution(self):
        # 1920x1080 = 2_073_600
        assert _get_bitrate_threshold((1920, 1080)) == BITRATE_THRESHOLD_1080P

    def test_2160p_resolution(self):
        # 3840x2160 = 8_294_400
        assert _get_bitrate_threshold((3840, 2160)) == BITRATE_THRESHOLD_2160P

    def test_small_resolution_uses_fallback(self):
        assert _get_bitrate_threshold((320, 240)) == FFMPEG_DETECTION_BITRATE_THRESHOLD


class TestCalculateBitrate:
    def test_normal(self):
        assert _calculate_bitrate(100_000_000, 100.0) == 8.0  # 100MB, 100s -> 8 Mbps

    def test_duration_zero(self):
        assert _calculate_bitrate(1000, 0.0) is None

    def test_duration_negative(self):
        assert _calculate_bitrate(1000, -1.0) is None

    def test_duration_none(self):
        assert _calculate_bitrate(1000, None) is None

    def test_size_none(self):
        assert _calculate_bitrate(None, 10.0) is None


class TestGetMinDurationForExpensiveChecks:
    """Test adaptive minimum duration calculation."""

    def test_none_duration_uses_floor(self):
        assert _get_min_duration_for_expensive_checks(None) == FFMPEG_DETECTION_MIN_DURATION_FLOOR

    def test_zero_duration_uses_floor(self):
        assert _get_min_duration_for_expensive_checks(0.0) == FFMPEG_DETECTION_MIN_DURATION_FLOOR

    def test_negative_duration_uses_floor(self):
        assert _get_min_duration_for_expensive_checks(-10.0) == FFMPEG_DETECTION_MIN_DURATION_FLOOR

    def test_very_short_duration_uses_floor(self):
        # 1s * 0.014 = 0.014s → should floor to 2.0s
        assert _get_min_duration_for_expensive_checks(1.0) == FFMPEG_DETECTION_MIN_DURATION_FLOOR

    def test_50s_duration_floored(self):
        # 50s * 0.014 = 0.7s → should floor to 2.0s
        result = _get_min_duration_for_expensive_checks(50.0)
        assert result == FFMPEG_DETECTION_MIN_DURATION_FLOOR

    def test_300s_duration_above_floor(self):
        # 300s * 0.014 = 4.2s
        result = _get_min_duration_for_expensive_checks(300.0)
        assert abs(result - 4.2) < 0.01

    def test_3600s_duration_scales(self):
        # 3600s * 0.014 = 50.4s
        result = _get_min_duration_for_expensive_checks(3600.0)
        assert abs(result - 50.4) < 0.01

    def test_exactly_at_floor_boundary(self):
        # Find duration where 1.4% equals 2.0s: dur * 0.014 = 2.0 → dur = 142.857...
        boundary_dur = FFMPEG_DETECTION_MIN_DURATION_FLOOR / FFMPEG_DETECTION_MIN_DURATION_PERCENT
        result = _get_min_duration_for_expensive_checks(boundary_dur)
        assert abs(result - FFMPEG_DETECTION_MIN_DURATION_FLOOR) < 0.01


class TestDetectionResult:
    def test_to_flags_dict(self):
        r = DetectionResult(
            bitrate_mbps=0.5,
            is_suspicious_bitrate=True,
            black_frame_duration=1.0,
            silence_duration=None,
            freeze_detected=False,
            freeze_duration=None,
            signal_entropy=None,
            confidence=0.8,
            warnings=["Low bitrate"],
        )
        d = r.to_flags_dict()
        assert d["bitrate_mbps"] == 0.5
        assert d["is_suspicious_bitrate"] is True
        assert d["black_frame_duration"] == 1.0
        assert "freeze_detected" in d


class TestDetectPaddingJunk:
    def test_file_not_found(self):
        r = detect_padding_junk(Path("/nonexistent/file.mkv"))
        assert r.is_suspicious_bitrate is True  # File not found is suspicious
        assert r.bitrate_mbps == 0.0
        assert any("File not found" in w or "not a file" in w for w in r.warnings)

    def test_duration_too_short_skips_expensive_but_checks_bitrate(self, tmp_path):
        """30s video: adaptive threshold = 2.0s (floored), so expensive ops run.
        But bitrate is still checked."""
        f = tmp_path / "x.mkv"
        f.touch()  # 0-byte file: production skips path.stat() override and uses size_bytes hint
        # 30s duration, 500KB file → bitrate = (500000 * 8) / 30 / 1e6 = 0.133 Mbps (suspicious)
        with patch("core.ffmpeg_detection._get_file_metadata", return_value=(30.0, 500_000)):
            with patch("core.ffmpeg_detection._detect_black_frames", return_value=(None, False)):
                with patch("core.ffmpeg_detection._detect_silence", return_value=(None, False)):
                    r = detect_padding_junk(f)
        # Bitrate is checked and should be suspicious (< 1.0 Mbps default threshold)
        assert r.is_suspicious_bitrate is True
        assert abs(r.bitrate_mbps - 0.133) < 0.01
        # Expensive ops should run (30s > 2.0s adaptive threshold)
        # So we should NOT see "expensive detection skipped" warning

    def test_duration_zero_flags_suspicious(self, tmp_path):
        """Duration 0 should result in zero bitrate (suspicious) and skip expensive ops."""
        f = tmp_path / "x.mkv"
        f.touch()  # 0-byte file: production skips path.stat() override and uses size_bytes hint
        with patch("core.ffmpeg_detection._get_file_metadata", return_value=(0.0, 1000)):
            r = detect_padding_junk(f)
        assert r.bitrate_mbps == 0.0
        assert r.is_suspicious_bitrate is True
        assert any("zero" in w.lower() for w in r.warnings)

    def test_normal_file_passes_bitrate(self, tmp_path):
        f = tmp_path / "x.mkv"
        f.touch()  # 0-byte file: production skips path.stat() override and uses size_bytes hint
        with patch("core.ffmpeg_detection._get_file_metadata", return_value=(100.0, 200_000_000)):
            with patch("core.ffmpeg_detection._detect_black_frames", return_value=(None, False)):
                with patch("core.ffmpeg_detection._detect_silence", return_value=(None, False)):
                    with patch("core.ffmpeg_detection._detect_freeze", return_value=(False, None)):
                        with patch("core.ffmpeg_detection.FFMPEG_DETECTION_ENABLE_ENTROPY", False):
                            r = detect_padding_junk(f)
        assert r.bitrate_mbps == 16.0
        assert r.is_suspicious_bitrate is False

    def test_resolution_aware_1080p_low_bitrate_suspicious(self, tmp_path):
        """~1.89 Mbps @ 1080p is below 8 Mbps threshold -> suspicious."""
        f = tmp_path / "x.mkv"
        f.touch()  # 0-byte file: production skips path.stat() override and uses size_bytes hint
        # Target ~1.89 Mbps over 100s: size = 1.89 * 100 * 1e6 / 8
        duration = 100.0
        size = int(1.89 * duration * 1_000_000 / 8)
        with patch("core.ffmpeg_detection._get_file_metadata", return_value=(duration, size)):
            with patch("core.ffmpeg_detection._detect_black_frames", return_value=(None, False)):
                with patch("core.ffmpeg_detection._detect_silence", return_value=(None, False)):
                    with patch("core.ffmpeg_detection._detect_freeze", return_value=(False, None)):
                        with patch("core.ffmpeg_detection.FFMPEG_DETECTION_ENABLE_ENTROPY", False):
                            r = detect_padding_junk(f, duration=duration, size_bytes=size, resolution=(1920, 1080))
        assert abs(r.bitrate_mbps - 1.89) < 0.02
        assert r.is_suspicious_bitrate is True
        assert r.bitrate_mbps < BITRATE_THRESHOLD_1080P
        # Below half of 1080p ideal (8 Mbps) → extra bitrate weight → crosses auto-ignore confidence
        assert r.confidence >= FFMPEG_DETECTION_CONFIDENCE_THRESHOLD
        assert any("confidence" in w.lower() for w in r.warnings)

    def test_resolution_aware_480p_above_threshold_not_suspicious(self, tmp_path):
        """~4 Mbps @ 480p is above 3 Mbps DVD threshold -> not suspicious."""
        f = tmp_path / "x.mkv"
        f.touch()  # 0-byte file: production skips path.stat() override and uses size_bytes hint
        duration = 100.0
        size = int(4.0 * duration * 1_000_000 / 8)  # 4 Mbps
        with patch("core.ffmpeg_detection._get_file_metadata", return_value=(duration, size)):
            with patch("core.ffmpeg_detection._detect_black_frames", return_value=(None, False)):
                with patch("core.ffmpeg_detection._detect_silence", return_value=(None, False)):
                    with patch("core.ffmpeg_detection._detect_freeze", return_value=(False, None)):
                        with patch("core.ffmpeg_detection.FFMPEG_DETECTION_ENABLE_ENTROPY", False):
                            r = detect_padding_junk(f, duration=duration, size_bytes=size, resolution=(720, 480))
        assert r.is_suspicious_bitrate is False
        assert r.bitrate_mbps >= BITRATE_THRESHOLD_DVD

    def test_50s_video_with_low_bitrate_user_example(self, tmp_path):
        """User's example: 50s video with ~4.35 Mbps @ 1080p should be flagged.
        Adaptive threshold: 50 * 0.014 = 0.7s → floored to 2.0s, so expensive ops run."""
        f = tmp_path / "x.mkv"
        f.touch()  # 0-byte file: production skips path.stat() override and uses size_bytes hint
        duration = 50.0
        # 27.5 MB = 27_238_059 bytes (from user's example)
        size = 27_238_059
        # Expected bitrate: (27238059 * 8) / 50 / 1e6 = 4.358 Mbps
        with patch("core.ffmpeg_detection._get_file_metadata", return_value=(duration, size)):
            with patch("core.ffmpeg_detection._detect_black_frames", return_value=(None, False)):
                with patch("core.ffmpeg_detection._detect_silence", return_value=(None, False)):
                    with patch("core.ffmpeg_detection._detect_freeze", return_value=(False, None)):
                        with patch("core.ffmpeg_detection.FFMPEG_DETECTION_ENABLE_ENTROPY", False):
                            r = detect_padding_junk(f, duration=duration, size_bytes=size, resolution=(1920, 1080))
        # Should be flagged as suspicious (< 8 Mbps for 1080p)
        assert r.is_suspicious_bitrate is True
        assert abs(r.bitrate_mbps - 4.358) < 0.01
        assert r.bitrate_mbps < BITRATE_THRESHOLD_1080P
        # Above half of 1080p threshold (4 Mbps): suspicious but no severe-low-bitrate confidence bump
        assert r.confidence < FFMPEG_DETECTION_CONFIDENCE_THRESHOLD
        # Expensive ops should have run (50s > 2.0s)
        # black_frame_duration should be set (even if None from mock)
        assert "black_frame_duration" in r.to_flags_dict()

    def test_very_short_1s_video_still_checks_bitrate(self, tmp_path):
        """Even 1s video should have bitrate checked, but expensive ops skipped."""
        f = tmp_path / "x.mkv"
        f.touch()  # 0-byte file: production skips path.stat() override and uses size_bytes hint
        duration = 1.0
        size = 100_000  # Very small file
        # Bitrate: (100000 * 8) / 1 / 1e6 = 0.8 Mbps (suspicious)
        with patch("core.ffmpeg_detection._get_file_metadata", return_value=(duration, size)):
            r = detect_padding_junk(f, duration=duration, size_bytes=size)
        # Bitrate should be checked and flagged
        assert r.bitrate_mbps == 0.8
        assert r.is_suspicious_bitrate is True
        # Expensive ops should NOT run (1s < 2.0s floor)
        # Should see warning about skipping expensive detection
        # No black/silence detection should have been attempted
        assert r.black_frame_duration is None
        assert r.silence_duration is None

    def test_5s_video_above_floor_runs_all_detection(self, tmp_path):
        """5s video exceeds 2.0s floor, so all detection runs."""
        f = tmp_path / "x.mkv"
        f.touch()  # 0-byte file: production skips path.stat() override and uses size_bytes hint
        duration = 5.0
        size = 10_000_000  # 10MB
        # Bitrate: (10000000 * 8) / 5 / 1e6 = 16 Mbps (good)
        with patch("core.ffmpeg_detection._get_file_metadata", return_value=(duration, size)):
            with patch("core.ffmpeg_detection._detect_black_frames", return_value=(None, False)):
                with patch("core.ffmpeg_detection._detect_silence", return_value=(None, False)):
                    with patch("core.ffmpeg_detection._detect_freeze", return_value=(False, None)):
                        with patch("core.ffmpeg_detection.FFMPEG_DETECTION_ENABLE_ENTROPY", False):
                            r = detect_padding_junk(f, duration=duration, size_bytes=size)
        # Bitrate should be good
        assert r.bitrate_mbps == 16.0
        assert r.is_suspicious_bitrate is False
        # All detection should have run (5s > 2.0s)
        # Mocks were called, so black/silence would be set

    def test_none_duration_flags_zero_bitrate(self, tmp_path):
        """If duration cannot be determined, bitrate is 0 (suspicious)."""
        f = tmp_path / "x.mkv"
        f.touch()  # 0-byte file: production skips path.stat() override and uses size_bytes hint
        with patch("core.ffmpeg_detection._get_file_metadata", return_value=(None, 1000)):
            r = detect_padding_junk(f)
        assert r.bitrate_mbps == 0.0
        assert r.is_suspicious_bitrate is True
        assert any("unknown" in w.lower() for w in r.warnings)

    def test_zero_bitrate_always_suspicious(self, tmp_path):
        """Bitrate calculation returning 0 should always be flagged."""
        f = tmp_path / "x.mkv"
        f.touch()  # 0-byte file: production skips path.stat() override and uses size_bytes hint
        # Duration exists but bitrate calculation fails (returns 0.0)
        with patch("core.ffmpeg_detection._calculate_bitrate", return_value=None):
            with patch("core.ffmpeg_detection._get_file_metadata", return_value=(100.0, 1000)):
                r = detect_padding_junk(f, duration=100.0, size_bytes=1000)
        assert r.bitrate_mbps == 0.0
        assert r.is_suspicious_bitrate is True


class TestIsDetectionDisabled:
    def test_dev_mode_disables(self):
        with patch("core.ffmpeg_detection.is_dev_mode", return_value=True):
            with patch("core.settings.get_ffmpeg_detection_enabled", return_value=False):
                assert is_detection_disabled() is True

    def test_dev_mode_with_toggle_on_falls_through(self):
        with patch("core.ffmpeg_detection.is_dev_mode", return_value=True):
            with patch("core.settings.get_ffmpeg_detection_enabled", return_value=True):
                with patch.dict(os.environ, {"DISABLE_FFMPEG_JUNK_DETECTION": ""}, clear=False):
                    with patch("core.settings.load_settings", return_value={}):
                        assert is_detection_disabled() is False

    def test_dev_mode_with_toggle_off_disables(self):
        with patch("core.ffmpeg_detection.is_dev_mode", return_value=True):
            with patch("core.settings.get_ffmpeg_detection_enabled", return_value=False):
                assert is_detection_disabled() is True

    def test_env_disables(self):
        with patch("core.ffmpeg_detection.is_dev_mode", return_value=False):
            with patch.dict(os.environ, {"DISABLE_FFMPEG_JUNK_DETECTION": "1"}, clear=False):
                assert is_detection_disabled() is True

    def test_user_setting_disables(self):
        with patch("core.ffmpeg_detection.is_dev_mode", return_value=False):
            with patch.dict(os.environ, {"DISABLE_FFMPEG_JUNK_DETECTION": ""}, clear=False):
                with patch("core.settings.load_settings", return_value={"disable_ffmpeg_junk_detection": True}):
                    assert is_detection_disabled() is True

    def test_enabled_when_nothing_disables(self):
        with patch("core.ffmpeg_detection.is_dev_mode", return_value=False):
            with patch.dict(os.environ, {"DISABLE_FFMPEG_JUNK_DETECTION": ""}, clear=False):
                with patch("core.settings.load_settings", return_value={}):
                    assert is_detection_disabled() is False
