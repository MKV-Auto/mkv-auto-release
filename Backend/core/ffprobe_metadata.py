"""
FFprobe-based metadata scan for ripped MKV files.

Read-only (no decode). Produces a structured "what's inside?" summary:
streams, chapters, attachments, audio/subs inventory, video quality hints (HDR, 10-bit, etc.).
Used by preview_and_detect alongside padding/junk detection. Enables "which duplicate has more stuff?"
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ─── Config ─────────────────────────────────────────────────────────────────

DISABLE_FFPROBE_METADATA_SCAN = os.getenv("DISABLE_FFPROBE_METADATA_SCAN", "").strip().lower() in ("1", "true", "yes", "on")
FFPROBE_METADATA_TIMEOUT_SEC = int(os.getenv("FFPROBE_METADATA_TIMEOUT_SEC", "30"))
# Retries for transient ffprobe failures / timeouts (exponential backoff between attempts).
FFPROBE_METADATA_RETRY_ATTEMPTS = max(1, int(os.getenv("FFPROBE_METADATA_RETRY_ATTEMPTS", "3")))
_FFPROBE_BACKOFF_RAW = os.getenv("FFPROBE_METADATA_RETRY_BACKOFF_SEC", "2,4,8").strip()


def is_metadata_scan_disabled() -> bool:
    """True if metadata scan should be skipped (preview_and_detect will not call scan_file_metadata)."""
    if DISABLE_FFPROBE_METADATA_SCAN:
        return True
    try:
        from core import settings
        if settings.load_settings().get("disable_ffprobe_metadata_scan", False):
            return True
    except Exception:
        pass
    return False


# ─── Data structures ─────────────────────────────────────────────────────────

@dataclass
class MetadataScanResult:
    """Structured result from ffprobe metadata scan."""

    format: dict[str, Any]  # duration, size, bit_rate
    stream_counts: dict[str, int]  # video, audio, subtitle
    chapters_count: int
    attachments_count: int
    video_hints: dict[str, Any] | None  # codec_name, profile, width, height, pix_fmt, color_*, bit_rate
    audio_summary: list[dict[str, Any]]  # [{index, codec_name, channels, channel_layout, language, default, forced}, ...]
    subtitle_summary: list[dict[str, Any]]  # [{index, codec_name, language, default, forced}, ...]
    warning: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "stream_counts": self.stream_counts,
            "chapters_count": self.chapters_count,
            "attachments_count": self.attachments_count,
            "video_hints": self.video_hints,
            "audio_summary": self.audio_summary,
            "subtitle_summary": self.subtitle_summary,
            "warning": self.warning,
        }


def _ffprobe_retry_backoff_seconds() -> list[float]:
    out: list[float] = []
    for part in _FFPROBE_BACKOFF_RAW.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(float(part))
        except ValueError:
            continue
    if not out:
        out = [2.0, 4.0, 8.0]
    return out


def _run_ffprobe_json_with_retries(file_path: Path, extra_entries: list[str] | None = None) -> dict | None:
    """Run ffprobe JSON probe; retry on failure with configurable backoff (for large / busy files)."""
    delays = _ffprobe_retry_backoff_seconds()
    last: dict | None = None
    for attempt in range(FFPROBE_METADATA_RETRY_ATTEMPTS):
        last = _run_ffprobe_json(file_path, extra_entries=extra_entries)
        if last is not None:
            return last
        if attempt < FFPROBE_METADATA_RETRY_ATTEMPTS - 1:
            idx = min(attempt, len(delays) - 1)
            time.sleep(delays[idx])
    return last


def _run_ffprobe_json(file_path: Path, extra_entries: list[str] | None = None) -> dict | None:
    """Run ffprobe with -of json; return parsed dict or None."""
    entries = [
        "format=filename,format_name,duration,size,bit_rate",
        "stream=index,codec_type,codec_name,profile,width,height,r_frame_rate,avg_frame_rate,bit_rate,channels,channel_layout,sample_rate,pix_fmt,color_space,color_transfer,color_primaries",
        "stream_tags=language,title",
        "stream_disposition=default,forced,hearing_impaired,visual_impaired",
    ]
    if extra_entries:
        entries.extend(extra_entries)
    cmd = [
        "ffprobe", "-hide_banner", "-v", "error",
        "-show_entries", ":".join(entries),
        "-of", "json",
        str(file_path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=FFPROBE_METADATA_TIMEOUT_SEC)
        if out.returncode != 0:
            log.debug("ffprobe metadata failed for %s: %s", file_path, out.stderr)
            return None
        return json.loads(out.stdout)
    except (json.JSONDecodeError, subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log.debug("ffprobe metadata error for %s: %s", file_path, e)
        return None


def _count_chapters(file_path: Path) -> int:
    """Run ffprobe -show_chapters -of csv=p=0 and return line count."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_chapters", "-of", "csv=p=0",
        str(file_path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if out.returncode != 0:
            return 0
        lines = [s for s in (out.stdout or "").strip().splitlines() if s.strip()]
        return len(lines)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return 0


def scan_file_metadata(file_path: Path) -> MetadataScanResult | None:
    """
    Run metadata-focused ffprobe scan on an MKV file (no decode).

    Returns MetadataScanResult, or None on critical failure (e.g. missing file).
    On parse/ffprobe errors, returns a result with empty/zero fields and warning set.
    """
    path = Path(file_path)
    if not path.exists() or not path.is_file():
        log.debug("scan_file_metadata: not a file or missing: %s", file_path)
        return None

    data = _run_ffprobe_json_with_retries(path)
    if data is None:
        return MetadataScanResult(
            format={},
            stream_counts={"video": 0, "audio": 0, "subtitle": 0},
            chapters_count=0,
            attachments_count=0,
            video_hints=None,
            audio_summary=[],
            subtitle_summary=[],
            warning="ffprobe failed or timed out",
        )

    fmt = (data.get("format") or {})
    duration = fmt.get("duration")
    size = fmt.get("size")
    bit_rate = fmt.get("bit_rate")
    format_dict = {
        "duration": float(duration) if duration is not None else None,
        "size": int(size) if size is not None else None,
        "bit_rate": int(bit_rate) if bit_rate is not None else None,
    }

    streams = data.get("streams") or []
    stream_counts = {"video": 0, "audio": 0, "subtitle": 0}
    video_hints: dict[str, Any] | None = None
    audio_summary: list[dict[str, Any]] = []
    subtitle_summary: list[dict[str, Any]] = []

    for s in streams:
        ctype = (s.get("codec_type") or "").strip().lower()
        if ctype == "video":
            stream_counts["video"] += 1
            if video_hints is None:
                tags = s.get("tags") or {}
                disp = s.get("disposition") or {}
                video_hints = {
                    "codec_name": s.get("codec_name"),
                    "profile": s.get("profile"),
                    "width": s.get("width"),
                    "height": s.get("height"),
                    "pix_fmt": s.get("pix_fmt"),
                    "color_space": s.get("color_space"),
                    "color_transfer": s.get("color_transfer"),
                    "color_primaries": s.get("color_primaries"),
                    "bit_rate": int(s["bit_rate"]) if s.get("bit_rate") is not None else None,
                }
        elif ctype in ("audio", "aud"):
            stream_counts["audio"] += 1
            disp = s.get("disposition") or {}
            tags = s.get("tags") or {}
            audio_summary.append({
                "index": s.get("index"),
                "codec_name": s.get("codec_name"),
                "channels": s.get("channels"),
                "channel_layout": s.get("channel_layout"),
                "language": tags.get("language"),
                "title": tags.get("title"),
                "default": bool(disp.get("default")),
                "forced": bool(disp.get("forced")),
            })
        elif ctype in ("subtitle", "subtitles", "sub"):
            stream_counts["subtitle"] += 1
            disp = s.get("disposition") or {}
            tags = s.get("tags") or {}
            subtitle_summary.append({
                "index": s.get("index"),
                "codec_name": s.get("codec_name"),
                "language": tags.get("language"),
                "title": tags.get("title"),
                "default": bool(disp.get("default")),
                "forced": bool(disp.get("forced")),
            })

    chapters_count = _count_chapters(path)

    # Attachments: in MKV, attachment streams may appear as codec_type not v/a/s; count "data" or similar
    attachments_count = sum(1 for s in streams if (s.get("codec_type") or "").strip().lower() not in ("video", "audio", "aud", "subtitle", "subtitles", "sub"))

    return MetadataScanResult(
        format=format_dict,
        stream_counts=stream_counts,
        chapters_count=chapters_count,
        attachments_count=attachments_count,
        video_hints=video_hints,
        audio_summary=audio_summary,
        subtitle_summary=subtitle_summary,
        warning=None,
    )


def metadata_scan_to_summary(metadata_scan: dict[str, Any] | None) -> dict[str, Any]:
    """
    Reduce full metadata_scan to a compact summary for UI (quality/subtitle/audio tiers + hints).

    Returns dict with: quality_tier, quality_hints, subtitle_tier, subtitle_hints, audio_tier, audio_hints.
    """
    if not metadata_scan or not isinstance(metadata_scan, dict):
        return {
            "quality_tier": "minimal",
            "quality_hints": [],
            "subtitle_tier": "minimal",
            "subtitle_hints": [],
            "audio_tier": "minimal",
            "audio_hints": [],
        }

    v = metadata_scan.get("video_hints") or {}
    stream_counts = metadata_scan.get("stream_counts") or {}
    audio_summary = metadata_scan.get("audio_summary") or []
    subtitle_summary = metadata_scan.get("subtitle_summary") or []

    # Quality tier and hints
    quality_hints: list[str] = []
    width = v.get("width")
    height = v.get("height")
    pix_fmt = (v.get("pix_fmt") or "").lower()
    color_transfer = (v.get("color_transfer") or "").lower()
    color_primaries = (v.get("color_primaries") or "").lower()

    if width and height:
        if width >= 3840 or height >= 2160:
            quality_hints.append("4K")
        elif width >= 1920 or height >= 1080:
            quality_hints.append("1080p")
        elif width >= 1280 or height >= 720:
            quality_hints.append("720p")

    if "10" in pix_fmt or "10le" in pix_fmt or "10be" in pix_fmt:
        quality_hints.append("10-bit")
    if "pq" in color_transfer or "smpte2084" in color_transfer or "arib-std-b67" in color_transfer or "hlg" in color_transfer:
        quality_hints.append("HDR")

    if not v:
        quality_tier = "minimal"
    elif ("10-bit" in quality_hints or "HDR" in quality_hints) and ("4K" in quality_hints or "1080p" in quality_hints):
        quality_tier = "best"
    elif stream_counts.get("video", 0) > 0 and (quality_hints or width):
        quality_tier = "ok"
    else:
        quality_tier = "minimal"

    if not quality_hints and width and height:
        quality_hints = [f"{width}x{height}"]

    # Subtitle tier and hints
    n_subs = len(subtitle_summary)
    has_forced = any(s.get("forced") for s in subtitle_summary)
    has_default = any(s.get("default") for s in subtitle_summary)
    languages = {s.get("language") for s in subtitle_summary if s.get("language")}

    if n_subs >= 2 and (has_forced or len(languages) >= 2):
        subtitle_tier = "full"
        subtitle_hints = []
        if has_forced:
            subtitle_hints.append("forced")
        if len(languages) >= 2:
            subtitle_hints.append("multiple languages")
    elif n_subs >= 1 and (has_forced or has_default):
        subtitle_tier = "partial"
        subtitle_hints = ["forced"] if has_forced else []
    elif n_subs >= 1:
        subtitle_tier = "partial"
        subtitle_hints = [f"{n_subs} track(s)"]
    else:
        subtitle_tier = "minimal"
        subtitle_hints = []

    # Audio tier and hints: best = 7.1 / Atmos / TrueHD / DTS-HD; ok = 5.1 / stereo; minimal = mono/few
    def _channel_count(entry: dict) -> int:
        ch = entry.get("channels")
        if isinstance(ch, int) and ch > 0:
            return ch
        layout = (entry.get("channel_layout") or "").lower()
        if "7.1" in layout or "7" in layout:
            return 8
        if "5.1" in layout or "6" in layout:
            return 6
        if "stereo" in layout or "2" in layout:
            return 2
        if "mono" in layout or "1" in layout:
            return 1
        return 0

    best_channels = max((_channel_count(a) for a in audio_summary), default=0)
    codecs = [a.get("codec_name") or "" for a in audio_summary]
    audio_hints_list: list[str] = []

    codecs_lower = " ".join(codecs).lower()
    if best_channels >= 8 or "truehd" in codecs_lower or "atmos" in codecs_lower or "dts_hd" in codecs_lower or "eac3" in codecs_lower:
        audio_tier = "best"
        if best_channels >= 8:
            audio_hints_list.append("7.1")
        for c in codecs:
            if c and c.lower() not in ("aac", "ac3", "mp3"):
                audio_hints_list.append(c)
                break
    elif best_channels >= 6:
        audio_tier = "ok"
        audio_hints_list.append("5.1")
    elif best_channels >= 2:
        audio_tier = "ok"
        audio_hints_list.append("stereo")
    else:
        audio_tier = "minimal"
        audio_hints_list.append("mono" if best_channels <= 1 else f"{best_channels}ch")

    return {
        "quality_tier": quality_tier,
        "quality_hints": quality_hints[:5],
        "subtitle_tier": subtitle_tier,
        "subtitle_hints": subtitle_hints[:5],
        "audio_tier": audio_tier,
        "audio_hints": audio_hints_list[:5],
    }
