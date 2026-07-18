"""
Bitrate-plausibility check — fourth obfuscation source.

Catches the post-rip remnant of a `duration_short` decoy: even after
MakeMKV trims a 120s-of-filler m2ts to its declared 10s, the resulting
MKV ffprobes with an implausibly low bitrate for its declared
resolution (e.g. Midway 00001.mpls is 3840×2160 @ 1.13 Mbps — 4K HEVC
content cannot physically be that low without being decoy padding).

The check is intentionally conservative: real 4K HEVC delivered on
commercial UHD discs is 30-100 Mbps; the floors here sit at roughly 1/6
of the low end of legit content for each tier, so the only things they
catch are titles that are *obviously* not real picture content.

Issue #374 (closes the part the duration-comparison detector can't).
"""
from __future__ import annotations

from typing import Literal, Optional

LowBitrateDecoyReason = Literal["low_bitrate_decoy"]

# Resolution → minimum bitrate (bits per second) below which the title
# cannot plausibly be real content. Tuned for HEVC; H.264 would need
# 2-3x higher floors, but UHD discs almost always ship HEVC and Blu-ray
# almost always ships H.264 or HEVC — both well above the SD floor at
# legit quality.
RESOLUTION_TIERS_PIXELS = [
    (8_000_000, 5_000_000),    # 4K UHD (≈3840×2160 = 8.29M px) → 5 Mbps
    (1_900_000, 1_500_000),    # 1080p   (≈1920×1080 = 2.07M px) → 1.5 Mbps
    (800_000,   800_000),      # 720p    (≈1280×720 = 0.92M px) → 800 Kbps
    (0,         400_000),      # SD       → 400 Kbps
]


def evaluate_low_bitrate_decoy(
    bit_rate: float | int | None,
    width: float | int | None,
    height: float | int | None,
) -> Optional[LowBitrateDecoyReason]:
    """Return `'low_bitrate_decoy'` when bit_rate is implausibly low for
    the declared video resolution, else None.

    `bit_rate` is the container-level bits/sec from ffprobe's
    `format.bit_rate`. Width / height are the video stream's pixel
    dimensions (ffprobe `video_hints.width` / `height`). All three must
    be positive numbers — any missing value returns None so we don't
    second-guess a partial scan.
    """
    if bit_rate is None or width is None or height is None:
        return None
    try:
        br = float(bit_rate)
        w = float(width)
        h = float(height)
    except (TypeError, ValueError):
        return None
    if br <= 0 or w <= 0 or h <= 0:
        return None
    pixels = w * h
    floor = next(
        (mbps_floor for px_min, mbps_floor in RESOLUTION_TIERS_PIXELS if pixels >= px_min),
        None,
    )
    if floor is None:
        return None
    if br < floor:
        return "low_bitrate_decoy"
    return None
