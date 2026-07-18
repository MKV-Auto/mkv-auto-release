"""
Unit tests for the bitrate-plausibility obfuscation helper (issue #374).
"""
from __future__ import annotations

from core.bitrate_plausibility import evaluate_low_bitrate_decoy


def test_midway_00001_fires():
    # Live data from Midway's 00001.mpls (the issue's concrete repro):
    # 3840×2160 (4K UHD) @ 1.13 Mbps. Real 4K HEVC delivery sits
    # 30-100 Mbps; 1.13 Mbps cannot be real picture content.
    assert evaluate_low_bitrate_decoy(bit_rate=1_134_058, width=3840, height=2160) == "low_bitrate_decoy"


def test_midway_main_movie_passes_through():
    # Midway 00504.mpls: 4K UHD @ 72.8 Mbps — typical UHD main feature.
    assert evaluate_low_bitrate_decoy(bit_rate=72_827_516, width=3840, height=2160) is None


def test_midway_1080p_extra_passes_through():
    # 00701.mpls "Getting it Right" — 1080p @ 16.2 Mbps, typical
    # Blu-ray extra encode. Well above the 1.5 Mbps floor.
    assert evaluate_low_bitrate_decoy(bit_rate=16_155_045, width=1920, height=1080) is None


def test_borderline_4k_just_under_floor_fires():
    # 4K at 4.9 Mbps — under the 5 Mbps floor, even though some legit
    # low-bitrate streaming content sits there. UHD-disc content
    # essentially never does.
    assert evaluate_low_bitrate_decoy(bit_rate=4_900_000, width=3840, height=2160) == "low_bitrate_decoy"


def test_borderline_4k_just_at_floor_passes_through():
    assert evaluate_low_bitrate_decoy(bit_rate=5_000_000, width=3840, height=2160) is None


def test_1080p_implausible_fires():
    # 1080p at 800 Kbps — below the 1.5 Mbps tier floor.
    assert evaluate_low_bitrate_decoy(bit_rate=800_000, width=1920, height=1080) == "low_bitrate_decoy"


def test_720p_implausible_fires():
    # 720p at 400 Kbps — below 800 Kbps tier floor.
    assert evaluate_low_bitrate_decoy(bit_rate=400_000, width=1280, height=720) == "low_bitrate_decoy"


def test_sd_legit_passes_through():
    # 720×480 DVD at 6 Mbps — well above the 400 Kbps SD floor.
    assert evaluate_low_bitrate_decoy(bit_rate=6_000_000, width=720, height=480) is None


def test_missing_inputs_return_none():
    assert evaluate_low_bitrate_decoy(bit_rate=None, width=3840, height=2160) is None
    assert evaluate_low_bitrate_decoy(bit_rate=1_000_000, width=None, height=2160) is None
    assert evaluate_low_bitrate_decoy(bit_rate=1_000_000, width=3840, height=None) is None


def test_zero_inputs_return_none():
    # ffprobe occasionally emits 0 for one or more fields on broken
    # files; don't flag those as obfuscation.
    assert evaluate_low_bitrate_decoy(bit_rate=0, width=3840, height=2160) is None
    assert evaluate_low_bitrate_decoy(bit_rate=1_000_000, width=0, height=2160) is None


def test_non_numeric_inputs_return_none():
    assert evaluate_low_bitrate_decoy(bit_rate="abc", width=3840, height=2160) is None
    assert evaluate_low_bitrate_decoy(bit_rate=1_000_000, width="x", height=2160) is None
