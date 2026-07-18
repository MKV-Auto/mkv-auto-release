# Integration-style tests for rip workflow with mkv_size and detection.
from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from api import crud, models
from api.models import DiscTitle
from core.ffprobe_metadata import MetadataScanResult
from workers import tasks
from workers.tasks import preview_and_detect


def test_disc_title_has_detection_fields():
    """DiscTitle model includes mkv_size and detection fields."""
    assert hasattr(DiscTitle, "mkv_size")
    assert hasattr(DiscTitle, "detection_flags")
    assert hasattr(DiscTitle, "detection_confidence")
    assert hasattr(DiscTitle, "detection_warning")


def test_disc_title_has_metadata_scan_field():
    """DiscTitle model includes metadata_scan for ffprobe scan results."""
    assert hasattr(DiscTitle, "metadata_scan")


def test_preview_and_detect_task_registered():
    """Legacy preview_and_detect alias and split tasks are registered."""
    assert preview_and_detect.name == "preview_and_detect"
    assert tasks.preview_raw_titles.name == "preview_raw_titles"
    assert tasks.detect_raw_titles.name == "detect_raw_titles"


@pytest.mark.integration
def test_preview_and_detect_sets_metadata_when_scan_enabled(test_db, tmp_path, monkeypatch):
    """With metadata scan enabled and mocked scan_file_metadata, preview_and_detect sets metadata_results and DiscTitle.metadata_scan."""
    monkeypatch.setattr(tasks, "DATA_ROOT", tmp_path / "data")
    monkeypatch.setattr(tasks.celery_app.conf, "task_always_eager", True)
    title_id = "00100"
    meta_dict = {
        "format": {"duration": 120.0, "size": 1000, "bit_rate": 10000},
        "stream_counts": {"video": 1, "audio": 2, "subtitle": 1},
        "chapters_count": 5,
        "attachments_count": 0,
        "video_hints": {"codec_name": "hevc", "width": 3840, "height": 2160},
        "audio_summary": [],
        "subtitle_summary": [],
        "warning": None,
    }
    fake_result = MetadataScanResult(
        format=meta_dict["format"],
        stream_counts=meta_dict["stream_counts"],
        chapters_count=meta_dict["chapters_count"],
        attachments_count=meta_dict["attachments_count"],
        video_hints=meta_dict["video_hints"],
        audio_summary=meta_dict["audio_summary"],
        subtitle_summary=meta_dict["subtitle_summary"],
        warning=meta_dict["warning"],
    )

    with test_db() as session:
        movie = models.Movie(id=str(uuid.uuid4()), name="Movie")
        release = models.Release(id=str(uuid.uuid4()), slug="movie", type="movie", name="Movie", movie_id=movie.id)
        disc = models.Disc(id=str(uuid.uuid4()), content_hash="hash", release_id=release.id, disc_number=1)
        title = models.DiscTitle(id=title_id, disc_id=disc.id, source_file="1", title="Title 1")
        session.add_all([movie, release, disc, title])
        session.commit()
        job = models.Job(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            disc_num="1",
            mount_point="/mnt/dvd",
            job_status="running",
            scan_state="completed",
            rip_state="completed",
            disc_payload={},
        )
        session.add(job)
        session.flush()
        job_id = str(job.id)
        job_dir = tmp_path / "data" / job_id
        raw_dir = job_dir / "raw"
        raw_dir.mkdir(parents=True)
        (raw_dir / "t.mkv").write_bytes(b"x")
        session.commit()

    with (
        patch("core.ffmpeg_detection.is_detection_disabled", return_value=True),
        patch("core.ffprobe_metadata.is_metadata_scan_disabled", return_value=False),
        patch("workers.preview_detect_phases.scan_file_metadata", return_value=fake_result),
    ):
        preview_and_detect.apply(
            args=[job_id, [title_id]],
            kwargs={"rel_path_overrides": {title_id: "raw/t.mkv"}},
        )

    with test_db() as session:
        job_row = crud.get_job(session, job_id)
        assert job_row is not None
        payload = job_row.disc_payload or {}
        assert "metadata_results" in payload
        assert title_id in payload["metadata_results"]
        assert payload["metadata_results"][title_id] == meta_dict
        tit = session.query(models.DiscTitle).filter(models.DiscTitle.id == title_id).first()
        assert tit is not None
        assert tit.metadata_scan == meta_dict
