"""
Post-preview duration-sanity check — the third obfuscation source.

Catches the *short-declared / long-actual* decoy pattern that
`makemkv_msg3307` and `segment_set_sibling` miss: a single isolated
playlist that MakeMKV reports as e.g. 10 seconds but actually contains
much longer low-bitrate filler when probed (Midway's 00001.mpls is the
canonical repro — declared 10s, ffprobe says 120+s).

The check is intentionally pure so it can be unit-tested without
spinning up the preview/detect worker. Issue #374.
"""
from __future__ import annotations

from typing import Literal, Optional

DurationShortReason = Literal["duration_short"]

# Ratio threshold — actual must exceed declared by this multiplier to fire.
# 1.5× is the spec value from #374; Midway's 12× clears it trivially.
RATIO_THRESHOLD = 1.5

# Absolute floor — ratio alone is noisy on short clips (a 20s clip that
# ffprobes at 35s is just a 1.75× ratio from rounding/intro chrome, not
# obfuscation). Require at least this many seconds of unaccounted-for
# content before flagging.
MIN_ABSOLUTE_DIFF_SECONDS = 30.0


def evaluate_duration_short(
    declared: float | int | None,
    actual: float | int | None,
) -> Optional[DurationShortReason]:
    """Return `'duration_short'` if the actual ffprobe'd duration is
    significantly longer than the MakeMKV-declared duration, else None.

    Both inputs are seconds. A None / non-positive `declared` returns
    None (we can't compute a ratio from missing data, and a 0-second
    title is either a scan glitch or a real empty container we shouldn't
    second-guess).
    """
    if declared is None or actual is None:
        return None
    try:
        d = float(declared)
        a = float(actual)
    except (TypeError, ValueError):
        return None
    if d <= 0 or a <= 0:
        return None
    if a / d < RATIO_THRESHOLD:
        return None
    if (a - d) < MIN_ABSOLUTE_DIFF_SECONDS:
        return None
    return "duration_short"
