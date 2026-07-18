"""
Unit tests for the duration-sanity obfuscation helper (issue #374).
"""
from __future__ import annotations

from core.duration_sanity import evaluate_duration_short


def test_midway_repro_fires():
    # Midway's 00001.mpls — declared 10s, ffprobe ≈ 120s. 12× ratio,
    # 110s diff. Both thresholds trip → flag.
    assert evaluate_duration_short(declared=10, actual=120) == "duration_short"


def test_normal_main_movie_passes_through():
    # Typical movie: declared and actual agree within seconds.
    assert evaluate_duration_short(declared=7200, actual=7205) is None


def test_short_clip_with_rounding_does_not_fire():
    # 20s declared, 35s actual = 1.75× ratio but only 15s of difference.
    # Ratio alone would trip; the 30s absolute floor saves us from
    # flagging legit short clips with chrome/rounding noise.
    assert evaluate_duration_short(declared=20, actual=35) is None


def test_short_clip_above_absolute_floor_fires():
    # Same shape as above but the difference clears 30s. 30s declared
    # vs 65s actual = 2.17× and 35s diff — both thresholds met, flag.
    assert evaluate_duration_short(declared=30, actual=65) == "duration_short"


def test_zero_declared_is_safe():
    # Avoid division-by-zero and don't second-guess scan glitches.
    assert evaluate_duration_short(declared=0, actual=120) is None


def test_negative_or_none_inputs_pass_through():
    assert evaluate_duration_short(declared=None, actual=120) is None
    assert evaluate_duration_short(declared=10, actual=None) is None
    assert evaluate_duration_short(declared=-5, actual=120) is None
    assert evaluate_duration_short(declared=10, actual=-1) is None


def test_borderline_just_under_ratio_does_not_fire():
    # 1.49× ratio, even with a big absolute diff, does NOT fire — the
    # rule is AND not OR. Future tuning could revisit.
    assert evaluate_duration_short(declared=100, actual=149) is None


def test_borderline_just_at_ratio_fires():
    assert evaluate_duration_short(declared=100, actual=150) == "duration_short"


def test_non_numeric_inputs_return_none():
    assert evaluate_duration_short(declared="abc", actual=120) is None
    assert evaluate_duration_short(declared=10, actual="xyz") is None


def test_extraction_from_scan_file_metadata_dict_shape():
    # The call site in preview_detect_phases.py pulls actual duration
    # via meta_dict["format"]["duration"]. Lock that shape so a future
    # MetadataScanResult.to_dict() refactor can't silently break the
    # detection path without lighting up a test.
    from core.ffprobe_metadata import MetadataScanResult

    res = MetadataScanResult(
        format={"duration": 120.0, "size": 1_634_304, "bit_rate": 108_000},
        stream_counts={"video": 1, "audio": 1, "subtitle": 0},
        chapters_count=1,
        attachments_count=0,
        video_hints=None,
        audio_summary=[],
        subtitle_summary=[],
    )
    meta_dict = res.to_dict()
    actual = (meta_dict.get("format") or {}).get("duration")
    assert evaluate_duration_short(declared=10, actual=actual) == "duration_short"
