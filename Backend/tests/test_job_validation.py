"""
Unit tests for core.job_validation.validate_previews.
Uses Mock job, monkeypatched JobPaths.from_job, and tmp_path for preview manifests.
"""
import pytest
from pathlib import Path
from unittest.mock import Mock

from core.job_validation import validate_previews


@pytest.fixture
def preview_root(tmp_path):
    """Create previews directory under tmp_path."""
    d = tmp_path / "previews"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def mock_paths(preview_root):
    """Fake JobPaths-like object with previews pointing to tmp_path."""
    p = Mock()
    p.previews = preview_root
    return p


def test_validate_previews_success(preview_root, mock_paths, monkeypatch):
    """Valid: preview Root exists, manifests exist with .ts, sources covered."""
    monkeypatch.setattr("core.job_validation.JobPaths.from_job", lambda j: mock_paths)
    (preview_root / "title-1").mkdir()
    (preview_root / "title-1" / "preview.m3u8").write_text("#EXTM3U\nsegment_0.ts\n")
    (preview_root / "title-2").mkdir()
    (preview_root / "title-2" / "preview.m3u8").write_text("#EXTM3U\nseg.ts\n")

    job = Mock()
    job.disc_payload = {
        "previews": {
            "tracks": {
                "title-1": {"manifest": "previews/title-1/preview.m3u8", "source": "movie_t01.mkv"},
                "title-2": {"manifest": "previews/title-2/preview.m3u8", "source": "movie_t02.mkv"},
            }
        }
    }
    job.post_paths = {"title-1": "movie_t01.mkv", "title-2": "movie_t02.mkv"}
    job.ripped_files = {}

    valid, errors = validate_previews(job, None)
    assert valid is True
    assert errors == []


def test_validate_previews_preview_dir_not_found(tmp_path, monkeypatch):
    """Preview directory does not exist."""
    mock_paths = Mock()
    mock_paths.previews = tmp_path / "nonexistent" / "previews"
    monkeypatch.setattr("core.job_validation.JobPaths.from_job", lambda j: mock_paths)

    job = Mock()
    job.disc_payload = {"previews": {"tracks": {}}}

    valid, errors = validate_previews(job, None)
    assert valid is False
    assert len(errors) == 1
    assert "Preview directory not found" in errors[0]


def test_validate_previews_no_preview_metadata(preview_root, mock_paths, monkeypatch):
    """disc_payload.previews is not a dict."""
    monkeypatch.setattr("core.job_validation.JobPaths.from_job", lambda j: mock_paths)

    job = Mock()
    job.disc_payload = {"previews": "not-a-dict"}

    valid, errors = validate_previews(job, None)
    assert valid is False
    assert any("No preview metadata" in e for e in errors)


def test_validate_previews_no_track_metadata(preview_root, mock_paths, monkeypatch):
    """previews.tracks is not a dict."""
    monkeypatch.setattr("core.job_validation.JobPaths.from_job", lambda j: mock_paths)

    job = Mock()
    job.disc_payload = {"previews": {"tracks": "not-a-dict"}}

    valid, errors = validate_previews(job, None)
    assert valid is False
    assert any("No track metadata" in e for e in errors)


def test_validate_previews_track_has_no_manifest_path(preview_root, mock_paths, monkeypatch):
    """Track entry without manifest path adds an error."""
    monkeypatch.setattr("core.job_validation.JobPaths.from_job", lambda j: mock_paths)

    job = Mock()
    job.disc_payload = {
        "previews": {
            "tracks": {
                "t1": {"source": "a.mkv"},
            }
        }
    }
    job.post_paths = {}
    job.ripped_files = {}

    valid, errors = validate_previews(job, None)
    assert valid is False
    assert any("has no manifest path" in e for e in errors)


def test_validate_previews_manifest_not_found(preview_root, mock_paths, monkeypatch):
    """Manifest path points to non-existent file."""
    monkeypatch.setattr("core.job_validation.JobPaths.from_job", lambda j: mock_paths)

    job = Mock()
    job.disc_payload = {
        "previews": {
            "tracks": {
                "t1": {"manifest": "previews/t1/preview.m3u8", "source": "a.mkv"},
            }
        }
    }
    job.post_paths = {"t1": "a.mkv"}
    job.ripped_files = {}

    valid, errors = validate_previews(job, None)
    assert valid is False
    assert any("Preview manifest not found" in e for e in errors)


def test_validate_previews_manifest_empty_or_invalid(preview_root, mock_paths, monkeypatch):
    """Manifest exists but content has no .ts reference."""
    (preview_root / "t1").mkdir()
    (preview_root / "t1" / "preview.m3u8").write_text("#EXTM3U\n")
    monkeypatch.setattr("core.job_validation.JobPaths.from_job", lambda j: mock_paths)

    job = Mock()
    job.disc_payload = {
        "previews": {
            "tracks": {
                "t1": {"manifest": "previews/t1/preview.m3u8", "source": "a.mkv"},
            }
        }
    }
    job.post_paths = {"t1": "a.mkv"}
    job.ripped_files = {}

    valid, errors = validate_previews(job, None)
    assert valid is False
    assert any("empty or invalid" in e for e in errors)


def test_validate_previews_missing_previews_for_tracks(preview_root, mock_paths, monkeypatch):
    """Expected sources (from file_paths) not fully covered by track sources."""
    (preview_root / "t1").mkdir()
    (preview_root / "t1" / "preview.m3u8").write_text("#EXTM3U\nx.ts\n")
    monkeypatch.setattr("core.job_validation.JobPaths.from_job", lambda j: mock_paths)

    job = Mock()
    job.disc_payload = {
        "previews": {
            "tracks": {
                "t1": {"manifest": "previews/t1/preview.m3u8", "source": "a.mkv"},
            }
        }
    }
    job.post_paths = {"t1": "a.mkv", "t2": "b.mkv"}
    job.ripped_files = {}

    valid, errors = validate_previews(job, None)
    assert valid is False
    assert any("Missing previews for tracks" in e for e in errors)
