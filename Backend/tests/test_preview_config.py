"""Unit tests for core.preview_config: delegates to core.settings."""
import pytest

from core import preview_config, settings as core_settings
from api.schemas import PreviewSettings


def test_load_preview_config_returns_patched_dict(monkeypatch):
    stub = {"duration_seconds": 60, "max_parallel": 2, "disable_ffmpeg_junk_detection": True}
    monkeypatch.setattr("core.preview_config.settings.get_preview_dict", lambda: stub)
    assert preview_config.load_preview_config() == stub


# --- #594: ceiling + clamp on read --------------------------------------------

def test_get_preview_dict_surfaces_max_parallel_ceiling(monkeypatch):
    monkeypatch.setattr("core.settings.load_settings", lambda: {"preview_max_parallel": 2})
    monkeypatch.setattr("core.settings.os.cpu_count", lambda: 6)
    result = core_settings.get_preview_dict()
    assert result["max_parallel_ceiling"] == 6
    assert result["max_parallel"] == 2  # under ceiling, untouched


def test_get_preview_dict_clamps_persisted_value_to_ceiling(monkeypatch):
    # Simulates a settings.json written on an 8-core host being loaded on a
    # 6-core host — the persisted 8 must clamp down to 6 so the UI slider can
    # render the thumb on its track.
    monkeypatch.setattr("core.settings.load_settings", lambda: {"preview_max_parallel": 8})
    monkeypatch.setattr("core.settings.os.cpu_count", lambda: 6)
    result = core_settings.get_preview_dict()
    assert result["max_parallel_ceiling"] == 6
    assert result["max_parallel"] == 6  # clamped


def test_preview_settings_schema_rejects_absurd_ceiling_overrun():
    # le=128 hard ceiling — defends against absurd writes regardless of host.
    with pytest.raises(ValueError):
        PreviewSettings(duration_seconds=120, max_parallel=999, max_parallel_ceiling=8)


def test_save_preview_config_calls_save_preview_dict_with_kwargs(monkeypatch):
    seen = []
    def capture(
        duration_seconds=None,
        max_parallel=None,
        disable_ffmpeg_junk_detection=None,
        disable_ffprobe_metadata_scan=None,
    ):
        seen.append({
            "duration_seconds": duration_seconds,
            "max_parallel": max_parallel,
            "disable_ffmpeg_junk_detection": disable_ffmpeg_junk_detection,
            "disable_ffprobe_metadata_scan": disable_ffprobe_metadata_scan,
        })
        return {}
    monkeypatch.setattr("core.preview_config.settings.save_preview_dict", capture)
    preview_config.save_preview_config(
        duration_seconds=5, max_parallel=2, disable_ffmpeg_junk_detection=True
    )
    assert seen == [{
        "duration_seconds": 5,
        "max_parallel": 2,
        "disable_ffmpeg_junk_detection": True,
        "disable_ffprobe_metadata_scan": None,
    }]
