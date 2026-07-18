"""
Persisted preview/transcode settings for HLS track previews.
Delegates to core.settings (settings.json).
"""
from typing import Optional

from core import settings


def load_preview_config():
    return settings.get_preview_dict()


def save_preview_config(
    duration_seconds: Optional[int] = None,
    max_parallel: Optional[int] = None,
    disable_ffmpeg_junk_detection: Optional[bool] = None,
    disable_ffprobe_metadata_scan: Optional[bool] = None,
):
    return settings.save_preview_dict(
        duration_seconds=duration_seconds,
        max_parallel=max_parallel,
        disable_ffmpeg_junk_detection=disable_ffmpeg_junk_detection,
        disable_ffprobe_metadata_scan=disable_ffprobe_metadata_scan,
    )
