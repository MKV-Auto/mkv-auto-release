"""
FFmpeg-based padding/junk detection for ripped MKV files.

Multi-tier detection: bitrate gate, black frames, silence, freeze, signal entropy.
Used by preview_and_detect to flag suspicious (e.g. padding) content.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.utils import is_dev_mode

log = logging.getLogger(__name__)


def is_detection_disabled() -> bool:
    """
    True if padding detection should be skipped (preview only).
    - Dev mode: disabled when dev.ffmpeg_detection_enabled is False (dev-menu toggle off);
      when True, fall through to env and user-setting checks.
    - Env DISABLE_FFMPEG_JUNK_DETECTION: disabled when set to 1/true/yes/on.
    - User setting disable_ffmpeg_junk_detection in preview config.
    """
    if is_dev_mode():
        pass
    if os.getenv("DISABLE_FFMPEG_JUNK_DETECTION", "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    try:
        from core import settings
        if settings.load_settings().get("disable_ffmpeg_junk_detection", False):
            return True
    except Exception:
        pass
    return False

# ─── Config (env) ───────────────────────────────────────────────────────────

# Disc-specific bitrate thresholds (Mbps) — assumes MakeMKV remux-quality output
BITRATE_THRESHOLD_DVD = float(os.getenv("FFMPEG_DETECTION_BITRATE_DVD", "3.0"))
BITRATE_THRESHOLD_720P = float(os.getenv("FFMPEG_DETECTION_BITRATE_720P", "5.0"))
BITRATE_THRESHOLD_1080P = float(os.getenv("FFMPEG_DETECTION_BITRATE_1080P", "8.0"))
BITRATE_THRESHOLD_2160P = float(os.getenv("FFMPEG_DETECTION_BITRATE_2160P", "15.0"))
FFMPEG_DETECTION_BITRATE_THRESHOLD = float(os.getenv("FFMPEG_DETECTION_BITRATE_THRESHOLD", "1.0"))

FFMPEG_DETECTION_CONFIDENCE_THRESHOLD = float(os.getenv("FFMPEG_DETECTION_CONFIDENCE_THRESHOLD", "0.7"))
FFMPEG_DETECTION_ENABLE_FREEZE = os.getenv("FFMPEG_DETECTION_ENABLE_FREEZE", "true").strip().lower() in ("1", "true", "yes", "on")
FFMPEG_DETECTION_ENABLE_ENTROPY = os.getenv("FFMPEG_DETECTION_ENABLE_ENTROPY", "false").strip().lower() in ("1", "true", "yes", "on")
FFMPEG_DETECTION_SAMPLE_COUNT = max(1, int(os.getenv("FFMPEG_DETECTION_SAMPLE_COUNT", "3")))

# Adaptive minimum duration for expensive operations (black/silence/freeze detection)
# Calculated as percentage of video duration with an absolute floor
FFMPEG_DETECTION_MIN_DURATION_PERCENT = 0.014  # 1.4% of video duration
FFMPEG_DETECTION_MIN_DURATION_FLOOR = 2.0  # Absolute minimum in seconds

# Sample positions as fractions of duration (e.g. [0.05, 0.5, 0.9])
def _sample_positions() -> list[float]:
    n = FFMPEG_DETECTION_SAMPLE_COUNT
    if n == 1:
        return [0.5]
    return [i / (n - 1) if n > 1 else 0.5 for i in range(n)]


@dataclass
class DetectionResult:
    """Results from ffmpeg-based padding detection."""

    bitrate_mbps: float
    is_suspicious_bitrate: bool
    black_frame_duration: float | None
    silence_duration: float | None
    freeze_detected: bool
    freeze_duration: float | None
    signal_entropy: float | None
    confidence: float  # 0.0–1.0, higher = more likely padding
    warnings: list[str] = field(default_factory=list)

    def to_flags_dict(self) -> dict[str, Any]:
        """Serialize for detection_flags JSON."""
        return {
            "bitrate_mbps": self.bitrate_mbps,
            "is_suspicious_bitrate": self.is_suspicious_bitrate,
            "black_frame_duration": self.black_frame_duration,
            "silence_duration": self.silence_duration,
            "freeze_detected": self.freeze_detected,
            "freeze_duration": self.freeze_duration,
            "signal_entropy": self.signal_entropy,
        }


def _get_file_metadata(file_path: Path) -> tuple[float | None, int | None]:
    """
    Use ffprobe to get duration (seconds) and size (bytes).
    Returns (duration, size); either may be None on error or missing stream.
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration,size",
        "-of", "json",
        str(file_path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            log.debug("ffprobe format failed for %s: %s", file_path, out.stderr)
            return None, None
        data = json.loads(out.stdout)
        fmt = data.get("format") or {}
        dur = fmt.get("duration")
        size = fmt.get("size")
        duration = float(dur) if dur is not None else None
        size_int = int(size) if size is not None else None
        return duration, size_int
    except (json.JSONDecodeError, subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log.debug("ffprobe error for %s: %s", file_path, e)
        return None, None


def _calculate_bitrate(size_bytes: int | None, duration_seconds: float | None) -> float | None:
    """Bitrate in Mbps. Returns None if duration/size invalid or would divide by zero."""
    if size_bytes is None or size_bytes < 0:
        return None
    if duration_seconds is None or duration_seconds <= 0:
        return None
    return (size_bytes * 8) / duration_seconds / 1_000_000


def _get_bitrate_threshold(resolution: tuple[int, int] | None) -> float:
    """
    Get appropriate bitrate threshold based on resolution.

    Thresholds are disc-specific: assumes MakeMKV remux or high-quality encode.
    DVD: 3 Mbps, 720p: 5 Mbps, 1080p: 8 Mbps, 2160p: 15 Mbps.
    """
    if resolution is None:
        return FFMPEG_DETECTION_BITRATE_THRESHOLD
    width, height = resolution
    pixels = width * height
    # Fallback: very small / unknown (< 300k pixels)
    # DVD: ~0.35M (720x480=345_600) or ~0.41M (720x576=414_720)
    # 720p: ~0.92M (1280x720=921_600)
    # 1080p: ~2.07M (1920x1080=2_073_600)
    # 2160p: ~8.29M (3840x2160=8_294_400)
    if pixels < 300_000:
        return FFMPEG_DETECTION_BITRATE_THRESHOLD
    if pixels < 920_000:  # DVD/SD: 720x480=345_600, 720x576=414_720; 1280x720=921_600 is 720p
        return BITRATE_THRESHOLD_DVD
    if pixels < 1_800_000:
        return BITRATE_THRESHOLD_720P
    if pixels < 4_000_000:
        return BITRATE_THRESHOLD_1080P
    return BITRATE_THRESHOLD_2160P


def _get_min_duration_for_expensive_checks(duration: float | None) -> float:
    """
    Calculate adaptive minimum duration for expensive operations (black/silence/freeze).
    
    Returns 1.4% of duration with 2-second floor. This allows short legitimate content
    (trailers, extras) while still catching junk, and scales proportionally with longer videos.
    
    Examples:
    - 50s video: 50 * 0.014 = 0.7s → floored to 2.0s
    - 300s video: 300 * 0.014 = 4.2s
    - 3600s video: 3600 * 0.014 = 50.4s
    """
    if duration is None or duration <= 0:
        return FFMPEG_DETECTION_MIN_DURATION_FLOOR
    return max(FFMPEG_DETECTION_MIN_DURATION_FLOOR, duration * FFMPEG_DETECTION_MIN_DURATION_PERCENT)


def _detect_black_frames(file_path: Path, duration: float, sample_fracs: list[float]) -> tuple[float | None, bool]:
    """
    Sample-based black detection at given fractions of duration.
    Returns (total_black_seconds, all_samples_black).
    Uses blackdetect on short segments; if all sampled segments are mostly black, all_samples_black=True.
    """
    segment_len = 2.0
    black_totals: list[float] = []
    for frac in sample_fracs:
        start = max(0.0, duration * frac - segment_len / 2)
        start = min(start, max(0.0, duration - segment_len))
        cmd = [
            "ffmpeg",
            "-v", "error",
            "-ss", str(start),
            "-t", str(segment_len),
            "-i", str(file_path),
            "-vf", "blackdetect=d=0.1:pix_th=0.10",
            "-f", "null",
            "-",
        ]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            # blackdetect prints to stderr: [blackdetect @ 0x...] black_start:0 black_end:2 black_duration:2
            err = (out.stderr or "") + (out.stdout or "")
            seg_black = 0.0
            for line in err.splitlines():
                if "black_duration:" in line:
                    try:
                        part = line.split("black_duration:")[-1].strip().split()[0]
                        seg_black += float(part)
                    except (IndexError, ValueError):
                        pass
            black_totals.append(seg_black)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            log.debug("blackdetect error for %s at %.2f: %s", file_path, frac, e)
            black_totals.append(0.0)  # assume not black on error

    total_black = sum(black_totals) if black_totals else 0.0
    # Consider "all black" if each sampled segment is at least 80% black
    threshold = segment_len * 0.8
    all_black = all(b >= threshold for b in black_totals) if black_totals else False
    return total_black if total_black > 0 else None, all_black


def _detect_silence(file_path: Path, duration: float, sample_fracs: list[float]) -> tuple[float | None, bool]:
    """
    Sample-based silence detection. Returns (total_silence_seconds, all_samples_silent).
    """
    segment_len = 2.0
    silence_totals: list[float] = []
    for frac in sample_fracs:
        start = max(0.0, duration * frac - segment_len / 2)
        start = min(start, max(0.0, duration - segment_len))
        cmd = [
            "ffmpeg",
            "-v", "error",
            "-ss", str(start),
            "-t", str(segment_len),
            "-i", str(file_path),
            "-af", "silencedetect=n=-50dB:d=0.5",
            "-f", "null",
            "-",
        ]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            err = (out.stderr or "") + (out.stdout or "")
            seg_silence = 0.0
            for line in err.splitlines():
                if "silence_duration:" in line:
                    try:
                        part = line.split("silence_duration:")[-1].strip().split()[0]
                        seg_silence += float(part)
                    except (IndexError, ValueError):
                        pass
            silence_totals.append(seg_silence)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            log.debug("silencedetect error for %s at %.2f: %s", file_path, frac, e)
            silence_totals.append(0.0)

    total_silence = sum(silence_totals) if silence_totals else 0.0
    threshold = segment_len * 0.8
    all_silent = all(s >= threshold for s in silence_totals) if silence_totals else False
    return total_silence if total_silence > 0 else None, all_silent


def _detect_freeze(file_path: Path, duration: float) -> tuple[bool, float | None]:
    """
    Optional freeze detection. Returns (freeze_detected, freeze_duration).
    """
    cmd = [
        "ffmpeg",
        "-v", "error",
        "-i", str(file_path),
        "-vf", "freezedetect=n=-60dB:d=2",
        "-f", "null",
        "-",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=min(120, int(duration) + 30))
        err = (out.stderr or "") + (out.stdout or "")
        freeze_dur = 0.0
        for line in err.splitlines():
            if "freeze_duration:" in line:
                try:
                    part = line.split("freeze_duration:")[-1].strip().split()[0]
                    freeze_dur += float(part)
                except (IndexError, ValueError):
                    pass
        # Flag if freeze spans a large fraction of duration
        detected = duration > 0 and (freeze_dur / duration) >= 0.5
        return detected, freeze_dur if freeze_dur > 0 else None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log.debug("freezedetect error for %s: %s", file_path, e)
        return False, None


def _calculate_signal_entropy(file_path: Path) -> float | None:
    """
    Optional: use signalstats to get a simple variance/entropy proxy.
    We use mean of Y (luminance); very flat values indicate static/blank.
    """
    # Sample first 5 seconds
    cmd = [
        "ffmpeg",
        "-v", "error",
        "-t", "5",
        "-i", str(file_path),
        "-vf", "signalstats",
        "-f", "null",
        "-",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        err = (out.stderr or "") + (out.stdout or "")
        # signalstats prints YAVG, etc. We look for very low variance / flat Y.
        # Heuristic: if we see many repeated YAVG values, entropy is low.
        values: list[float] = []
        for line in err.splitlines():
            if "YAVG:" in line or "lavfi.signalstats.YAVG=" in line:
                try:
                    part = line.split("YAVG")[-1].replace("=", " ").strip().split()[0]
                    values.append(float(part))
                except (IndexError, ValueError):
                    pass
        if len(values) < 2:
            return None
        avg = sum(values) / len(values)
        var = sum((x - avg) ** 2 for x in values) / len(values)
        # Normalize to 0–1; 0 = no variance (suspicious), 1 = high variance (normal)
        return min(1.0, max(0.0, var / 100.0)) if var else 0.0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log.debug("signalstats error for %s: %s", file_path, e)
        return None


def _calculate_confidence(
    r: "DetectionResult",
    resolution: tuple[int, int] | None = None,
) -> float:
    """
    Aggregate confidence that the file is padding/junk (0.0–1.0).

    When bitrate is suspicious and below half the resolution-aware ideal threshold,
    add extra weight so pathologically low bitrates can reach the auto-ignore line
    without lowering the global confidence threshold for mildly low bitrates.
    """
    c = 0.0
    w = 0.0
    # Bitrate
    if r.is_suspicious_bitrate:
        c += 0.4
        thr = _get_bitrate_threshold(resolution)
        if thr > 0 and r.bitrate_mbps < 0.5 * thr:
            c += 0.35
    w += 0.4
    # Black
    if r.black_frame_duration is not None and r.black_frame_duration > 0:
        c += 0.2  # any significant black contributes
    w += 0.2
    # Silence
    if r.silence_duration is not None and r.silence_duration > 0:
        c += 0.15
    w += 0.15
    # Freeze
    if r.freeze_detected:
        c += 0.15
    w += 0.15
    # Entropy (low = suspicious)
    if r.signal_entropy is not None:
        c += (1.0 - r.signal_entropy) * 0.1
    w += 0.1
    return min(1.0, c / max(w, 0.01))


def detect_padding_junk(
    file_path: Path,
    duration: float | None = None,
    size_bytes: int | None = None,
    resolution: tuple[int, int] | None = None,
) -> DetectionResult:
    """
    Run multi-tier detection on an MKV file.

    Tiers:
    1. Bitrate (ffprobe) — ALWAYS runs, resolution-aware, flags 0-bitrate as suspicious
    2. Black frame detection (sampled at configured positions) — only if duration ≥ adaptive threshold
    3. Silence detection (sampled) — only if duration ≥ adaptive threshold
    4. Freeze detection (optional) — only if duration ≥ adaptive threshold
    5. Signal entropy (optional) — only if duration ≥ adaptive threshold

    Adaptive threshold is 1.4% of video duration with 2-second floor. This allows short legitimate
    content (trailers, extras, menu screens) while catching junk, and scales with longer videos.

    If duration is not provided, it is fetched via ffprobe. size_bytes can be passed in
    as a hint; when the file exists, on-disk ``stat().st_size`` is preferred (avoids stale ``mkv_size``).
    resolution is (width, height) from metadata_scan; used for disc-specific bitrate thresholds.

    Returns DetectionResult with confidence and warnings.
    """
    path = Path(file_path)
    log.debug("detect_padding_junk: starting for %s", file_path)
    if not path.exists() or not path.is_file():
        bad = DetectionResult(
            bitrate_mbps=0.0,
            is_suspicious_bitrate=True,  # File not found is suspicious
            black_frame_duration=None,
            silence_duration=None,
            freeze_detected=False,
            freeze_duration=None,
            signal_entropy=None,
            confidence=0.0,
            warnings=["File not found or not a file"],
        )
        bad.confidence = _calculate_confidence(bad, resolution=None)
        return bad

    dur = duration
    size = size_bytes
    try:
        st_len = path.stat().st_size
        if st_len > 0:
            size = st_len
    except OSError:
        pass
    if dur is None or size is None:
        meta_dur, meta_size = _get_file_metadata(path)
        if dur is None:
            dur = meta_dur
        if size is None:
            size = meta_size
    if size is None:
        try:
            size = path.stat().st_size
        except OSError:
            size = None

    warnings: list[str] = []

    # ─── Tier 1: Bitrate (ALWAYS runs) ─────────────────────────────────────────
    bitrate = _calculate_bitrate(size, dur)
    bitrate_mbps = bitrate if bitrate is not None else 0.0
    threshold = _get_bitrate_threshold(resolution)
    is_suspicious_bitrate = bitrate_mbps < threshold
    
    if bitrate_mbps == 0.0:
        warnings.append("Zero bitrate detected (invalid file or calculation failed)")
    elif is_suspicious_bitrate:
        res_str = f"{resolution[0]}x{resolution[1]}" if resolution else "unknown"
        warnings.append(f"Low bitrate {bitrate_mbps:.2f} Mbps for {res_str} (threshold {threshold} Mbps)")

    # ─── Tier 2-5: Expensive operations (only if duration meets adaptive threshold) ───
    min_dur = _get_min_duration_for_expensive_checks(dur)
    
    black_dur: float | None = None
    silence_dur: float | None = None
    freeze_detected = False
    freeze_dur: float | None = None
    entropy: float | None = None

    if dur is not None and dur >= min_dur:
        # Run expensive detection operations
        sample_fracs = _sample_positions()
        black_dur, all_black = _detect_black_frames(path, dur, sample_fracs)
        if all_black:
            warnings.append("All sampled segments appear black")
        
        silence_dur, all_silent = _detect_silence(path, dur, sample_fracs)
        if all_silent:
            warnings.append("All sampled segments appear silent")

        if FFMPEG_DETECTION_ENABLE_FREEZE:
            freeze_detected, freeze_dur = _detect_freeze(path, dur)
            if freeze_detected and freeze_dur is not None:
                warnings.append(f"Freeze detected (duration {freeze_dur:.1f}s)")

        if FFMPEG_DETECTION_ENABLE_ENTROPY:
            entropy = _calculate_signal_entropy(path)
            if entropy is not None and entropy < 0.1:
                warnings.append("Low signal variance (possible static/blank)")
    else:
        # Duration too short for expensive operations
        if dur is None:
            warnings.append("Duration unknown; expensive detection skipped")
        elif dur == 0:
            warnings.append("Duration is zero; expensive detection skipped")
        else:
            warnings.append(f"Duration {dur:.1f}s below adaptive threshold {min_dur:.1f}s; expensive detection skipped")

    result = DetectionResult(
        bitrate_mbps=bitrate_mbps,
        is_suspicious_bitrate=is_suspicious_bitrate,
        black_frame_duration=black_dur,
        silence_duration=silence_dur,
        freeze_detected=freeze_detected,
        freeze_duration=freeze_dur,
        signal_entropy=entropy,
        confidence=0.0,
        warnings=warnings,
    )
    result.confidence = _calculate_confidence(result, resolution=resolution)
    if result.confidence >= FFMPEG_DETECTION_CONFIDENCE_THRESHOLD:
        warnings.append(f"Confidence {result.confidence:.2f} >= {FFMPEG_DETECTION_CONFIDENCE_THRESHOLD} (likely padding/junk)")

    return result
